# ruff: noqa: ANN001, ANN003, ARG001, E501, PLR2004, RUF001
"""群聊分条防三段式：说明一条为主、示例不写三条、条数上限 4、套模板时在易变层提醒。"""

from __future__ import annotations

import re
from typing import Any

import pytest
from test_agent_speech_rhythm import _Clock, _ok, _prepared

from src.plugins.yawn_core.yawn_agent import (
    group_rhythm,
    outbound,
    prompt_sections,
    speech_delivery,
    speech_policy,
    speech_rhythm,
)

R = speech_rhythm
GROUP = 7001
_EXAMPLE_RE = re.compile(r"【节奏:[^】]+】([^；。]*)")


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    group_rhythm.reset()
    monkeypatch.delenv("AGENT_REPLY_MAX_PARTS", raising=False)
    monkeypatch.delenv("AGENT_REPLY_MIX_HINT", raising=False)
    yield
    group_rhythm.reset()


# ── 说明 ───────────────────────────────────────────────────────────


def _group_text(**kwargs: Any) -> str:
    params = {"scene": "direct_reply", "act": "answer", "complexity": "normal"}
    params.update(kwargs)
    return R.rhythm_instruction(**params)


def test_group_instruction_makes_single_message_the_default() -> None:
    text = _group_text()
    assert "大多数回复一条就够" in text and "不是固定格式" in text
    assert "群里刷屏会打扰别人" in text
    assert "别每次都分成同样的几条" in text
    assert not re.search(r"2\s*[~～-]\s*\d\s*条", text)
    assert "平常闲聊，把同一句话" not in text


def test_group_examples_cover_many_counts_and_never_exactly_three() -> None:
    text = _group_text()
    counts = [example.count("|||") + 1 for example in _EXAMPLE_RE.findall(text)]
    assert 1 in counts and 2 in counts and max(counts) >= 4
    assert 3 not in counts
    for word in ("【节奏:蹦字】", "空行分好段", "[表情:名字]", "只发一个表情包"):
        assert word in text


def test_group_instruction_keeps_guarantees_and_variants() -> None:
    text = _group_text(act="continue")
    assert "每次都要选" in text and "加起来的字数也和发一条时一样" in text
    assert "分条最多 4 条" in text
    assert "复杂" in _group_text(complexity="complex")
    assert "短回应" in _group_text(act="acknowledge")
    loud = _group_text(style=type("S", (), {"expressiveness": 3})())
    assert "平常照样一条为主" in loud
    # 私聊场景不提“群里刷屏”
    assert "群里刷屏" not in R.rhythm_instruction(scene="private", act="continue", complexity="normal")


def test_short_scenes_unchanged() -> None:
    assert "一条就够" in R.rhythm_instruction(scene="warmup", act="continue", complexity="normal")


# ── 上限 ───────────────────────────────────────────────────────────


def test_default_cap_is_four_so_four_parts_survive() -> None:
    assert R.DEFAULT_MAX_PARTS == 4
    assert R.rhythm_settings().max_parts == 4
    plan = R.plan_text_rhythm("", parts=("刚到家", "饿死了", "先点个外卖", "你们吃了没"), declared="split")
    assert len(plan.parts) == 4


def test_cap_still_configurable(monkeypatch) -> None:
    monkeypatch.setenv("AGENT_REPLY_MAX_PARTS", "3")
    plan = R.plan_text_rhythm("", parts=("a1", "b2", "c3", "d4"), declared="split")
    assert len(plan.parts) == 3
    assert "分条最多 3 条" in _group_text()


# ── 套模板判断与提醒 ────────────────────────────────────────────────


def test_mix_streak_rules() -> None:
    assert R.mix_streak([3, 3, 3]) == "same"
    assert R.mix_streak((2, 3, 2, 4)) == "always_split"
    assert R.mix_streak([1, 1, 1]) == ""
    assert R.mix_streak([3, 1, 3]) == ""


def test_mix_instruction_text() -> None:
    assert R.rhythm_mix_instruction([1, 2, 1]) == ""
    text = R.rhythm_mix_instruction([1, 3, 3, 3])
    assert "1、3、3、3" in text and "一条" in text


def test_group_tracking_and_hint() -> None:
    for _ in range(3):
        group_rhythm.note_group_parts(GROUP, 3)
    assert group_rhythm.recent_group_counts(GROUP) == [3, 3, 3]
    assert "像在套模板" in group_rhythm.group_mix_instruction(GROUP)
    # 别的群不受影响
    assert group_rhythm.group_mix_instruction(GROUP + 1) == ""
    group_rhythm.note_group_parts(GROUP, 1)
    assert group_rhythm.group_mix_instruction(GROUP) == ""
    snap = group_rhythm.snapshot(GROUP)
    assert snap["byParts"]["3"] == 3 and snap["byParts"]["1"] == 1 and snap["mixHints"] == 1


