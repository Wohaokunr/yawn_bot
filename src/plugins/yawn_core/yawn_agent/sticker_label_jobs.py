# ruff: noqa: E501, TID252, BLE001, PLR0913
"""批量 AI 重新标注：WebUI 选一批表情包 → 后台逐张让识图模型标注 → 按模式写回。

- 进程内任务（不落库）：同一时间只跑一个，保留最近 ``MAX_JOBS`` 个结果给 WebUI 看；
- 并发数看策略 ``ai_label_concurrency``（默认 ``CONCURRENCY``），走 LLM 后台车道（不和群聊抢名额）；
- 范围除了“选中的 / 没标过的 / 全部”，还能只标“名字还是文件名的”“AI 没把握的”“上一批失败的”；
- 进度里带预计还要多久（按已完成的平均耗时估）；
- 写回走 ``sticker_library.apply_label``，每张都留“标注前”的快照，可以一键撤销；
- 识图模型说不适合当表情包、且策略开着“不安全进待审核”时，把那张改成待审核；
- 随时可以取消：已经标完的保留，没开始的不再请求。
"""

from __future__ import annotations

import asyncio
import contextlib
import itertools
import time
from dataclasses import dataclass, field
from typing import Any, Literal

from .sticker_labeler import label_image, merge_label
from .sticker_library import (
    STATUS_ACTIVE,
    STATUS_PENDING,
    StickerError,
    all_stickers,
    apply_label,
    get_sticker,
    known_names,
    sticker_path,
    update_sticker,
)
from .sticker_policy import load_settings, note_labeled

MAX_JOBS = 10
MAX_IDS = 500
CONCURRENCY = 2
Mode = Literal["fill", "merge", "replace"]
FIELDS = ("name", "tags", "description")
MODE_LABELS = {"fill": "只补空的", "merge": "补空 + 合并标签", "replace": "全部换成 AI 的"}
SCOPE_LABELS = {
    "selected": "选中的",
    "unlabeled": "没标过的",
    "all": "全部",
    "placeholder": "名字还是文件名的",
    "low_confidence": "AI 没把握的",
    "retry_failed": "上一批失败的",
}
LOW_CONFIDENCE = 0.6

_counter = itertools.count(1)


class LabelJobError(RuntimeError):
    def __init__(self, reason: str, message: str) -> None:
        super().__init__(message)
        self.reason = reason


@dataclass(slots=True)
class LabelJob:
    job_id: str
    ids: list[str]
    mode: str
    fields: tuple[str, ...]
    created_at: float = field(default_factory=time.time)
    finished_at: float | None = None
    status: str = "running"  # running / done / cancelled / failed
    results: list[dict[str, Any]] = field(default_factory=list)
    cancel_requested: bool = False
    task: asyncio.Task[None] | None = None
    scope: str = "selected"

    @property
    def done(self) -> int:
        return len(self.results)

    def summary(self) -> dict[str, Any]:
        changed = sum(1 for row in self.results if row.get("changed"))
        failed = sum(1 for row in self.results if not row.get("ok"))
        return {
            "id": self.job_id,
            "status": self.status,
            "mode": self.mode,
            "modeLabel": MODE_LABELS.get(self.mode, self.mode),
            "fields": list(self.fields),
            "total": len(self.ids),
            "done": self.done,
            "changed": changed,
            "unchanged": self.done - changed - failed,
            "failed": failed,
            "held": sum(1 for row in self.results if row.get("held")),
            "createdAt": int(self.created_at * 1000),
            "finishedAt": int(self.finished_at * 1000) if self.finished_at else None,
            "elapsedMs": int(((self.finished_at or time.time()) - self.created_at) * 1000),
            "scope": self.scope,
            "scopeLabel": SCOPE_LABELS.get(self.scope, self.scope),
            "etaMs": self.eta_ms(),
            "retried": sum(1 for row in self.results if row.get("retried")),
        }

    def eta_ms(self) -> int | None:
        """预计还要多久；没开始出结果或已经结束时为 None。"""

        if self.status != "running" or not self.done:
            return None
        elapsed = time.time() - self.created_at
        return int(elapsed / self.done * (len(self.ids) - self.done) * 1000)

    def failed_ids(self) -> list[str]:
        return [str(row.get("id")) for row in self.results if not row.get("ok") and row.get("reason") not in {"not_found", "missing_file"}]

    def detail(self) -> dict[str, Any]:
        return {**self.summary(), "results": list(self.results)}


