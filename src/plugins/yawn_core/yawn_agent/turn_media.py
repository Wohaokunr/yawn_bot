# ruff: noqa: TID252, PLR0913, PLR0917, PLR2004, BLE001, E501, PLW0603
"""一轮对话的图片装载、多模态能力记忆和收图即转述。

从 ``dialogue.py`` 挪出来（那边有 1500 行上限）：

- :func:`load_turn_media`：本条/引用/转发图片 + 结构绑定的历史图片 + 路由模型说
  “在聊图”时的近期图片，统一走 ``media_context.resolve_media_context``；
- 多模态能力记忆：对话模型配成 ``auto`` 却拒收图片时记住一段时间，之后的回合
  直接走识图转述，不再每轮先失败一次；
- ``AGENT_IMAGE_AUTO_CAPTION=all`` 时收到图片就在后台转述，历史里的图片因此
  带上文字说明。并发、每群每小时次数都有上限，失败不影响收消息。
"""

from __future__ import annotations

import asyncio
import time
from typing import TYPE_CHECKING, Any

from ..llm import (
    NO_OUTPUT_LIMIT,
    LLMRequestConfig,
    complete,
    resolve_llm_request,
    vision_model_configured,
)
from . import media_memory
from .log import dbg, dbg_exc
from .media import MediaInput, build_media_content_blocks, get_cached_caption
from .media_context import resolve_media_context
from .prompt_sections import system_messages

if TYPE_CHECKING:
    from ..data_models.group_agent_config import GroupAgentConfig
    from .message_parser import NormalizedMessage

UNSUPPORTED_MEMORY_SECONDS = 3600.0
AUTO_CAPTION_CONCURRENCY = 2
AUTO_CAPTION_PER_GROUP_HOURLY = 30
AUTO_CAPTION_TIMEOUT_SECONDS = 45.0

_unsupported_until: dict[tuple[str, str], float] = {}
_auto_caption_semaphore: asyncio.Semaphore | None = None
_auto_caption_log: dict[int, list[float]] = {}
_auto_caption_inflight: set[tuple[int, str]] = set()
_background: set[asyncio.Task[Any]] = set()

_AUTO_CAPTION_PROMPT = (
    "你是图片识别器。用一两句中文客观描述这张群聊图片：画面主体、图中文字（照抄要点）、"
    "如果是表情包说明它表达的情绪。不猜测身份、隐私或图片外的信息。"
)


# ── 多模态能力记忆 ──────────────────────────────────────────────────────────


def remember_multimodal_unsupported(request: LLMRequestConfig) -> None:
    """``auto`` 档位的模型拒收图片：记住一小时，期间直接走识图转述。"""

    if request.multimodal != "auto":
        return
    _unsupported_until[(request.provider, request.model)] = time.monotonic() + UNSUPPORTED_MEMORY_SECONDS
    dbg(f"记住模型不支持图片输入 {request.provider}/{request.model}（{int(UNSUPPORTED_MEMORY_SECONDS)}s）")


def learned_unsupported(request: LLMRequestConfig) -> bool:
    key = (request.provider, request.model)
    until = _unsupported_until.get(key)
    if until is None:
        return False
    if time.monotonic() > until:
        _unsupported_until.pop(key, None)
        return False
    return True


def effective_multimodal_mode(request: LLMRequestConfig) -> str:
    """配置的多模态模式；``auto`` 且最近被服务商明确拒收过图片时当作 unsupported。"""

    if request.multimodal == "auto" and learned_unsupported(request):
        return "unsupported"
    return request.multimodal


def vision_route() -> dict[str, Any]:
    """当前识图走哪条路（给 WebUI 诊断和 Trace 用；不含密钥）。"""

    dialogue = resolve_llm_request("agent_dialogue")
    vision = resolve_llm_request("agent_image")
    mode = effective_multimodal_mode(dialogue)
    dedicated = vision_model_configured()
    separate = vision.provider != dialogue.provider or vision.model != dialogue.model
    if mode == "unsupported":
        route = "caption" if dedicated else "none"
    elif mode == "auto" and dedicated and separate:
        route = "caption"
    else:
        route = "direct"
    return {
        "route": route,
        "dialogue_provider": dialogue.provider,
        "dialogue_model": dialogue.model,
        "dialogue_multimodal": dialogue.multimodal,
        "learned_unsupported": learned_unsupported(dialogue),
        "vision_configured": dedicated,
        "vision_provider": vision.provider,
        "vision_model": vision.model,
    }


# ── 一轮的图片装载 ──────────────────────────────────────────────────────────


