# ruff: noqa: ANN001, ARG001, ARG002, E501, F811, PLR2004, RUF001
"""私聊分条：节奏说明不再偏向三段式、条数分布与“别套模板”提醒、分条发到一半被打断。"""

from __future__ import annotations

import importlib
import re
from typing import Any

import pytest
from test_private_agent import (
    USER,
    _Bot,
    _message,
    _request,
    _script,
    bridge,
    maker,  # noqa: F401  (fixture)
    pagent,
)
from test_private_agent_multi import (
    _isolated,  # noqa: F401  (autouse fixture)
    _no_sleep,
    computer,
    delivery,
    fake_client,  # noqa: F401  (fixture)
)

_PKG = "src.plugins.yawn_core"
rhythm = importlib.import_module(f"{_PKG}.yawn_agent.private_rhythm")
speech_rhythm = importlib.import_module(f"{_PKG}.yawn_agent.speech_rhythm")
prompt = importlib.import_module(f"{_PKG}.yawn_agent.private_prompt")
sections = importlib.import_module(f"{_PKG}.yawn_agent.prompt_sections")
registry = importlib.import_module(f"{_PKG}.yawn_agent.private_prompt_registry")
ptasks = importlib.import_module(f"{_PKG}.yawn_agent.private_tasks")
store = importlib.import_module(f"{_PKG}.yawn_agent.private_store")
chat_state = importlib.import_module(f"{_PKG}.chat_state")
routes = importlib.import_module(f"{_PKG}.webui.private_agent_routes")
orm = importlib.import_module("nonebot_plugin_orm")
CFG = bridge.private_agent_config

_EXAMPLE_RE = re.compile(r"【节奏:[^】]+】([^；。]*)")


@pytest.fixture(autouse=True)
def _clean_rhythm():
    rhythm.reset()
    chat_state._arrivals.pop(USER, None)
    yield
    rhythm.reset()
    chat_state._arrivals.pop(USER, None)


class _Counter:
    """假的到达计数：调用 ``bump()`` 就像他又发了一条消息。"""

    def __init__(self) -> None:
        self.value = 0

    def __call__(self, _user_id: int) -> int:
        return self.value

    def bump(self) -> None:
        self.value += 1


# ── 节奏说明：不再偏向三段式 ─────────────────────────────────────────


def test_instruction_makes_single_message_the_default() -> None:
    text = rhythm.private_rhythm_instruction()
    assert "大多数回复一条就够" in text
    assert "不是固定格式" in text
    assert "别每次都分成同样的几条" in text
    # 旧说明里“2~3 条”的区间说法会让模型把条数填满，不能再出现。
    assert not re.search(r"2\s*[~～-]\s*\d\s*条", text)
    assert "平常闲聊，把同一句话" not in text


def test_instruction_examples_cover_many_counts_and_never_exactly_three() -> None:
    text = rhythm.private_rhythm_instruction()
    counts = [example.count("|||") + 1 for example in _EXAMPLE_RE.findall(text)]
    assert 1 in counts and 2 in counts and max(counts) >= 4
    assert 3 not in counts
    # 其它支持的格式也给了用法：蹦字、长内容分段、表情包单独一条。
    for word in ("【节奏:蹦字】", "空行分好段", "[表情:名字]", "只发一个表情包"):
        assert word in text


def test_instruction_keeps_old_guarantees() -> None:
    text = rhythm.private_rhythm_instruction()
    assert "每次都要选" in text
    assert "略略略" in text and "不会替你硬拆" in text
    assert "加起来的字数也和发一条时一样" in text
    assert "最要紧的话放第一条" in text


def test_instruction_is_byte_stable_and_only_follows_persona() -> None:
    base = rhythm.private_rhythm_instruction()
    assert base == rhythm.private_rhythm_instruction()
    quiet = rhythm.private_rhythm_instruction(verbosity=0)
    loud = rhythm.private_rhythm_instruction(expressiveness=3)
    shy = rhythm.private_rhythm_instruction(expressiveness=0)
    assert len({base, quiet, loud, shy}) == 4
    assert "基本都是一条" in quiet and "平常照样一条为主" in loud


