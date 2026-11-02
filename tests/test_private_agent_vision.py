# ruff: noqa: ANN001, ANN003, ARG001, E501, F811, TRY003
"""私聊/群聊识图修复（2026-11）：已知纯文字模型改走转述、私聊图片真正交给模型看。"""

from __future__ import annotations

import importlib
from types import SimpleNamespace

import pytest
from test_private_agent import (  # noqa: F401  (fixture)
    NOW,
    _Bot,
    _message,
    _no_sleep,
    _request,
    maker,
)

_PKG = "src.plugins.yawn_core"
llm = importlib.import_module(f"{_PKG}.llm")
pagent = importlib.import_module(f"{_PKG}.yawn_agent.private_agent")
pvision = importlib.import_module(f"{_PKG}.yawn_agent.private_vision")
turn_media = importlib.import_module(f"{_PKG}.yawn_agent.turn_media")
MediaInput = importlib.import_module(f"{_PKG}.yawn_agent.media_provider").MediaInput

IMAGE_BLOCK = {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}}


@pytest.fixture(autouse=True)
def _reset():
    pvision.reset_for_tests()
    turn_media.reset_for_tests()
    yield
    pvision.reset_for_tests()
    turn_media.reset_for_tests()


def _media(digest: str = "d1") -> MediaInput:
    return MediaInput(kind="image", content_hash=digest, mime_type="image/png", local_path=None, data=b"png")


# ── 已知纯文字模型 ────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "model",
    ["mimo-v2.5-pro", "MiMo-V2.5-Pro", "xiaomi/mimo-v2.5-pro", "mimo-v2-flash", "deepseek-chat", "deepseek-reasoner"],
)
def test_known_text_only_models(model: str) -> None:
    assert turn_media.known_text_only(model)


@pytest.mark.parametrize("model", ["mimo-v2.5", "gpt-4o", "qwen-vl-max", "", "deepseek-v4"])
def test_other_models_are_not_assumed_text_only(model: str) -> None:
    assert not turn_media.known_text_only(model)


def _request_config(model: str, multimodal: str):
    return llm.LLMRequestConfig(
        task="agent_dialogue",
        profile="default",
        provider="default",
        model=model,
        thinking="auto",
        multimodal=multimodal,
    )


def test_auto_text_only_model_routes_images_to_caption() -> None:
    assert turn_media.effective_multimodal_mode(_request_config("mimo-v2.5-pro", "auto")) == "unsupported"
    # 管理员明确说支持就信他
    assert turn_media.effective_multimodal_mode(_request_config("mimo-v2.5-pro", "supported")) == "supported"
    assert turn_media.effective_multimodal_mode(_request_config("mimo-v2.5", "auto")) == "auto"


# ── 私聊识图准备 ──────────────────────────────────────────────────────────


def test_image_refs_only_keeps_images() -> None:
    refs = pvision.image_refs(
        [{"type": "file", "name": "a.txt"}, {"type": "image", "file": "x.png", "url": "https://gchat.qpic.cn/x"}]
    )
    assert refs == [{"type": "image", "file": "x.png", "url": "https://gchat.qpic.cn/x", "source": "current"}]


@pytest.mark.asyncio
async def test_no_route_gives_honest_note() -> None:
    vision = await pvision.prepare_private_vision(None, 42, [{"type": "image", "file": "x"}], route="none")
    assert vision.notes == [pvision.NO_ROUTE_NOTE]
    assert not vision.blocks


@pytest.mark.asyncio
async def test_caption_route_attaches_description_and_remembers(monkeypatch) -> None:
    async def load(_bot, refs):
        return [_media()]

    async def caption(_scope, _media_input):
        return "一只趴在键盘上的橘猫"

    monkeypatch.setattr(turn_media, "caption_media", caption)
    vision = await pvision.prepare_private_vision(None, 42, [{"type": "image", "file": "x"}], load=load, route="caption")
    assert any("橘猫" in note for note in vision.notes)
    assert pvision.recent_captions(42) == ["一只趴在键盘上的橘猫"]
    assert pvision.history_notes(vision) == [n for n in vision.notes if "橘猫" in n]
    messages = [{"role": "user", "content": "[图片] 你看这是啥"}]
    pvision.attach_to_messages(messages, vision)
    assert "橘猫" in messages[0]["content"] and "你看这是啥" in messages[0]["content"]


