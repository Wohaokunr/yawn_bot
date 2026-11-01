# ruff: noqa: BLE001, E501, TC001, TC003, TID252
"""私聊 Agent 识图：把他发来的图片真的交给模型看。

之前私聊只把图片当文件放进虚拟电脑，模型拿到的是尺寸、颜色这类文件信息，
只能说“看不到图里画的是什么”。这里复用群聊同一套取图（``media.prepare_media_inputs``）
和识图路线（``turn_media.vision_route``）：

- ``direct``：对话模型能收图片，图片作为 image 块附在他这条消息里；
- ``caption``：对话模型只收文字，识图模型（``agent_image``）先转述，转述附在消息里；
- ``none``：两条路都不通，附一句“图片没能识别”，让模型如实说明。

转述按人在进程内记 30 分钟，后几轮他接着问“那张图”时还能对上；直接看图的路线
记住最近的图 5 分钟，他没发新图又紧接着追问时重新附上。
"""

from __future__ import annotations

import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from .log import dbg, dbg_exc
from .media_provider import MediaInput

MAX_IMAGES_PER_TURN = 4
# 私聊没有群号；取图、转述的日志和提示词覆盖用 0 作为私聊的伪群号（与表情包统计一致）。
PRIVATE_MEDIA_SCOPE = 0
CAPTION_MEMORY_SECONDS = 30 * 60
CAPTION_MEMORY_PER_USER = 6
RECENT_IMAGE_SECONDS = 5 * 60
RECENT_IMAGE_PER_USER = 2

UNSEEN_NOTE = "（他发了图片，但这次没能识别出图片内容，你看不到画面；如实说看不到，别假装看过，也别用代码去猜。）"
NO_ROUTE_NOTE = "（他发了图片，但现在没有能看图的模型，你看不到画面；如实说暂时看不了图，别假装看过，也别用代码去猜。）"

_captions: dict[int, list[tuple[float, str]]] = {}
_recent_images: dict[int, list[tuple[float, MediaInput]]] = {}


@dataclass(slots=True)
class PrivateVision:
    """一轮的识图结果：要附进他消息里的 image 块和/或文字。"""

    route: str = "none"
    blocks: list[dict[str, Any]] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    media_inputs: list[MediaInput] = field(default_factory=list)
    requested: int = 0
    loaded: int = 0
    replayed: bool = False

    @property
    def empty(self) -> bool:
        return not self.blocks and not self.notes

    def trace(self) -> dict[str, Any]:
        return {
            "route": self.route,
            "requested": self.requested,
            "loaded": self.loaded,
            "blocks": len(self.blocks),
            "notes": len(self.notes),
            "replayed": self.replayed,
        }


# ── 进程内记忆 ──────────────────────────────────────────────────────────────


def _remember_caption(user_id: int, caption: str, now: float) -> None:
    rows = [row for row in _captions.get(user_id, []) if now - row[0] < CAPTION_MEMORY_SECONDS]
    rows.append((now, caption))
    _captions[user_id] = rows[-CAPTION_MEMORY_PER_USER:]


def recent_captions(user_id: int, *, now: float | None = None) -> list[str]:
    """他最近发过的图片的转述（旧→新），给资料里的 recent_images 用。"""

    now = time.monotonic() if now is None else now
    rows = [row for row in _captions.get(user_id, []) if now - row[0] < CAPTION_MEMORY_SECONDS]
    _captions[user_id] = rows
    return [caption for _ts, caption in rows]


def _remember_images(user_id: int, media: list[MediaInput], now: float) -> None:
    rows = [
        row for row in _recent_images.get(user_id, []) if now - row[0] < RECENT_IMAGE_SECONDS
    ]
    rows.extend((now, item) for item in media)
    _recent_images[user_id] = rows[-RECENT_IMAGE_PER_USER:]


def _replay_images(user_id: int, now: float) -> list[MediaInput]:
    rows = [
        row for row in _recent_images.get(user_id, []) if now - row[0] < RECENT_IMAGE_SECONDS
    ]
    _recent_images[user_id] = rows
    return [item for _ts, item in rows]


def reset_for_tests() -> None:
    _captions.clear()
    _recent_images.clear()


# ── 一轮 ───────────────────────────────────────────────────────────────────