def test_instruction_states_private_cap_and_off_switch(monkeypatch) -> None:
    monkeypatch.setattr(CFG, "private_agent_reply_max_parts", 2)
    assert "分条最多 2 条" in rhythm.private_rhythm_instruction()
    monkeypatch.setenv("AGENT_REPLY_SPLIT", "off")
    assert rhythm.private_rhythm_instruction() == ""


def test_rhythm_style_uses_private_instruction() -> None:
    persona = pagent.resolve_private_persona(USER)
    style = pagent.rhythm_style(persona)
    assert "大多数回复一条就够" in style and "平常闲聊，把同一句话" not in style


# ── 私聊自己的条数上限 ───────────────────────────────────────────────


def test_private_cap_defaults_to_four_and_is_clamped(monkeypatch) -> None:
    assert rhythm.private_max_parts() == 4
    monkeypatch.setattr(CFG, "private_agent_reply_max_parts", 9)
    assert rhythm.private_max_parts() == speech_rhythm.MAX_SPLIT_PARTS
    monkeypatch.setattr(CFG, "private_agent_reply_max_parts", 0)
    assert rhythm.private_max_parts() == 1


def test_four_split_parts_are_not_squashed_into_three(monkeypatch) -> None:
    monkeypatch.setenv("AGENT_REPLY_MAX_PARTS", "3")
    plan, items = delivery.plan_private_reply("【节奏:分条】刚到家|||饿死了|||先点个外卖|||你吃了没")
    assert len(plan.parts) == 4 and len(items) == 4
    # 群聊仍按 AGENT_REPLY_MAX_PARTS
    group = speech_rhythm.plan_text_rhythm(
        "", parts=("a1", "b2", "c3", "d4"), declared="split", settings=speech_rhythm.rhythm_settings()
    )
    assert len(group.parts) == 3


def test_single_and_sticker_only_replies_stay_single() -> None:
    plan, items = delivery.plan_private_reply("【节奏:整段】好耶 那我等你消息")
    assert len(items) == 1 and plan.rhythm == "block"
    _plan, sticker = delivery.plan_private_reply("[表情:捂脸笑]")
    assert [item.kind for item in sticker] == ["sticker"]


# ── 条数分布与“别套模板” ─────────────────────────────────────────────


def test_mix_streak_detects_templates_only() -> None:
    assert rhythm.mix_streak([3, 3, 3]) == "same"
    assert rhythm.mix_streak([2, 3, 2, 4]) == "always_split"
    assert rhythm.mix_streak([1, 1, 1]) == ""
    assert rhythm.mix_streak([3, 1, 3]) == ""
    assert rhythm.mix_streak([2, 3]) == ""


def test_mix_note_from_recorded_deliveries() -> None:
    for _ in range(3):
        rhythm.note_delivery(USER, parts=3, rhythm="split")
    note = rhythm.mix_note(USER)
    assert note == {"last_reply_parts": [3, 3, 3], "pattern": "same"}
    rhythm.note_delivery(USER, parts=1, rhythm="block")
    assert rhythm.mix_note(USER) is None


def test_mix_note_seeds_from_history_after_restart() -> None:
    history = [
        {"role": "assistant", "content": "a\nb\nc"},
        {"role": "user", "content": "嗯"},
        {"role": "assistant", "content": "d\ne\nf"},
        {"role": "assistant", "content": "g\nh\ni"},
        {"role": "user", "content": "然后呢"},
    ]
    assert rhythm.recent_counts(USER, history) == [3, 3, 3]
    assert rhythm.mix_note(USER, history)["pattern"] == "same"


def test_mix_note_can_be_turned_off(monkeypatch) -> None:
    for _ in range(4):
        rhythm.note_delivery(USER, parts=2, rhythm="split")
    monkeypatch.setattr(CFG, "private_agent_rhythm_mix_hint", False)
    assert rhythm.mix_note(USER) is None


