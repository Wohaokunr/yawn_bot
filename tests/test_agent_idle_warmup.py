# ruff: noqa: ANN001,ANN002,ANN003,ARG001,DTZ001,E501,FBT003,PLR2004,RUF001
"""冷场暖场回归：群里长期没人说话时 Bot 不再无限自言自语。"""

from __future__ import annotations

import importlib
import json
import sys
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import nonebot
import pytest
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

try:
    nonebot.get_driver()
except ValueError:
    nonebot.init()

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))
if nonebot.get_plugin("yawn_core") is None:
    nonebot.load_from_toml("pyproject.toml")

_PKG = "src.plugins.yawn_core.yawn_agent"
proactive = importlib.import_module(f"{_PKG}.proactive")
policy = importlib.import_module(f"{_PKG}.proactive_policy")
warmup = importlib.import_module(f"{_PKG}.idle_warmup")
activity = importlib.import_module(f"{_PKG}.activity")
outbound = importlib.import_module(f"{_PKG}.outbound")
context_mod = importlib.import_module(f"{_PKG}.context")
memory_models = importlib.import_module("src.plugins.yawn_core.data_models.agent_memory")
message_models = importlib.import_module(
    "src.plugins.yawn_core.data_models.group_agent_message"
)
config_models = importlib.import_module(
    "src.plugins.yawn_core.data_models.group_agent_config"
)
audit_models = importlib.import_module("src.plugins.yawn_core.data_models.agent_audit")

GroupAgentConfig = config_models.GroupAgentConfig
GroupAgentMessage = message_models.GroupAgentMessage
AgentAudit = audit_models.AgentAudit
WarmupState = warmup.WarmupState

GROUP = 4242
BOT = 10001
DAY = datetime(2026, 10, 20, 0, 0)


def _config(**kwargs: Any) -> Any:
    values: dict[str, Any] = {
        "group_id": GROUP,
        "enabled": True,
        "proactive_enabled": True,
        "proactive_active_enabled": True,
        "short_conversation_enabled": False,
        "proactive_probability": 1.0,
        "proactive_active_probability": 0.25,
        "proactive_active_window_minutes": 12,
        "idle_threshold_minutes": 15,
        "cooldown_minutes": 8,
        "daily_limit": 30,
        "raw_retention_days": 7,
        "persona_enabled": False,
        "persona_profile": {},
        "recent_response_fingerprints": [],
        "proactive_count": 0,
    }
    values.update(kwargs)
    return GroupAgentConfig(**values)


# ---------------------------------------------------------------- 纯策略


def test_gate_stops_after_unanswered_cap() -> None:
    now = DAY.replace(hour=12)
    state = WarmupState(
        last_human_at=now - timedelta(hours=2),
        last_bot_at=now - timedelta(hours=1),
        unanswered=2,
    )
    gate = warmup.evaluate_warmup_gate(_config(), state, now)
    assert not gate.allowed
    assert gate.code == "unanswered_cap"


def test_gate_low_sociability_persona_tries_only_once() -> None:
    behavior = SimpleNamespace(sociability=1)
    now = DAY.replace(hour=12)
    state = WarmupState(
        last_human_at=now - timedelta(hours=2),
        last_bot_at=now - timedelta(hours=1),
        unanswered=1,
    )
    gate = warmup.evaluate_warmup_gate(_config(), state, now, behavior=behavior)
    assert gate.code == "unanswered_cap"
    assert gate.max_unanswered == 1


def test_gate_backoff_doubles_with_each_unanswered_message() -> None:
    config = _config(idle_threshold_minutes=15)
    assert warmup.required_idle_minutes(config, 0) == 15
    assert warmup.required_idle_minutes(config, 1) == 30
    assert warmup.required_idle_minutes(config, 2) == 60
    assert warmup.required_idle_minutes(config, 30) == warmup.WARMUP_MAX_BACKOFF_MINUTES
    now = DAY.replace(hour=12)
    state = WarmupState(
        last_human_at=now - timedelta(minutes=50),
        last_bot_at=now - timedelta(minutes=20),
        unanswered=1,
    )
    gate = warmup.evaluate_warmup_gate(config, state, now)
    assert gate.code == "backoff"
    assert gate.next_eligible_at == state.last_bot_at + timedelta(minutes=30)


