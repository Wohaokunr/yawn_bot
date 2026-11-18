"""DeepSeek's first-party application APIs."""

from __future__ import annotations

import asyncio
import json
import time
from contextlib import asynccontextmanager, contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal
from urllib.parse import urlsplit

import anyio
import httpx
from loguru import logger
from nonebot import get_plugin_config
from pydantic import BaseModel, Field, SecretStr

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Iterator

BASE_URL = "https://api.deepseek.com"
MODEL = "deepseek-flash"
PROVIDER = "@deepseek"
REFUSAL = "这条请求未通过内容审核。"
_REFUSAL_MARKER = "content exists risk"
_BAD_REQUEST = 400
MAX_FILE_BYTES = 16 * 1024 * 1024
MAX_AUDIO_BYTES = 8 * 1024 * 1024
MAX_TTS_CHARS = 2_000
MAX_RESPONSE_BYTES = 8 * 1024 * 1024
_MIN_WAV_HEADER = 44
TRANSCRIPT_PREFIX = "〔本轮语音转写〕\n"
ParseMode = Literal["text_recognition", "rich_layout_recognition"]
_IMAGES = {".jpg", ".jpeg", ".png", ".webp", ".gif"}
_DOCUMENTS = {".pdf", ".doc", ".docx", ".ppt", ".pptx", ".bmp", ".tif", ".tiff"}
_USER_DATA = {".txt", ".md", ".csv", ".json", ".xlsx", ".xls", ".epub", ".mobi"}


class DeepSeekConfig(BaseModel):
    deepseek_api_key: SecretStr | None = Field(default=None, repr=False)
    deepseek_webui_enabled: bool = False
    deepseek_qq_group: bool = False
    deepseek_qq_private: bool = False
    deepseek_qq_documents: bool = True
    deepseek_qq_asr: bool = True
    deepseek_qq_tts: bool = False
    deepseek_document_mode: ParseMode = "text_recognition"


config = get_plugin_config(DeepSeekConfig)
_qq_owner: ContextVar[asyncio.Task[Any] | None] = ContextVar(
    "deepseek_qq_owner", default=None
)
_slots = asyncio.Semaphore(4)


def api_key() -> str | None:
    from .llm import ai_config, resolve_provider

    configured = config.deepseek_api_key
    if configured and configured.get_secret_value().strip():
        return configured.get_secret_value().strip()
    for provider in ("default", *(item.id for item in ai_config.ai_providers)):
        url, key = resolve_provider(provider)
        if key and url.rstrip("/") in {BASE_URL, f"{BASE_URL}/v1"}:
            return key
    return None


@contextmanager
def qq_turn(*, enabled: bool) -> Iterator[None]:
    owner = asyncio.current_task() if enabled and api_key() else None
    token = _qq_owner.set(owner)
    try:
        yield
    finally:
        _qq_owner.reset(token)


def in_qq_turn() -> bool:
    owner = _qq_owner.get()
    return owner is not None and owner is asyncio.current_task()


class DeepSeekError(Exception):
    def __init__(self, code: str, status: int = 502) -> None:
        super().__init__(code)
        self.code = code
        self.status = status


def file_purpose(filename: str, mode: ParseMode) -> str:
    suffix = Path(filename).suffix.lower()
    if suffix in _IMAGES | _USER_DATA:
        return "user_data"
    if suffix in _DOCUMENTS:
        return mode
    raise DeepSeekError("unsupported_file", 400)


def safe_url(value: Any) -> bool:
    if not isinstance(value, str):
        return False
    try:
        url = urlsplit(value)
        return (
            url.scheme in {"http", "https"}
            and bool(url.hostname)
            and url.username is None
        )
    except ValueError:
        return False