def test_recent_shapes_are_bounded(monkeypatch) -> None:
    monkeypatch.setattr(rhythm, "MAX_TRACKED_USERS", 3)
    for uid in range(1, 6):
        rhythm.note_delivery(uid, parts=1, rhythm="block", now=float(uid))
    assert len(rhythm._recent) == 3
    for _ in range(20):
        rhythm.note_delivery(USER, parts=2, rhythm="split")
    assert len(rhythm._recent[USER]) == rhythm.RECENT_SHAPES


def test_stats_snapshot_counts_parts_and_rhythms() -> None:
    rhythm.note_delivery(USER, parts=1, rhythm="block")
    rhythm.note_delivery(USER, parts=2, rhythm="split", media=1)
    rhythm.note_delivery(USER, parts=6, rhythm="pop", interrupted=True, skipped=2)
    snap = rhythm.snapshot(USER)
    stats = snap["stats"]
    assert stats["deliveries"] == 3 and stats["interrupted"] == 1 and stats["skippedItems"] == 2
    assert stats["byParts"] == {"1": 1, "2": 1, "3": 0, "4": 0, "5+": 1}
    assert stats["byRhythm"] == {"分条": 1, "整段": 1, "蹦字": 1}
    assert stats["withMedia"] == 1
    assert [row["parts"] for row in snap["recent"]] == [1, 2, 6]
    assert snap["settings"]["interrupt"] is True and snap["settings"]["maxParts"] == 4


# ── 打断：检测 ─────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_interrupt_watch_fires_on_new_arrival_and_stays_fired() -> None:
    counter = _Counter()
    watch = rhythm.InterruptWatch.start(USER, counter=counter)
    assert watch.check() is False
    counter.bump()
    assert watch.check() is True
    counter.value = 0  # 计数回不去也一直算打断过
    assert watch.check() is True


@pytest.mark.asyncio
async def test_pause_wakes_early_when_he_talks() -> None:
    counter = _Counter()
    watch = rhythm.InterruptWatch.start(USER, counter=counter)
    slept: list[float] = []

    async def sleep(seconds):
        slept.append(seconds)
        if len(slept) == 2:
            counter.bump()

    assert await watch.pause(3.0, sleep) is True
    assert len(slept) == 2 and all(step <= rhythm.POLL_SECONDS for step in slept)


@pytest.mark.asyncio
async def test_pause_sleeps_once_when_disabled() -> None:
    counter = _Counter()
    watch = rhythm.InterruptWatch.start(USER, enabled=False, counter=counter)
    slept: list[float] = []

    async def sleep(seconds):
        slept.append(seconds)
        counter.bump()

    assert await watch.pause(2.0, sleep) is False
    assert slept == [2.0] and watch.check() is False


def test_watch_reads_real_queue_arrivals(monkeypatch) -> None:
    monkeypatch.setattr(CFG, "private_agent_interrupt", True)
    watch = rhythm.InterruptWatch.start(USER)
    assert watch.check() is False
    chat_state._arrivals[USER] = chat_state._arrivals.get(USER, 0) + 1
    assert watch.check() is True


def test_watch_respects_switch(monkeypatch) -> None:
    monkeypatch.setattr(CFG, "private_agent_interrupt", False)
    watch = rhythm.InterruptWatch.start(USER)
    chat_state._arrivals[USER] = chat_state._arrivals.get(USER, 0) + 5
    assert watch.check() is False


# ── 打断：发送 ─────────────────────────────────────────────────────


def _interrupting_send(counter: _Counter, after: int, sent: list[str]):
    async def send(message):
        sent.append(str(message))
        if len(sent) == after:
            counter.bump()

    return send


@pytest.mark.asyncio
async def test_deliver_stops_after_he_talks_and_hides_unsent() -> None:
    counter = _Counter()
    sent: list[str] = []
    result = await delivery.deliver(
        USER,
        "【节奏:分条】我跟你说|||今天超离谱|||老板居然|||请我们喝奶茶",
        _interrupting_send(counter, 1, sent),
        sleep=_no_sleep,
        interrupt=rhythm.InterruptWatch.start(USER, counter=counter),
    )
    assert sent == ["我跟你说"]
    assert result.delivered == ["我跟你说"] and result.text == "我跟你说"
    assert result.interrupted and result.skipped == 3 and result.planned == 4
    # 只记条数，不记没发出去的内容。
    cut = rhythm.cut_note(USER)
    assert cut == {"sent_parts": 1, "unsent_parts": 3, "seconds_ago": 0}
    assert "奶茶" not in str(cut)


