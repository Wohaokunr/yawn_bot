# ruff: noqa: ANN001, ANN003, ARG001, ARG002, ARG005, E501, F811, PLR2004
"""私聊主从 Agent “只有一个我”：对外口吻统一、进度像本人、结果顺进聊天、汇报合并，
以及主 Agent 更愿意派活（只写 goal、自动带上下文、重活提醒、轮次用完自动转后台）。"""

from __future__ import annotations

import asyncio
import importlib
import json
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
    sections,
)
from test_private_agent_multi import (
    _isolated,  # noqa: F401  (autouse fixture)
    _settle,
    _start,
    _worker_script,
    recorder,  # noqa: F401  (fixture)
)

_PKG = "src.plugins.yawn_core"
store = importlib.import_module(f"{_PKG}.yawn_agent.private_store")
ptools = importlib.import_module(f"{_PKG}.yawn_agent.private_tools")
ptasks = importlib.import_module(f"{_PKG}.yawn_agent.private_tasks")
worker = importlib.import_module(f"{_PKG}.yawn_agent.private_worker")
voice = importlib.import_module(f"{_PKG}.yawn_agent.private_voice")
prompt = importlib.import_module(f"{_PKG}.yawn_agent.private_prompt")
orm = importlib.import_module("nonebot_plugin_orm")
CFG = bridge.private_agent_config


def _seed_tasks(*tasks: dict[str, Any]) -> None:
    def seed(record):
        record["tasks"] = [dict(item) for item in tasks]
        record["next_id"] = 100

    store.update(USER, seed)


def _no_spawn(monkeypatch) -> list[str]:
    spawned: list[str] = []
    monkeypatch.setattr(
        ptasks, "_spawn", lambda user_id, task_id, **_kw: spawned.append(task_id)
    )
    return spawned


def _ctx(**extra) -> Any:
    fields = {
        "session": None,
        "user_id": USER,
        "bot_id": None,
        "groups": [],
        "now": pagent.now_beijing(),
        "bot": object(),
    }
    fields.update(extra)
    return ptools.PrivateToolContext(**fields)


class _PrivateBot(_Bot):
    async def send_private_msg(self, *, user_id, message) -> None:
        self.sent.append(str(message))


# ── 对外只有一个“我” ────────────────────────────────────────────


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("交给小助手了，弄好叫你", "安排上了，弄好叫你"),
        ("我已经交给小助手去查了", "我已经去查了"),
        ("小助手说北京晴", "北京晴"),
        ("小助手想问你：哪三个城市？", "我想问你：哪三个城市？"),
        ("让小助手帮你查（t12）", "让我帮你查"),
        ("任务 t3 还在跑", "那件事还在跑"),
        ("子智能体们正在做", "我正在做"),
        ("我派给后台小助手了", "我安排上了"),
        ("用 delegate_task 派出去了", "派出去了"),
        ("主助手让我问你", "我问你"),
        ("今天天气不错", "今天天气不错"),
    ],
)
def test_scrub_internal_words(raw, expected) -> None:
    assert voice.scrub_internal(raw) == expected


def test_scrub_keeps_persona_named_like_helper() -> None:
    assert voice.scrub_internal("小助手来啦", persona_names=("小助手",)) == "小助手来啦"
    # 任务编号仍然清掉
    assert "t3" not in voice.scrub_internal("小助手在做（t3）", persona_names=("小助手",))


def test_voice_card_and_dialogue_excerpt() -> None:
    persona = {
        "name": "阿喵",
        "compiled": {
            "name": "阿喵",
            "self_name": "本喵",
            "speaking_style": "懒洋洋",
            "catchphrases": ["喵~", "哼"],
            "examples": ["不该带上"],
        },
    }
    card = voice.voice_card(persona)
    assert card["self_name"] == "本喵" and card["catchphrases"] == ["喵~", "哼"]
    assert "examples" not in card
    assert voice.persona_names(persona) == ("阿喵", "本喵")
    history = [
        {"role": "user", "content": "帮我比较下三家手机"},
        {"role": "assistant", "content": "好的"},
        {"role": "user", "content": "[系统通知：不该出现]"},
        {"role": "user", "content": "重点看续航"},
    ]
    excerpt = voice.dialogue_excerpt(history)
    assert excerpt.splitlines() == ["他：帮我比较下三家手机", "我：好的", "他：重点看续航"]
    assert len(voice.dialogue_excerpt([{"role": "user", "content": "x" * 5000}])) < 400


