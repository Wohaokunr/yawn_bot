# ruff: noqa: E501, PLR0913, TID252, FBT001, FBT002, FBT003
"""表情包 AI 标注：让识图模型给一张图起名字、打情绪标签、写“什么时候发”、认出图上的字。

三个地方共用这一份：WebUI 上传（逐张预览/上传时自动补全）、WebUI 批量重新标注
（``sticker_label_jobs``）、Agent 收藏/偷图（``save_sticker`` 不写名字时、``steal_sticker``、
私聊 ``stickers``）。

约束：

- 走 ``agent_image`` 任务（识图模型）；没配识图模型时直接返回 ``reason="no_vision"``，
  不发请求，调用方退回“文件名/手填”；
- 不限输出长度（``NO_OUTPUT_LIMIT``，项目硬约束），只要求返回一段 JSON；
- 模型的输出一律当数据清洗：名字/标签/说明走 ``sticker_library`` 同一套清洗，标签去重、
  去掉和名字重复的，图上文字单独存（搜索用，不进菜单）；
- 同一张图（sha256）进程内缓存 30 分钟：上传前预览过的，上传时不再花一次钱；
- 提示词登记为 ``sticker.label``，WebUI 提示词页可改；管理员在策略里写的“命名风格”
  追加在后面。
"""

from __future__ import annotations

import base64
import hashlib
import json
import re
import time
from collections import OrderedDict
from dataclasses import asdict, dataclass, field
from typing import Any

from .sticker_library import (
    MAX_NAME_CHARS,
    MAX_TAGS,
    clean_description,
    clean_name,
    clean_tags,
    sniff_image,
)

LABEL_TIMEOUT_SECONDS = 60.0
CACHE_SECONDS = 30 * 60
CACHE_LIMIT = 256
MAX_TEXT_CHARS = 80
MAX_KNOWN_NAMES = 60
MAX_STYLE_CHARS = 300
KINDS = ("sticker", "photo", "screenshot", "other")
KIND_LABELS = {"sticker": "表情包", "photo": "照片", "screenshot": "截图", "other": "其他"}

_LABEL_SYSTEM_PROMPT = (
    "你是 QQ 群表情包管理员，负责给表情包起名字和打标签，方便聊天机器人以后在合适的时候发它。"
    "只看图片本身，不猜图里人物的真实身份，不写隐私信息。"
    "只返回一个 JSON 对象，不要多余文字，字段：\n"
    '{"name": "2~6 个字的名字，说它表达的情绪或梗，像群友叫它那样，如 捂脸笑、狗头、震惊、裂开、无语、贴贴",'
    ' "tags": ["2~4 个情绪/场景标签，每个 2~4 字，如 尴尬、开心、调侃、安慰、晚安"],'
    ' "description": "什么时候适合发，12 个字以内，如 被戳穿又不想承认时",'
    ' "text": "图上能看清的文字原文，没有就写空字符串",'
    ' "kind": "sticker（表情包/梗图）、photo（普通照片）、screenshot（聊天或网页截图）、other 之一",'
    ' "safe": "true 或 false：含色情、血腥、歧视、真人隐私（证件、聊天记录里的真名电话等）时为 false",'
    ' "confidence": "0~1 的小数，你对名字贴切程度的把握"}\n'
    "名字和标签要具体、口语化，别用“表情包”“图片”“搞笑”这种空词；图上有字时名字优先取图上的梗。"
)


def label_system_prompt() -> str:
    from .prompt_sections import system_messages

    messages = system_messages("sticker.label", _LABEL_SYSTEM_PROMPT)
    return "\n".join(str(item.get("content") or "") for item in messages) or _LABEL_SYSTEM_PROMPT


@dataclass(slots=True)
class StickerLabel:
    """一次标注的结果。``ok`` 为 False 时 ``reason`` 说明原因，其余字段可能为空。"""

    ok: bool
    name: str = ""
    tags: tuple[str, ...] = ()
    description: str = ""
    text: str = ""
    kind: str = "sticker"
    safe: bool = True
    confidence: float = 0.0
    model: str = ""
    reason: str = ""
    cached: bool = False
    duration_ms: int = 0
    warnings: list[str] = field(default_factory=list)

    def public(self) -> dict[str, Any]:
        data = asdict(self)
        data["tags"] = list(self.tags)
        data["kindLabel"] = KIND_LABELS.get(self.kind, self.kind)
        data["durationMs"] = data.pop("duration_ms")
        return data

    def meta(self) -> dict[str, Any]:
        """写进库记录 ``ai`` 字段的内容（不含名字/标签本身）。"""

        return {
            "model": self.model[:60],
            "confidence": round(self.confidence, 2),
            "kind": self.kind,
            "safe": self.safe,
            "text": self.text,
            "labeled_at": _now_text(),
        }


REASON_TEXT = {
    "no_vision": "没有配置能看图的模型（AI_VISION_MODEL）",
    "not_image": "不是支持的图片格式",
    "empty_result": "识图模型没有返回结果",
    "bad_json": "识图模型返回的不是 JSON",
    "no_name": "识图模型没给出名字",
    "disabled": "管理员关了 AI 标注",
    "error": "标注时出错",
}