@pytest.mark.asyncio
async def test_first_part_always_goes_out() -> None:
    counter = _Counter()
    counter.bump()
    watch = rhythm.InterruptWatch(USER, since=0, counter=counter)
    sent: list[str] = []

    async def send(message):
        sent.append(str(message))

    result = await delivery.deliver(USER, "在吗|||问你个事", send, sleep=_no_sleep, interrupt=watch)
    assert sent == ["在吗"] and result.skipped == 1


@pytest.mark.asyncio
async def test_interrupt_during_pause_skips_the_next_part() -> None:
    counter = _Counter()
    sent: list[str] = []
    slept: list[float] = []

    async def sleep(seconds):
        slept.append(seconds)
        counter.bump()

    async def send(message):
        sent.append(str(message))

    result = await delivery.deliver(
        USER,
        "【节奏:分条】这个嘛|||我想想啊 应该是周三",
        send,
        sleep=sleep,
        interrupt=rhythm.InterruptWatch.start(USER, counter=counter),
    )
    assert sent == ["这个嘛"] and result.interrupted and slept


@pytest.mark.asyncio
async def test_without_interrupt_everything_is_sent() -> None:
    counter = _Counter()
    sent: list[str] = []
    result = await delivery.deliver(
        USER,
        "一|||二|||三三",
        _interrupting_send(counter, 1, sent),
        sleep=_no_sleep,
        interrupt=rhythm.InterruptWatch.start(USER, enabled=False, counter=counter),
    )
    assert len(sent) == 3 and not result.interrupted
    assert rhythm.cut_note(USER) is None


@pytest.mark.asyncio
async def test_sandbox_files_still_go_out_after_interrupt(fake_client) -> None:
    fake_client.files = {"out/a.png": (b"png", "image/png", "image")}
    counter = _Counter()
    sent: list[str] = []
    result = await delivery.deliver(
        USER,
        "做好啦|||你看看|||[图片:sandbox:out/a.png]|||[表情:开心]",
        _interrupting_send(counter, 1, sent),
        sleep=_no_sleep,
        interrupt=rhythm.InterruptWatch.start(USER, counter=counter),
    )
    assert result.images == 1 and result.delivered == ["做好啦", "[图片]"]
    assert result.skipped == 2 and result.skipped_stickers == 1
    assert rhythm.cut_note(USER)["unsent_stickers"] == 1


@pytest.mark.asyncio
async def test_sandbox_files_can_be_dropped_too(fake_client, monkeypatch) -> None:
    monkeypatch.setattr(CFG, "private_agent_interrupt_keep_media", False)
    fake_client.files = {"out/a.png": (b"png", "image/png", "image")}
    counter = _Counter()
    sent: list[str] = []
    result = await delivery.deliver(
        USER,
        "做好啦|||[图片:sandbox:out/a.png]",
        _interrupting_send(counter, 1, sent),
        sleep=_no_sleep,
        interrupt=rhythm.InterruptWatch.start(USER, counter=counter),
    )
    assert result.images == 0 and result.delivered == ["做好啦"]
    assert computer is not None


@pytest.mark.asyncio
async def test_deliveries_feed_the_mix_stats() -> None:
    async def send(message):
        return None

    for _ in range(3):
        await delivery.deliver(USER, "一|||二二|||三三三", send, sleep=_no_sleep)
    assert rhythm.recent_counts(USER) == [3, 3, 3]
    assert rhythm.mix_note(USER)["pattern"] == "same"


# ── 被打断的记录 ───────────────────────────────────────────────────