def test_natural_progress_and_template() -> None:
    assert voice.natural_progress("查到三家了", title="比价·调研员", step=2, total=5, several=True) == "比价那边：查到三家了（2/5）"
    assert voice.natural_progress("查到三家了", title="比价", step=1, total=1) == "查到三家了"
    task = {"title": "查天气"}
    assert ptasks.progress_text(task, "已查到北京", 1, 2, style="template") == "「查天气」 1/2：已查到北京"
    assert ptasks.progress_text(task, "已查到北京", 1, 2) == "已查到北京（1/2）"


def test_short_title_infer_tools_and_findings() -> None:
    assert voice.short_title("查一下周末北京的天气，然后做个表") == "查一下周末北京的天气"
    assert voice.infer_toolsets(computer=False) == ["toolbox", "web"]
    assert "computer" in voice.infer_toolsets(computer=False, has_refs=True)
    messages = [
        {"role": "user", "content": "旧"},
        {"role": "assistant", "tool_calls": [{"id": "a", "function": {"name": "web_search"}}]},
        {"role": "tool", "tool_call_id": "a", "content": "旧结果"},
        {"role": "user", "content": "新问题"},
        {"role": "assistant", "tool_calls": [{"id": "b", "function": {"name": "web_fetch"}}]},
        {"role": "tool", "tool_call_id": "b", "content": "网页正文"},
    ]
    found = voice.handoff_findings(messages)
    assert found == "[web_fetch] 网页正文"


# ── 进度像本人在说话 ────────────────────────────────────────────


@pytest.mark.asyncio
async def test_progress_dedupes_and_respects_min_gap(monkeypatch, recorder) -> None:
    _worker_script(
        monkeypatch,
        [
            _message(calls=[("report_progress", {"note": "查到北京了"})]),
            _message(calls=[("report_progress", {"note": "查到北京了"})]),
            _message(calls=[("finish", {"summary": "好了"})]),
        ],
    )
    _start()
    await _settle()
    assert recorder.progress == ["查到北京了"]  # 重复的一句不再发
    task = ptasks.get_task(USER, "t1")
    assert task["told"] == ["查到北京了"] and len(task["progress"]) == 2

    monkeypatch.setattr(CFG, "private_agent_progress_min_gap_seconds", 600)
    _worker_script(
        monkeypatch,
        [
            _message(calls=[("report_progress", {"note": "开始查了"})]),
            _message(calls=[("finish", {"summary": "好了"})]),
        ],
    )
    _start("二")
    await _settle()
    # 刚回过他“我去查”，紧跟着的进度只记录不发
    assert recorder.progress == ["查到北京了"]
    assert ptasks.get_task(USER, "t2")["progress"][-1]["note"] == "开始查了"


@pytest.mark.asyncio
async def test_progress_names_the_thing_only_when_several_open(monkeypatch, recorder) -> None:
    _seed_tasks({"id": "t9", "title": "订机票", "status": "running"})
    _worker_script(
        monkeypatch,
        [
            _message(calls=[("report_progress", {"note": "北京查好了", "step": 1, "total": 3})]),
            _message(calls=[("finish", {"summary": "好了"})]),
        ],
    )
    created = _start("查天气")
    await _settle()
    assert created["task_id"] == "t100"
    assert recorder.progress == ["查天气那边：北京查好了（1/3）"]


# ── 派活更省事：只写 goal、自动带上下文 ──────────────────────────


