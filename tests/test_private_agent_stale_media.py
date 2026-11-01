# ruff: noqa: ANN001, ANN003, ARG001, E501, F811, TRY003
"""私聊旧媒体修复（2026-11-01）：旧表情包不再每轮当新图、发过的文件后几轮还能找到。"""

from __future__ import annotations

import asyncio
import importlib
from types import SimpleNamespace

import pytest
from sqlalchemy import select
from test_private_agent import (  # noqa: F401  (fixture)
    NOW,
    USER,
    ChatMessage,
    ChatSession,
    _Bot,
    _message,
    _no_sleep,
    _request,
    _script,
    maker,
    prod_maker,
)

_PKG = "src.plugins.yawn_core"
llm = importlib.import_module(f"{_PKG}.llm")
chat_state = importlib.import_module(f"{_PKG}.chat_state")
pagent = importlib.import_module(f"{_PKG}.yawn_agent.private_agent")
pcomputer = importlib.import_module(f"{_PKG}.yawn_agent.private_computer")
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


def _media() -> MediaInput:
    return MediaInput(kind="image", content_hash="d1", mime_type="image/png", local_path=None, data=b"png")


# ── 直接看图：旧图不挂到新消息上 ───────────────────────────────────────────


def test_replay_without_earlier_image_message_is_dropped() -> None:
    vision = pvision.PrivateVision(route="direct", blocks=[IMAGE_BLOCK], replayed=True)
    messages = [{"role": "user", "content": "你随便说一组数字"}]
    pvision.attach_to_messages(messages, vision)
    assert messages[0]["content"] == "你随便说一组数字"
    assert not vision.blocks and not vision.replayed


def test_replay_never_lands_on_current_message_across_turns() -> None:
    """截图里的情况：表情包之后连着几条文字/文件，每轮都只有历史那条带图。"""

    for current in ("?", "...", "[文件: SOP.docx]"):
        vision = pvision.PrivateVision(route="direct", blocks=[IMAGE_BLOCK], replayed=True)
        messages = [
            {"role": "user", "content": "[图片]"},
            {"role": "assistant", "content": "哈哈 这图还挺配你"},
            {"role": "user", "content": current},
        ]
        pvision.attach_to_messages(messages, vision)
        assert isinstance(messages[0]["content"], list)
        assert isinstance(messages[-1]["content"], str)
        assert messages[-1]["content"].startswith(current)


def test_history_notes_skip_replay_and_unseen_notes() -> None:
    assert pvision.history_notes(pvision.PrivateVision(notes=[pvision.UNSEEN_NOTE])) == []
    caption = "（图片内容，识图模型看到的：初音哭着说为什么要这样对我）"
    assert pvision.history_notes(pvision.PrivateVision(notes=[caption])) == [caption]
    assert pvision.history_notes(pvision.PrivateVision(notes=[caption], replayed=True)) == []


@pytest.mark.asyncio
async def test_replayed_image_rejected_restores_history_message(maker, monkeypatch) -> None:
    """对话模型拒收重附的旧图：重试时历史那条也要还原成纯文字。"""

    seen: list[list[dict]] = []

    async def fake(messages, tools, **kwargs):
        seen.append([dict(m) for m in messages])
        if kwargs.get("raise_on_unsupported"):
            raise llm.LLMMultimodalUnsupportedError("unknown variant `image_url`")
        return SimpleNamespace(message=_message("咋了"), outcome="success")

    async def prepare(_bot, _user_id, media):
        return pvision.PrivateVision(route="direct", blocks=[IMAGE_BLOCK], media_inputs=[_media()], replayed=True)

    async def fallback(_user_id, vision):
        return pvision.PrivateVision(route="caption", replayed=True)

    monkeypatch.setattr(llm, "complete_with_tools_result", fake)
    monkeypatch.setattr(pagent.asyncio, "sleep", _no_sleep)
    monkeypatch.setattr(pagent, "now_beijing", lambda: NOW)
    monkeypatch.setattr(pvision, "prepare_private_vision", prepare)
    monkeypatch.setattr(pvision, "fallback_to_captions", fallback)
    history = [
        {"role": "user", "content": "[图片]"},
        {"role": "assistant", "content": "哈哈"},
        {"role": "user", "content": "?"},
    ]
    async with maker() as session:
        text = await pagent.handle_private_turn(_request(session, _Bot(), "?", history=history))
    assert text == "咋了"
    assert any(isinstance(m.get("content"), list) for m in seen[0])
    assert all(not isinstance(m.get("content"), list) for m in seen[1])


# ── 识图转述、附件位置写回历史 ─────────────────────────────────────────────


def test_attachment_notes() -> None:
    notes = pagent.attachment_notes(
        [
            {"id": "m1", "type": "file", "name": "SOP.docx", "ref": "sandbox:inbox/SOP.docx", "size": "45.8KB"},
            {"id": "m2", "type": "file", "name": "big.zip", "failed": "太大了"},
            {"id": "m3", "type": "video"},
        ]
    )
    assert notes == [
        "（这条里的文件「SOP.docx」已放进他的虚拟电脑：sandbox:inbox/SOP.docx，45.8KB）",
        "（这条里的文件「big.zip」没能放进虚拟电脑：太大了）",
    ]


async def _seed_chat(session, contents: list[tuple[str, str]]) -> None:
    chat = ChatSession(user_id=USER, group_id=None, created_at=NOW, updated_at=NOW)
    session.add(chat)
    await session.flush()
    chat_id = chat.id
    for role, content in contents:
        session.add(ChatMessage(session_id=chat_id, role=role, content=content, created_at=NOW))
    await session.commit()