def test_gate_zero_threshold_is_floored() -> None:
    config = _config(idle_threshold_minutes=0)
    assert warmup.effective_idle_threshold_minutes(config) == warmup.WARMUP_MIN_IDLE_MINUTES
    now = DAY.replace(hour=12)
    state = WarmupState(last_human_at=now - timedelta(minutes=1))
    assert warmup.evaluate_warmup_gate(config, state, now).code == "human_idle"


@pytest.mark.parametrize(
    ("state_kwargs", "hour", "code"),
    [
        ({"last_human_at": None}, 12, "no_human"),
        ({"last_human_at": DAY - timedelta(days=3)}, 12, "dead_group"),
        ({"last_human_at": DAY, "closed": True}, 12, "closed"),
        ({"last_human_at": DAY, "warmups_today": 4}, 12, "daily_cap"),
        ({"last_human_at": DAY}, 3, "quiet_hours"),
    ],
)
def test_gate_refusals(state_kwargs: dict[str, Any], hour: int, code: str) -> None:
    now = DAY.replace(hour=hour)
    gate = warmup.evaluate_warmup_gate(_config(), WarmupState(**state_kwargs), now)
    assert not gate.allowed
    assert gate.code == code


def test_quiet_hours_gate_reports_when_it_ends() -> None:
    now = DAY.replace(hour=3, minute=20)
    gate = warmup.evaluate_warmup_gate(
        _config(), WarmupState(last_human_at=DAY - timedelta(hours=1)), now
    )
    assert gate.next_eligible_at == DAY.replace(hour=8)


def test_daily_cap_never_exceeds_daily_limit() -> None:
    assert warmup.warmup_daily_cap(_config(daily_limit=2)) == 2
    assert warmup.warmup_daily_cap(_config(daily_limit=30)) == warmup.WARMUP_DAILY_CAP


def test_skip_backoff_grows_and_caps() -> None:
    config = _config(idle_threshold_minutes=15)
    assert warmup.skip_retry_minutes(config, 0) == 0
    assert warmup.skip_retry_minutes(config, 1) == 15
    assert warmup.skip_retry_minutes(config, 3) == 60
    assert warmup.skip_retry_minutes(config, 20) == warmup.WARMUP_SKIP_BACKOFF_MAX_MINUTES
    now = DAY.replace(hour=12)
    state = WarmupState(
        last_human_at=now - timedelta(hours=3),
        skips=2,
        last_skip_at=now - timedelta(minutes=10),
    )
    assert warmup.evaluate_warmup_gate(config, state, now).code == "skip_backoff"


def test_probability_decays_with_silence_and_streak() -> None:
    config = _config(proactive_probability=0.35)
    threshold = 15 * 60
    fresh = policy.warmup_probability(config, threshold * 2, state=WarmupState(DAY))
    long_silence = policy.warmup_probability(
        config, 7 * 3600, state=WarmupState(DAY)
    )
    very_long = policy.warmup_probability(config, 25 * 3600, state=WarmupState(DAY))
    streak = policy.warmup_probability(
        config, threshold * 2, state=WarmupState(DAY, unanswered=1)
    )
    assert long_silence == pytest.approx(fresh / 2)
    assert very_long == pytest.approx(fresh / 4)
    assert streak == pytest.approx(fresh / 2)


def test_episode_roll_is_stable_within_an_episode() -> None:
    anchor = DAY.replace(hour=9)
    first = warmup.episode_roll(GROUP, anchor, 0)
    assert first == warmup.episode_roll(GROUP, anchor, 0)
    assert 0.0 <= first < 1.0
    assert first != warmup.episode_roll(GROUP, anchor, 1)
    assert first != warmup.episode_roll(GROUP, anchor + timedelta(minutes=1), 0)


def test_episode_roll_means_probability_is_per_silence(monkeypatch) -> None:
    """不传 rng 时同一段冷场每分钟结果一致：概率不再被每分钟重掷放大。"""

    config = _config(proactive_probability=0.35)
    last_human = DAY.replace(hour=10)
    state = WarmupState(last_human_at=last_human)
    outcomes = set()
    for minute in range(16, 120):
        now = last_human + timedelta(minutes=minute)
        snapshot = context_mod.ActivitySnapshot(last_human, warmup=state)
        outcomes.add(policy.should_proactively_speak(config, snapshot, now))
        if "warmup" in outcomes:
            break
    roll = warmup.episode_roll(GROUP, last_human, 0)
    peak = policy.warmup_probability(config, 30 * 60, state=state)
    assert ("warmup" in outcomes) == (roll < peak)