def test_cut_note_expires_and_is_cleared_once() -> None:
    rhythm.note_cut(USER, sent_parts=1, unsent_parts=2, now=100.0)
    assert rhythm.cut_note(USER, now=130.0)["seconds_ago"] == 30
    assert rhythm.cut_note(USER, now=130.0) is not None  # 不取走
    assert rhythm.clear_cut(USER) is True and rhythm.clear_cut(USER) is False
    rhythm.note_cut(USER, sent_parts=1, unsent_parts=2, now=100.0)
    assert rhythm.cut_note(USER, now=100.0 + rhythm.CUT_TTL_SECONDS + 1) is None
    assert not rhythm.pending_cut(USER)


def test_cut_note_mentions_report_pipeline() -> None:
    rhythm.note_cut(USER, sent_parts=1, unsent_parts=1, pipeline="report", now=10.0)
    assert rhythm.take_cut(USER, now=11.0)["was"] == "告诉他后台结果"
    assert rhythm.take_cut(USER, now=11.0) is None


def test_nothing_unsent_means_no_cut() -> None:
    rhythm.note_cut(USER, sent_parts=2, unsent_parts=0)
    assert rhythm.cut_note(USER) is None


# ── 提示词：只进本轮层，不碰前缀 ─────────────────────────────────────


def test_new_sections_are_registered_in_system3() -> None:
    for section_id in ("private.reply_cut", "private.rhythm_mix"):
        section = sections.get_section(section_id)
        assert section.default_text()
        assert registry.pipelines_of(section_id)
    steps = {step.section_id: step.where for step in registry.PIPELINES[0].steps}
    assert steps["private.reply_cut"] == "system#3" and steps["private.rhythm_mix"] == "system#3"
    assert "unsent_parts" in prompt.PRIVATE_REPLY_CUT and "不要原样重发" in prompt.PRIVATE_REPLY_CUT
    assert "一条" in prompt.PRIVATE_RHYTHM_MIX


@pytest.mark.asyncio
async def test_interrupted_turn_keeps_unsent_out_of_history_and_tells_next_turn(maker, monkeypatch) -> None:
    seen: list[dict] = []
    _script(
        monkeypatch,
        [_message("【节奏:分条】先说结论|||能用|||不过有个坑|||要先重启"), _message("好嘞")],
        seen,
    )

    class _TalkingBot(_Bot):
        async def send(self, _event, message) -> None:
            self.sent.append(str(message))
            if len(self.sent) == 1:
                chat_state._arrivals[USER] = chat_state._arrivals.get(USER, 0) + 1

    async with maker() as session:
        bot = _TalkingBot()
        text = await pagent.handle_private_turn(_request(session, bot, "这个能用吗"))
    # 返回值就是写进历史的内容：只有发出去的第一条。
    assert text == "先说结论" and bot.sent == ["先说结论"]
    history = [
        {"role": "user", "content": "这个能用吗"},
        {"role": "assistant", "content": text},
        {"role": "user", "content": "等下 我换个问法"},
    ]
    async with maker() as session:
        bot = _Bot()
        await pagent.handle_private_turn(_request(session, bot, "等下 我换个问法", history=history))
    first, second = seen[0]["messages"], seen[1]["messages"]
    # 没发出去的内容不在下一轮的任何地方。
    assert all("要先重启" not in str(m.get("content")) for m in second)
    turn_layer = second[-1]["content"]
    assert prompt.PRIVATE_REPLY_CUT in turn_layer and '"unsent_parts":3' in turn_layer
    # 规则层（system#1）逐字节不变：前缀缓存不受影响。
    assert first[0]["content"] == second[0]["content"]
    assert prompt.PRIVATE_REPLY_CUT not in second[0]["content"] + second[1]["content"]
    # 用过一次就清掉。
    assert rhythm.cut_note(USER) is None


@pytest.mark.asyncio
async def test_silent_turn_keeps_the_cut_for_later(maker, monkeypatch) -> None:
    rhythm.note_cut(USER, sent_parts=1, unsent_parts=2)
    _script(monkeypatch, [_message("【不回】")])
    async with maker() as session:
        assert await pagent.handle_private_turn(_request(session, _Bot(), "嗯")) == ""
    assert rhythm.pending_cut(USER)