@pytest.mark.asyncio
async def test_delegate_with_only_goal_carries_context(monkeypatch) -> None:
    spawned = _no_spawn(monkeypatch)
    ctx = _ctx(
        dialogue="他：帮我把这份表整理一下\n我：好",
        user_input="帮我把这份表整理一下",
        voice={"name": "阿喵", "self_name": "本喵"},
        call_them="小明",
        computer=True,
        attachment_refs=["sandbox:inbox/a.xlsx"],
    )
    payload = await ptools.execute_private_tool(
        "delegate_task", {"goal": "把表格按月份汇总，输出新表"}, ctx
    )
    assert payload["ok"] is True, payload
    assert "交给小助手" not in payload["result"]["next"]
    task = ptasks.get_task(USER, spawned[0])
    assert task["title"] == "把表格按月份汇总"
    assert "computer" in task["toolsets"] and "web" in task["toolsets"]
    assert "sandbox:inbox/a.xlsx" in task["brief"]
    assert task["dialogue"].startswith("他：帮我把这份表")
    assert task["asked"] == "帮我把这份表整理一下"
    assert task["voice"]["self_name"] == "本喵" and task["call_them"] == "小明"
    assert "delegate_task" in ctx.succeeded
    # 电脑不可用时不给 computer
    payload = await ptools.execute_private_tool(
        "delegate_task", {"goal": "查天气", "tools": ["computer"]}, _ctx()
    )
    assert ptasks.get_task(USER, payload["result"]["task_id"])["toolsets"] == ["toolbox"]


@pytest.mark.asyncio
async def test_follow_up_of_carries_previous_result(monkeypatch) -> None:
    spawned = _no_spawn(monkeypatch)
    _seed_tasks(
        {
            "id": "t1",
            "title": "比手机",
            "status": "done",
            "summary": "A 续航最好",
            "details": "第一点…第二点…",
            "files": ["sandbox:out/cmp.png"],
        }
    )
    payload = await ptools.execute_private_tool(
        "delegate_task", {"goal": "展开第二点", "follow_up_of": "t1"}, _ctx()
    )
    assert payload["ok"] is True
    task = ptasks.get_task(USER, spawned[0])
    assert "A 续航最好" in task["brief"] and "sandbox:out/cmp.png" in task["brief"]
    assert task["follow_up_of"] == "t1"
    bad = await ptools.execute_private_tool(
        "delegate_task", {"goal": "x", "follow_up_of": "t77"}, _ctx()
    )
    assert bad["ok"] is False


@pytest.mark.asyncio
async def test_follow_up_files_can_be_delivered(monkeypatch, recorder) -> None:
    _worker_script(
        monkeypatch,
        [_message(calls=[("finish", {"summary": "好了", "files": ["sandbox:out/cmp.png"]})])],
    )
    ptasks.start_task(
        object(),
        USER,
        title="改图",
        goal="改图",
        brief="上次的文件：sandbox:out/cmp.png",
        toolsets=["computer"],
        save_to_notes=False,
        now=pagent.now_beijing(),
    )
    await _settle()
    assert ptasks.get_task(USER, "t1")["files"] == ["sandbox:out/cmp.png"]


def test_worker_brief_has_voice_chat_and_call_name() -> None:
    task = {
        "id": "t1",
        "title": "查天气",
        "goal": "查三地天气",
        "toolsets": ["web"],
        "dialogue": "他：周末去哪玩好",
        "asked": "周末去哪玩好",
        "voice": {"name": "阿喵"},
        "call_them": "小明",
    }
    messages = worker.build_worker_messages(task, pagent.now_beijing())
    assert prompt.PRIVATE_WORKER_VOICE in messages[0]["content"]
    assert "子智能体" not in messages[0]["content"].split("【直接对他说的话】")[0]
    brief = json.loads(messages[1]["content"].split("：", 1)[1])
    assert brief["chat_so_far"] == "他：周末去哪玩好"
    assert brief["voice"] == {"name": "阿喵"} and brief["call_them"] == "小明"
    assert brief["asked"] == "周末去哪玩好"