_jobs: dict[str, LabelJob] = {}


def reset_for_tests() -> None:
    for job in _jobs.values():
        if job.task is not None and not job.task.done():
            job.task.cancel()
    _jobs.clear()


def running_job() -> LabelJob | None:
    return next((job for job in _jobs.values() if job.status == "running"), None)


def list_jobs() -> list[dict[str, Any]]:
    return [job.summary() for job in sorted(_jobs.values(), key=lambda item: -item.created_at)]


def get_job(job_id: str) -> LabelJob | None:
    return _jobs.get(job_id)


def unlabeled_ids() -> list[str]:
    """从没被 AI 标注过的（含撤销过的）表情包编号：批量“只标没标过的”用。"""

    return [item.sticker_id for item in all_stickers() if not item.ai or item.ai.get("undone")]


def placeholder_ids() -> list[str]:
    """名字还是文件名/编号的。"""

    return [item.sticker_id for item in all_stickers() if item.placeholder_name]


def low_confidence_ids(threshold: float = LOW_CONFIDENCE) -> list[str]:
    """AI 标注过、但它自己把握不大的（重标一次往往更好）。"""

    output: list[str] = []
    for item in all_stickers():
        ai = item.ai
        confidence = ai.get("confidence")
        if ai and not ai.get("undone") and isinstance(confidence, (int, float)) and confidence < threshold:
            output.append(item.sticker_id)
    return output


def scope_ids(scope: str, ids: list[str] | None = None, *, retry_of: str = "") -> list[str]:
    if scope == "unlabeled":
        return unlabeled_ids()
    if scope == "all":
        return [item.sticker_id for item in all_stickers()]
    if scope == "placeholder":
        return placeholder_ids()
    if scope == "low_confidence":
        return low_confidence_ids()
    if scope == "retry_failed":
        previous = _jobs.get(retry_of) or next((job for job in sorted(_jobs.values(), key=lambda item: -item.created_at) if job.status != "running" and job.failed_ids()), None)
        return previous.failed_ids() if previous is not None else []
    return list(ids or [])


async def label_one(sticker_id: str, *, mode: str, fields: tuple[str, ...], hold_unsafe: bool, style: str, retry: bool = False) -> dict[str, Any]:
    """标注并写回一张；返回给 WebUI 的这一行结果。"""

    item = get_sticker(sticker_id)
    if item is None:
        return {"id": sticker_id, "ok": False, "reason": "not_found", "reasonText": "表情包不存在"}
    try:
        data = sticker_path(item).read_bytes()
    except (StickerError, OSError):
        return {"id": sticker_id, "name": item.name, "ok": False, "reason": "missing_file", "reasonText": "图片文件丢了"}
    hint_parts = [] if item.placeholder_name else [f"现在叫“{item.name}”"]
    if item.tags:
        hint_parts.append("标签：" + "、".join(item.tags))
    names = [name for name in known_names() if name != item.name]
    label = await label_image(data, hint="；".join(hint_parts), known_names=names, style=style, background=True, retry=retry)
    row: dict[str, Any] = {"id": sticker_id, "before": {"name": item.name, "tags": list(item.tags), "description": item.description}}
    if not label.ok:
        from .sticker_labeler import REASON_TEXT

        return {**row, "name": item.name, "ok": False, "reason": label.reason, "reasonText": REASON_TEXT.get(label.reason, label.reason)}
    current = {"name": item.name, "tags": list(item.tags), "description": item.description, "aliases": list(item.aliases), "name_is_placeholder": item.placeholder_name}
    changes = merge_label(current, label, mode=mode, fields=fields)
    if not load_settings().ai_label_aliases:
        changes.pop("aliases", None)
    updated = apply_label(sticker_id, changes, label.meta())
    held = False
    if hold_unsafe and not label.safe and updated.status == STATUS_ACTIVE:
        updated = update_sticker(sticker_id, status=STATUS_PENDING)
        held = True
    return {
        **row,
        "ok": True,
        "name": updated.name,
        "changed": sorted(changes),
        "after": {"name": updated.name, "tags": list(updated.tags), "description": updated.description},
        "text": label.text,
        "kind": label.kind,
        "safe": label.safe,
        "confidence": round(label.confidence, 2),
        "held": held,
        "cached": label.cached,
        "retried": label.retried,
        "mood": label.mood,
    }