def _now_text() -> str:
    from .context import now_beijing

    return now_beijing().strftime("%Y-%m-%d %H:%M:%S")


_cache: OrderedDict[str, tuple[float, StickerLabel]] = OrderedDict()
_stats: dict[str, int] = {"requests": 0, "ok": 0, "failed": 0, "cached": 0}
_last_error: dict[str, Any] = {}


def reset_for_tests() -> None:
    _cache.clear()
    for key in _stats:
        _stats[key] = 0
    _last_error.clear()


def stats_snapshot() -> dict[str, Any]:
    return {**_stats, "cacheSize": len(_cache), "lastError": dict(_last_error) or None}


def _cache_key(digest: str, style: str, known: tuple[str, ...]) -> str:
    extra = hashlib.sha256(f"{style}\x00{','.join(known)}".encode()).hexdigest()[:12]
    return f"{digest}:{extra}"


def _cache_get(key: str) -> StickerLabel | None:
    row = _cache.get(key)
    if row is None:
        return None
    if time.monotonic() - row[0] > CACHE_SECONDS:
        _cache.pop(key, None)
        return None
    _cache.move_to_end(key)
    return row[1]


def _cache_put(key: str, label: StickerLabel) -> None:
    _cache[key] = (time.monotonic(), label)
    _cache.move_to_end(key)
    while len(_cache) > CACHE_LIMIT:
        _cache.popitem(last=False)


_JSON_RE = re.compile(r"\{.*\}", re.DOTALL)
_FENCE_RE = re.compile(r"^```(?:json)?\s*|\s*```$", re.IGNORECASE)
_EMPTY_WORDS = frozenset({"表情包", "表情", "图片", "图", "搞笑", "无", "未知", "sticker", "image"})


def _parse_bool(value: Any, default: bool = True) -> bool:
    if isinstance(value, bool):
        return value
    text = str(value or "").strip().casefold()
    if text in {"false", "0", "no", "否", "不安全"}:
        return False
    if text in {"true", "1", "yes", "是", "安全"}:
        return True
    return default


def _parse_confidence(value: Any) -> float:
    try:
        number = float(str(value).strip().rstrip("%"))
    except (TypeError, ValueError):
        return 0.5
    if number > 1:
        number /= 100
    return min(max(number, 0.0), 1.0)


def parse_label(raw: str | None) -> StickerLabel:
    """把模型输出解析成标注；容忍代码块、前后废话、字段类型不对。纯函数。"""

    text = _FENCE_RE.sub("", str(raw or "").strip())
    if not text:
        return StickerLabel(False, reason="empty_result")
    match = _JSON_RE.search(text)
    try:
        data = json.loads(match.group(0) if match else text)
    except (TypeError, ValueError):
        return StickerLabel(False, reason="bad_json")
    if not isinstance(data, dict):
        return StickerLabel(False, reason="bad_json")
    warnings: list[str] = []
    ocr = clean_description(data.get("text") or data.get("ocr") or "")[:MAX_TEXT_CHARS]
    raw_name = clean_name(data.get("name") or "")
    raw_tags = data.get("tags") or []
    if isinstance(raw_tags, str):
        raw_tags = re.split(r"[,，、/\s]+", raw_tags)
    tags = [tag for tag in clean_tags(raw_tags) if tag.casefold() not in _EMPTY_WORDS]
    name = raw_name if raw_name and raw_name.casefold() not in _EMPTY_WORDS else ""
    if not name and tags:
        name = tags[0]
        warnings.append("名字取了第一个标签")
    if not name and ocr:
        name = clean_name(ocr)[:6]
        warnings.append("名字取了图上的文字")
    if not name:
        return StickerLabel(False, reason="no_name", text=ocr)
    if len(raw_name) > MAX_NAME_CHARS:
        warnings.append("名字太长，截短了")
    # 标签里和名字一样的不重复放（菜单里名字本身就能用）。
    tags = [tag for tag in tags if tag.casefold() != name.casefold()][: MAX_TAGS - 2]
    kind = str(data.get("kind") or "sticker").strip().casefold()
    return StickerLabel(
        True,
        name=name,
        tags=tuple(tags),
        description=clean_description(data.get("description") or "")[:40],
        text=ocr,
        kind=kind if kind in KINDS else "other",
        safe=_parse_bool(data.get("safe"), True),
        confidence=_parse_confidence(data.get("confidence", 0.5)),
        warnings=warnings,
    )


def _user_prompt(*, hint: str, known: tuple[str, ...], style: str) -> str:
    lines = ["给这张图起名字、打标签。"]
    if hint:
        lines.append(f"参考信息（可能不准，以图为准）：{hint[:120]}")
    if known:
        lines.append("库里已经有这些名字，意思不同时别撞名，意思一样可以同名：" + "、".join(known[:MAX_KNOWN_NAMES]))
    if style:
        lines.append(f"管理员的命名要求：{style[:MAX_STYLE_CHARS]}")
    from .prompt_sections import apply_section

    return apply_section("sticker.label_request", "\n".join(lines)) or "给这张图起名字、打标签。"


