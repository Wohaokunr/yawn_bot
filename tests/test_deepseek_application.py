# ruff: noqa: PLR2004
"""Exercise the first-party protocol and the real WebUI authorization paths."""

from __future__ import annotations

import asyncio
import importlib
import io
import json
import sys
import wave
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import httpx
import nonebot
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import SecretStr

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))
try:
    nonebot.get_driver()
except ValueError:
    nonebot.init()
if nonebot.get_plugin("yawn_core") is None:
    nonebot.load_from_toml("pyproject.toml")

deepseek = importlib.import_module("src.plugins.yawn_core.deepseek")
llm = importlib.import_module("src.plugins.yawn_core.llm")
routes = importlib.import_module("src.plugins.yawn_core.webui.deepseek_routes")
auth = importlib.import_module("src.plugins.yawn_core.webui.auth")
turn = importlib.import_module("src.plugins.yawn_core.yawn_agent.deepseek_turn")
delivery = importlib.import_module("src.plugins.yawn_core.yawn_agent.private_delivery")
capture = importlib.import_module("src.plugins.yawn_core.yawn_agent.prompt_capture")


def _sse(*frames: dict[str, Any], done: bool = True) -> httpx.Response:
    content = "".join(f"data: {json.dumps(frame)}\n\n" for frame in frames)
    if done:
        content += "data: [DONE]\n\n"
    return httpx.Response(
        200, text=content, headers={"content-type": "text/event-stream"}
    )


def _frame(finish: str | None = None, **delta: Any) -> dict[str, Any]:
    return {"choices": [{"index": 0, "delta": delta, "finish_reason": finish}]}


def _wav() -> bytes:
    output = io.BytesIO()
    with wave.open(output, "wb") as writer:
        writer.setparams((1, 2, 16000, 100, "NONE", "not compressed"))
        writer.writeframes(bytes(200))
    return output.getvalue()