def test_tasks_result_action_and_digest_keeps_summary() -> None:
    now = pagent.now_beijing()
    stamp = now.strftime("%Y-%m-%d %H:%M:%S")
    _seed_tasks(
        {
            "id": "t1",
            "title": "比手机",
            "status": "done",
            "summary": "A 续航最好",
            "details": "长长的正文",
            "files": ["sandbox:out/a.png"],
            "finished_at": stamp,
            "told": ["查到三家了", "在比价格"],
            "asked": "帮我比下手机",
        }
    )
    digest = ptasks.context_digest(USER, now)
    assert digest[0]["summary"] == "A 续航最好"
    assert digest[0]["told_him"] == ["查到三家了", "在比价格"]
    assert digest[0]["reported"] is False and digest[0]["files"] == 1
    detail = ptasks.result_detail(USER, "t1")
    assert detail["details"] == "长长的正文" and detail["asked"] == "帮我比下手机"


def test_task_tools_come_first_and_new_sections_registered() -> None:
    names = [item["function"]["name"] for item in ptools.tool_schemas(tasks=True)]
    assert names[:3] == ["delegate_task", "delegate_project", "tasks"]
    assert "delegate_task" not in [item["function"]["name"] for item in ptools.tool_schemas()]
    ids = {section.id for section in sections.all_sections()}
    assert {
        "private.worker_voice",
        "private.just_finished",
        "private.handoff_nudge",
        "private.auto_handoff",
    } <= ids
    schema = ptools.TOOLS_BY_NAME["delegate_task"]
    assert schema.required == ("goal",)
    assert "简单问题自己答，不要派" not in schema.description
    assert "result" in ptools.TOOLS_BY_NAME["tasks"].properties["action"]["enum"]


# ── 主回合：重活提醒、自动转后台、对外清词、结果顺进聊天 ──────────


def _web_search(query="手机") -> Any:
    return _message(calls=[("web_search", {"query": query})])


def _system_texts(seen_call: dict) -> list[str]:
    return [m["content"] for m in seen_call["messages"] if m.get("role") == "system"]


@pytest.mark.asyncio
async def test_handoff_nudge_after_heavy_calls(maker, monkeypatch) -> None:
    _no_spawn(monkeypatch)
    monkeypatch.setattr(CFG, "private_agent_max_tool_rounds", 6)
    monkeypatch.setattr(CFG, "private_agent_handoff_after", 2)
    seen: list[dict] = []
    _script(monkeypatch, [_web_search(), _web_search(), _web_search(), _message("好啦")], seen)
    async with maker() as session:
        text = await pagent.handle_private_turn(_request(session, _Bot(), "帮我比较三家手机"))
    assert text == "好啦"
    nudge = prompt.PRIVATE_HANDOFF_NUDGE
    assert nudge not in _system_texts(seen[1])
    assert nudge in _system_texts(seen[2])
    assert sum(1 for m in seen[-1]["messages"] if m.get("content") == nudge) == 1


@pytest.mark.asyncio
async def test_auto_handoff_when_rounds_run_out(maker, monkeypatch) -> None:
    spawned = _no_spawn(monkeypatch)
    monkeypatch.setattr(CFG, "private_agent_max_tool_rounds", 1)
    seen: list[dict] = []
    _script(monkeypatch, [_web_search(), _message("我还在弄，好了跟你说")], seen)
    async with maker() as session:
        bot = _Bot()
        text = await pagent.handle_private_turn(
            _request(session, bot, "帮我把三家手机的续航价格都比一下")
        )
    assert text == "我还在弄，好了跟你说"
    assert spawned and seen[-1]["tool_choice"] == "none"
    systems = _system_texts(seen[-1])
    assert prompt.PRIVATE_AUTO_HANDOFF in systems
    assert prompt.PRIVATE_FINAL_ROUND not in systems
    task = ptasks.get_task(USER, spawned[0])
    assert task["origin"] == "auto_handoff"
    assert "[web_search]" in task["brief"]
    assert task["asked"] == "帮我把三家手机的续航价格都比一下"


