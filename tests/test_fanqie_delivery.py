from __future__ import annotations

# Fake protocol errors and tiny chunk sizes are intentional in this regression file.
# ruff: noqa: E501, PLR2004, TRY003

import base64
import importlib
import sys
from pathlib import Path
from typing import Any

import nonebot
import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="module")
def delivery_module() -> Any:
    if str(PROJECT_ROOT) not in sys.path:
        sys.path.insert(0, str(PROJECT_ROOT))
    try:
        nonebot.get_driver()
    except ValueError:
        nonebot.init()
    if nonebot.get_plugin("yawn_core") is None:
        nonebot.load_from_toml("pyproject.toml")
    return importlib.import_module("src.plugins.yawn_core.yawn_fanqie.delivery")


class _StreamBot:
    def __init__(self, remote_path: str) -> None:
        self.remote_path = remote_path
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.private_segment_calls = 0

    async def call_api(self, api: str, **kwargs: Any) -> Any:
        self.calls.append((api, kwargs))
        if api == "upload_file_stream":
            if kwargs.get("is_complete"):
                return {
                    "status": "file_complete",
                    "file_path": self.remote_path,
                }
            return {"status": "chunk_received"}
        if api == "upload_private_file":
            return {"file_id": "test-file-id"}
        raise AssertionError(f"unexpected api: {api}")

    async def send_private_msg(self, **_kwargs: Any) -> None:
        self.private_segment_calls += 1
        raise AssertionError("file segment fallback should not be used")


class _LegacyBot:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.private_segment_calls = 0

    async def call_api(self, api: str, **kwargs: Any) -> Any:
        self.calls.append((api, kwargs))
        if api == "upload_file_stream":
            raise RuntimeError("unsupported action")
        if api == "upload_private_file":
            return {"file_id": "legacy-file-id"}
        raise AssertionError(f"unexpected api: {api}")

    async def send_private_msg(self, **_kwargs: Any) -> None:
        self.private_segment_calls += 1
        raise AssertionError("file segment fallback should not be used")


@pytest.mark.asyncio
async def test_delivery_streams_file_before_private_upload(
    delivery_module: Any,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(delivery_module, "_STREAM_CHUNK_BYTES", 4)
    content = b"cross-container-file"
    output = tmp_path / "novel.txt"
    output.write_bytes(content)
    remote_path = "/app/.config/QQ/NapCat/temp/novel.txt"
    bot = _StreamBot(remote_path)

    await delivery_module.send_file_to_user(bot, 123456, output, "novel.txt")

    stream_calls = [call for call in bot.calls if call[0] == "upload_file_stream"]
    chunk_calls = [call for call in stream_calls if "chunk_data" in call[1]]
    complete_calls = [call for call in stream_calls if call[1].get("is_complete")]
    private_calls = [call for call in bot.calls if call[0] == "upload_private_file"]

    assert len(chunk_calls) == 5
    assert len(complete_calls) == 1
    assert len(private_calls) == 1
    assert b"".join(base64.b64decode(call[1]["chunk_data"]) for call in chunk_calls) == content
    assert [call[1]["chunk_index"] for call in chunk_calls] == list(range(5))
    assert all(call[1]["total_chunks"] == 5 for call in chunk_calls)
    assert private_calls[0][1]["file"] == remote_path
    assert private_calls[0][1]["name"] == "novel.txt"
    assert private_calls[0][1]["upload_file"] is True
    assert bot.private_segment_calls == 0


@pytest.mark.asyncio
async def test_delivery_accepts_nested_stream_response(
    delivery_module: Any,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(delivery_module, "_STREAM_CHUNK_BYTES", 1024)
    output = tmp_path / "novel.txt"
    output.write_text("hello", encoding="utf-8")
    remote_path = "/napcat/temp/hello.txt"

    class NestedResultBot(_StreamBot):
        async def call_api(self, api: str, **kwargs: Any) -> Any:
            self.calls.append((api, kwargs))
            if api == "upload_file_stream" and kwargs.get("is_complete"):
                return {"data": {"status": "file_complete", "file_path": remote_path}}
            if api == "upload_file_stream":
                return {"data": {"status": "chunk_received"}}
            if api == "upload_private_file":
                return {"file_id": "nested-file-id"}
            raise AssertionError(f"unexpected api: {api}")

    bot = NestedResultBot(remote_path)
    await delivery_module.send_file_to_user(bot, 123456, output, "hello.txt")

    private_call = next(call for call in bot.calls if call[0] == "upload_private_file")
    assert private_call[1]["file"] == remote_path
    assert bot.private_segment_calls == 0


@pytest.mark.asyncio
async def test_delivery_falls_back_for_old_protocol_implementations(
    delivery_module: Any,
    tmp_path: Path,
) -> None:
    output = tmp_path / "legacy.txt"
    output.write_text("legacy", encoding="utf-8")
    bot = _LegacyBot()

    await delivery_module.send_file_to_user(bot, 654321, output, "legacy.txt")

    private_call = next(call for call in bot.calls if call[0] == "upload_private_file")
    assert private_call[1]["file"] == str(output.resolve())
    assert "upload_file" not in private_call[1]
    assert bot.private_segment_calls == 0