@pytest.fixture
def configured(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(deepseek.config, "deepseek_api_key", SecretStr("mock-key"))
    monkeypatch.setattr(deepseek.config, "deepseek_webui_enabled", True)
    monkeypatch.setattr(routes, "_sessions", {})


def test_workbench_authorizes_upload_and_preserves_provider_blocks(  # noqa: PLR0915
    configured: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    del configured
    sent: list[dict[str, Any]] = []
    deleted: list[str] = []
    calls = [
        {
            "index": 0,
            "reasoning_content": "",
            "calls": [
                {"id": "link", "type": "deepseek_read_url", "signature": "opaque"},
            ],
        }
    ]

    def respond(request: httpx.Request) -> httpx.Response:
        assert request.url.host == "api.deepseek.com"
        assert request.headers["authorization"] == "Bearer mock-key"
        if request.url.path == "/files":
            assert request.content.index(b'name="purpose"') < request.content.index(
                b'name="file"'
            )
            assert b"rich_layout_recognition" in request.content
            return httpx.Response(200, json={"id": "file-api-document"})
        if request.method == "DELETE":
            deleted.append(request.url.path)
            return httpx.Response(200, json={"deleted": True})
        sent.append(json.loads(request.content))
        if len(sent) == 1:
            return _sse(
                _frame(content="文档回答"),
                _frame(
                    "stop",
                    title="文件摘要",
                    server_tool_calls=calls,
                    annotations=[
                        {
                            "type": "url_citation",
                            "url_citation": {
                                "url": "https://example.com",
                                "title": "来源",
                            },
                        }
                    ],
                ),
                {"choices": [], "usage": {"prompt_tokens": 10}},
            )
        if len(sent) == 2:
            return _sse(_frame(content="审核前内容"), _frame("content_filter"))
        return _sse(_frame(content="中断内容"), done=False)

    real_client = deepseek.DeepSeekClient
    monkeypatch.setattr(
        deepseek,
        "DeepSeekClient",
        lambda key: real_client(key, transport=httpx.MockTransport(respond)),
    )
    app = FastAPI()
    app.include_router(routes.router)
    url = f"{routes.API_PATH}/deepseek"
    owner_cookie, owner = auth.create_session()
    other_cookie, other = auth.create_session()
    guest_cookie, guest = auth.create_session(role="guest")

    with TestClient(app) as client:
        assert client.get(f"{url}/status").status_code == 401
        client.cookies.set(auth.COOKIE_NAME, guest_cookie)
        assert (
            client.post(
                f"{url}/sessions", headers={"X-CSRF-Token": guest.csrf_token}
            ).status_code
            == 403
        )

        client.cookies.set(auth.COOKIE_NAME, owner_cookie)
        assert client.post(f"{url}/sessions").status_code == 403
        headers = {"X-CSRF-Token": owner.csrf_token}
        workspace_id = client.post(f"{url}/sessions", headers=headers).json()["data"][
            "id"
        ]
        workspace = f"{url}/sessions/{workspace_id}"
        uploaded = client.post(
            f"{workspace}/files?name=sample.pdf&mode=rich_layout_recognition",
            content=b"%PDF-sample",
            headers=headers,
        ).json()["data"]["id"]
        assert uploaded != "file-api-document"

        client.cookies.set(auth.COOKIE_NAME, other_cookie)
        assert (
            client.post(
                f"{workspace}/chat",
                json={"text": "别人的会话"},
                headers={"X-CSRF-Token": other.csrf_token},
            ).status_code
            == 404
        )
        client.cookies.set(auth.COOKIE_NAME, owner_cookie)

        answer = client.post(
            f"{workspace}/chat",
            json={
                "text": "阅读 https://example.com",
                "files": [uploaded],
                "read_url": True,
            },
            headers=headers,
        ).json()["data"]
        assert answer["text"] == "文档回答"
        assert answer["title"] == "文件摘要"
        assert answer["citations"] == [{"url": "https://example.com", "title": "来源"}]
        assert answer["usage"] == {"prompt_tokens": 10}
        assert "opaque" not in json.dumps(answer)
        assert sent[0]["messages"][-1]["content"][0]["file_id"] == "file-api-document"
        assert sent[0]["safety_check"] == {"level": "high"}

        refused = client.post(
            f"{workspace}/chat", json={"text": "还有呢？"}, headers=headers
        ).json()["data"]
        assert refused["refused"]
        assert refused["text"] == deepseek.REFUSAL
        assert sent[1]["messages"][2]["server_tool_calls"] == calls
        interrupted = client.post(
            f"{workspace}/chat", json={"text": "还有呢？"}, headers=headers
        )
        assert interrupted.status_code == 502
        assert "中断内容" not in interrupted.text
        assert len(sent[2]["messages"]) == len(sent[1]["messages"])

        assert client.delete(workspace, headers=headers).status_code == 200
        assert deleted == ["/files/file-api-document"]


@pytest.mark.asyncio
async def test_standalone_media_handles_formats_and_limits() -> None:
    audio = _wav()

    def respond(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/asr"):
            assert b"speech.wav" in request.content
            return httpx.Response(200, json={"text": "识别文字", "duration_ms": 6})
        if request.url.path.endswith("/ocr"):
            assert b"text_recognition" in request.content
            assert b"sample.pdf" in request.content
            return httpx.Response(200, json={"pages": [{"index": 0, "text": "一页"}]})
        if request.url.path.endswith("/tts"):
            if json.loads(request.content)["voice_id"] == "tide":
                return httpx.Response(429)
            return httpx.Response(200, content=audio)
        raise AssertionError(request.url)

    client = deepseek.DeepSeekClient("mock-key", transport=httpx.MockTransport(respond))
    assert (await client.asr(audio))["text"] == "识别文字"
    assert (await client.ocr("sample.pdf", b"%PDF-sample", "text_recognition"))["pages"]
    assert await client.tts("朗读这个") == audio
    with pytest.raises(deepseek.DeepSeekError, match="unsupported_audio"):
        await client.asr(b"invalid")
    with pytest.raises(deepseek.DeepSeekError, match="upstream_429"):
        await client.tts("限流", "tide")


@pytest.mark.asyncio
async def test_qq_media_stays_in_the_current_actor_turn(
    configured: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    del configured
    provider = SimpleNamespace(
        upload=AsyncMock(return_value="file-api-current"),
        asr=AsyncMock(return_value={"text": "私密转写"}),
        delete=AsyncMock(),
    )
    monkeypatch.setattr(deepseek, "client", lambda: provider)
    fetch = AsyncMock(return_value=SimpleNamespace(data=b"fixture"))
    monkeypatch.setattr(turn, "fetch_bytes", fetch)
    refs = [
        {
            "type": "file",
            "name": "sample.pdf",
            "source": "current",
            "source_user_id": 1,
        },
        {
            "type": "file",
            "name": "sample.pdf",
            "source": "current",
            "source_user_id": 1,
        },
        {"type": "record", "source": "current", "source_user_id": 1},
        {"type": "file", "name": "private.pdf", "source": "reply", "source_user_id": 2},
    ]

    async def child_model() -> str:
        return llm.resolve_llm_request("agent_dialogue").model

    original = llm.resolve_llm_request("agent_dialogue").model
    with deepseek.qq_turn(enabled=True):
        assert llm.resolve_llm_request("agent_dialogue").extra_body["safety_check"] == {
            "level": "high"
        }
        assert await asyncio.create_task(child_model()) == original
        async with turn.attachments(None, 10, refs, actor_id=1) as current:
            messages = [{"role": "user", "content": "它说了什么？"}]
            turn.add_to_messages(
                messages, current, section="speech.deepseek", voice_allowed=True
            )
            captured = json.dumps(
                capture._jsonable(messages, capture._Redactor()), ensure_ascii=False
            )
            assert current.blocks == [{"type": "file", "file_id": "file-api-current"}]
            assert "私密转写" not in captured and "file-api-current" not in captured
            assert turn.drop_rejected_files(
                messages, current, "context_length", section="speech.deepseek"
            )
            assert not current.blocks
            assert "file-api-current" not in json.dumps(messages)
            assert not turn.drop_rejected_files(
                messages, current, "context_length", section="speech.deepseek"
            )
        provider.delete.assert_awaited_once_with("file-api-current")
    assert llm.resolve_llm_request("agent_dialogue").model == original
    provider.upload.assert_awaited_once()
    provider.asr.assert_awaited_once()
    assert fetch.await_count == 3


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("error", "sent_types", "unknown"),
    [
        (None, ["record"], False),
        (ValueError, ["record", "text"], False),
        (asyncio.TimeoutError, ["record"], True),
    ],
)
async def test_private_voice_preserves_text_and_avoids_duplicate_delivery(
    error: type[Exception] | None,
    sent_types: list[str],
    unknown: bool,  # noqa: FBT001
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(turn, "synthesize", AsyncMock(return_value=_wav()))
    sent: list[str] = []

    async def send(message: Any) -> None:
        kind = message[0].type
        sent.append(kind)
        if kind == "record" and error:
            raise error()

    result = await delivery.deliver(1, "这样读出来就行。", send, speak=True)
    assert sent == sent_types
    assert result.delivery_unknown == unknown
    assert result.delivered == ([] if unknown else ["这样读出来就行。"])


@pytest.mark.asyncio
async def test_qq_content_filter_becomes_a_terminal_refusal(
    configured: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    del configured
    from openai import AsyncOpenAI

    def respond(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, json={"error": {"message": "Content Exists Risk"}})

    stub = AsyncOpenAI(
        api_key="mock-key",
        base_url=deepseek.BASE_URL,
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(respond)),
    )
    monkeypatch.setattr(llm, "get_client", lambda _provider: stub)
    try:
        with deepseek.qq_turn(enabled=True):
            response = await llm.complete_with_tools_result(
                [{"role": "user", "content": "测试"}], [], task="agent_dialogue"
            )
        assert response.finish_reason == "content_filter"
        assert response.message.content == deepseek.REFUSAL
        assert not response.retryable
    finally:
        await stub.close()


@pytest.mark.asyncio
async def test_group_voice_uses_checked_text_on_adapter_rejection() -> None:
    outbound = importlib.import_module("src.plugins.yawn_core.yawn_agent.outbound")
    sent: list[Any] = []

    async def send(_action: str, **params: Any) -> dict[str, int]:
        message = params["message"]
        sent.append(message)
        if message[0].type == "record":
            msg = "adapter rejected the audio"
            raise ValueError(msg)
        return {"message_id": 1}

    bot = SimpleNamespace(self_id="1000192", call_api=send)
    text = "这一段可以直接念。"
    voice = outbound.prepare_record_reply(
        outbound.prepare_text_message(text), text, _wav()
    )
    result = await outbound.send_prepared_outbound(bot, 1, voice, source="dialogue")
    assert [message[0].type for message in sent] == ["record", "text"]
    assert sent[1][0].data["text"] == text
    assert result.sent
    assert result.normalized_text == text