async def _run(job: LabelJob) -> None:
    settings = load_settings()
    semaphore = asyncio.Semaphore(min(max(int(settings.ai_label_concurrency), 1), 4))

    async def worker(sticker_id: str) -> None:
        async with semaphore:
            if job.cancel_requested:
                return
            try:
                row = await label_one(
                    sticker_id,
                    mode=job.mode,
                    fields=job.fields,
                    hold_unsafe=settings.ai_label_review_unsafe,
                    style=settings.ai_label_style,
                    retry=settings.ai_label_retry,
                )
            except Exception as exc:  # 单张失败不拖垮整批
                row = {"id": sticker_id, "ok": False, "reason": "error", "reasonText": type(exc).__name__}
            job.results.append(row)
            if row.get("ok"):
                note_labeled(1)

    try:
        await asyncio.gather(*(worker(sticker_id) for sticker_id in job.ids))
        job.status = "cancelled" if job.cancel_requested else "done"
    except asyncio.CancelledError:
        job.status = "cancelled"
        raise
    except Exception:
        job.status = "failed"
    finally:
        job.finished_at = time.time()
        with contextlib.suppress(Exception):
            from ..webui.hub import hub

            await hub.notify_change("stickers")


def start_job(ids: list[str], *, mode: str = "fill", fields: tuple[str, ...] | list[str] = FIELDS, scope: str = "selected") -> LabelJob:
    """开一个批量标注任务；已有任务在跑时拒绝（``busy``）。"""

    settings = load_settings()
    if not settings.ai_label_enabled:
        raise LabelJobError("disabled", "管理员关了 AI 标注")
    from .sticker_labeler import vision_ready

    if not vision_ready():
        raise LabelJobError("no_vision", "没有配置能看图的模型（AI_VISION_MODEL），没法标注")
    if running_job() is not None:
        raise LabelJobError("busy", "已经有一批在标注了，等它跑完或先取消")
    unique = [item for item in dict.fromkeys(str(value) for value in ids) if item][:MAX_IDS]
    if not unique:
        raise LabelJobError("empty", "没有要标注的表情包")
    clean_fields = tuple(item for item in FIELDS if item in set(fields)) or FIELDS
    job = LabelJob(f"j{next(_counter)}", unique, mode if mode in MODE_LABELS else "fill", clean_fields, scope=scope if scope in SCOPE_LABELS else "selected")
    _jobs[job.job_id] = job
    while len(_jobs) > MAX_JOBS:
        oldest = min((item for item in _jobs.values() if item.status != "running"), key=lambda item: item.created_at, default=None)
        if oldest is None:
            break
        _jobs.pop(oldest.job_id, None)
    job.task = asyncio.create_task(_run(job))
    return job


def cancel_job(job_id: str) -> LabelJob:
    job = _jobs.get(job_id)
    if job is None:
        raise LabelJobError("not_found", "任务不存在")
    job.cancel_requested = True
    return job


__all__ = [
    "CONCURRENCY",
    "FIELDS",
    "MODE_LABELS",
    "SCOPE_LABELS",
    "LabelJob",
    "LabelJobError",
    "cancel_job",
    "get_job",
    "label_one",
    "list_jobs",
    "low_confidence_ids",
    "placeholder_ids",
    "reset_for_tests",
    "running_job",
    "scope_ids",
    "start_job",
    "unlabeled_ids",
]
