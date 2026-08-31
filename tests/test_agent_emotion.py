from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import nonebot
import pytest

try:
    nonebot.get_driver()
except ValueError:
    nonebot.init()

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))
if nonebot.get_plugin("yawn_core") is None:
    nonebot.load_from_toml("pyproject.toml")

from src.plugins.yawn_core.yawn_agent.emotion import (
    EMOTION_HALF_LIFE_MINUTES,
    detect_emotion_signal,
    emotion_context_state,
    emotion_public_state,
    resolve_emotion_state,
    update_emotion_state,
)


def _now() -> datetime:
    return datetime(2026, 8, 31, 3, 30, tzinfo=timezone.utc)


def test_dynamic_emotion_starts_neutral_and_keeps_prompt_sparse() -> None:
    public = emotion_public_state({}, now=_now(), expressiveness=4)
    assert public["label"] == "neutral"
    assert public["intensity"] == 0.0
    assert public["updatedAt"] is None
    assert emotion_context_state({}, now=_now(), expressiveness=4) == {}


def test_direct_positive_feedback_updates_private_bot_stance_without_raw_text() -> None:
    mutation = update_emotion_state(
        {}, text="谢谢你，刚才解释得真棒", directed=True, now=_now()
    )
    assert mutation.storage_changed is True
    assert mutation.signal is not None and mutation.signal.label == "warm"
    assert mutation.state["label"] == "warm"
    assert mutation.state["source"] == "direct"
    assert mutation.state["reason"] == "收到友好或积极反馈"
    assert "谢谢" not in repr(mutation.state)
    assert "解释" not in repr(mutation.state)


def test_ambient_conflict_is_guarded_while_direct_hostility_is_irritated() -> None:
    ambient = update_emotion_state(
        {}, text="你们都闭嘴，烦死了", directed=False, now=_now()
    )
    direct = update_emotion_state(
        {}, text="你这个废物，闭嘴", directed=True, now=_now()
    )
    assert ambient.state["label"] == "guarded"
    assert ambient.state["source"] == "ambient"
    assert direct.state["label"] == "irritated"
    assert direct.state["source"] == "direct"
    assert float(direct.state["intensity"]) > float(ambient.state["intensity"])


def test_emotion_decays_lazily_toward_neutral() -> None:
    initial = update_emotion_state(
        {}, text="哈哈哈哈笑死", directed=True, now=_now()
    ).state
    first = resolve_emotion_state(initial, now=_now())
    half = resolve_emotion_state(
        initial, now=_now() + timedelta(minutes=EMOTION_HALF_LIFE_MINUTES)
    )
    stale = resolve_emotion_state(initial, now=_now() + timedelta(hours=4))

    assert first.label == "amused"
    assert half.intensity == pytest.approx(first.intensity / 2, rel=0.02)
    assert abs(half.valence) < abs(first.valence)
    assert stale.label == "neutral"


def test_no_signal_prunes_only_stale_state() -> None:
    state = update_emotion_state(
        {}, text="谢谢，太棒了", directed=True, now=_now()
    ).state
    fresh = update_emotion_state(
        state, text="今天周一", directed=False, now=_now() + timedelta(minutes=5)
    )
    stale = update_emotion_state(
        state, text="今天周一", directed=False, now=_now() + timedelta(hours=4)
    )
    assert fresh.storage_changed is False
    assert fresh.state == state
    assert stale.storage_changed is True
    assert stale.state == {}


def test_persona_expressiveness_controls_visibility_not_the_underlying_state() -> None:
    state = update_emotion_state(
        {}, text="哈哈这个真的太好笑了", directed=True, now=_now()
    ).state
    quiet = emotion_public_state(state, now=_now(), expressiveness=0)
    expressive = emotion_public_state(state, now=_now(), expressiveness=4)

    assert quiet["label"] == expressive["label"] == "amused"
    assert quiet["intensity"] == expressive["intensity"]
    assert quiet["expressionIntensity"] < expressive["expressionIntensity"]


def test_irritated_expression_hint_never_authorizes_retaliation() -> None:
    state = update_emotion_state(
        {}, text="你是废物，滚", directed=True, now=_now()
    ).state
    context = emotion_context_state(state, now=_now(), expressiveness=4)
    assert context["label"] == "irritated"
    assert "不得攻击" in context["expression_hint"]
    assert "报复" in context["expression_hint"]