@pytest.mark.asyncio
async def test_template_streak_adds_turn_hint(maker, monkeypatch) -> None:
    for _ in range(3):
        rhythm.note_delivery(USER, parts=3, rhythm="split")
    seen: list[dict] = []
    _script(monkeypatch, [_message("【节奏:整段】好呀")], seen)
    async with maker() as session:
        await pagent.handle_private_turn(_request(session, _Bot(), "晚上吃啥"))
    messages = seen[0]["messages"]
    assert prompt.PRIVATE_RHYTHM_MIX in messages[-1]["content"]
    assert '"last_reply_parts":[3,3,3]' in messages[-1]["content"]
    assert prompt.PRIVATE_RHYTHM_MIX not in messages[0]["content"]
    assert rhythm.recent_counts(USER)[-1] == 1


@pytest.mark.asyncio
async def test_just_finished_not_marked_when_cut(maker, monkeypatch) -> None:
    def seed(record):
        record["tasks"] = [{"id": "t1", "title": "查天气", "status": "done", "summary": "周六晴"}]
        record["next_id"] = 100

    store.update(USER, seed)

    class _TalkingBot(_Bot):
        async def send(self, _event, message) -> None:
            self.sent.append(str(message))
            chat_state._arrivals[USER] = chat_state._arrivals.get(USER, 0) + 1

    _script(monkeypatch, [_message("在呢|||对了 天气查好了 周六晴")])
    async with maker() as session:
        await pagent.handle_private_turn(_request(session, _TalkingBot(), "在吗"))
    assert ptasks.get_task(USER, "t1").get("reported_state") is None


class _PrivateBot(_Bot):
    def __init__(self, *, talk_after: int | None = None) -> None:
        super().__init__()
        self.talk_after = talk_after

    async def send_private_msg(self, *, user_id, message) -> None:
        self.sent.append(str(message))
        if self.talk_after is not None and len(self.sent) == self.talk_after:
            chat_state._arrivals[USER] = chat_state._arrivals.get(USER, 0) + 1


@pytest.mark.asyncio
async def test_interrupted_report_stays_unreported(maker, monkeypatch) -> None:
    monkeypatch.setattr(orm, "get_session", maker)

    def seed(record):
        record["tasks"] = [{"id": "t1", "title": "查天气", "status": "done", "summary": "周六晴"}]
        record["next_id"] = 100

    store.update(USER, seed)
    _script(monkeypatch, [_message("查好啦|||周六晴|||周日有雨记得带伞")])
    bot = _PrivateBot(talk_after=1)
    assert await pagent.report_task(bot, USER, ptasks.get_task(USER, "t1")) is False
    assert bot.sent == ["查好啦"]
    assert rhythm.cut_note(USER)["was"] == "告诉他后台结果"
    # 完整发完的汇报照旧算告诉过了。
    rhythm.reset()
    _script(monkeypatch, [_message("查好啦|||周六晴")])
    assert await pagent.report_task(_PrivateBot(), USER, ptasks.get_task(USER, "t1")) is True


# ── 配置与 WebUI ───────────────────────────────────────────────────


def test_new_settings_are_in_env_catalog() -> None:
    from pathlib import Path

    catalog = (
        Path(__file__).resolve().parents[1] / "src/plugins/yawn_core/webui/environment_catalog.dotenv"
    ).read_text(encoding="utf-8")
    for key in (
        "PRIVATE_AGENT_INTERRUPT",
        "PRIVATE_AGENT_INTERRUPT_KEEP_MEDIA",
        "PRIVATE_AGENT_REPLY_MAX_PARTS",
        "PRIVATE_AGENT_RHYTHM_MIX_HINT",
    ):
        assert f"\n{key}=" in catalog


@pytest.mark.asyncio
async def test_rhythm_route_returns_snapshot() -> None:
    rhythm.note_delivery(USER, parts=2, rhythm="split")
    body: dict[str, Any] = await routes.get_private_rhythm_stats(object(), user_id=USER)
    data = body["data"]
    assert data["stats"]["deliveries"] == 1 and data["recent"][0]["parts"] == 2
    overall = (await routes.get_private_rhythm_stats(object(), user_id=None))["data"]
    assert "recent" not in overall