@pytest.mark.asyncio
async def test_direct_route_attaches_image_and_replays_on_follow_up(monkeypatch) -> None:
    async def load(_bot, refs):
        return [_media()]

    async def blocks(media_inputs):
        return [IMAGE_BLOCK for _ in media_inputs]

    monkeypatch.setattr(pvision, "_blocks", blocks)
    vision = await pvision.prepare_private_vision(None, 42, [{"type": "image", "file": "x"}], load=load, route="direct")
    messages = [{"role": "user", "content": "看图"}]
    pvision.attach_to_messages(messages, vision)
    assert messages[0]["content"] == [{"type": "text", "text": "看图"}, IMAGE_BLOCK]
    follow = await pvision.prepare_private_vision(None, 42, [], route="direct")
    assert follow.replayed and follow.blocks == [IMAGE_BLOCK]
    # 重附的旧图回到当初发图的那条历史消息上，本条只注明没有新图片。
    later = [
        {"role": "user", "content": "[图片]"},
        {"role": "assistant", "content": "哈哈"},
        {"role": "user", "content": "..."},
    ]
    pvision.attach_to_messages(later, follow)
    assert later[0]["content"] == [{"type": "text", "text": "[图片]"}, IMAGE_BLOCK]
    assert isinstance(later[2]["content"], str)
    assert pvision.NO_NEW_IMAGE_NOTE in later[2]["content"]


@pytest.mark.asyncio
async def test_failed_download_says_unseen() -> None:
    async def load(_bot, refs):
        return []

    vision = await pvision.prepare_private_vision(None, 42, [{"type": "image", "file": "x"}], load=load, route="caption")
    assert vision.notes == [pvision.UNSEEN_NOTE]


# ── 私聊一轮 ──────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_turn_sends_image_to_model_and_falls_back_to_caption(maker, monkeypatch) -> None:
    seen: list[dict] = []
    calls = {"n": 0}

    async def fake(messages, tools, **kwargs):
        calls["n"] += 1
        last_user = [m for m in messages if m.get("role") == "user"][-1]
        seen.append({"content": last_user["content"], "kwargs": kwargs})
        if kwargs.get("raise_on_unsupported"):
            raise llm.LLMMultimodalUnsupportedError("unknown variant `image_url`")
        return SimpleNamespace(message=_message("是只橘猫诶"), outcome="success")

    async def prepare(_bot, _user_id, media):
        assert media and media[0]["type"] == "image"
        return pvision.PrivateVision(route="direct", blocks=[IMAGE_BLOCK], media_inputs=[_media()], requested=1, loaded=1)

    async def fallback(_user_id, vision):
        return pvision.PrivateVision(route="caption", notes=["（图片内容，识图模型看到的：一只橘猫）"], media_inputs=vision.media_inputs)

    monkeypatch.setattr(llm, "complete_with_tools_result", fake)
    monkeypatch.setattr(pagent.asyncio, "sleep", _no_sleep)
    monkeypatch.setattr(pagent, "now_beijing", lambda: NOW)
    monkeypatch.setattr(pvision, "prepare_private_vision", prepare)
    monkeypatch.setattr(pvision, "fallback_to_captions", fallback)
    async with maker() as session:
        bot = _Bot()
        request = _request(session, bot, "[图片] 这是啥")
        request.media = [{"type": "image", "file": "x.png"}]
        text = await pagent.handle_private_turn(request)
    assert text == "是只橘猫诶"
    assert seen[0]["content"][1] == IMAGE_BLOCK and seen[0]["kwargs"]["multimodal"] is True
    assert isinstance(seen[1]["content"], str) and "一只橘猫" in seen[1]["content"]
    assert "这是啥" in seen[1]["content"]


# ── 没填识图模型时按对话模型推断同家识图模型 ──────────────────────────────


def test_mimo_pro_implies_mimo_vision_model(monkeypatch) -> None:
    monkeypatch.setattr(llm.ai_config, "ai_model", "mimo-v2.5-pro")
    monkeypatch.setattr(llm.ai_config, "ai_vision_model", None)
    monkeypatch.setattr(llm.ai_config, "ai_default_provider", "mimo")
    monkeypatch.setattr(llm, "get_client", lambda _provider="default": object())
    request = llm.resolve_llm_request("agent_image")
    assert request.model == "mimo-v2.5"
    assert request.provider == "mimo"
    assert llm.vision_model_configured()
    route = turn_media.vision_route()
    assert route["route"] == "caption" and route["known_text_only"]


def test_explicit_vision_model_wins(monkeypatch) -> None:
    monkeypatch.setattr(llm.ai_config, "ai_model", "mimo-v2.5-pro")
    monkeypatch.setattr(llm.ai_config, "ai_vision_model", "qwen-vl-max")
    assert llm.resolve_llm_request("agent_image").model == "qwen-vl-max"


def test_unknown_model_implies_nothing(monkeypatch) -> None:
    monkeypatch.setattr(llm.ai_config, "ai_model", "some-model")
    monkeypatch.setattr(llm.ai_config, "ai_vision_model", None)
    assert llm.implied_vision_model() == ""
    monkeypatch.setattr(llm, "get_client", lambda _provider="default": object())
    assert not llm.vision_model_configured()