def test_opted_out_human_activity_counts_as_not_idle() -> None:
    now = DAY.replace(hour=12)
    snapshot = context_mod.ActivitySnapshot(
        now - timedelta(hours=2),
        warmup=WarmupState(last_human_at=now - timedelta(minutes=2)),
    )
    assert (
        policy.should_proactively_speak(
            _config(proactive_active_enabled=False),
            snapshot,
            now,
            rng=SimpleNamespace(random=lambda: 0.0),
        )
        is None
    )


def test_near_duplicate_catches_rewrites() -> None:
    previous = ["今天有人看昨晚那场比赛了吗？太离谱了"]
    assert warmup.near_duplicate_of("今天有人看昨晚那场比赛了吗，太离谱了！", previous)
    assert warmup.near_duplicate_of("周末大家打算去哪玩", previous) is None
    assert warmup.near_duplicate_of("", previous) is None


def test_prompt_mentions_unanswered_streak_and_topics() -> None:
    now = DAY.replace(hour=12)
    state = WarmupState(
        last_human_at=now - timedelta(minutes=47),
        unanswered=1,
        recent_topics=("新番",),
    )
    prompt = policy.build_user_prompt("warmup", _config(), warmup_state=state, now=now)
    assert "约 47 分钟" in prompt
    assert "1 条，都没有人接" in prompt
    assert "新番" in prompt
    assert "自问自答" in prompt
    active = policy.build_user_prompt("active", _config(), warmup_state=state, now=now)
    assert "都没有人接" not in active


# ---------------------------------------------------------------- 数据库事实


async def _session_factory() -> tuple[Any, Any]:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    tables = [
        GroupAgentConfig.__table__,
        GroupAgentMessage.__table__,
        AgentAudit.__table__,
        memory_models.AgentPrivacy.__table__,
    ]
    async with engine.begin() as connection:
        await connection.run_sync(
            lambda sync: GroupAgentConfig.metadata.create_all(sync, tables=tables)
        )
    return engine, async_sessionmaker(engine, expire_on_commit=False)


_message_ids = iter(range(1, 1_000_000))


def _message(at: datetime, *, role: str = "member", user_id: int = 7) -> Any:
    return GroupAgentMessage(
        bot_id=BOT,
        message_id=next(_message_ids),
        group_id=GROUP,
        user_id=BOT if role == "bot" else user_id,
        role=role,
        normalized_text="hi",
        received_at=at,
        expires_at=at + timedelta(days=7),
    )


def _audit(at: datetime, *, scene: str, action: str, result: str, topic: str = "") -> Any:
    return AgentAudit(
        group_id=GROUP,
        tool_name="proactive_reply",
        arguments={"scene": scene, "action": action, "topic": topic},
        result=result,
        created_at=at,
    )


@pytest.mark.asyncio
async def test_warmup_state_counts_only_since_last_human() -> None:
    engine, factory = await _session_factory()
    base = DAY.replace(hour=9)
    async with factory() as session:
        session.add_all(
            [
                _message(base),
                _audit(base - timedelta(minutes=5), scene="warmup", action="speak", result="speak"),
                _message(base + timedelta(minutes=15), role="bot"),
                _audit(base + timedelta(minutes=15), scene="warmup", action="speak", result="speak", topic="新番"),
                _audit(base + timedelta(minutes=50), scene="warmup", action="wait", result="wait"),
                _audit(base + timedelta(minutes=70), scene="warmup", action="speak", result="delivery_unknown"),
            ]
        )
        await session.commit()
        state = await activity.warmup_state(session, GROUP, base + timedelta(hours=2))
    await engine.dispose()
    assert state.last_human_at == base
    # 投递未知的消息没写历史，但同样算“说了没人接”。
    assert state.unanswered == 2
    assert state.last_bot_at == base + timedelta(minutes=70)
    assert state.skips == 1
    assert state.warmups_today == 3
    assert state.recent_topics == ("新番",)
    assert not state.closed


