"""群聊 Agent 的纯函数回归测试（不依赖数据库与 LLM）。"""

# ruff: noqa: PLR2004

import sys
import types
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

PLUGIN_ROOT = Path(__file__).resolve().parents[2]
PACKAGE = types.ModuleType("yawn_core")
PACKAGE.__path__ = [str(PLUGIN_ROOT)]  # pyright: ignore[reportAttributeAccessIssue]
sys.modules.setdefault("yawn_core", PACKAGE)
AGENT_PACKAGE = types.ModuleType("yawn_core.yawn_agent")
AGENT_PACKAGE.__path__ = [str(PLUGIN_ROOT / "yawn_agent")]  # pyright: ignore[reportAttributeAccessIssue]
sys.modules.setdefault("yawn_core.yawn_agent", AGENT_PACKAGE)

from nonebot.adapters.onebot.v11 import Message, MessageSegment
from nonebot.adapters.onebot.v11.event import Reply, Sender
from yawn_core.yawn_agent import message_parser as parser_module
from yawn_core.yawn_agent.context import (
    ActivitySnapshot,
    coldness_score,
    is_cooldown_active,
    now_beijing,
)
from yawn_core.yawn_agent.message_parser import (
    MAX_DEPTH,
    MAX_REPLY_ENTRY_CHARS,
    MAX_TOTAL_BYTES,
    ForwardNode,
    NormalizedMessage,
    _message_from_api,
    normalize_message,
    parse_message,
)


class FakeBot:
    """按 (action, 排序后的 params) 查表返回 payload 的假机器人。"""

    def __init__(
        self,
        payloads: dict[tuple[str, tuple[tuple[str, Any], ...]], Any] | None = None,
        *,
        error: Exception | None = None,
    ) -> None:
        self.payloads = payloads or {}
        self.error = error
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def call_api(self, action: str, **params: Any) -> Any:
        self.calls.append((action, params))
        if self.error is not None:
            raise self.error
        return self.payloads.get((action, tuple(sorted(params.items()))))


def _msg_payload(
    text: str,
    *,
    reply_to: int | None = None,
    user_id: int = 0,
    nickname: str = "用户",
) -> dict[str, Any]:
    segments: list[dict[str, Any]] = []
    if reply_to is not None:
        segments.append({"type": "reply", "data": {"id": str(reply_to)}})
    segments.append({"type": "text", "data": {"text": text}})
    return {
        "message": segments,
        "sender": {"user_id": user_id, "nickname": nickname},
    }


def _make_reply(
    message_id: int, user_id: int, nickname: str, message: Message
) -> Reply:
    """构造适配器 _check_reply 填入 event.reply 的 Reply 对象。"""

    return Reply(
        time=0,
        message_type="group",
        message_id=message_id,
        real_id=message_id,
        sender=Sender(user_id=user_id, nickname=nickname),
        message=message,
    )


def test_now_beijing_matches_codebase_convention() -> None:
    now = now_beijing()
    assert now.tzinfo is None
    expected = datetime.now(timezone(timedelta(hours=8))).replace(tzinfo=None)
    assert abs((expected - now).total_seconds()) < 5


def test_coldness_score_bounds() -> None:
    now = now_beijing()
    silent = ActivitySnapshot(None)
    assert coldness_score(silent, now) == 1.0
    lively = ActivitySnapshot(
        now,
        messages_5m=5,
        messages_20m=20,
        messages_60m=40,
        participants_60m=8,
        replies_60m=4,
        mentions_60m=4,
    )
    assert coldness_score(lively, now) < 0.35


def test_cooldown_gate() -> None:
    now = now_beijing()
    recent = ActivitySnapshot(now, last_agent_at=now - timedelta(minutes=5))
    assert is_cooldown_active(recent, now, cooldown_minutes=20)
    stale = ActivitySnapshot(now, last_agent_at=now - timedelta(minutes=30))
    assert not is_cooldown_active(stale, now, cooldown_minutes=20)