@pytest.mark.asyncio
async def test_auto_handoff_off_or_unavailable_falls_back(maker, monkeypatch) -> None:
    spawned = _no_spawn(monkeypatch)
    monkeypatch.setattr(CFG, "private_agent_max_tool_rounds", 1)
    monkeypatch.setattr(CFG, "private_agent_auto_handoff", False)
    seen: list[dict] = []
    _script(monkeypatch, [_web_search(), _message("先告诉你查到的")], seen)
    async with maker() as session:
        await pagent.handle_private_turn(_request(session, _Bot(), "查手机"))
    assert not spawned
    assert prompt.PRIVATE_FINAL_ROUND in _system_texts(seen[-1])
    # 没调过重活工具时也不自动转
    monkeypatch.setattr(CFG, "private_agent_auto_handoff", True)
    seen.clear()
    _script(monkeypatch, [_message(calls=[("my_overview", {})]), _message("你是熟人啦")], seen)
    async with maker() as session:
        await pagent.handle_private_turn(_request(session, _Bot(), "我们关系怎样"))
    assert not spawned


@pytest.mark.asyncio
async def test_outgoing_reply_is_one_voice(maker, monkeypatch) -> None:
    _script(monkeypatch, [_message("交给小助手了，弄好叫你")])
    async with maker() as session:
        bot = _Bot()
        await pagent.handle_private_turn(_request(session, bot, "帮我查查"))
    assert bot.sent == ["安排上了，弄好叫你"]


@pytest.mark.asyncio
async def test_just_finished_folds_into_current_reply(maker, monkeypatch) -> None:
    _seed_tasks(
        {"id": "t1", "title": "查天气", "status": "done", "summary": "周六晴", "asked": "周末天气"}
    )
    seen: list[dict] = []
    _script(monkeypatch, [_message("在呢！对了周六是晴天")], seen)
    async with maker() as session:
        await pagent.handle_private_turn(_request(session, _Bot(), "在吗"))
    # 刚做完的结果是本轮资料：在最后一条消息后面，不进开头那层（否则每次有结果都击穿前缀缓存）。
    turn = seen[0]["messages"][-1]["content"]
    assert seen[0]["messages"][-1]["role"] == "system"
    assert "just_finished" in turn and "周六晴" in turn
    assert prompt.PRIVATE_JUST_FINISHED in turn
    assert prompt.PRIVATE_JUST_FINISHED not in seen[0]["messages"][0]["content"]
    task = ptasks.get_task(USER, "t1")
    assert task["reported_state"] == "done" and task["reported_via"] == "chat"


@pytest.mark.asyncio
async def test_just_finished_stays_pending_when_turn_is_silent(maker, monkeypatch) -> None:
    _seed_tasks({"id": "t1", "title": "查天气", "status": "done", "summary": "周六晴"})
    _script(monkeypatch, [_message("【不回】")])
    async with maker() as session:
        assert await pagent.handle_private_turn(_request(session, _Bot(), "嗯")) == ""
    assert ptasks.get_task(USER, "t1").get("reported_state") is None


# ── 汇报：合并、不重复、等回答时能自己答 ────────────────────────


@pytest.mark.asyncio
async def test_reports_are_batched(monkeypatch, recorder) -> None:
    monkeypatch.setattr(CFG, "private_agent_report_batch_seconds", 0.05)
    _worker_script(
        monkeypatch,
        [
            _message(calls=[("finish", {"summary": "北京晴"})]),
            _message(calls=[("finish", {"summary": "上海雨"})]),
        ],
    )
    _start("北京")
    _start("上海")
    await _settle()
    assert len(recorder.reports) == 1
    assert len(recorder.reports[0]["batch"]) == 1
    for task_id in ("t1", "t2"):
        assert ptasks.get_task(USER, task_id)["reported_state"] == "done"
    payload = pagent.report_payload(recorder.reports[0])
    assert len(payload["results"]) == 2
    fallback = pagent.fallback_report(recorder.reports[0])
    assert "北京晴" in fallback and "上海雨" in fallback