@pytest.mark.asyncio
async def test_warmup_state_includes_opted_out_humans_and_close() -> None:
    engine, factory = await _session_factory()
    base = DAY.replace(hour=9)
    async with factory() as session:
        session.add(_message(base, user_id=99))
        session.add(
            memory_models.AgentPrivacy(group_id=GROUP, user_id=99, opted_out=True)
        )
        session.add(
            _audit(base + timedelta(minutes=20), scene="warmup", action="close", result="close")
        )
        await session.commit()
        state = await activity.warmup_state(session, GROUP, base + timedelta(hours=1))
    await engine.dispose()
    assert state.last_human_at == base
    assert state.closed


# ---------------------------------------------------------------- 端到端模拟


class _Sim:
    """逐分钟推进模拟时钟，跑真实的候选筛选 + 主动执行流程。"""

    def __init__(self, monkeypatch, factory, replies) -> None:
        self.factory = factory
        self.now = DAY
        self.replies = replies
        self.llm_calls = 0
        self.sent: list[tuple[datetime, str]] = []
        self.last_audit_id = 0
        clock = lambda: self.now  # noqa: E731
        monkeypatch.setattr(proactive, "now_beijing", clock)
        monkeypatch.setattr(proactive, "get_session", factory)
        monkeypatch.setattr(warmup, "episode_roll", lambda *_a: 0.0)
        monkeypatch.setattr(policy, "episode_roll", lambda *_a: 0.0)

        async def enabled(*_a, **_k):
            return True

        async def load_context(*_a, **_k):
            return {"messages": []}

        async def generate(_messages):
            self.llm_calls += 1
            return self.replies(self.llm_calls)

        async def prepare(plan, **_k):
            return plan

        async def send(_bot, _group_id, prepared, **_k):
            text = prepared.visible_text
            self.sent.append((self.now, text))
            return outbound.SendResult(
                sent=True,
                message_id=next(_message_ids),
                normalized_text=text,
                segment_types=("text",),
            )

        async def persist(session, bot_id, group_id, message_id, text, *_a, **_k):
            row = _message(self.now, role="bot")
            row.normalized_text = text
            session.add(row)

        monkeypatch.setattr(proactive, "agent_runtime_enabled", enabled)
        monkeypatch.setattr(proactive, "_load_context", load_context)
        monkeypatch.setattr(proactive, "build_messages", lambda **_k: ([], None))
        monkeypatch.setattr(proactive, "_generate_proactive_reply", generate)
        monkeypatch.setattr(proactive, "_prepare_proactive_message", prepare)
        monkeypatch.setattr(proactive, "send_prepared_outbound", send)
        monkeypatch.setattr(proactive, "persist_bot_reply", persist)

    async def human_says(self, at: datetime) -> None:
        async with self.factory() as session:
            session.add(_message(at))
            await session.commit()

    async def tick(self) -> None:
        bots = [SimpleNamespace(self_id=str(BOT))]
        async with self.factory() as session:
            candidates = await proactive._collect_candidates(session, self.now)
        for candidate in candidates:
            await proactive._process_candidate_impl(candidate, bots)
        # 审计 created_at 由 ORM 默认值写真实时间，这里改写成模拟时钟。
        async with self.factory() as session:
            last = await session.scalar(
                select(AgentAudit.id).order_by(AgentAudit.id.desc()).limit(1)
            )
            if last and last > self.last_audit_id:
                await session.execute(
                    update(AgentAudit)
                    .where(AgentAudit.id > self.last_audit_id)
                    .values(created_at=self.now)
                )
                await session.commit()
                self.last_audit_id = last

    async def run_until(self, end: datetime) -> None:
        while self.now < end:
            self.now += timedelta(minutes=1)
            await self.tick()


_TOPICS = [
    "有人看了昨晚的流星雨吗",
    "突然想吃火锅，这天气太合适了",
    "周末那个新番更新了没",
    "楼下猫咪今天又来蹭饭",
    "最近有什么好听的歌推荐",
    "咖啡喝多了睡不着怎么办",
]


def _speak(index: int) -> str:
    text = _TOPICS[(index - 1) % len(_TOPICS)] + f"（第{index}次）"
    return json.dumps(
        {"action": "speak", "speak": True, "text": text, "topic": f"话题{index}"},
        ensure_ascii=False,
    )