@pytest.mark.asyncio
async def test_annotate_appends_to_latest_user_message_once(prod_maker) -> None:
    async with prod_maker() as session:
        await _seed_chat(session, [("user", "早"), ("assistant", "早呀"), ("user", "[文件: SOP.docx]")])
    note = "（这条里的文件「SOP.docx」已放进他的虚拟电脑：sandbox:inbox/SOP.docx）"
    async with prod_maker() as session:
        await pagent._annotate_turn_message(session, USER, [note])
        await pagent._annotate_turn_message(session, USER, [note])
    async with prod_maker() as session:
        rows = (await session.execute(select(ChatMessage.role, ChatMessage.content).order_by(ChatMessage.id))).all()
    assert rows[0][1] == "早"
    assert rows[-1][1] == f"[文件: SOP.docx]\n{note}"


@pytest.mark.asyncio
async def test_file_turn_writes_inbox_ref_into_history_for_later_turns(maker, monkeypatch) -> None:
    """发文件那轮入箱成功：之后“帮我把文件整理成PPT”时历史里看得到 sandbox 引用。"""

    async def status():
        return True, None

    async def import_media(_bot, _user_id, media):
        assert media[0]["type"] == "file"
        return [{"id": "m1", "type": "file", "name": "SOP.docx", "ref": "sandbox:inbox/SOP.docx", "size": "45.8KB"}]

    monkeypatch.setattr(pcomputer, "computer_status", status)
    monkeypatch.setattr(pcomputer, "import_media", import_media)
    monkeypatch.setattr(pagent, "now_beijing", lambda: NOW)
    seen: list[dict] = []
    _script(monkeypatch, [_message("收到啦"), _message("好，我来做")], seen)
    async with maker() as session:
        await _seed_chat(session, [("user", "[文件: SOP.docx]")])
        request = _request(session, _Bot(), "[文件: SOP.docx]")
        request.media = [{"type": "file", "name": "SOP.docx", "file_id": "f1"}]
        assert await pagent.handle_private_turn(request) == "收到啦"
        history = await pagent._recent_history(session, USER)
        assert "sandbox:inbox/SOP.docx" in history[-1]["content"]
        history.append({"role": "user", "content": "帮我把文件整理成PPT"})
        follow = _request(session, _Bot(), "帮我把文件整理成PPT", history=history)
        assert await pagent.handle_private_turn(follow) == "好，我来做"
    messages = seen[-1]["messages"]
    assert any("sandbox:inbox/SOP.docx" in str(m.get("content")) for m in messages if m["role"] == "user")
    assert "recent_images" not in messages[1]["content"]


# ── 连发合并：上一轮没取走的媒体不留给下一轮 ──────────────────────────────


@pytest.mark.asyncio
async def test_merge_burst_drops_stale_burst_media(monkeypatch) -> None:
    monkeypatch.setattr(chat_state, "_merge_window_seconds", lambda: 0.0)
    chat_state._burst_media[USER] = [{"type": "image", "file": "old"}]
    state = chat_state.UserChatState(queue=asyncio.Queue())
    event = SimpleNamespace(get_user_id=lambda: str(USER), get_plaintext=lambda: "摸摸头")
    await chat_state._merge_burst(state, event)
    assert chat_state.take_burst_media(USER) == []


# ── 表情包：接情绪，不描述画面 ─────────────────────────────────────────────


def test_sticker_placeholder() -> None:
    bridge = importlib.import_module(f"{_PKG}.private_chat_bridge")
    assert bridge.describe_media([{"type": "image", "sub_type": 1, "file": "a"}]) == "[表情包]"
    assert bridge.describe_media([{"type": "image", "sub_type": 0, "file": "a"}]) == "[图片]"


@pytest.mark.asyncio
async def test_sticker_caption_is_framed_as_mood(monkeypatch) -> None:
    async def load(_bot, refs):
        return [_media()]

    async def caption(_scope, _media_input):
        return "初音哭着喊为什么要这样对我"

    monkeypatch.setattr(turn_media, "caption_media", caption)
    vision = await pvision.prepare_private_vision(
        None, USER, [{"type": "image", "sub_type": "1", "file": "x"}], load=load, route="caption"
    )
    assert vision.sticker
    assert pvision.STICKER_NOTE in vision.notes
    assert any(n.startswith("（表情包内容") for n in vision.notes)
    # 写回历史的只有转述，不带一次性的语气指引
    assert pvision.STICKER_NOTE not in pvision.history_notes(vision)


@pytest.mark.asyncio
async def test_photo_gets_no_sticker_note(monkeypatch) -> None:
    async def load(_bot, refs):
        return [_media()]

    async def blocks(media_inputs):
        return [IMAGE_BLOCK]

    monkeypatch.setattr(pvision, "_blocks", blocks)
    vision = await pvision.prepare_private_vision(None, USER, [{"type": "image", "file": "x"}], load=load, route="direct")
    assert not vision.sticker and pvision.STICKER_NOTE not in vision.notes


def test_replay_finds_sticker_placeholder() -> None:
    vision = pvision.PrivateVision(route="direct", blocks=[IMAGE_BLOCK], replayed=True)
    messages = [{"role": "user", "content": "[表情包]"}, {"role": "user", "content": "?"}]
    pvision.attach_to_messages(messages, vision)
    assert isinstance(messages[0]["content"], list)