@dataclass(slots=True)
class ChatResult:
    message: dict[str, Any] = field(
        default_factory=lambda: {"role": "assistant", "content": ""}
    )
    usage: dict[str, Any] = field(default_factory=dict)
    title: str = ""
    finish_reason: str = ""
    _reasoning: list[str] = field(default_factory=list)
    _content: list[str] = field(default_factory=list)

    def apply(self, frame: dict[str, Any]) -> None:
        if frame.get("error"):
            raise DeepSeekError("upstream_error")
        if isinstance(frame.get("usage"), dict):
            self.usage = frame["usage"]
        for choice in frame.get("choices", []):
            delta = choice.get("delta") or {}
            self._content.append(delta.get("content") or "")
            self._reasoning.append(delta.get("reasoning_content") or "")
            if (choice.get("server_tool_event") or {}).get(
                "type"
            ) == "step_reasoning_done":
                self._reasoning.clear()
            if choice.get("finish_reason"):
                self.finish_reason = choice["finish_reason"]
                self.title = delta.get("title") or ""
                for key in ("server_tool_calls", "annotations"):
                    if key in delta:
                        self.message[key] = delta[key]

    def finish(self) -> None:
        if self.finish_reason == "content_filter":
            self.message = {"role": "assistant", "content": REFUSAL}
            self.title = ""
        elif self.finish_reason in {"stop", "length"}:
            self.message["content"] = "".join(self._content)
            if reasoning := "".join(self._reasoning):
                self.message["reasoning_content"] = reasoning
        else:
            raise DeepSeekError("stream_interrupted")

    def public(self) -> dict[str, Any]:
        citations = []
        for annotation in self.message.get("annotations") or []:
            citation = annotation.get("url_citation") or {}
            url = citation.get("url")
            if safe_url(url):
                citations.append({"url": url, "title": citation.get("title") or url})
        usage = {
            key: self.usage[key]
            for key in ("prompt_tokens", "completion_tokens", "total_tokens")
            if isinstance(self.usage.get(key), int)
        }
        tools = self.usage.get("server_tool_use") or {}
        usage.update(
            {
                key: tools[key]
                for key in ("tool_search", "tool_fetch", "tool_image_search")
                if isinstance(tools.get(key), int)
            }
        )
        return {
            "text": self.message["content"],
            "citations": citations,
            "usage": usage,
            "title": self.title,
            "refused": self.finish_reason == "content_filter",
            "truncated": self.finish_reason == "length",
        }


def is_input_refusal(status: int | None, detail: str) -> bool:
    return status == _BAD_REQUEST and _REFUSAL_MARKER in detail.casefold()


async def _check_status(response: httpx.Response) -> None:
    if not response.is_success:
        status = response.status_code
        if status == _BAD_REQUEST:
            async for chunk in response.aiter_bytes(chunk_size=8192):
                if is_input_refusal(status, chunk.decode("utf-8", errors="replace")):
                    raise DeepSeekError("content_filter", _BAD_REQUEST)
                break
        raise DeepSeekError(
            f"upstream_{status}", status if status in {400, 413, 429} else 502
        )


