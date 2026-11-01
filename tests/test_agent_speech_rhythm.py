# ruff: noqa: PLR2004, ARG001, E501
"""发言节奏：||| 分条、整段/分条/连发/蹦字、兜底拆分、停顿、依次发送与逐条落库。"""

from __future__ import annotations

import importlib
import sys
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import nonebot
import pytest
from sqlalchemy import select
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
    outbound,
    progress_notice,
    speech,
    speech_delivery,
    speech_policy,
    speech_quality,
    speech_rhythm,
)
from src.plugins.yawn_core.yawn_agent.message_parser import (
    NormalizedMessage,
    SegmentNode,
)

dialogue = importlib.import_module("src.plugins.yawn_core.yawn_agent.dialogue")
proactive = importlib.import_module("src.plugins.yawn_core.yawn_agent.proactive")
message_handler = importlib.import_module(
    "src.plugins.yawn_core.yawn_agent.tool_handlers.message"
)
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

R = speech_rhythm
BOT_ID = 100
GROUP_ID = 1
ALICE = 201
BOB = 202


@pytest.fixture(autouse=True)
def _default_rhythm_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for key in (
        "AGENT_REPLY_SPLIT",
        "AGENT_REPLY_MAX_PARTS",
        "AGENT_REPLY_PACE",
        "AGENT_REPLY_MAX_SPREAD_SECONDS",
    ):
        monkeypatch.delenv(key, raising=False)


# ── 标记解析 ─────────────────────────────────────────────


def test_marker_splits_into_parts_and_strips_whitespace() -> None:
    markup = R.parse_rhythm_markup("我看了一下 ||| 其实挺简单的|||你试试")
    assert markup.parts == ("我看了一下", "其实挺简单的", "你试试")
    assert markup.text == "我看了一下\n其实挺简单的\n你试试"
    assert markup.marked


def test_fullwidth_and_bracket_markers_are_accepted() -> None:
    assert R.parse_rhythm_markup("啊｜｜｜不是吧").parts == ("啊", "不是吧")
    assert R.parse_rhythm_markup("好【分条】下次见").parts == ("好", "下次见")


def test_rhythm_tag_is_parsed_and_removed() -> None:
    markup = R.parse_rhythm_markup("【节奏:连发】啊|||啊|||不会吧")
    assert markup.rhythm == R.RHYTHM_BURST
    assert "节奏" not in markup.text
    assert R.parse_rhythm_markup("[rhythm=pop]你好呀").rhythm == R.RHYTHM_POP
    assert R.parse_rhythm_markup("（节奏：整段）认真说").rhythm == R.RHYTHM_BLOCK


def test_unknown_tag_value_falls_back_to_auto() -> None:
    assert R.parse_rhythm_markup("【节奏:随便】嗯").rhythm == R.RHYTHM_AUTO


def test_markers_inside_code_fence_are_not_split() -> None:
    text = "这样跑：\n```bash\ncat a ||| b\n```"
    markup = R.parse_rhythm_markup(text)
    assert markup.parts == ()
    assert "|||" in markup.text
    assert markup.rhythm == R.RHYTHM_BLOCK


def test_strip_markup_leaves_single_readable_message() -> None:
    assert R.strip_rhythm_markup("【节奏:连发】好|||好|||好") == "好\n好\n好"
    assert not R.has_rhythm_markup("普通一句话")
    assert R.has_rhythm_markup("a|||b")


def test_empty_parts_and_leading_trailing_markers_are_dropped() -> None:
    assert R.parse_rhythm_markup("|||嗯||| |||好|||").parts == ("嗯", "好")
    assert R.parse_rhythm_markup("只有一句|||").parts == ()


def test_speech_plan_from_text_keeps_parts_and_rhythm() -> None:
    plan = speech.speech_plan_from_text("【节奏:分条】第一句|||第二句")
    assert plan.parts == ("第一句", "第二句")
    assert plan.rhythm == R.RHYTHM_SPLIT
    assert plan.visible_text == "第一句\n第二句"
    assert plan.part_count == 2
    assert plan.trace_payload()["parts"] == 2


def test_plain_text_plan_is_unchanged() -> None:
    plan = speech.speech_plan_from_text("  原样保留  ")
    assert plan.text == "  原样保留  "
    assert plan.parts == ()


def test_segment_markers_become_break_segments() -> None:
    segments, declared = R.split_segment_markup(
        [
            {"type": "at", "user_id": 5},
            {"type": "text", "text": "【节奏:连发】你看|||快看"},
            {"type": "face", "id": 14},
        ]
    )
    assert declared == R.RHYTHM_BURST
    assert [item["type"] for item in segments] == ["at", "text", "break", "text", "face"]
    groups = R.segment_groups(segments)
    assert [[item["type"] for item in group] for group in groups] == [
        ["at", "text"],
        ["text", "face"],
    ]


def test_segments_without_markup_are_untouched() -> None:
    raw = [{"type": "at", "user_id": 5}, {"type": "text", "text": " 你好"}]
    segments, declared = R.split_segment_markup(raw)
    assert list(segments) == raw
    assert declared == R.RHYTHM_AUTO


def test_strip_break_segments_joins_with_newline() -> None:
    merged = R.strip_break_segments(
        [{"type": "text", "text": "a"}, {"type": "break"}, {"type": "text", "text": "b"}]
    )
    assert merged == [{"type": "text", "text": "a\nb"}]


def test_plan_visible_text_with_break_segments() -> None:
    plan = speech.speech_plan_from_segments([{"type": "text", "text": "一|||二"}])
    assert plan.visible_text == "一\n二"
    assert plan.part_count == 2
    assert "break" not in plan.trace_payload()["segment_types"]


# ── 节奏规划 ─────────────────────────────────────────────


def test_model_parts_become_split_rhythm() -> None:
    plan = R.plan_text_rhythm("", parts=("我刚看了你发的配置", "端口写错了，改成 8080 就行"))
    assert plan.rhythm == R.RHYTHM_SPLIT
    assert plan.source == "model"
    assert plan.delays[0] == 0.0
    assert plan.delays[1] > 0


def test_short_model_parts_are_inferred_as_burst() -> None:
    plan = R.plan_text_rhythm("", parts=("啊？", "不是吧", "真的假的"))
    assert plan.rhythm == R.RHYTHM_BURST