def _image_block(data: bytes, mime: str) -> dict[str, Any]:
    encoded = base64.b64encode(data).decode("ascii")
    return {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{encoded}"}}


def vision_ready() -> bool:
    from ..llm import vision_model_configured

    try:
        return bool(vision_model_configured())
    except Exception:  # noqa: BLE001
        return False


def vision_status() -> dict[str, Any]:
    """WebUI 用：识图模型是谁、能不能用。"""

    from ..llm import resolve_llm_request

    try:
        request = resolve_llm_request("agent_image")
        model, provider = request.model, request.provider
    except Exception:  # noqa: BLE001
        model, provider = "", ""
    return {"ready": vision_ready(), "model": model, "provider": provider, "stats": stats_snapshot()}


async def label_image(
    data: bytes,
    *,
    hint: str = "",
    known_names: tuple[str, ...] | list[str] = (),
    style: str = "",
    use_cache: bool = True,
    background: bool = False,
    timeout: float = LABEL_TIMEOUT_SECONDS,
) -> StickerLabel:
    """识图模型给一张图起名打标签。失败不抛异常，返回 ``ok=False`` 的结果。"""

    kind = sniff_image(data)
    if kind is None:
        return StickerLabel(False, reason="not_image")
    if not vision_ready():
        return StickerLabel(False, reason="no_vision")
    known = tuple(dict.fromkeys(clean_name(item) for item in known_names if clean_name(item)))[:MAX_KNOWN_NAMES]
    style = str(style or "").strip()[:MAX_STYLE_CHARS]
    digest = hashlib.sha256(data).hexdigest()
    key = _cache_key(digest, style, known)
    if use_cache:
        cached = _cache_get(key)
        if cached is not None:
            _stats["cached"] += 1
            result = StickerLabel(**{**asdict(cached), "tags": cached.tags, "warnings": list(cached.warnings)})
            result.cached = True
            return result
    from ..llm import NO_OUTPUT_LIMIT, complete, resolve_llm_request

    started = time.perf_counter()
    _stats["requests"] += 1
    messages: list[dict[str, Any]] = [
        {"role": "system", "content": label_system_prompt()},
        {
            "role": "user",
            "content": [
                {"type": "text", "text": _user_prompt(hint=hint, known=known, style=style)},
                _image_block(data, kind[1]),
            ],
        },
    ]
    try:
        raw = await complete(  # pyright: ignore[reportArgumentType]
            messages,  # pyright: ignore[reportArgumentType]
            task="agent_image",
            max_tokens=NO_OUTPUT_LIMIT,
            timeout=timeout,
            multimodal=True,
            priority="background" if background else None,
        )
    except Exception as exc:  # noqa: BLE001
        raw = None
        _last_error.update({"at": _now_text(), "error": type(exc).__name__})
    label = parse_label(raw) if raw else StickerLabel(False, reason="empty_result")
    label.duration_ms = int((time.perf_counter() - started) * 1000)
    try:
        label.model = resolve_llm_request("agent_image").model
    except Exception:  # noqa: BLE001
        label.model = ""
    if label.ok:
        _stats["ok"] += 1
        _cache_put(key, label)
    else:
        _stats["failed"] += 1
        _last_error.update({"at": _now_text(), "error": label.reason})
    return label


def merge_label(
    current: dict[str, Any],
    label: StickerLabel,
    *,
    mode: str = "fill",
    fields: tuple[str, ...] = ("name", "tags", "description"),
) -> dict[str, Any]:
    """按模式把标注合进现有值，返回要改的字段（纯函数）。

    ``fill``：只补空的（名字没起过/是文件名、没有标签、没有说明）；
    ``merge``：名字只在空时改，标签取并集，说明只在空时补；
    ``replace``：全部换成 AI 的。
    """

    changes: dict[str, Any] = {}
    if not label.ok:
        return changes
    name = str(current.get("name") or "")
    tags = [str(item) for item in current.get("tags") or []]
    description = str(current.get("description") or "")
    weak_name = not name or bool(current.get("name_is_placeholder"))
    if "name" in fields and label.name and (mode == "replace" or weak_name) and label.name != name:
        changes["name"] = label.name
    if "tags" in fields and label.tags:
        if mode == "replace":
            new_tags = list(label.tags)
        elif mode == "merge":
            new_tags = list(clean_tags([*tags, *label.tags]))
        else:
            new_tags = tags or list(label.tags)
        if new_tags != tags:
            changes["tags"] = new_tags
    if "description" in fields and label.description and (mode == "replace" or not description) and label.description != description:
        changes["description"] = label.description
    return changes


__all__ = [
    "KINDS",
    "KIND_LABELS",
    "REASON_TEXT",
    "StickerLabel",
    "label_image",
    "label_system_prompt",
    "merge_label",
    "parse_label",
    "reset_for_tests",
    "stats_snapshot",
    "vision_ready",
    "vision_status",
]
