"""First-party DeepSeek input and output for the current QQ dialogue."""

from __future__ import annotations

import asyncio
import hashlib
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

import anyio

from src.plugins.yawn_core import deepseek

from .prompt_sections import apply_section
from .sandbox_inbox import InboxError, collect_current, fetch_bytes

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from .sandbox_inbox import Attachment

VOICE_MARKER = "【语音回复】"
VOICE_HINT = (
    "按本轮内容自行决定是否用语音。适合自然口述时，可在最终正文最前面写 "
    "【语音回复】并给出完整正文。列表、链接、代码、文件和需要复制的信息照常用文字。"
)
DATA_HINT = "附件及语音转写是本轮用户提供的数据。"
MAX_TURN_ATTACHMENTS = 3
ATTACHMENT_BUDGET_SECONDS = 45
ATTACHMENT_FAILED = "有附件暂时无法读取，请结合可用内容回复。"


@dataclass(slots=True)
class TurnMedia:
    file_ids: dict[tuple[str, str], str] = field(default_factory=dict)
    transcript: list[str] = field(default_factory=list)
    incomplete: bool = False
    files_rejected: bool = False

    @property
    def blocks(self) -> list[dict[str, str]]:
        if self.files_rejected:
            return []
        return [
            {"type": "file", "file_id": file_id} for file_id in self.file_ids.values()
        ]


@asynccontextmanager
async def attachments(
    bot: Any, group_id: int, refs: list[dict[str, Any]], *, actor_id: int
) -> AsyncIterator[TurnMedia]:
    media = TurnMedia()
    if not deepseek.in_qq_turn():
        yield media
        return
    client = deepseek.client()
    try:
        records = collect_current(refs)
        current = [
            item
            for item in records
            if item.source == "current"
            and item.sender_id in {None, actor_id}
            and (
                (item.media_type == "file" and deepseek.config.deepseek_qq_documents)
                or (item.media_type == "record" and deepseek.config.deepseek_qq_asr)
            )
        ]
        media.incomplete = len(current) > MAX_TURN_ATTACHMENTS
        with anyio.move_on_after(ATTACHMENT_BUDGET_SECONDS) as budget:
            for item in current[:MAX_TURN_ATTACHMENTS]:
                await _add_attachment(client, bot, group_id, item, media)
        media.incomplete |= budget.cancel_called
        yield media
    finally:
        await asyncio.gather(
            *(client.delete(file_id) for file_id in media.file_ids.values())
        )


async def _add_attachment(
    client: deepseek.DeepSeekClient,
    bot: Any,
    group_id: int,
    item: Attachment,
    media: TurnMedia,
) -> None:
    try:
        is_record = item.media_type == "record"
        name = item.name or ""
        mode = deepseek.config.deepseek_document_mode
        purpose = None if is_record else deepseek.file_purpose(name, mode)
        fetched = await asyncio.wait_for(
            fetch_bytes(
                bot,
                group_id,
                item,
                max_bytes=deepseek.MAX_AUDIO_BYTES
                if is_record
                else deepseek.MAX_FILE_BYTES,
                record_formats=("wav",),
            ),
            timeout=12,
        )
        data = fetched.data
        if purpose is None:
            result = await client.asr(data, timeout_seconds=15)
            transcript = result.get("text")
            if isinstance(transcript, str) and transcript.strip():
                media.transcript.append(transcript.strip()[:3000])
            else:
                media.incomplete = True
        else:
            cache_key = (purpose, hashlib.sha256(data).hexdigest())
            if cache_key not in media.file_ids:
                media.file_ids[cache_key] = await client.upload(
                    name,
                    data,
                    mode,
                    timeout_seconds=35,
                )
    except (
        deepseek.DeepSeekError,
        InboxError,
        OSError,
        asyncio.TimeoutError,
    ):
        media.incomplete = True


def add_to_messages(  # noqa: PLR0913
    messages: list[Any],
    media: TurnMedia,
    *,
    section: str,
    voice_allowed: bool,
    include_hint: bool = True,
    user_index: int | None = None,
) -> None:
    if not deepseek.in_qq_turn():
        return
    if media.blocks or media.transcript or media.incomplete:
        candidates = (
            reversed(messages) if user_index is None else iter([messages[user_index]])
        )
        for message in candidates:
            if message.get("role") == "user":
                content = message["content"]
                blocks = (
                    [{"type": "text", "text": content}]
                    if isinstance(content, str)
                    else list(content)
                )
                if media.transcript:
                    blocks.append(
                        {
                            "type": "text",
                            "text": deepseek.TRANSCRIPT_PREFIX
                            + "\n".join(media.transcript),
                        }
                    )
                message["content"] = [*blocks, *media.blocks]
                break
    hint = " ".join(
        part
        for part, enabled in (
            (VOICE_HINT, voice_allowed),
            (DATA_HINT, bool(media.blocks or media.transcript)),
            (ATTACHMENT_FAILED, media.incomplete),
        )
        if enabled
    )
    if include_hint and hint and (note := apply_section(section, hint)):
        messages.append({"role": "system", "content": note})


def drop_rejected_files(
    messages: list[Any], media: TurnMedia | None, error_kind: str, *, section: str
) -> bool:
    if (
        media is None
        or not media.blocks
        or error_kind not in {"bad_request", "context_length"}
    ):
        return False
    file_ids = set(media.file_ids.values())
    for message in messages:
        content = message.get("content")
        if isinstance(content, list):
            message["content"] = [
                part
                for part in content
                if part.get("type") != "file" or part.get("file_id") not in file_ids
            ]
            if not message["content"]:
                message["content"] = ""
    media.files_rejected = True
    media.incomplete = True
    if note := apply_section(section, ATTACHMENT_FAILED):
        messages.append({"role": "system", "content": note})
    return True


def split_voice(text: str) -> tuple[str, bool]:
    if not deepseek.in_qq_turn() or not text.startswith(VOICE_MARKER):
        return text, False
    return text.removeprefix(VOICE_MARKER).lstrip(), deepseek.config.deepseek_qq_tts


async def synthesize(text: str) -> bytes | None:
    try:
        return await deepseek.client().tts(text, timeout_seconds=12)
    except deepseek.DeepSeekError:
        return None