def test_signal_detection_does_not_infer_emotion_from_plain_neutral_text() -> None:
    assert detect_emotion_signal("明天几点开会", directed=False) is None
    curious = detect_emotion_signal("这到底是为什么", directed=True)
    assert curious is not None and curious.label == "curious"


@pytest.mark.asyncio
async def test_group_listener_updates_dynamic_emotion_before_message_persist(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from types import SimpleNamespace

    from src.plugins.yawn_core.yawn_agent import agent
    from src.plugins.yawn_core.yawn_agent.message_parser import (
        NormalizedMessage,
        SegmentNode,
    )

    config = SimpleNamespace(
        enabled=True,
        reply_trigger_enabled=True,
        explicit_wakeup_enabled=True,
        short_conversation_enabled=False,
        emotion_state={},
    )
    persisted_states: list[dict[str, object]] = []

    class Session:
        async def get(self, *_args: object, **_kwargs: object) -> None:
            return None

    async def get_config(*_args: object, **_kwargs: object) -> object:
        return config

    async def runtime_enabled(*_args: object, **_kwargs: object) -> bool:
        return True

    async def allowed(*_args: object, **_kwargs: object) -> bool:
        return True

    async def parse(*_args: object, **_kwargs: object) -> NormalizedMessage:
        return NormalizedMessage(
            plain_text="谢谢你，刚才解释得真棒",
            segments=[
                SegmentNode(
                    type="text",
                    data={"text": "谢谢你，刚才解释得真棒"},
                    text="谢谢你，刚才解释得真棒",
                )
            ],
        )

    async def persist(*_args: object, **_kwargs: object) -> None:
        persisted_states.append(dict(config.emotion_state))

    monkeypatch.setattr(agent, "GroupMessageEvent", SimpleNamespace)
    monkeypatch.setattr(agent, "get_or_create_config", get_config)
    monkeypatch.setattr(agent, "agent_runtime_enabled", runtime_enabled)
    monkeypatch.setattr(agent, "check_feature_permission", allowed)
    monkeypatch.setattr(agent, "parse_message", parse)
    monkeypatch.setattr(agent, "_persist_message", persist)
    monkeypatch.setattr(
        agent,
        "resolve_trigger",
        lambda *_args, **_kwargs: agent.TriggerDecision(
            respond=True, source="mention", mentioned=True
        ),
    )
    monkeypatch.setattr(agent, "enqueue", lambda *_args, **_kwargs: False)

    event = SimpleNamespace(
        group_id=123,
        message_id=456,
        message=(),
        reply=None,
        to_me=True,
        get_user_id=lambda: "789",
    )
    bot = SimpleNamespace(self_id="100")
    await agent.handle_group_agent_message(bot, event, Session())

    assert persisted_states
    assert persisted_states[0]["label"] == "warm"
    assert persisted_states[0]["source"] == "direct"
    assert config.emotion_state["reason"] == "收到友好或积极反馈"


@pytest.mark.asyncio
async def test_privacy_opt_out_stops_emotion_update_before_message_parse(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from types import SimpleNamespace

    from src.plugins.yawn_core.yawn_agent import agent

    config = SimpleNamespace(enabled=True, emotion_state={})
    parsed = False

    class Session:
        async def get(self, *_args: object, **_kwargs: object) -> object:
            return SimpleNamespace(opted_out=True)

    async def get_config(*_args: object, **_kwargs: object) -> object:
        return config

    async def runtime_enabled(*_args: object, **_kwargs: object) -> bool:
        return True

    async def allowed(*_args: object, **_kwargs: object) -> bool:
        return True

    async def parse(*_args: object, **_kwargs: object) -> None:
        nonlocal parsed
        parsed = True

    monkeypatch.setattr(agent, "GroupMessageEvent", SimpleNamespace)
    monkeypatch.setattr(agent, "get_or_create_config", get_config)
    monkeypatch.setattr(agent, "agent_runtime_enabled", runtime_enabled)
    monkeypatch.setattr(agent, "check_feature_permission", allowed)
    monkeypatch.setattr(agent, "parse_message", parse)

    event = SimpleNamespace(
        group_id=123,
        message_id=456,
        message=(),
        reply=None,
        to_me=True,
        get_user_id=lambda: "789",
    )
    bot = SimpleNamespace(self_id="100")
    await agent.handle_group_agent_message(bot, event, Session())

    assert parsed is False
    assert config.emotion_state == {}