@pytest.mark.asyncio
async def test_report_turn_skips_what_chat_already_told(monkeypatch) -> None:
    _seed_tasks({"id": "t1", "title": "查天气", "status": "done", "summary": "晴", "reported_state": "done"})
    bot = _PrivateBot()
    assert await pagent.report_task(bot, USER, ptasks.get_task(USER, "t1")) is True
    assert bot.sent == []


@pytest.mark.asyncio
async def test_blocked_report_answers_from_chat_without_bothering(maker, monkeypatch) -> None:
    spawned = _no_spawn(monkeypatch)
    monkeypatch.setattr(orm, "get_session", maker)
    _seed_tasks(
        {"id": "t1", "title": "查天气", "status": "blocked", "question": "查哪三个城市？"}
    )
    seen: list[dict] = []
    _script(
        monkeypatch,
        [
            _message(calls=[("tasks", {"action": "answer", "id": "t1", "answer": "北京上海广州"})]),
            _message("【不回】"),
        ],
        seen,
    )
    bot = _PrivateBot()
    assert await pagent.report_task(bot, USER, ptasks.get_task(USER, "t1")) is True
    assert bot.sent == []
    assert spawned == ["t1"]
    # 工具表和主回合同一份（前缀缓存），只放行 tasks（执行时拦截）。
    assert "tasks" in {t["function"]["name"] for t in seen[0]["tools"]}
    assert len(seen[0]["tools"]) > 1 and seen[0]["tool_choice"] is None


@pytest.mark.asyncio
async def test_report_turn_text_is_one_voice(maker, monkeypatch) -> None:
    monkeypatch.setattr(orm, "get_session", maker)
    _seed_tasks({"id": "t1", "title": "查天气", "status": "done", "summary": "晴", "asked": "周末天气", "told": ["北京查好了"]})
    seen: list[dict] = []
    _script(monkeypatch, [_message("小助手说周末是晴天~")], seen)
    bot = _PrivateBot()
    assert await pagent.report_task(bot, USER, ptasks.get_task(USER, "t1")) is True
    assert bot.sent == ["周末是晴天~"]
    notice = [m for m in seen[0]["messages"] if m["role"] == "user"][-1]["content"]
    assert "周末天气" in notice and "北京查好了" in notice
    assert seen[0]["tool_choice"] == "none"
    assert prompt.PRIVATE_REPORT_TURN in seen[0]["messages"][-1]["content"]


@pytest.mark.asyncio
async def test_progress_notifier_is_one_voice() -> None:
    bot = _PrivateBot()
    assert await pagent.notify_progress(bot, USER, "小助手查到北京了") is True
    assert bot.sent == ["我查到北京了"]
    assert await pagent.notify_progress(bot, USER, "") is False


@pytest.mark.asyncio
async def test_project_progress_uses_project_title(monkeypatch, recorder) -> None:
    _worker_script(
        monkeypatch,
        [
            _message(calls=[("report_progress", {"note": "A 家查好了"})]),
            _message(calls=[("finish", {"summary": "A 好"})]),
        ]
        + [_message(calls=[("finish", {"summary": "汇总好了"})])] * 3,
    )
    ptasks.start_project(
        object(),
        USER,
        title="比手机",
        goal="出对比",
        brief="",
        members=[
            {"name": "调研员", "goal": "查 A", "tools": ["web"]},
            {"name": "写手", "goal": "汇总", "tools": ["toolbox"], "depends_on": ["调研员"]},
        ],
        save_to_notes=False,
        now=pagent.now_beijing(),
        asked="帮我比下手机",
    )
    for _ in range(5):
        await _settle()
        await asyncio.sleep(0)
    assert recorder.progress == ["A 家查好了"]
    assert recorder.reports and recorder.reports[-1]["asked"] == "帮我比下手机"