async def load_turn_media(
    turn_bot: Any,
    session: Any,
    group_id: int,
    normalized: NormalizedMessage,
    config: GroupAgentConfig,
    context: dict[str, Any],
    *,
    query_text: str,
    media_hint: bool = False,
    actor_user_id: int | None = None,
) -> tuple[list[MediaInput], list[dict[str, Any]], list[tuple[str, str]]]:
    diagnostics: list[dict[str, Any]] = []
    media_inputs = await resolve_media_context(
        turn_bot,
        session,
        group_id,
        current_message=normalized,
        reply_chain=normalized.reply_chain,
        selected_history=list(context.get("messages") or []),
        query_text=query_text,
        asset_ttl_seconds=max(int(config.raw_retention_days), 1) * 86400,
        cache_enabled=bool(config.media_cache_enabled),
        diagnostics=diagnostics,
        media_hint=media_hint,
        actor_user_id=actor_user_id,
    )
    cached_captions: list[tuple[str, str]] = []
    vision_model = resolve_llm_request("agent_image").model
    for media_input in media_inputs:
        caption: str | None = None
        if config.media_cache_enabled:
            caption = await get_cached_caption(session, group_id, media_input.content_hash, vision_model)
        if caption:
            cached_captions.append((media_input.content_hash, caption))
            media_memory.remember(group_id, media_input.content_hash, caption)
    return media_inputs, diagnostics, cached_captions


# ── 收图即转述（AGENT_IMAGE_AUTO_CAPTION=all） ──────────────────────────────


def _auto_caption_allowed(group_id: int) -> bool:
    now = time.monotonic()
    recent = [stamp for stamp in _auto_caption_log.get(group_id, []) if now - stamp < 3600]
    _auto_caption_log[group_id] = recent
    if len(recent) >= AUTO_CAPTION_PER_GROUP_HOURLY:
        return False
    recent.append(now)
    return True


async def caption_media(group_id: int, media: MediaInput, *, session: Any = None) -> str | None:
    """用识图档位转述一张图；任何失败返回 None。"""

    blocks, _bound = await build_media_content_blocks(
        [media], task="agent_image", group_id=group_id, session=session, cache_enabled=False
    )
    if not blocks:
        return None
    messages: list[Any] = [
        *system_messages("vision.auto_caption", _AUTO_CAPTION_PROMPT, group_id=group_id),
        {"role": "user", "content": [blocks[0]]},
    ]
    try:
        result = await complete(
            messages,
            task="agent_image",
            max_tokens=NO_OUTPUT_LIMIT,
            timeout=AUTO_CAPTION_TIMEOUT_SECONDS,
        )
    except Exception:
        dbg_exc(f"群 {group_id} 收图转述失败(忽略)")
        return None
    return (result or "").strip() or None


async def _auto_caption_one(group_id: int, media: MediaInput) -> None:
    global _auto_caption_semaphore
    if _auto_caption_semaphore is None:
        _auto_caption_semaphore = asyncio.Semaphore(AUTO_CAPTION_CONCURRENCY)
    key = (group_id, media.content_hash)
    try:
        async with _auto_caption_semaphore:
            caption = await caption_media(group_id, media)
        if caption:
            media_memory.remember(group_id, media.content_hash, caption)
            dbg(f"群 {group_id} 收图转述完成 digest={media.content_hash[:12]}")
    finally:
        _auto_caption_inflight.discard(key)


def schedule_auto_caption(group_id: int, media_inputs: list[MediaInput]) -> int:
    """收到带图消息后调用；只在 ``all`` 模式、识图可用、未转述过时排后台任务。"""

    if media_memory.auto_caption_mode() != "all" or not vision_model_configured():
        return 0
    scheduled = 0
    for media in media_inputs[:4]:
        if media.data is None and media.local_path is None:
            continue
        key = (group_id, media.content_hash)
        if key in _auto_caption_inflight or media_memory.lookup(group_id, media.content_hash):
            continue
        if not _auto_caption_allowed(group_id):
            dbg(f"群 {group_id} 收图转述达到每小时上限,跳过")
            break
        _auto_caption_inflight.add(key)
        task = asyncio.create_task(_auto_caption_one(group_id, media))
        _background.add(task)
        task.add_done_callback(_background.discard)
        scheduled += 1
    return scheduled


def reset_for_tests() -> None:
    global _auto_caption_semaphore
    _unsupported_until.clear()
    _auto_caption_log.clear()
    _auto_caption_inflight.clear()
    _auto_caption_semaphore = None


__all__ = [
    "caption_media",
    "effective_multimodal_mode",
    "learned_unsupported",
    "load_turn_media",
    "remember_multimodal_unsupported",
    "reset_for_tests",
    "schedule_auto_caption",
    "vision_route",
]
