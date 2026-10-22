# ruff: noqa: PLR2004, RUF001, TRY003, FBT001, FBT003
"""群对话系统回归：入口合批、只喊一声的续话、繁忙群引用、短会话节奏与配额顺序。"""

from __future__ import annotations

import asyncio
import importlib
import sys
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import nonebot
import pytest
from sqlalchemy.exc import OperationalError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))
try:
    nonebot.get_driver()
except ValueError:
    nonebot.init()
if (
    nonebot.get_plugin("yawn_core") is None
    and nonebot.get_plugin("src.plugins.yawn_core") is None
):
    nonebot.load_from_toml("pyproject.toml")

from src.plugins.yawn_core.yawn_agent import (
    capabilities,
    collector,
    conversation,
    outbound,
    proactive,
    speech,
    tools,
    turn_intake,
)
from src.plugins.yawn_core.yawn_agent.message_parser import (
    NormalizedMessage,
    SegmentNode,
)

dialogue = importlib.import_module("src.plugins.yawn_core.yawn_agent.dialogue")
orm_base = importlib.import_module("nonebot_plugin_orm")
config_models = importlib.import_module(
    "src.plugins.yawn_core.data_models.group_agent_config"
)
message_models = importlib.import_module(
    "src.plugins.yawn_core.data_models.group_agent_message"
)
bot_group_models = importlib.import_module(
    "src.plugins.yawn_core.data_models.bot_group"
)

BOT_ID = 100
GROUP_ID = 1
ALICE = 201
BOB = 202


def _text(text: str, *, trigger: str | None = "mention") -> NormalizedMessage:
    message = NormalizedMessage(
        plain_text=text,
        segments=[SegmentNode(type="text", data={"text": text}, text=text)]
        if text
        else [],
    )
    message.trigger_source = trigger
    message.trigger_signals = {"mention": trigger == "mention"}
    return message


def _bare_mention() -> NormalizedMessage:
    message = NormalizedMessage(
        plain_text="@机器人",
        segments=[SegmentNode(type="at", data={"qq": str(BOT_ID)})],
        mentions=[BOT_ID],
    )
    message.trigger_source = "mention"
    message.trigger_signals = {"mention": True}
    return message


def _event(user_id: int, message_id: int) -> Any:
    return SimpleNamespace(
        group_id=GROUP_ID,
        message_id=message_id,
        get_user_id=lambda: str(user_id),
        sender=SimpleNamespace(
            card=f"成员{user_id}", nickname=None, role="member", title=None
        ),
    )


# ── 入口整形 ─────────────────────────────────────────────


def test_bare_call_detection_ignores_wake_words_and_punctuation() -> None:
    words = ("小助手", "yawn")
    assert turn_intake.is_bare_call(_bare_mention(), wake_words=words)
    assert turn_intake.is_bare_call(
        _text("小助手～", trigger="wake_word"), wake_words=words
    )
    assert not turn_intake.is_bare_call(
        _text("小助手 在吗", trigger="wake_word"), wake_words=words
    )
    assert not turn_intake.is_bare_call(_text("帮我查一下"), wake_words=words)
    # 回复 Bot 的消息带着引用上下文，不算“只喊一声”。
    replied = _bare_mention()
    replied.reply_chain = [{"message_id": 9, "user_id": BOT_ID, "text": "之前"}]
    assert not turn_intake.is_bare_call(replied, wake_words=words)


def test_merge_normalized_keeps_order_and_bounded_media() -> None:
    first = _text("第一句")
    first.media_refs = [{"type": "image", "source": "current"}]
    second = _text("第二句", trigger=None)
    second.reply_chain = [{"message_id": 5, "user_id": BOB, "text": "原话"}]
    merged = turn_intake.merge_normalized([first, second])

    assert merged.plain_text == "第一句\n第二句"
    assert merged.intent_text() == "第一句\n第二句"
    assert merged.trigger_source == "mention"
    assert merged.reply_chain == second.reply_chain
    assert merged.media_refs == first.media_refs