class DeepSeekClient:
    def __init__(
        self, key: str, *, transport: httpx.AsyncBaseTransport | None = None
    ) -> None:
        self._key = key
        self._transport = transport

    @asynccontextmanager
    async def _response(
        self,
        operation: str,
        path: str,
        *,
        method: str = "POST",
        timeout_seconds: float = 60,
        **kwargs: Any,
    ) -> AsyncIterator[httpx.Response]:
        from .metrics import record_ai_degradation, record_ai_request

        start = time.monotonic()
        outcome = "error"
        try:
            with anyio.fail_after(timeout_seconds):
                async with (
                    _slots,
                    httpx.AsyncClient(
                        base_url=BASE_URL,
                        headers={"Authorization": f"Bearer {self._key}"},
                        timeout=httpx.Timeout(timeout_seconds, connect=8),
                        follow_redirects=False,
                        transport=self._transport,
                    ) as client,
                    client.stream(method, path, **kwargs) as response,
                ):
                    await _check_status(response)
                    yield response
                    outcome = "success"
        except DeepSeekError as exc:
            record_ai_degradation("deepseek", exc.code)
            raise
        except (asyncio.TimeoutError, TimeoutError, httpx.TimeoutException) as exc:
            outcome = "timeout"
            record_ai_degradation("deepseek", "upstream_timeout")
            raise DeepSeekError("upstream_timeout", 504) from exc
        except httpx.HTTPError as exc:
            record_ai_degradation("deepseek", "upstream_connection")
            raise DeepSeekError("upstream_connection") from exc
        finally:
            record_ai_request(
                f"deepseek_{operation}", outcome, time.monotonic() - start
            )

    @staticmethod
    async def _bytes(response: httpx.Response, limit: int) -> bytes:
        body = bytearray()
        async for chunk in response.aiter_bytes():
            if len(body) + len(chunk) > limit:
                raise DeepSeekError("response_too_large")
            body.extend(chunk)
        return bytes(body)

    async def _json(self, operation: str, path: str, **kwargs: Any) -> dict[str, Any]:
        async with self._response(operation, path, **kwargs) as response:
            try:
                result = json.loads(await self._bytes(response, MAX_RESPONSE_BYTES))
            except (UnicodeError, ValueError) as exc:
                raise DeepSeekError("invalid_response") from exc
            if not isinstance(result, dict):
                raise DeepSeekError("invalid_response")
            return result

    async def chat(self, body: dict[str, Any]) -> ChatResult:
        result = ChatResult()
        async with self._response(
            "chat",
            "/chat/completions",
            timeout_seconds=150,
            json={
                **body,
                "model": MODEL,
                "stream": True,
                "stream_options": {"include_usage": True},
            },
            headers={"Accept": "text/event-stream"},
        ) as response:
            received = 0
            async for line in response.aiter_lines():
                received += len(line)
                if received > MAX_RESPONSE_BYTES:
                    raise DeepSeekError("response_too_large")
                if not line.startswith("data:"):
                    continue
                payload = line[5:].strip()
                if payload == "[DONE]":
                    result.finish()
                    from .llm import _record_ai_usage

                    _record_ai_usage("deepseek_chat", {"usage": result.usage})
                    return result
                try:
                    result.apply(json.loads(payload))
                except (ValueError, AttributeError, TypeError) as exc:
                    raise DeepSeekError("invalid_response") from exc
            raise DeepSeekError("stream_interrupted")

    async def upload(
        self,
        filename: str,
        data: bytes,
        mode: ParseMode,
        *,
        timeout_seconds: float = 150,
    ) -> str:
        if not data or len(data) > MAX_FILE_BYTES:
            raise DeepSeekError("invalid_file_size", 413)
        purpose = file_purpose(filename, mode)
        result = await self._json(
            "files",
            "/files",
            timeout_seconds=timeout_seconds,
            data={
                "purpose": purpose,
                "expires_after[anchor]": "created_at",
                "expires_after[seconds]": "3600",
            },
            files={"file": (filename, data)},
        )
        file_id = result.get("id")
        if (
            not isinstance(file_id, str)
            or not file_id.startswith("file-")
            or not file_id.replace("-", "").isalnum()
        ):
            raise DeepSeekError("invalid_file_id")
        return file_id

    async def delete(self, file_id: str) -> None:
        try:
            await self._json(
                "file_delete", f"/files/{file_id}", method="DELETE", timeout_seconds=5
            )
        except DeepSeekError as exc:
            logger.debug("DeepSeek 临时文件清理结果 {}", exc.code)

    async def ocr(self, filename: str, data: bytes, mode: ParseMode) -> dict[str, Any]:
        if not data or len(data) > MAX_FILE_BYTES:
            raise DeepSeekError("invalid_file_size", 413)
        if Path(filename).suffix.lower() not in _IMAGES | _DOCUMENTS:
            raise DeepSeekError("unsupported_file", 400)
        return await self._json(
            "ocr",
            "/v1/tools/ocr",
            timeout_seconds=180,
            data={"purpose": mode},
            files={"file": (filename, data)},
        )

    async def asr(self, data: bytes, *, timeout_seconds: float = 60) -> dict[str, Any]:
        if not data or len(data) > MAX_AUDIO_BYTES:
            raise DeepSeekError("invalid_audio_size", 413)
        if data[:4] == b"RIFF" and data[8:12] == b"WAVE":
            name = "speech.wav"
        elif data[:4] == b"OggS" and b"OpusHead" in data[:128]:
            name = "speech.ogg"
        else:
            raise DeepSeekError("unsupported_audio", 400)
        return await self._json(
            "asr",
            "/v1/tools/asr",
            timeout_seconds=timeout_seconds,
            files={"file": (name, data)},
        )

    async def tts(
        self, text: str, voice: str = "stella", *, timeout_seconds: float = 60
    ) -> bytes:
        if (
            not text.strip()
            or len(text) > MAX_TTS_CHARS
            or voice not in {"stella", "tide"}
        ):
            raise DeepSeekError("invalid_tts_input", 400)
        async with self._response(
            "tts",
            "/v1/tools/tts",
            timeout_seconds=timeout_seconds,
            json={
                "text": text,
                "voice_id": voice,
                "format": "wav",
                "filter_markdown": True,
            },
        ) as response:
            data = await self._bytes(response, MAX_AUDIO_BYTES)
            if (
                len(data) < _MIN_WAV_HEADER
                or data[:4] != b"RIFF"
                or data[8:12] != b"WAVE"
            ):
                raise DeepSeekError("invalid_audio_response")
            size = int.from_bytes(data[4:8], "little")
            if size not in {0, 0xFFFFFFFF} and size + 8 > len(data):
                raise DeepSeekError("audio_interrupted")
            return data


def client() -> DeepSeekClient:
    key = api_key()
    if not key:
        raise DeepSeekError("not_configured", 503)
    return DeepSeekClient(key)
