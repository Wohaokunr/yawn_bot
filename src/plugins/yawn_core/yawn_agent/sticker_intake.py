# ruff: noqa: E501, PLR0913, FBT003, C901, PLR0912
"""一张图进表情包库的统一入口：去重 → （需要时）AI 标注 → 按安全判断定状态 → 入库。

群聊 ``save_sticker`` / ``steal_sticker``、私聊 ``stickers``、WebUI 上传都走这里，
保证“没写名字就让识图模型起”“识图模型说不适合就进待审核”“同一张图不重复标注”
这些规则只有一份。

3.0 起这里还管：被管理员删进回收站 / 拉黑的图不再收（上传除外，上传会把它捞回来）；
偷图/收藏来的图按策略只在来源群用（``steal_scope`` / ``collect_scope``）；识图模型
把握太低的先进待审核；记下宽高帧数；AI 给的别名一并入库。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .sticker_labeler import StickerLabel, label_image
from .sticker_library import (
    STATUS_ACTIVE,
    STATUS_PENDING,
    Sticker,
    StickerError,
    add_sticker,
    clean_name,
    clean_tags,
    digest_of,
    find_by_sha256,
    is_blocked,
    known_names,
)
from .sticker_policy import StickerSettings, load_settings, note_labeled


@dataclass(slots=True)
class IntakeResult:
    sticker: Sticker
    created: bool
    label: StickerLabel | None = None
    # 谁起的名字：given（调用方给的）/ ai / fallback（文件名之类）
    named_by: str = "given"
    held_for_review: bool = False
    notes: list[str] = field(default_factory=list)
    # 这张图原来在回收站里，这次（管理员上传）把它捞回来了。
    restored: bool = False
    # 只在来源群可用（策略 steal_scope / collect_scope = origin）。
    scoped_to_origin: bool = False


def check_image_shape(data: bytes, settings: StickerSettings) -> None:
    """偷图前的结构检查：动图偷不偷、是不是小到看不清。只看文件头，不看内容。"""

    from .sticker_meta import image_info

    info = image_info(data)
    if info.animated and not settings.steal_animated:
        raise StickerError("animated", "这张是动图，管理员设了不偷动图")
    if info.known and settings.steal_min_side > 0 and min(info.width, info.height) < settings.steal_min_side:
        raise StickerError("too_small", f"这张图太小了（{info.width}×{info.height}），不偷")


def needs_label(name: str, tags: Any) -> bool:
    return not clean_name(name) or not clean_tags(tags or [])


async def intake_image(
    data: bytes,
    *,
    name: str = "",
    tags: Any = (),
    description: str = "",
    source: str,
    status: str,
    added_by: int | None = None,
    added_group: int | None = None,
    origin: dict[str, Any] | None = None,
    hint: str = "",
    fallback_name: str = "",
    use_ai: bool = True,
    settings: StickerSettings | None = None,
    background: bool = False,
    packs: Any = (),
) -> IntakeResult:
    """存一张图；库里已有同一张图时直接返回旧记录（不标注、不改状态）。"""

    settings = settings or load_settings()
    is_upload = source == "upload"
    digest = digest_of(data) if data else ""
    existing = find_by_sha256(digest, include_trashed=True) if data else None
    if existing is not None and not existing.deleted:
        return IntakeResult(existing, False, named_by="existing")
    if existing is not None:
        if not is_upload:
            raise StickerError("trashed", "这张图被管理员删掉了（还在回收站里），不再收")
        from .sticker_library import restore_sticker

        return IntakeResult(restore_sticker(existing.sticker_id), False, named_by="existing", restored=True, notes=["这张图原来在回收站里，已经捞回来了"])
    if digest and is_blocked(digest) and not is_upload:
        # 先查黑名单再花钱标注。
        raise StickerError("blocked", "这张图被管理员拉黑了，不再收")
    label: StickerLabel | None = None
    final_name = clean_name(name)
    final_tags = list(clean_tags(tags or []))
    final_description = description
    named_by = "given" if final_name else "fallback"
    if use_ai and settings.ai_label_enabled and needs_label(final_name, final_tags):
        label = await label_image(
            data,
            hint=hint,
            known_names=known_names(),
            style=settings.ai_label_style,
            background=background,
            retry=settings.ai_label_retry,
        )
        if label.ok:
            note_labeled(1)
            if not final_name:
                final_name = label.name
                named_by = "ai"
            if not final_tags:
                final_tags = list(label.tags)
            if not final_description:
                final_description = label.description
    if not final_name:
        final_name = clean_name(fallback_name)
    if not final_name:
        reason = label.reason if label is not None else "no_name"
        raise StickerError("no_name", "没能给这张图起名字" + (f"（{reason}）" if reason else ""))
    final_status = status
    held = False
    notes: list[str] = []
    if label is not None and label.ok and not label.safe and settings.ai_label_review_unsafe and status == STATUS_ACTIVE:
        final_status = STATUS_PENDING
        held = True
        notes.append("识图模型觉得这张不太适合当表情包，先交给管理员审核")
    elif (
        label is not None
        and label.ok
        and not is_upload
        and named_by == "ai"
        and settings.review_below_confidence > 0
        and label.confidence < settings.review_below_confidence
        and status == STATUS_ACTIVE
    ):
        final_status = STATUS_PENDING
        held = True
        notes.append("识图模型对起的名字没什么把握，先交给管理员看一眼")
    scope = settings.steal_scope if source == "stolen" else settings.collect_scope if source == "member" else "all"
    scoped = scope == "origin" and bool(added_group)
    sticker, created = add_sticker(
        data,
        name=final_name,
        tags=final_tags,
        description=final_description,
        source=source,
        status=final_status,
        added_by=added_by,
        added_group=added_group,
        max_bytes=settings.max_bytes,
        ai=label.meta() if label is not None and label.ok else None,
        origin=origin,
        groups=(added_group,) if scoped else (),
        aliases=label.aliases if label is not None and label.ok and named_by == "ai" and settings.ai_label_aliases else (),
        packs=packs,
        restore_trashed=is_upload,
        allow_blocked=is_upload,
    )
    if scoped and created:
        notes.append("这张先只在来源群用")
    return IntakeResult(sticker, created, label, named_by=named_by, held_for_review=held, notes=notes, scoped_to_origin=scoped and created)


__all__ = ["IntakeResult", "check_image_shape", "intake_image", "needs_label"]
