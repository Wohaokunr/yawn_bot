# ruff: noqa: E501, PLR0913, FBT003
"""一张图进表情包库的统一入口：去重 → （需要时）AI 标注 → 按安全判断定状态 → 入库。

群聊 ``save_sticker`` / ``steal_sticker``、私聊 ``stickers``、WebUI 上传都走这里，
保证“没写名字就让识图模型起”“识图模型说不适合就进待审核”“同一张图不重复标注”
这些规则只有一份。
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
) -> IntakeResult:
    """存一张图；库里已有同一张图时直接返回旧记录（不标注、不改状态）。"""

    settings = settings or load_settings()
    existing = find_by_sha256(digest_of(data)) if data else None
    if existing is not None:
        return IntakeResult(existing, False, named_by="existing")
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
    )
    return IntakeResult(sticker, created, label, named_by=named_by, held_for_review=held, notes=notes)


__all__ = ["IntakeResult", "intake_image", "needs_label"]