def test_single_char_parts_are_inferred_as_pop() -> None:
    plan = R.plan_text_rhythm("", parts=("哈", "哈", "笑", "死", "我", "了", "啦"))
    assert plan.rhythm == R.RHYTHM_POP


def test_block_tag_keeps_one_message() -> None:
    plan = R.plan_text_rhythm(
        "", parts=("这件事要认真说", "第一点是这样"), declared=R.RHYTHM_BLOCK
    )
    assert plan.parts == ("这件事要认真说\n第一点是这样",)
    assert not plan.multi


def test_pop_splits_short_sentence_into_characters() -> None:
    plan = R.plan_text_rhythm("我才不在乎呢！", declared=R.RHYTHM_POP)
    assert plan.rhythm == R.RHYTHM_POP
    assert plan.parts == ("我", "才", "不", "在", "乎", "呢！")


def test_pop_keeps_ascii_words_and_attaches_brackets() -> None:
    assert R.pop_pieces("好的OK") == ("好", "的", "OK")
    assert R.pop_pieces("「哼」") == ("「哼」",)
    assert R.pop_pieces("（哼）") == ("（哼）",)


def test_pop_never_exceeds_max_parts() -> None:
    assert len(R.pop_pieces("一二三四五六七八九十")) <= R.POP_MAX_PARTS


def test_long_pop_downgrades_to_burst() -> None:
    plan = R.plan_text_rhythm(
        "这句话太长了，一个字一个字蹦会刷屏，所以改成按短句连发。", declared=R.RHYTHM_POP
    )
    assert plan.rhythm == R.RHYTHM_BURST
    assert plan.source == "pop_downgraded"
    assert 2 <= len(plan.parts) <= R.BURST_MAX_PARTS


def test_burst_tag_without_markers_splits_clauses() -> None:
    plan = R.plan_text_rhythm("啊，不会吧，真的假的", declared=R.RHYTHM_BURST)
    assert plan.rhythm == R.RHYTHM_BURST
    assert plan.parts == ("啊，", "不会吧，", "真的假的")


def test_auto_splits_blank_line_paragraphs() -> None:
    plan = R.plan_text_rhythm("先说结论：能用。\n\n不过要先重启一下")
    assert plan.source == "auto_paragraph"
    assert plan.parts == ("先说结论：能用", "不过要先重启一下")


def test_auto_splits_short_lines() -> None:
    plan = R.plan_text_rhythm("我懂你意思了\n明天我再提醒你")
    assert plan.source == "auto_line"
    assert len(plan.parts) == 2


def test_auto_no_longer_chops_single_paragraph_into_sentences() -> None:
    """模型没选节奏时系统不替它按句号切：那只是把长话伪装成分条。"""

    text = "这个问题我之前也遇到过，原因是缓存没清。你先把浏览器缓存清掉再刷新一次。还不行的话把报错截图发我看看。"
    plan = R.plan_text_rhythm(text)
    assert not plan.multi
    assert plan.source == "single"


def test_declared_split_without_markers_still_splits_sentences() -> None:
    text = "这个问题我之前也遇到过，原因是缓存没清。你先把浏览器缓存清掉再刷新一次。还不行的话把报错截图发我看看。"
    plan = R.plan_text_rhythm(text, declared=R.RHYTHM_SPLIT)
    assert plan.source == "auto_sentence"
    assert 2 <= len(plan.parts) <= 3
    assert "".join(plan.parts).replace("。", "") == text.replace("。", "")


def test_declared_burst_with_long_parts_uses_normal_pace() -> None:
    plan = R.plan_text_rhythm(
        "", parts=("这个问题我之前也遇到过原因就是缓存没清", "你先清掉"), declared=R.RHYTHM_BURST
    )
    assert plan.rhythm == R.RHYTHM_SPLIT
    short = R.plan_text_rhythm("", parts=("不是吧", "真的假的", "我不信"), declared=R.RHYTHM_BURST)
    assert short.rhythm == R.RHYTHM_BURST


def test_tagged_model_output_round_trip() -> None:
    plan = speech.speech_plan_from_text("【节奏:连发】不是吧|||真的假的|||我不信")
    assert plan.rhythm == R.RHYTHM_BURST
    rhythm = R.plan_text_rhythm(plan.text, parts=plan.parts, declared=plan.rhythm)
    assert rhythm.parts == ("不是吧", "真的假的", "我不信")
    assert rhythm.source == "model"

    block = speech.speech_plan_from_text("【节奏:整段】\n这个得先看日志再说")
    assert block.text == "这个得先看日志再说"
    assert block.rhythm == R.RHYTHM_BLOCK
    assert not R.plan_text_rhythm(block.text, declared=block.rhythm).multi

    pop = speech.speech_plan_from_text("【节奏:蹦字】你在干嘛呀")
    popped = R.plan_text_rhythm(pop.text, declared=pop.rhythm)
    assert popped.rhythm == R.RHYTHM_POP
    assert "".join(popped.parts) == "你在干嘛呀"


def test_short_text_is_not_auto_split() -> None:
    plan = R.plan_text_rhythm("好的，没问题。马上来。")
    assert not plan.multi


def test_complex_answers_are_not_auto_split() -> None:
    text = "这个问题我之前也遇到过，原因是缓存没清。你先把浏览器缓存清掉再刷新一次。还不行的话把报错截图发我看看。"
    plan = R.plan_text_rhythm(text, complexity="complex")
    assert not plan.multi


def test_short_acts_are_not_auto_split() -> None:
    plan = R.plan_text_rhythm("在的在的\n怎么啦", act="ping_ack")
    assert not plan.multi


def test_lists_and_code_are_not_auto_split() -> None:
    assert not R.plan_text_rhythm("步骤：\n1. 打开设置\n2. 点保存").multi
    assert not R.plan_text_rhythm("· 第一\n· 第二").multi
    assert R.is_structured_text("```py\nprint(1)\n```")