def test_hint_can_be_turned_off(monkeypatch) -> None:
    for _ in range(4):
        group_rhythm.note_group_parts(GROUP, 2)
    monkeypatch.setenv("AGENT_REPLY_MIX_HINT", "false")
    assert group_rhythm.group_mix_instruction(GROUP) == ""


def test_tracking_is_bounded(monkeypatch) -> None:
    monkeypatch.setattr(group_rhythm, "MAX_TRACKED_GROUPS", 2)
    for gid in (1, 2, 3):
        group_rhythm.note_group_parts(gid, 1, now=float(gid))
    assert len(group_rhythm._recent) == 2
    for _ in range(20):
        group_rhythm.note_group_parts(GROUP, 2)
    assert len(group_rhythm.recent_group_counts(GROUP)) == group_rhythm.RECENT_SHAPES
    group_rhythm.note_group_parts(None, 3)
    group_rhythm.note_group_parts(GROUP, 0)


# ── 发送时记录 ─────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_deliver_parts_records_planned_count_even_when_hurried() -> None:
    clock = _Clock()

    async def send_one(part: Any, index: int) -> outbound.SendResult:
        return _ok(part.normalized_text, 10 + index)

    await speech_delivery.deliver_parts(
        [_prepared("一"), _prepared("二"), _prepared("三")],
        delays=(0.0, 1.0, 1.0),
        send_one=send_one,
        hurry=lambda: True,
        sleep=clock.sleep,
        clock=clock,
        group_id=GROUP,
    )
    assert group_rhythm.recent_group_counts(GROUP) == [3]


@pytest.mark.asyncio
async def test_deliver_parts_skips_record_when_first_fails() -> None:
    async def send_one(part: Any, index: int) -> outbound.SendResult:
        return outbound.SendResult(sent=False, message_id=None, normalized_text=part.normalized_text, segment_types=("text",))

    await speech_delivery.deliver_parts([_prepared("一"), _prepared("二")], send_one=send_one, group_id=GROUP)
    assert group_rhythm.recent_group_counts(GROUP) == []


@pytest.mark.asyncio
async def test_single_deliveries_are_recorded_too() -> None:
    async def send_one(part: Any, index: int) -> outbound.SendResult:
        return _ok(part.normalized_text, 1)

    await speech_delivery.deliver_parts([_prepared("好")], send_one=send_one, group_id=GROUP)
    assert group_rhythm.recent_group_counts(GROUP) == [1]


# ── 进发言策略（易变层）────────────────────────────────────────────


def test_speech_parts_include_mix_section_only_when_templated() -> None:
    turn = {"trigger": "mention"}
    plain = dict(speech_policy.build_speech_parts(None, turn, group_id=GROUP))
    assert plain["speech.rhythm_mix"] == ""
    for _ in range(3):
        group_rhythm.note_group_parts(GROUP, 3)
    parts = speech_policy.build_speech_parts(None, turn, group_id=GROUP)
    ids = [section for section, _text in parts]
    assert ids.index("speech.rhythm_mix") == ids.index("speech.rhythm") + 1
    assert "像在套模板" in dict(parts)["speech.rhythm_mix"]
    section = prompt_sections.get_section("speech.rhythm_mix")
    assert section.layer == "realtime" and section.kind == prompt_sections.KIND_GENERATED


def test_mix_section_can_be_overridden(monkeypatch) -> None:
    for _ in range(3):
        group_rhythm.note_group_parts(GROUP, 3)
    calls: list[str] = []
    original = prompt_sections.apply_section

    def spy(section_id, text, **kwargs):
        calls.append(section_id)
        return original(section_id, text, **kwargs)

    monkeypatch.setattr(speech_policy, "apply_section", spy)
    speech_policy.build_speech_parts(None, {"trigger": "mention"}, group_id=GROUP)
    assert "speech.rhythm_mix" in calls


def test_mix_hint_key_in_catalog() -> None:
    from pathlib import Path

    catalog = (
        Path(__file__).resolve().parents[1] / "src/plugins/yawn_core/webui/environment_catalog.dotenv"
    ).read_text(encoding="utf-8")
    assert "\nAGENT_REPLY_MIX_HINT=true" in catalog and "\nAGENT_REPLY_MAX_PARTS=4" in catalog