async def _seed(factory, **config_kwargs: Any) -> None:
    async with factory() as session:
        session.add(_config(**config_kwargs))
        await session.commit()


@pytest.mark.asyncio
async def test_silent_group_gets_at_most_two_warmups_then_resumes(monkeypatch) -> None:
    engine, factory = await _session_factory()
    await _seed(factory)
    sim = _Sim(monkeypatch, factory, _speak)
    sim.now = DAY.replace(hour=10)
    await sim.human_says(sim.now)
    await sim.run_until(DAY.replace(hour=15))
    # 旧逻辑：每 15 分钟一条，5 小时约 20 条直到每日上限。
    assert len(sim.sent) == 2
    first, second = (at for at, _ in sim.sent)
    assert first - DAY.replace(hour=10) >= timedelta(minutes=15)
    assert second - first >= timedelta(minutes=30)
    assert sim.llm_calls == 2

    await sim.human_says(sim.now)
    await sim.run_until(DAY.replace(hour=15, minute=40))
    assert len(sim.sent) == 3
    # 第二段冷场同样最多两条；当天暖场总数到 4 次后不再暖场。
    await sim.human_says(DAY.replace(hour=17))
    await sim.run_until(DAY.replace(hour=20))
    assert len(sim.sent) == warmup.WARMUP_DAILY_CAP
    await engine.dispose()


@pytest.mark.asyncio
async def test_model_close_pauses_until_a_human_speaks(monkeypatch) -> None:
    engine, factory = await _session_factory()
    await _seed(factory)
    close = json.dumps({"action": "close", "speak": False, "reason": "大家在忙"})
    sim = _Sim(monkeypatch, factory, lambda _i: close)
    sim.now = DAY.replace(hour=10)
    await sim.human_says(sim.now)
    await sim.run_until(DAY.replace(hour=20))
    assert sim.sent == []
    assert sim.llm_calls == 1
    await engine.dispose()


@pytest.mark.asyncio
async def test_model_wait_backs_off_exponentially(monkeypatch) -> None:
    engine, factory = await _session_factory()
    await _seed(factory)
    wait = json.dumps({"action": "wait", "speak": False, "reason": "没切入点"})
    sim = _Sim(monkeypatch, factory, lambda _i: wait)
    sim.now = DAY.replace(hour=9)
    await sim.human_says(sim.now)
    await sim.run_until(DAY.replace(hour=21))
    assert sim.sent == []
    # 旧逻辑每 ~4 分钟重抽一次，12 小时上百次 LLM 调用。
    assert sim.llm_calls <= 8
    await engine.dispose()


@pytest.mark.asyncio
async def test_no_warmup_overnight(monkeypatch) -> None:
    engine, factory = await _session_factory()
    await _seed(factory)
    sim = _Sim(monkeypatch, factory, _speak)
    sim.now = DAY.replace(hour=0, minute=50)
    await sim.human_says(sim.now)
    await sim.run_until(DAY.replace(hour=7, minute=59))
    assert sim.sent == []
    await sim.run_until(DAY.replace(hour=8, minute=5))
    assert len(sim.sent) == 1
    await engine.dispose()


@pytest.mark.asyncio
async def test_near_duplicate_warmup_is_not_sent(monkeypatch) -> None:
    engine, factory = await _session_factory()
    await _seed(
        factory,
        recent_response_fingerprints=[
            {"text": "有人看了昨晚的流星雨吗", "at": DAY.isoformat(), "input": "proactive"}
        ],
    )
    same = json.dumps(
        {"action": "speak", "speak": True, "text": "有人看了昨晚的流星雨吗？"},
        ensure_ascii=False,
    )
    sim = _Sim(monkeypatch, factory, lambda _i: same)
    sim.now = DAY.replace(hour=10)
    await sim.human_says(sim.now)
    await sim.run_until(DAY.replace(hour=11))
    assert sim.sent == []
    await engine.dispose()


def test_runtime_summary_lists_warmup_rules() -> None:
    commands = importlib.import_module(f"{_PKG}.commands")
    text = commands._format_agent_runtime_summary(_config(), True)
    assert "连续 2 条没人接就停到有人说话" in text
    assert "1–8 点不暖场" in text
    off = commands._format_agent_runtime_summary(_config(proactive_enabled=False), True)
    assert "冷场暖场" not in off