def test_marked_mode_only_splits_model_markers(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AGENT_REPLY_SPLIT", "marked")
    assert not R.plan_text_rhythm("先说结论：能用。\n\n不过要先重启一下").multi
    assert R.plan_text_rhythm("", parts=("这是一条", "这是两条")).multi


def test_off_mode_never_splits(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AGENT_REPLY_SPLIT", "off")
    plan = R.plan_text_rhythm("", parts=("一条", "两条"))
    assert plan.parts == ("一条\n两条",)
    assert plan.source == "disabled"
    assert R.rhythm_instruction(scene="conversation", act="answer", complexity="normal") == ""


def test_overflow_parts_merge_into_last(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AGENT_REPLY_MAX_PARTS", "2")
    plan = R.plan_text_rhythm(
        "", parts=("第一段是比较长的一句话", "第二段也是比较长的话", "第三段还在继续说")
    )
    assert plan.parts == ("第一段是比较长的一句话", "第二段也是比较长的话\n第三段还在继续说")


def test_max_parts_env_is_clamped(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AGENT_REPLY_MAX_PARTS", "99")
    assert R.rhythm_settings().max_parts == R.MAX_SPLIT_PARTS
    monkeypatch.setenv("AGENT_REPLY_MAX_PARTS", "abc")
    assert R.rhythm_settings().max_parts == R.DEFAULT_MAX_PARTS
    monkeypatch.setenv("AGENT_REPLY_PACE", "nan")
    assert R.rhythm_settings().pace == 1.0


def test_punctuation_only_part_is_merged_and_trailing_period_dropped() -> None:
    plan = R.plan_text_rhythm("", parts=("我觉得可以。", "。", "就这样吧。"))
    assert plan.parts == ("我觉得可以", "就这样吧")
    question = R.plan_text_rhythm("", parts=("真的吗", "？？", "我不信"))
    assert question.parts[0] == "真的吗？？"


def test_ellipsis_is_kept_when_dropping_trailing_period() -> None:
    plan = R.plan_text_rhythm("", parts=("嗯……", "好吧..."))
    assert plan.parts == ("嗯……", "好吧...")


def test_repeated_parts_are_kept_for_burst_but_not_split() -> None:
    burst = R.plan_text_rhythm("", parts=("啊啊啊", "啊啊啊", "啊啊啊"), declared=R.RHYTHM_BURST)
    assert burst.parts == ("啊啊啊", "啊啊啊", "啊啊啊")
    split = R.plan_text_rhythm("", parts=("这个可以用", "这个可以用"))
    assert split.parts == ("这个可以用",)


def test_short_scene_caps_split_at_two() -> None:
    plan = R.plan_text_rhythm(
        "", parts=("这个梗好好笑", "我笑了半天", "真的绝了"), scene="active_interject"
    )
    assert len(plan.parts) == 2


# ── 停顿 ───────────────────────────────────────────────────


def test_delays_are_deterministic_for_same_text() -> None:
    parts = ("第一条消息", "第二条稍微长一点的消息")
    first = R.pacing_delays(parts, R.RHYTHM_SPLIT, seed="x")
    second = R.pacing_delays(parts, R.RHYTHM_SPLIT, seed="x")
    assert first == second
    assert first[0] == 0.0


def test_burst_is_faster_than_split_and_pop_fastest() -> None:
    parts = ("不会吧", "真的假的", "我不信")
    split = sum(R.pacing_delays(parts, R.RHYTHM_SPLIT, seed="s"))
    burst = sum(R.pacing_delays(parts, R.RHYTHM_BURST, seed="s"))
    pop = sum(R.pacing_delays(parts, R.RHYTHM_POP, seed="s"))
    assert split > burst > pop


def test_longer_next_part_waits_longer() -> None:
    short = R.pacing_delays(("a", "短"), R.RHYTHM_SPLIT, seed="k")[1]
    long = R.pacing_delays(("a", "这一条要打很多字才发得出来呢"), R.RHYTHM_SPLIT, seed="k")[1]
    assert long > short


def test_emotion_changes_pace() -> None:
    assert R.emotion_pace_factor({"label": "amused", "expression_intensity": 1.0}) < 1
    assert R.emotion_pace_factor({"label": "concerned", "intensity": 1.0}) > 1
    assert R.emotion_pace_factor({"label": "amused", "intensity": 0.0}) == 1.0
    assert R.emotion_pace_factor(None) == 1.0
    assert R.emotion_pace_factor({"label": "amused", "intensity": "bad"}) == 1.0


def test_persona_expressiveness_speeds_up() -> None:
    lively = SimpleNamespace(expressiveness=4, verbosity=1)
    calm = SimpleNamespace(expressiveness=0, verbosity=4)
    assert R.persona_pace_factor(lively) < 1 < R.persona_pace_factor(calm)
    assert R.persona_pace_factor(object()) == pytest.approx(1.0)


def test_pace_zero_disables_waiting(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AGENT_REPLY_PACE", "0")
    assert R.pacing_delays(("a", "b", "c"), R.RHYTHM_SPLIT) == (0.0, 0.0, 0.0)


def test_total_spread_is_capped(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AGENT_REPLY_MAX_SPREAD_SECONDS", "2")
    delays = R.pacing_delays(
        ("a", "很长很长的一条消息" * 4, "很长很长的一条消息" * 4), R.RHYTHM_SPLIT
    )
    assert sum(delays) <= 2.01


def test_split_delay_bounds() -> None:
    delays = R.pacing_delays(("a", "b" * 400), R.RHYTHM_SPLIT, seed="b")
    assert delays[1] <= 4.5 * 1.15 + 1e-6


def test_trace_payload_and_preview() -> None:
    plan = R.plan_text_rhythm("", parts=("第一条在这", "第二条在这"))
    payload = plan.trace_payload()
    assert payload["parts"] == 2
    assert payload["rhythm_label"]
    preview = plan.preview()
    assert preview[0]["delayMs"] == 0
    assert preview[1]["text"] == "第二条在这"


# ── 提示词 ─────────────────────────────────────────────────


def test_rhythm_instruction_mentions_marker_and_rhythms() -> None:
    text = R.rhythm_instruction(scene="conversation", act="answer", complexity="normal")
    for word in ("|||", "整段", "连发", "蹦字"):
        assert word in text


def test_rhythm_instruction_asks_for_a_choice_every_time_and_caps_length() -> None:
    text = R.rhythm_instruction(scene="conversation", act="continue", complexity="normal")
    # 每次都要先选节奏，并且给出可照抄的格式示例。
    assert "每次都要选" in text
    assert "【节奏:连发】不是吧|||" in text
    # 节奏不能变成多说话的理由。
    assert "加起来的字数也和发一条时一样" in text


def test_rhythm_instruction_follows_emotion_state_not_keywords() -> None:
    calm = R.rhythm_instruction(scene="conversation", act="continue", complexity="normal")
    amused = R.rhythm_instruction(
        scene="conversation",
        act="continue",
        complexity="normal",
        emotion={"label": "amused", "expression_intensity": 0.6},
    )
    guarded = R.rhythm_instruction(
        scene="conversation",
        act="continue",
        complexity="normal",
        emotion={"label": "guarded", "expression_intensity": 0.6},
    )
    faint = R.rhythm_instruction(
        scene="conversation",
        act="continue",
        complexity="normal",
        emotion={"label": "amused", "expression_intensity": 0.1},
    )
    assert "心情不错" in amused and "心情不错" not in calm
    assert "别连发也别蹦字" in guarded
    assert faint == calm


def test_speech_instruction_passes_emotion_to_rhythm() -> None:
    instruction = speech_policy.build_speech_instruction(
        None,
        {"trigger": "mention"},
        context={"emotion_state": {"label": "irritated", "expression_intensity": 0.7}},
    )
    assert "短句连发更像你" in instruction


def test_rhythm_instruction_adapts_to_scene_and_complexity() -> None:
    short = R.rhythm_instruction(scene="warmup", act="continue", complexity="normal")
    assert "一条就够" in short
    complex_ = R.rhythm_instruction(scene="direct_reply", act="answer", complexity="complex")
    assert "复杂" in complex_


def test_speech_instruction_includes_rhythm_rule() -> None:
    instruction = speech_policy.build_speech_instruction(None, {"trigger": "mention"})
    assert "|||" in instruction


def test_send_message_schema_has_rhythm() -> None:
    from src.plugins.yawn_core.yawn_agent.tool_registry import TOOL_BY_NAME

    definition = TOOL_BY_NAME["send_message"]
    assert "rhythm" in definition.properties
    assert "|||" in definition.description


# ── 分条清洗 ───────────────────────────────────────────────


def test_part_cleaning_is_position_aware() -> None:
    plan = speech.speech_plan_from_text(
        "好的！我看了一下|||好的，你这里写错了|||有其他问题可以继续问我"
    )
    final = speech_quality.finalize_speech_plan(plan)
    assert final.parts[0] == "我看了一下"
    # 第二条不是开场，不当客套开场去掉
    assert final.parts[1].startswith("好的")
    assert all("继续问我" not in part for part in final.parts)


def test_near_duplicate_part_is_removed() -> None:
    plan = speech.speech_plan_from_text("这个配置文件写错了|||这个配置文件写错了！")
    final = speech_quality.finalize_speech_plan(plan)
    assert final.parts == ()
    assert final.text == "这个配置文件写错了"
    assert "duplicate_part" in {item.code for item in final.issues}


def test_markdown_is_cleaned_per_part() -> None:
    plan = speech.speech_plan_from_text("**重点**是端口|||### 然后重启")
    final = speech_quality.finalize_speech_plan(plan)
    assert final.parts == ("重点是端口", "然后重启")


def test_unverified_claim_is_checked_on_parts() -> None:
    plan = speech.speech_plan_from_text("好|||已经帮你禁言了")
    plan = speech.SpeechPlan(**{**_plan_fields(plan), "verified_tools": ()})
    final = speech_quality.finalize_speech_plan(plan)
    assert "unverified_action_claim" in {item.code for item in final.issues}


def test_short_scene_parts_obey_budget() -> None:
    style = speech.SpeechStyle(soft_target_chars=12)
    plan = speech.speech_plan_from_text(
        "这条比较长的插话内容第一条|||第二条也很长很长的内容啊啊|||第三条更长更长更长更长的",
        scene="active_interject",
        style=style,
    )
    final = speech_quality.finalize_speech_plan(plan)
    assert len(final.visible_text) <= 48 or len(final.parts) < 3


def test_lint_only_keeps_parts() -> None:
    plan = speech.speech_plan_from_text("好的|||嗯")
    final = speech_quality.finalize_speech_plan(plan, autofix=False)
    assert final.parts == ("好的", "嗯")


def _plan_fields(plan: speech.SpeechPlan) -> dict[str, Any]:
    return {name: getattr(plan, name) for name in plan.__dataclass_fields__}


# ── 准备多条消息 ───────────────────────────────────────────


class _KnownMessageSession:
    async def scalar(self, _stmt: Any) -> Any:
        return SimpleNamespace(user_id=ALICE, sender_name="成员201", normalized_text="问题")


@pytest.mark.asyncio
async def test_prepare_parts_quotes_only_first() -> None:
    plan = speech.speech_plan_from_text(
        "我看了一下|||端口写错了",
        scene="direct_reply",
        target=speech.SpeechTarget(reply_to_message_id=42),
    )
    prepared = await outbound.prepare_speech_parts(
        plan, session=_KnownMessageSession(), group_id=GROUP_ID
    )
    assert len(prepared.messages) == 2
    assert prepared.messages[0].segment_records[0]["type"] == "reply"
    assert [item["type"] for item in prepared.messages[1].segment_records] == ["text"]
    assert prepared.normalized_text == "我看了一下\n端口写错了"
    assert prepared.delays[0] == 0.0


@pytest.mark.asyncio
async def test_prepare_parts_single_message_when_not_split() -> None:
    plan = speech.speech_plan_from_text("就一句")
    prepared = await outbound.prepare_speech_parts(plan, session=None, group_id=GROUP_ID)
    assert len(prepared.messages) == 1
    assert prepared.rhythm.rhythm == R.RHYTHM_BLOCK


@pytest.mark.asyncio
async def test_prepare_parts_segment_groups_keep_at_in_its_part() -> None:
    plan = speech.speech_plan_from_segments(
        [{"type": "text", "text": "等下|||你看这个"}, {"type": "face", "id": 14}]
    )
    prepared = await outbound.prepare_speech_parts(plan, session=None, group_id=GROUP_ID)
    assert [
        [item["type"] for item in message.segment_records] for message in prepared.messages
    ] == [["text"], ["text", "face"]]


@pytest.mark.asyncio
async def test_prepare_parts_falls_back_to_single_when_a_part_is_invalid() -> None:
    plan = speech.speech_plan_from_segments(
        [{"type": "text", "text": "第一条|||第二条"}, {"type": "contact", "contact_type": "qq", "id": 1}]
    )
    with pytest.raises((ValueError, PermissionError)):
        # 单条也不合法时仍然报错，不吞掉
        await outbound.prepare_speech_parts(
            plan, session=None, group_id=GROUP_ID, allowed_segment_types=frozenset({"text"})
        )


@pytest.mark.asyncio
async def test_prepare_parts_raises_when_not_speaking() -> None:
    plan = speech.speech_plan_from_text("")
    with pytest.raises(ValueError):
        await outbound.prepare_speech_parts(plan, session=None, group_id=GROUP_ID)


@pytest.mark.asyncio
async def test_compat_prepare_speech_plan_joins_parts() -> None:
    plan = speech.speech_plan_from_text("一|||二")
    prepared = await outbound.prepare_speech_plan(plan, session=None, group_id=GROUP_ID)
    assert prepared.normalized_text == "一\n二"


@pytest.mark.asyncio
async def test_compat_prepare_outbound_message_accepts_markers() -> None:
    prepared = await outbound.prepare_outbound_message(
        [{"type": "text", "text": "一|||二"}], session=None, group_id=GROUP_ID
    )
    assert prepared.normalized_text == "一\n二"


# ── 依次发送 ───────────────────────────────────────────────


def _prepared(text: str, files: tuple[Path, ...] = ()) -> outbound.PreparedOutboundMessage:
    base = outbound.prepare_text_message(text)
    return outbound.PreparedOutboundMessage(
        message=base.message,
        normalized_text=base.normalized_text,
        segment_records=base.segment_records,
        temporary_files=files,
    )


def _ok(text: str, message_id: int) -> outbound.SendResult:
    return outbound.SendResult(
        sent=True, message_id=message_id, normalized_text=text, segment_types=("text",)
    )


class _Clock:
    def __init__(self) -> None:
        self.now = 0.0
        self.sleeps: list[float] = []

    async def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds

    def __call__(self) -> float:
        return self.now


@pytest.mark.asyncio
async def test_deliver_sends_in_order_with_delays() -> None:
    clock = _Clock()
    sent: list[str] = []

    async def send_one(part: Any, index: int) -> outbound.SendResult:
        sent.append(part.normalized_text)
        return _ok(part.normalized_text, 10 + index)

    outcome = await speech_delivery.deliver_parts(
        [_prepared("一"), _prepared("二"), _prepared("三")],
        delays=(0.0, 1.0, 0.5),
        send_one=send_one,
        sleep=clock.sleep,
        clock=clock,
        max_spread_seconds=10,
    )
    assert sent == ["一", "二", "三"]
    assert clock.sleeps == [1.0, 0.5]
    assert [item.message_id for item in outcome.results] == [10, 11, 12]
    assert outcome.ends_turn
    assert outcome.visible_text == "一\n二\n三"
    combined = outcome.combined()
    assert combined.message_id == 10
    assert combined.normalized_text == "一\n二\n三"


@pytest.mark.asyncio
async def test_deliver_flushes_rest_when_someone_is_waiting() -> None:
    clock = _Clock()
    sent: list[str] = []
    calls = {"n": 0}

    def hurry() -> bool:
        calls["n"] += 1
        return calls["n"] >= 2  # 第一次停顿后有人排队

    async def send_one(part: Any, index: int) -> outbound.SendResult:
        sent.append(part.normalized_text)
        return _ok(part.normalized_text, 10 + index)

    outcome = await speech_delivery.deliver_parts(
        [_prepared("一"), _prepared("二"), _prepared("三")],
        delays=(0.0, 1.0, 1.0),
        send_one=send_one,
        hurry=hurry,
        sleep=clock.sleep,
        clock=clock,
        max_spread_seconds=10,
    )
    assert sent == ["一", "二\n三"]
    assert outcome.hurried == speech_delivery.HURRY_QUEUE
    assert outcome.flushed == 2


@pytest.mark.asyncio
async def test_deliver_flushes_when_spread_limit_reached() -> None:
    clock = _Clock()
    sent: list[str] = []

    async def send_one(part: Any, index: int) -> outbound.SendResult:
        sent.append(part.normalized_text)
        return _ok(part.normalized_text, index)

    outcome = await speech_delivery.deliver_parts(
        [_prepared("一"), _prepared("二"), _prepared("三")],
        delays=(0.0, 2.0, 2.0),
        send_one=send_one,
        sleep=clock.sleep,
        clock=clock,
        max_spread_seconds=3,
    )
    assert sent == ["一", "二", "三"]
    assert outcome.hurried == speech_delivery.HURRY_SPREAD
    assert sum(clock.sleeps) <= 3


@pytest.mark.asyncio
async def test_deliver_stops_after_failure_and_cleans_files(tmp_path: Path) -> None:
    leftover = tmp_path / "x.png"
    leftover.write_bytes(b"x")

    async def send_one(part: Any, index: int) -> outbound.SendResult:
        if index == 1:
            return outbound.SendResult(
                sent=False,
                message_id=None,
                normalized_text="",
                segment_types=(),
                outcome="send_failed",
                delivery_state=outbound.DELIVERY_CONFIRMED_FAILURE,
            )
        return _ok(part.normalized_text, index)

    clock = _Clock()
    outcome = await speech_delivery.deliver_parts(
        [_prepared("一"), _prepared("二"), _prepared("三", (leftover,))],
        delays=(0.0, 0.1, 0.1),
        send_one=send_one,
        sleep=clock.sleep,
        clock=clock,
    )
    assert len(outcome.results) == 2
    assert outcome.stopped == speech_delivery.STOP_SEND_FAILED
    assert outcome.ends_turn  # 第一条已经发出去了
    assert not leftover.exists()
    assert outcome.combined().outcome == "partial"


@pytest.mark.asyncio
async def test_deliver_stops_after_unknown_delivery() -> None:
    async def send_one(part: Any, index: int) -> outbound.SendResult:
        return outbound.SendResult(
            sent=False,
            message_id=None,
            normalized_text=part.normalized_text,
            segment_types=("text",),
            outcome="delivery_unknown",
            delivery_state=outbound.DELIVERY_UNKNOWN,
        )

    clock = _Clock()
    outcome = await speech_delivery.deliver_parts(
        [_prepared("一"), _prepared("二")],
        delays=(0.0, 0.1),
        send_one=send_one,
        sleep=clock.sleep,
        clock=clock,
    )
    assert len(outcome.results) == 1
    assert outcome.stopped == speech_delivery.STOP_DELIVERY_UNKNOWN
    assert outcome.ends_turn
    assert outcome.delivered == ()


@pytest.mark.asyncio
async def test_deliver_reraises_first_part_exception() -> None:
    async def send_one(part: Any, index: int) -> outbound.SendResult:
        raise RuntimeError("boom")

    with pytest.raises(RuntimeError):
        await speech_delivery.deliver_parts(
            [_prepared("一"), _prepared("二")], send_one=send_one
        )


@pytest.mark.asyncio
async def test_deliver_swallows_later_part_exception() -> None:
    async def send_one(part: Any, index: int) -> outbound.SendResult:
        if index:
            raise RuntimeError("boom")
        return _ok(part.normalized_text, 1)

    clock = _Clock()
    outcome = await speech_delivery.deliver_parts(
        [_prepared("一"), _prepared("二")],
        delays=(0.0, 0.0),
        send_one=send_one,
        sleep=clock.sleep,
        clock=clock,
    )
    assert outcome.stopped == speech_delivery.STOP_EXCEPTION
    assert len(outcome.results) == 1


@pytest.mark.asyncio
async def test_hurry_check_errors_are_ignored() -> None:
    def hurry() -> bool:
        raise RuntimeError("bad")

    async def send_one(part: Any, index: int) -> outbound.SendResult:
        return _ok(part.normalized_text, index)

    clock = _Clock()
    outcome = await speech_delivery.deliver_parts(
        [_prepared("一"), _prepared("二")],
        delays=(0.0, 0.2),
        send_one=send_one,
        hurry=hurry,
        sleep=clock.sleep,
        clock=clock,
    )
    assert len(outcome.results) == 2
    assert outcome.hurried is None


def test_merge_prepared_keeps_only_first_reply() -> None:
    first = outbound.PreparedOutboundMessage(
        message=outbound.Message(
            [outbound.MessageSegment.reply(5), outbound.MessageSegment.text("一")]
        ),
        normalized_text="一",
        segment_records=(
            {"type": "reply", "data": {"id": "5"}, "text": "", "children": [], "depth": 0},
            {"type": "text", "data": {"text": "一"}, "text": "一", "children": [], "depth": 0},
        ),
    )
    merged = speech_delivery.merge_prepared([first, _prepared("二")])
    assert [segment.type for segment in merged.message] == ["reply", "text", "text", "text"]
    assert merged.normalized_text == "一\n二"


@pytest.mark.asyncio
async def test_persist_delivered_writes_each_sent_part() -> None:
    rows: list[tuple[Any, str]] = []

    async def persist(_session: Any, _bot: int, _group: int, message_id: Any, text: str, *_a: Any, **_k: Any) -> None:
        rows.append((message_id, text))

    outcome = speech_delivery.DeliveryOutcome(
        results=(_ok("一", 1), _ok("二", 2)), planned=2
    )
    count = await speech_delivery.persist_delivered(outcome, persist, None, BOT_ID, GROUP_ID, 7)
    assert count == 2
    assert rows == [(1, "一"), (2, "二")]


# ── 进度提示 ───────────────────────────────────────────────


def test_progress_preamble_strips_markup() -> None:
    assert progress_notice.clean_preamble("我看看|||马上") == "我看看 马上"


# ── 端到端：对话路径逐条发送并逐条落库 ─────────────────────


def _text(text: str) -> NormalizedMessage:
    message = NormalizedMessage(
        plain_text=text,
        segments=[SegmentNode(type="text", data={"text": text}, text=text)],
    )
    message.trigger_source = "mention"
    message.trigger_signals = {"mention": True}
    return message


def _event(user_id: int, message_id: int) -> Any:
    return SimpleNamespace(
        group_id=GROUP_ID,
        message_id=message_id,
        get_user_id=lambda: str(user_id),
        sender=SimpleNamespace(card=f"成员{user_id}", nickname=None, role="member", title=None),
    )


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
            config_models.GroupAgentConfig(group_id=GROUP_ID, short_conversation_enabled=False)
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


@pytest.mark.asyncio
async def test_dialogue_sends_marked_parts_and_persists_each(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine, maker = await _dialogue_db(
        (ALICE, 1, "端口怎么配"), (BOB, 2, "我也想知道")
    )
    sent: list[Any] = []
    ids = iter(range(900, 999))

    async def fake_complete(*_a: Any, **_k: Any) -> Any:
        return _completion("我看了一下|||端口写成 8080 就行")

    async def fake_send(_bot: Any, _group_id: int, prepared: Any, **_kwargs: Any) -> Any:
        sent.append(prepared)
        return outbound.SendResult(
            sent=True,
            message_id=next(ids),
            normalized_text=prepared.normalized_text,
            segment_types=tuple(item["type"] for item in prepared.segment_records),
            segments=prepared.segment_records,
        )

    monkeypatch.setattr(dialogue, "get_session", _SessionFactory(maker))
    monkeypatch.setattr(dialogue, "complete_with_tools_result", fake_complete)
    monkeypatch.setattr(dialogue, "send_prepared_outbound", fake_send)
    monkeypatch.setenv("AGENT_AI_WAIT_NOTICE_DELAY", "0")
    monkeypatch.setenv("AGENT_REPLY_PACE", "0")
    bot = SimpleNamespace(self_id=str(BOT_ID))
    try:
        await dialogue.process_group_message(
            bot, _event(ALICE, 1), _text("端口怎么配"), enqueued_at=None
        )
        async with maker() as session:
            rows = (
                await session.execute(
                    select(message_models.GroupAgentMessage)
                    .where(message_models.GroupAgentMessage.role == "bot")
                    .order_by(message_models.GroupAgentMessage.message_id)
                )
            ).scalars().all()
            texts = [(row.message_id, row.normalized_text) for row in rows]
            config = await session.scalar(select(config_models.GroupAgentConfig))
            recent = list(config.recent_response_fingerprints or [])
    finally:
        await engine.dispose()
    assert [item.normalized_text for item in sent] == ["我看了一下", "端口写成 8080 就行"]
    # 繁忙群自动引用只在第一条
    assert sent[0].segment_records[0]["type"] == "reply"
    assert sent[1].segment_records[0]["type"] == "text"
    assert texts == [(900, "我看了一下"), (901, "端口写成 8080 就行")]
    assert recent[-1]["text"] == "我看了一下\n端口写成 8080 就行"


# ── send_message 工具 ─────────────────────────────────────


@pytest.mark.asyncio
async def test_send_message_tool_splits_on_markers(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AGENT_REPLY_PACE", "0")
    sent: list[str] = []
    ids = iter(range(50, 60))

    async def fake_send(_bot: Any, _group_id: int, prepared: Any, **_k: Any) -> Any:
        sent.append(prepared.normalized_text)
        return outbound.SendResult(
            sent=True,
            message_id=next(ids),
            normalized_text=prepared.normalized_text,
            segment_types=("text",),
        )

    from src.plugins.yawn_core.yawn_agent import capabilities

    monkeypatch.setattr(outbound, "send_prepared_outbound", fake_send)
    monkeypatch.setattr(
        capabilities,
        "get_segment_capabilities",
        lambda _bot, _group: SimpleNamespace(allowed=frozenset({"text"})),
    )
    context = SimpleNamespace(
        bot=SimpleNamespace(self_id=str(BOT_ID)),
        group_id=GROUP_ID,
        actor_user_id=ALICE,
        session=None,
    )
    handled = await message_handler.handle(
        "send_message",
        {"segments": [{"type": "text", "text": "好耶|||终于修好了"}], "rhythm": "split"},
        context,
    )
    result = handled.result
    assert sent == ["好耶", "终于修好了"]
    assert result["parts"] == 2
    assert [item["message_id"] for item in result["outbound_parts"]] == [50, 51]
    assert handled.ends_turn


@pytest.mark.asyncio
async def test_send_message_tool_without_markers_uses_single_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[Any] = []

    async def fake_single(_bot: Any, _group: int, segments: Any, **_k: Any) -> Any:
        calls.append(segments)
        return outbound.SendResult(
            sent=True, message_id=7, normalized_text="一句", segment_types=("text",)
        )

    monkeypatch.setattr(outbound, "send_outbound_message", fake_single)
    context = SimpleNamespace(
        bot=SimpleNamespace(self_id=str(BOT_ID)), group_id=GROUP_ID, actor_user_id=ALICE, session=None
    )
    handled = await message_handler.handle(
        "send_message", {"segments": [{"type": "text", "text": "一句"}]}, context
    )
    assert len(calls) == 1
    assert "outbound_parts" not in handled.result


# ── 主动发言 / 续聊 ─────────────────────────────────────────


@pytest.mark.asyncio
async def test_proactive_split_prepare_returns_parts() -> None:
    decision = proactive._decide_proactive_reply(
        '{"action":"speak","speak":true,"topic":"t","reason":"r","text":"哈哈哈|||这个太真实了"}'
    )
    prepared = await proactive._prepare_proactive_message(
        decision, session=None, group_id=GROUP_ID, split=True
    )
    assert isinstance(prepared, outbound.PreparedSpeechParts)
    assert [item.normalized_text for item in prepared.messages] == ["哈哈哈", "这个太真实了"]
    single = await proactive._prepare_proactive_message(decision, session=None, group_id=GROUP_ID)
    assert isinstance(single, outbound.PreparedOutboundMessage)


@pytest.mark.asyncio
async def test_proactive_history_text_has_no_markers() -> None:
    decision = proactive._decide_proactive_reply(
        '{"action":"speak","speak":true,"topic":"t","reason":"r","text":"哈哈哈|||这个太真实了"}'
    )
    plan = speech.speech_plan_from_text(decision.text)
    assert "|||" not in plan.visible_text


@pytest.mark.asyncio
async def test_deliver_proactive_wraps_single_prepared(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_send(_bot: Any, _group: int, prepared: Any, **_k: Any) -> Any:
        return _ok(prepared.normalized_text, 3)

    monkeypatch.setattr(proactive, "send_prepared_outbound", fake_send)
    outcome = await proactive._deliver_proactive(
        SimpleNamespace(self_id=str(BOT_ID)), GROUP_ID, _prepared("一句"), session=None, source="proactive"
    )
    assert outcome.combined().message_id == 3


@pytest.mark.asyncio
async def test_apply_result_persists_each_part() -> None:
    rows: list[Any] = []

    class _Session:
        def add(self, row: Any) -> None:
            rows.append(row)

    async def persist(_session: Any, _bot: int, _group: int, message_id: Any, text: str, *_a: Any, **_k: Any) -> None:
        rows.append((message_id, text))

    config = SimpleNamespace(
        group_id=GROUP_ID,
        recent_response_fingerprints=[],
        raw_retention_days=7,
        last_proactive_at=None,
        last_agent_at=None,
        proactive_day=None,
        proactive_count=0,
        cooldown_minutes=30,
    )
    delivery = speech_delivery.DeliveryOutcome(results=(_ok("一", 1), _ok("二", 2)), planned=2)
    original = proactive.persist_bot_reply
    proactive.persist_bot_reply = persist  # type: ignore[assignment]
    try:
        await proactive._apply_result(
            _Session(),
            config,  # type: ignore[arg-type]
            now=dialogue.now_beijing(),
            send_result=delivery.combined(),
            history_text="一\n二",
            day_count=0,
            bot_id=BOT_ID,
            scene="active",
            reason="r",
            session_turn=1,
            delivery=delivery,
        )
    finally:
        proactive.persist_bot_reply = original  # type: ignore[assignment]
    assert (1, "一") in rows and (2, "二") in rows
    audits = [row for row in rows if hasattr(row, "tool_name")]
    assert len(audits) == 1
    assert audits[0].arguments["parts"] == 2


# ── WebUI 调试预览 ─────────────────────────────────────────


def test_debug_payload_includes_delivery_preview() -> None:
    webui_agent = importlib.import_module("src.plugins.yawn_core.webui.agent")
    result = SimpleNamespace(
        message=SimpleNamespace(content="我看了一下|||端口写错了", tool_calls=[]),
        outcome="success",
        finish_reason="stop",
        prompt_tokens=1,
        completion_tokens=1,
        cached_tokens=0,
        cache_miss_tokens=1,
        duration_ms=1.0,
    )
    payload = webui_agent._debug_model_payload(result, "dialogue")
    delivery = payload["delivery"]
    assert [item["text"] for item in delivery["parts"]] == ["我看了一下", "端口写错了"]
    assert delivery["parts"][0]["delayMs"] == 0
    assert delivery["rhythmLabel"]


def test_debug_payload_skips_preview_for_tool_calls() -> None:
    webui_agent = importlib.import_module("src.plugins.yawn_core.webui.agent")
    call = SimpleNamespace(function=SimpleNamespace(name="discover_tools", arguments="{}"))
    result = SimpleNamespace(
        message=SimpleNamespace(content="稍等", tool_calls=[call]),
        outcome="success",
        finish_reason="tool_calls",
        prompt_tokens=1,
        completion_tokens=1,
        cached_tokens=0,
        cache_miss_tokens=1,
        duration_ms=1.0,
    )
    assert "delivery" not in webui_agent._debug_model_payload(result, "dialogue")


def test_rhythm_env_keys_are_in_catalog() -> None:
    catalog = (
        PROJECT_ROOT / "src/plugins/yawn_core/webui/environment_catalog.dotenv"
    ).read_text(encoding="utf-8")
    for key in (
        "AGENT_REPLY_SPLIT",
        "AGENT_REPLY_MAX_PARTS",
        "AGENT_REPLY_PACE",
        "AGENT_REPLY_MAX_SPREAD_SECONDS",
    ):
        assert f"\n{key}=" in catalog


# ── 2026-11 分条失灵修复：短回复不拆、长回复按段落分 ─────────────────────────


@pytest.mark.parametrize(
    ("parts", "declared"),
    [
        (("好", "的"), R.RHYTHM_SPLIT),
        (("嗯", "嗯", "嗯"), R.RHYTHM_BURST),
        (("在",), R.RHYTHM_POP),
        (("哈哈", "行吧"), R.RHYTHM_AUTO),
    ],
)
def test_tiny_replies_are_never_split(parts: tuple[str, ...], declared: str) -> None:
    plan = R.plan_text_rhythm("", parts=parts, declared=declared)
    assert not plan.multi
    assert plan.source == "short"


def test_tiny_split_without_markers_stays_single() -> None:
    plan = R.plan_text_rhythm("好的。收到！", declared=R.RHYTHM_SPLIT)
    assert not plan.multi


def test_pop_needs_a_few_characters() -> None:
    assert not R.plan_text_rhythm("在吗", declared=R.RHYTHM_POP).multi
    assert R.plan_text_rhythm("你在干嘛呀", declared=R.RHYTHM_POP).multi


def test_fragment_count_shrinks_with_total_length() -> None:
    plan = R.plan_text_rhythm("", parts=("好", "啊", "那", "就", "这样吧"), declared=R.RHYTHM_SPLIT)
    # 7 个字最多 2 条；单字碎片并进相邻一条
    assert len(plan.parts) <= 2


LONG_REPLY = (
    "因为之前那些活都不值得派，签到画爱心看图查记录这种两三步就办完了。\n\n"
    "签到、画爱心、看图、查记录，都是两三步就能办完的，派出去反而绕远路，还得等它回来。\n\n"
    "小助手适合耗时的、多步骤的，比如联网查一堆资料整理、处理你发来的大文件、写长东西。\n\n"
    "你要是有什么这种活，我随时可以派给它去做，做完了它会一步一步给你报进度的。"
)


def test_long_block_reply_is_split_by_paragraph() -> None:
    plan = R.plan_text_rhythm(LONG_REPLY, declared=R.RHYTHM_BLOCK)
    assert plan.multi
    assert plan.source == "long_paragraph"
    assert len(plan.parts) <= R.rhythm_settings().max_parts
    assert "".join(plan.parts).replace("\n", "") == LONG_REPLY.replace("\n", "")


def test_long_auto_reply_with_more_paragraphs_than_cap_is_grouped() -> None:
    plan = R.plan_text_rhythm(LONG_REPLY)
    assert plan.multi
    assert 2 <= len(plan.parts) <= R.rhythm_settings().max_parts


def test_long_reply_lines_are_grouped() -> None:
    text = "\n".join(
        [
            "说句实话我只能读到文件信息，看不到图里画的是什么",
            "这张是七百乘七百的粉色系图，主色是浅粉白，其他就读不出来了",
            "你要不跟我说说画的是啥，或者你想要我拿它做点什么",
            "真要看内容你可以直接跟我说画的是啥，或者我用代码跑一下看颜色尺寸这类",
            "但是说实话意义不大，还是你直接告诉我比较快，我们省点时间",
        ]
    )
    plan = R.plan_text_rhythm(text, declared=R.RHYTHM_BLOCK)
    assert plan.multi


def test_short_block_reply_stays_one_message() -> None:
    plan = R.plan_text_rhythm("先说结论：能用。\n\n不过要先重启一下", declared=R.RHYTHM_BLOCK)
    assert not plan.multi


def test_long_structured_block_reply_is_not_split() -> None:
    text = "步骤如下：\n" + "\n".join(f"{i}. 这是第{i}步要做的事情，写得长一点免得太短" for i in range(1, 9))
    plan = R.plan_text_rhythm(text, declared=R.RHYTHM_BLOCK)
    assert not plan.multi


def test_instruction_says_tiny_replies_stay_whole() -> None:
    text = R.rhythm_instruction(scene="private", act="continue", complexity="normal")
    assert "不要拆" in text
    assert "别回回同一种" not in text