@pytest.mark.asyncio
async def test_worker_merges_same_actor_triggers_but_keeps_other_actors_fifo(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(collector, "DEBOUNCE_SECONDS", 0.0)
    collector.reset_for_tests()
    processed: list[tuple[int, str, list[int]]] = []
    first_started = asyncio.Event()
    release_first = asyncio.Event()
    done = asyncio.Event()

    async def process(
        _bot: Any, event: Any, normalized: Any, *, enqueued_at: float
    ) -> None:
        assert enqueued_at > 0
        processed.append(
            (
                int(event.message_id),
                normalized.plain_text,
                list(normalized.merged_message_ids),
            )
        )
        if len(processed) == 1:
            first_started.set()
            await release_first.wait()
        if len(processed) == 3:
            done.set()

    bot = SimpleNamespace(self_id=str(BOT_ID))
    try:
        assert collector.enqueue(
            GROUP_ID, (bot, _event(BOB, 1), _text("先来的")), BOT_ID
        )
        collector.ensure_worker(
            GROUP_ID, process, BOT_ID, coalesce=turn_intake.coalesce_same_actor
        )
        await asyncio.wait_for(first_started.wait(), timeout=1)
        # Bob 的回合进行中，Alice 连发三条、Bob 又插一条。
        for message_id, text in ((2, "问个事"), (3, "就是那个"), (4, "怎么配置")):
            assert collector.enqueue(
                GROUP_ID, (bot, _event(ALICE, message_id), _text(text)), BOT_ID
            )
        assert collector.enqueue(
            GROUP_ID, (bot, _event(BOB, 5), _text("我也想问")), BOT_ID
        )
        release_first.set()
        await asyncio.wait_for(done.wait(), timeout=1)
    finally:
        collector.reset_for_tests()
        await asyncio.sleep(0)

    assert processed == [
        (1, "先来的", []),
        (4, "问个事\n就是那个\n怎么配置", [2, 3]),
        (5, "我也想问", []),
    ]


@pytest.mark.asyncio
async def test_bare_call_waits_for_followup_text_then_merges() -> None:
    turn_intake.reset_for_tests()
    released: list[Any] = []
    bot = SimpleNamespace(self_id=str(BOT_ID))
    key = (BOT_ID, GROUP_ID, ALICE)
    turn_intake.hold_bare_call(
        key, (bot, _event(ALICE, 10), _bare_mention()), released.append
    )
    held = turn_intake.take_bare_call(key)
    assert held is not None
    assert turn_intake.held_call_count() == 0

    _bot, event, merged = turn_intake.continue_bare_call(
        held, (bot, _event(ALICE, 11), _text("帮我看看日程", trigger=None))
    )
    await asyncio.sleep(0)
    assert released == []
    assert int(event.message_id) == 11
    assert merged.trigger_source == "mention"
    assert merged.trigger_signals["continuation"] is True
    assert merged.merged_message_ids == [10]
    assert merged.intent_text() == "帮我看看日程"


@pytest.mark.asyncio
async def test_bare_call_without_followup_is_still_answered() -> None:
    turn_intake.reset_for_tests()
    released: list[Any] = []
    bot = SimpleNamespace(self_id=str(BOT_ID))
    item = (bot, _event(ALICE, 10), _bare_mention())
    turn_intake.hold_bare_call(
        (BOT_ID, GROUP_ID, ALICE), item, released.append, delay=0.01
    )
    await asyncio.sleep(0.05)
    assert released == [item]
    assert turn_intake.held_call_count() == 0


# ── 短会话节奏 ─────────────────────────────────────────────


def test_short_conversation_waits_less_when_one_member_is_replying() -> None:
    session = conversation.ConversationSession(
        session_id=1,
        started_at=100.0,
        last_bot_at=100.0,
        bot_turns=1,
        topic="测试",
        batch_first_at=110.0,
        batch_last_at=112.0,
        batch_user_ids=[ALICE],
    )
    assert (
        conversation.batch_due_at(session)
        == 112.0 + conversation.CONVERSATION_DIRECT_QUIET_SECONDS
    )
    session.batch_user_ids.append(BOB)
    assert (
        conversation.batch_due_at(session)
        == 112.0 + conversation.CONVERSATION_QUIET_SECONDS
    )


# ── 主动发言选对机器人 ─────────────────────────────────────


def test_proactive_prefers_the_bot_that_is_in_the_group() -> None:
    first = SimpleNamespace(self_id="1")
    second = SimpleNamespace(self_id="2")
    assert proactive._order_bots_for_group([first, second], 2) == [second, first]
    assert proactive._order_bots_for_group([first, second], None) == [first, second]
    assert proactive._order_bots_for_group([first, second], 3) == [first, second]


# ── 特权工具：先扣配额再执行 ─────────────────────────────────


class _QuotaConfig:
    tool_allowlist = ("mute_member",)
    admin_tool_count = 0
    admin_tool_daily_limit = 30
    critical_tool_count = 0
    critical_tool_daily_limit = 5

    def __init__(self) -> None:
        self.tool_day = tools.now_beijing().strftime("%Y-%m-%d")


class _QuotaSession:
    def __init__(self, config: _QuotaConfig, *, fail_commit: bool = False) -> None:
        self.config = config
        self.fail_commit = fail_commit
        self.events: list[str] = []
        self.committed_counts: list[int] = []

    async def get(self, _model: object, _key: object) -> _QuotaConfig:
        return self.config

    async def commit(self) -> None:
        if self.fail_commit:
            raise OperationalError("commit", {}, RuntimeError("database locked"))
        self.events.append("commit")
        self.committed_counts.append(self.config.admin_tool_count)

    async def rollback(self) -> None:
        self.events.append("rollback")

    async def refresh(self, _obj: object) -> None:
        return None

    async def flush(self) -> None:
        return None


class _MuteBot:
    self_id = str(BOT_ID)

    def __init__(self, session: _QuotaSession, *, fail: bool = False) -> None:
        self.session = session
        self.fail = fail
        self.muted = False

    async def call_api(self, action: str, **params: Any) -> dict[str, Any]:
        if action == "get_group_member_info":
            user_id = int(params["user_id"])
            return {
                "user_id": user_id,
                "role": "admin" if user_id in {BOT_ID, ALICE} else "member",
            }
        if action == "set_group_ban":
            self.session.events.append("side_effect")
            if self.fail:
                raise RuntimeError("OneBot 拒绝")
            self.muted = True
            return {}
        raise AssertionError(action)


@pytest.fixture
def mute_caps(monkeypatch: pytest.MonkeyPatch) -> Any:
    current = capabilities.BotGroupCapabilities(
        role="admin", can_manage=True, actions=frozenset({"set_group_ban"})
    )

    async def probe(*_args: Any, **_kwargs: Any) -> Any:
        return current

    async def no_audit(*_args: Any, **_kwargs: Any) -> None:
        return None

    monkeypatch.setattr(tools, "probe_group_capabilities", probe)
    monkeypatch.setattr(tools, "_audit", no_audit)
    return current


async def _mute(bot: _MuteBot, session: _QuotaSession, caps: Any) -> dict[str, Any]:
    return await tools.execute_tool(
        "mute_member",
        {"user_id": 300, "duration": 60},
        bot=bot,
        group_id=GROUP_ID,
        actor_user_id=ALICE,
        session=session,
        capabilities=caps,
    )


@pytest.mark.asyncio
async def test_privileged_quota_is_committed_before_side_effect(mute_caps: Any) -> None:
    config = _QuotaConfig()
    session = _QuotaSession(config)
    bot = _MuteBot(session)

    result = await _mute(bot, session, mute_caps)

    assert result["ok"] is True
    assert bot.muted is True
    assert session.events[:2] == ["commit", "side_effect"]
    assert session.committed_counts[0] == 1
    assert config.admin_tool_count == 1


@pytest.mark.asyncio
async def test_failed_side_effect_refunds_reserved_quota(mute_caps: Any) -> None:
    config = _QuotaConfig()
    session = _QuotaSession(config)
    bot = _MuteBot(session, fail=True)

    result = await _mute(bot, session, mute_caps)

    assert result["ok"] is False
    assert config.admin_tool_count == 0
    assert session.committed_counts == [1, 0]


@pytest.mark.asyncio
async def test_quota_commit_failure_prevents_side_effect(mute_caps: Any) -> None:
    config = _QuotaConfig()
    session = _QuotaSession(config, fail_commit=True)
    bot = _MuteBot(session)

    result = await _mute(bot, session, mute_caps)

    assert result["ok"] is False
    assert bot.muted is False
    assert "side_effect" not in session.events


# ── 繁忙群自动引用 ─────────────────────────────────────────


class _KnownMessageSession:
    def __init__(self, known: bool) -> None:
        self.known = known

    async def scalar(self, _stmt: Any) -> Any:
        if not self.known:
            return None
        return SimpleNamespace(
            user_id=ALICE, sender_name="成员201", normalized_text="问题"
        )


@pytest.mark.asyncio
async def test_speech_plan_target_adds_reply_segment() -> None:
    plan = speech.speech_plan_from_text(
        "这样配置就行",
        scene="direct_reply",
        target=speech.SpeechTarget(reply_to_message_id=42),
    )
    prepared = await outbound.prepare_speech_plan(
        plan, session=_KnownMessageSession(True), group_id=GROUP_ID
    )
    assert [item["type"] for item in prepared.segment_records][:1] == ["reply"]
    assert prepared.normalized_text == "这样配置就行"


@pytest.mark.asyncio
async def test_speech_plan_target_falls_back_when_message_unknown() -> None:
    plan = speech.speech_plan_from_text(
        "这样配置就行",
        scene="direct_reply",
        target=speech.SpeechTarget(reply_to_message_id=42),
    )
    prepared = await outbound.prepare_speech_plan(
        plan, session=_KnownMessageSession(False), group_id=GROUP_ID
    )
    assert [item["type"] for item in prepared.segment_records] == ["text"]


# ── 端到端：一轮对话 ───────────────────────────────────────


class _SessionFactory:
    def __init__(self, maker: Any) -> None:
        self.maker = maker

    def __call__(self) -> Any:
        return self.maker()


async def _dialogue_db(*messages: tuple[int, int, str]) -> Any:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(orm_base.Model.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=True)
    now = dialogue.now_beijing()
    async with maker() as session:
        session.add(bot_group_models.BotGroup(group_id=GROUP_ID, group_name="测试群"))
        session.add(
            config_models.GroupAgentConfig(
                group_id=GROUP_ID, short_conversation_enabled=False
            )
        )
        for index, (user_id, message_id, text) in enumerate(messages):
            session.add(
                message_models.GroupAgentMessage(
                    bot_id=BOT_ID,
                    message_id=message_id,
                    group_id=GROUP_ID,
                    user_id=user_id,
                    sender_name=f"成员{user_id}",
                    role="member",
                    normalized_text=text,
                    segments=[],
                    reply_chain=[],
                    forward_tree=[],
                    media_refs=[],
                    received_at=now - timedelta(seconds=len(messages) - index),
                    expires_at=now + timedelta(days=7),
                )
            )
        await session.commit()
    return engine, maker


def _completion(content: str) -> Any:
    return SimpleNamespace(
        message=SimpleNamespace(content=content, tool_calls=[]),
        finish_reason="stop",
        outcome="success",
        prompt_tokens=10,
        completion_tokens=5,
        cached_tokens=0,
        cache_miss_tokens=10,
    )


async def _run_dialogue_turn(
    monkeypatch: pytest.MonkeyPatch,
    messages: tuple[tuple[int, int, str], ...],
    trigger_message_id: int,
    normalized: NormalizedMessage,
) -> tuple[list[Any], list[Any]]:
    engine, maker = await _dialogue_db(*messages)
    sent: list[Any] = []
    prompts: list[Any] = []

    async def fake_complete(messages_: Any, _tools: Any, **_kwargs: Any) -> Any:
        prompts.append(messages_)
        return _completion("这样配置就行")

    async def fake_send(
        _bot: Any, _group_id: int, prepared: Any, **_kwargs: Any
    ) -> Any:
        sent.append(prepared)
        return outbound.SendResult(
            sent=True,
            message_id=999,
            normalized_text=prepared.normalized_text,
            segment_types=tuple(item["type"] for item in prepared.segment_records),
        )

    monkeypatch.setattr(dialogue, "get_session", _SessionFactory(maker))
    monkeypatch.setattr(dialogue, "complete_with_tools_result", fake_complete)
    monkeypatch.setattr(dialogue, "send_prepared_outbound", fake_send)
    monkeypatch.setenv("AGENT_AI_WAIT_NOTICE_DELAY", "0")
    bot = SimpleNamespace(self_id=str(BOT_ID))
    try:
        await dialogue.process_group_message(
            bot, _event(ALICE, trigger_message_id), normalized, enqueued_at=None
        )
    finally:
        await engine.dispose()
    return sent, prompts


@pytest.mark.asyncio
async def test_dialogue_quotes_trigger_when_others_spoke_after_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sent, prompts = await _run_dialogue_turn(
        monkeypatch,
        ((ALICE, 1, "怎么配置提醒"), (BOB, 2, "我也在等"), (BOB, 3, "顺便问下别的")),
        1,
        _text("怎么配置提醒"),
    )
    assert len(prompts) == 1
    assert len(sent) == 1
    assert [item["type"] for item in sent[0].segment_records][:1] == ["reply"]


@pytest.mark.asyncio
async def test_dialogue_does_not_quote_in_one_to_one_exchange(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sent, _prompts = await _run_dialogue_turn(
        monkeypatch,
        ((BOB, 1, "早上好"), (ALICE, 2, "怎么配置提醒")),
        2,
        _text("怎么配置提醒"),
    )
    assert len(sent) == 1
    assert [item["type"] for item in sent[0].segment_records] == ["text"]


@pytest.mark.asyncio
async def test_merged_turn_excludes_absorbed_messages_from_history(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    merged = turn_intake.merge_normalized(
        [_text("问个事"), _text("提醒怎么设", trigger=None)]
    )
    merged.merged_message_ids = [2]
    sent, prompts = await _run_dialogue_turn(
        monkeypatch,
        ((BOB, 1, "早上好"), (ALICE, 2, "问个事"), (ALICE, 3, "提醒怎么设")),
        3,
        merged,
    )
    rendered = "\n".join(str(item.get("content")) for item in prompts[0])
    # 被吸收的消息只出现在当前回合，不在历史里重复。
    assert rendered.count("问个事") == 1
    # 合并回合总是引用最新一条，读者能看出 Bot 在回应哪几句。
    assert [item["type"] for item in sent[0].segment_records][:1] == ["reply"]