def image_refs(media: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """本轮媒体段里的图片（不含文件/语音/视频），最多 MAX_IMAGES_PER_TURN 张。"""

    refs = []
    for item in media or []:
        if str(item.get("type") or "") != "image":
            continue
        refs.append({**item, "source": "current", "type": "image"})
        if len(refs) >= MAX_IMAGES_PER_TURN:
            break
    return refs


async def _load(bot: Any, refs: list[dict[str, Any]]) -> list[MediaInput]:
    from .media import prepare_media_inputs

    media_inputs, _captions_unused, _digests = await prepare_media_inputs(
        bot, PRIVATE_MEDIA_SCOPE, refs, session=None, cache_enabled=False
    )
    return media_inputs


async def caption_images(
    user_id: int, media_inputs: list[MediaInput], *, now: float | None = None
) -> list[str]:
    """识图模型逐张转述；转述不出来的那张给一句看不到。"""

    from .turn_media import caption_media

    now = time.monotonic() if now is None else now
    notes: list[str] = []
    total = len(media_inputs)
    for index, item in enumerate(media_inputs, start=1):
        label = f"图片{index}" if total > 1 else "图片"
        caption = None
        try:
            caption = await caption_media(PRIVATE_MEDIA_SCOPE, item)
        except Exception:
            dbg_exc(f"私聊用户 {user_id} 图片转述失败(忽略)")
        if caption:
            _remember_caption(user_id, caption, now)
            notes.append(f"（{label}内容，识图模型看到的：{caption}）")
        else:
            notes.append(f"（{label}没能识别出内容，你看不到这张图；如实说明。）")
    return notes


async def prepare_private_vision(
    bot: Any,
    user_id: int,
    media: list[dict[str, Any]],
    *,
    load: Callable[[Any, list[dict[str, Any]]], Awaitable[list[MediaInput]]] | None = None,
    route: str | None = None,
) -> PrivateVision:
    """取本轮图片并按识图路线准备好要附进消息的内容。没有图片时返回空结果。"""

    from .turn_media import vision_route

    now = time.monotonic()
    refs = image_refs(media)
    resolved_route = route or str(vision_route().get("route") or "none")
    vision = PrivateVision(route=resolved_route, requested=len(refs))
    if not refs:
        if resolved_route == "direct":
            replay = _replay_images(user_id, now)
            if replay:
                vision.replayed = True
                vision.media_inputs = replay
                vision.blocks = await _blocks(replay)
        return vision
    if resolved_route == "none":
        vision.notes.append(NO_ROUTE_NOTE)
        return vision
    try:
        media_inputs = await (load or _load)(bot, refs)
    except Exception:
        dbg_exc(f"私聊用户 {user_id} 取图失败(忽略)")
        media_inputs = []
    vision.loaded = len(media_inputs)
    vision.media_inputs = media_inputs
    if not media_inputs:
        vision.notes.append(UNSEEN_NOTE)
        return vision
    if resolved_route == "direct":
        vision.blocks = await _blocks(media_inputs)
        if vision.blocks:
            _remember_images(user_id, media_inputs, now)
        else:
            vision.notes.append(UNSEEN_NOTE)
    else:
        vision.notes.extend(await caption_images(user_id, media_inputs, now=now))
    if len(media_inputs) < len(refs):
        vision.notes.append(f"（他发了 {len(refs)} 张图，其中 {len(refs) - len(media_inputs)} 张没取到。）")
    dbg(f"私聊用户 {user_id} 识图 {vision.trace()}")
    return vision


async def _blocks(media_inputs: list[MediaInput]) -> list[dict[str, Any]]:
    from .media import build_media_content_blocks

    try:
        blocks, _bound = await build_media_content_blocks(
            media_inputs, task="agent_dialogue", group_id=PRIVATE_MEDIA_SCOPE
        )
    except Exception:
        dbg_exc("私聊图片转成模型输入失败(忽略)")
        return []
    return blocks


async def fallback_to_captions(user_id: int, vision: PrivateVision) -> PrivateVision:
    """对话模型拒收图片：改用识图模型转述（没配识图模型就给一句看不到）。"""

    from ..llm import resolve_llm_request, vision_model_configured
    from .turn_media import remember_multimodal_unsupported

    remember_multimodal_unsupported(resolve_llm_request("agent_dialogue"))
    _recent_images.pop(user_id, None)
    fallback = PrivateVision(
        route="caption",
        media_inputs=vision.media_inputs,
        requested=vision.requested,
        loaded=vision.loaded,
        replayed=vision.replayed,
    )
    if vision.replayed:
        # 追问时重附的旧图被拒：转述已在资料里的 recent_images，不再重复。
        return fallback
    if vision_model_configured() and vision.media_inputs:
        fallback.notes = await caption_images(user_id, vision.media_inputs)
    else:
        fallback.route = "none"
        fallback.notes = [NO_ROUTE_NOTE]
    return fallback


def attach_to_messages(messages: list[dict[str, Any]], vision: PrivateVision) -> None:
    """把识图结果附到最后一条 user 消息上（只改 user 消息，不碰历史 assistant）。"""

    if vision.empty:
        return
    for message in reversed(messages):
        if message.get("role") != "user":
            continue
        content = message.get("content")
        if isinstance(content, list):
            text = "\n".join(
                str(block.get("text") or "")
                for block in content
                if isinstance(block, dict) and block.get("type") == "text"
            )
        else:
            text = str(content or "")
        if vision.notes:
            text = "\n".join([text, *vision.notes]).strip()
        if vision.blocks:
            message["content"] = [{"type": "text", "text": text or "[图片]"}, *vision.blocks]
        else:
            message["content"] = text
        return


__all__ = [
    "PrivateVision",
    "attach_to_messages",
    "caption_images",
    "fallback_to_captions",
    "image_refs",
    "prepare_private_vision",
    "recent_captions",
    "reset_for_tests",
]