def test_truncation_still_collects_trailing_media() -> None:
    message = Message()
    message.append(MessageSegment.text("长" * (MAX_TOTAL_BYTES // 3 + 100)))
    message.append(MessageSegment.image(file="deadbeef.jpg"))
    normalized = normalize_message(message)
    assert normalized.truncated
    assert [ref.get("type") for ref in normalized.media_refs] == ["image"]


def test_storage_dict_redacts_local_paths() -> None:
    message = Message()
    message.append(MessageSegment.image(file="C:\\Users\\secret\\photo.jpg"))
    normalized = normalize_message(message)
    stored = normalized.storage_dict()
    refs = stored["media_refs"]
    assert refs and refs[0]["file"] == "[redacted]"
    assert "path" not in refs[0]
    assert "url" not in refs[0]


def test_storage_dict_keeps_opaque_identifiers() -> None:
    message = Message()
    message.append(MessageSegment.image(file="{deadbeef-cafe}.image"))
    normalized = normalize_message(message)
    stored = normalized.storage_dict()
    assert stored["media_refs"][0]["file"] == "{deadbeef-cafe}.image"


def test_reply_placeholder_is_not_noise() -> None:
    message = Message()
    message.append(MessageSegment("reply", {"id": 123}))
    message.append(MessageSegment.text("正文"))
    normalized = normalize_message(message)
    assert "[回复]" not in normalized.plain_text
    assert "正文" in normalized.plain_text


@pytest.mark.asyncio
async def test_parse_message_follows_nested_reply_chain() -> None:
    payloads = {
        ("get_msg", (("message_id", 3),)): _msg_payload(
            "第二层", reply_to=2, user_id=201, nickname="张三"
        ),
        ("get_msg", (("message_id", 2),)): _msg_payload(
            "第一层", reply_to=1, user_id=202, nickname="李四"
        ),
        ("get_msg", (("message_id", 1),)): _msg_payload(
            "源头", user_id=203, nickname="王五"
        ),
    }
    bot = FakeBot(payloads)
    message = Message()
    message.append(MessageSegment("reply", {"id": "3"}))
    message.append(MessageSegment.text("现在的问题"))
    normalized = await parse_message(bot, message)
    assert [entry["message_id"] for entry in normalized.reply_chain] == [3, 2, 1]
    assert normalized.reply_chain[0]["user_id"] == 201
    assert normalized.reply_chain[0]["nickname"] == "张三"
    assert "第二层" in normalized.reply_chain[0]["text"]
    assert len(bot.calls) == 3
    prompt = normalized.prompt_text()
    assert (
        prompt.index("源头")
        < prompt.index("第一层")
        < prompt.index("第二层")
        < prompt.index("现在的问题")
    )


@pytest.mark.asyncio
async def test_parse_message_seeds_chain_from_event_reply() -> None:
    """适配器的 _check_reply 已拉取直接引用并移除 reply 段,链从 event.reply 种入。"""

    replied_msg = Message()
    replied_msg.append(MessageSegment.text("被引用的话"))
    bot = FakeBot()
    message = Message()
    message.append(MessageSegment.text("现在的问题"))
    normalized = await parse_message(
        bot, message, reply=_make_reply(3, 201, "张三", replied_msg)
    )
    assert len(normalized.reply_chain) == 1
    entry = normalized.reply_chain[0]
    assert entry["message_id"] == 3
    assert entry["user_id"] == 201
    assert entry["nickname"] == "张三"
    assert "被引用的话" in entry["text"]
    assert bot.calls == []
    prompt = normalized.prompt_text()
    assert prompt.index("被引用的话") < prompt.index("现在的问题")


@pytest.mark.asyncio
async def test_parse_message_continues_chain_after_event_reply() -> None:
    replied_msg = Message()
    replied_msg.append(MessageSegment("reply", {"id": "2"}))
    replied_msg.append(MessageSegment.text("第二层"))
    payloads = {
        ("get_msg", (("message_id", 2),)): _msg_payload(
            "源头", user_id=202, nickname="李四"
        )
    }
    bot = FakeBot(payloads)
    message = Message()
    message.append(MessageSegment.text("现在的问题"))
    normalized = await parse_message(
        bot, message, reply=_make_reply(3, 201, "张三", replied_msg)
    )
    assert [entry["message_id"] for entry in normalized.reply_chain] == [3, 2]
    assert "源头" in normalized.reply_chain[1]["text"]
    prompt = normalized.prompt_text()
    assert prompt.index("源头") < prompt.index("第二层") < prompt.index("现在的问题")


@pytest.mark.asyncio
async def test_parse_message_breaks_reply_cycle() -> None:
    payloads = {
        ("get_msg", (("message_id", 3),)): _msg_payload(
            "第二层", reply_to=2, user_id=201, nickname="张三"
        ),
        ("get_msg", (("message_id", 2),)): _msg_payload(
            "循环层", reply_to=3, user_id=202, nickname="李四"
        ),
    }
    bot = FakeBot(payloads)
    message = Message()
    message.append(MessageSegment("reply", {"id": "3"}))
    message.append(MessageSegment.text("正文"))
    normalized = await parse_message(bot, message)
    assert [entry["message_id"] for entry in normalized.reply_chain] == [3, 2]


@pytest.mark.asyncio
async def test_parse_message_degrades_when_api_fails() -> None:
    bot = FakeBot(error=RuntimeError("api down"))
    message = Message()
    message.append(MessageSegment("reply", {"id": "3"}))
    message.append(MessageSegment.text("正文"))
    normalized = await parse_message(bot, message)
    assert normalized.reply_chain == []
    assert normalized.plain_text == "正文"


@pytest.mark.asyncio
async def test_parse_message_respects_max_depth() -> None:
    payloads: dict[tuple[str, tuple[tuple[str, Any], ...]], Any] = {}
    for mid in range(2, 8):
        payloads[("get_msg", (("message_id", mid),))] = _msg_payload(
            f"第{mid}层", reply_to=mid - 1
        )
    payloads[("get_msg", (("message_id", 1),))] = _msg_payload("源头")
    bot = FakeBot(payloads)
    message = Message()
    message.append(MessageSegment("reply", {"id": "7"}))
    message.append(MessageSegment.text("正文"))
    normalized = await parse_message(bot, message)
    assert len(normalized.reply_chain) == MAX_DEPTH == 4
    assert [entry["message_id"] for entry in normalized.reply_chain] == [7, 6, 5, 4]


@pytest.mark.asyncio
async def test_parse_message_real_id_fallback() -> None:
    payloads = {
        ("get_msg", (("real_id", 9),)): _msg_payload(
            "按 real_id 取到", user_id=301, nickname="赵六"
        )
    }
    bot = FakeBot(payloads)
    message = Message()
    message.append(MessageSegment("reply", {"id": "9"}))
    message.append(MessageSegment.text("正文"))
    normalized = await parse_message(bot, message)
    assert len(normalized.reply_chain) == 1
    assert "按 real_id 取到" in normalized.reply_chain[0]["text"]
    assert [params for _action, params in bot.calls] == [
        {"message_id": 9},
        {"real_id": 9},
    ]


@pytest.mark.asyncio
async def test_parse_message_deadline_stops_chain(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(parser_module, "FORWARD_EXPAND_DEADLINE", -1.0)
    payloads = {("get_msg", (("message_id", 3),)): _msg_payload("第二层")}
    bot = FakeBot(payloads)
    message = Message()
    message.append(MessageSegment("reply", {"id": "3"}))
    normalized = await parse_message(bot, message)
    assert normalized.reply_chain == []
    assert bot.calls == []


@pytest.mark.asyncio
async def test_forward_sender_from_nested_sender_dict() -> None:
    payloads = {
        ("get_forward_msg", (("id", 77),)): {
            "messages": [
                {
                    "content": [{"type": "text", "data": {"text": "转发内容"}}],
                    "sender": {"user_id": 42, "nickname": "李四"},
                    "time": 1700000000,
                }
            ]
        }
    }
    bot = FakeBot(payloads)
    message = Message()
    message.append(MessageSegment("forward", {"id": "77"}))
    normalized = await parse_message(bot, message)
    assert len(normalized.forward_tree) == 1
    node = normalized.forward_tree[0]
    assert node.user_id == 42
    assert node.nickname == "李四"
    assert "转发内容" in node.content


@pytest.mark.asyncio
async def test_inline_node_sender_from_nested_sender_dict() -> None:
    bot = FakeBot()
    message = Message()
    message.append(
        MessageSegment(
            "node",
            {
                "sender": {"user_id": 7, "nickname": "内层"},
                "content": [{"type": "text", "data": {"text": "内层内容"}}],
            },
        )
    )
    normalized = await parse_message(bot, message)
    assert len(normalized.forward_tree) == 1
    node = normalized.forward_tree[0]
    assert node.user_id == 7
    assert node.nickname == "内层"
    assert "内层内容" in node.content


def test_message_from_api_prefers_structured_over_raw() -> None:
    msg = _message_from_api(
        {
            "message": [{"type": "text", "data": {"text": "结构化文本"}}],
            "raw_message": "[CQ:image,file=bad]",
        }
    )
    assert "结构化文本" in msg.extract_plain_text()


def test_message_from_api_falls_back_to_raw_when_structured_empty() -> None:
    msg = _message_from_api({"message": [], "raw_message": "原始文本"})
    assert "原始文本" in msg.extract_plain_text()
    msg_raw_only = _message_from_api({"raw_message": "只有 raw"})
    assert "只有 raw" in msg_raw_only.extract_plain_text()


def test_truncation_still_collects_trailing_mentions() -> None:
    message = Message()
    message.append(MessageSegment.text("长" * (MAX_TOTAL_BYTES // 3 + 100)))
    message.append(MessageSegment.at(999))
    normalized = normalize_message(message)
    assert normalized.truncated
    assert 999 in normalized.mentions


def test_prompt_text_renders_reply_chain_deepest_first() -> None:
    chain = [
        {"message_id": 3, "user_id": 1, "nickname": "直接", "text": "直接引用内容"},
        {"message_id": 2, "user_id": 2, "nickname": "中层", "text": "中层内容"},
        {"message_id": 1, "user_id": 3, "nickname": "深层", "text": "深层内容"},
    ]
    normalized = NormalizedMessage(
        plain_text="当前正文", segments=[], reply_chain=chain
    )
    prompt = normalized.prompt_text()
    assert (
        prompt.index("[引用消息 直接: 直接引用内容]")
        > prompt.index("[引用消息 中层: 中层内容]")
        > prompt.index("[引用消息 深层: 深层内容]")
    )
    assert prompt.index("[引用消息 直接: 直接引用内容]") < prompt.index("当前正文")


def test_prompt_text_truncates_long_quote_entry() -> None:
    chain = [
        {
            "message_id": 1,
            "user_id": 1,
            "nickname": "某人",
            "text": "长" * (MAX_REPLY_ENTRY_CHARS + 60),
        }
    ]
    normalized = NormalizedMessage(plain_text="", segments=[], reply_chain=chain)
    prompt = normalized.prompt_text()
    assert "…" in prompt
    assert "长" * (MAX_REPLY_ENTRY_CHARS + 1) not in prompt
    assert "长" * MAX_REPLY_ENTRY_CHARS in prompt


def test_prompt_text_total_cap_omits_earlier_quotes() -> None:
    chain = [
        {"message_id": i, "user_id": i, "nickname": f"用户{i}", "text": "词" * 240}
        for i in range(4)
    ]
    normalized = NormalizedMessage(plain_text="", segments=[], reply_chain=chain)
    prompt = normalized.prompt_text()
    assert "[更早的引用已省略]" in prompt
    assert prompt.count("[引用消息 ") == 2


def test_prompt_text_quote_only_message_is_not_placeholder() -> None:
    chain = [{"message_id": 1, "user_id": 1, "nickname": "某人", "text": "被引用的话"}]
    normalized = NormalizedMessage(plain_text="", segments=[], reply_chain=chain)
    assert normalized.prompt_text() != "[非文本消息]"
    assert "被引用的话" in normalized.prompt_text()


def test_prompt_text_empty_and_media_only_keep_legacy_format() -> None:
    assert NormalizedMessage(plain_text="", segments=[]).prompt_text() == "[非文本消息]"
    message = Message()
    message.append(MessageSegment.text("你好"))
    message.append(MessageSegment.image(file="deadbeef.jpg"))
    normalized = normalize_message(message)
    normalized.forward_tree = [ForwardNode()]
    # 图片占位符沿用既有行为并入正文;媒体与转发标记保持原格式。
    assert normalized.prompt_text() == "你好[图片] [媒体: image] [包含转发消息]"
