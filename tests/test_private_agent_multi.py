# ruff: noqa: ANN001, ANN003, ANN201, ARG001, ARG002, E501, FBT003, PLR2004, RUF012
"""私聊 Agent 主从多智能体 + 群聊能力移植：分条/表情包/图片文件发送、虚拟电脑、
收媒体、后台小助手（派活 → 进度 → 汇报 / 追问 / 取消 / 超时 / 限额 / 重启）。"""

from __future__ import annotations

import asyncio
import importlib
import json
from datetime import timedelta
from types import SimpleNamespace
from typing import Any

import pytest
from test_private_agent import USER, _message, _private_event, bridge, pagent

_PKG = "src.plugins.yawn_core"
store = importlib.import_module(f"{_PKG}.yawn_agent.private_store")
ptools = importlib.import_module(f"{_PKG}.yawn_agent.private_tools")
ptasks = importlib.import_module(f"{_PKG}.yawn_agent.private_tasks")
worker = importlib.import_module(f"{_PKG}.yawn_agent.private_worker")
delivery = importlib.import_module(f"{_PKG}.yawn_agent.private_delivery")
computer = importlib.import_module(f"{_PKG}.yawn_agent.private_computer")
policy = importlib.import_module(f"{_PKG}.yawn_agent.sandbox_policy")
ai_chat = importlib.import_module(f"{_PKG}.ai_chat")
chat_state = importlib.import_module(f"{_PKG}.chat_state")
tool_args = importlib.import_module(f"{_PKG}.yawn_agent.tool_args")

HEALTHY = {
    "ok": True,
    "isolation": {"isolated": True},
    "accepting_runs": True,
    "packages": [],
    "python": "3.12",
}


@pytest.fixture(autouse=True)
def _isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("PRIVATE_AGENT_DATA_DIR", str(tmp_path / "users"))
    monkeypatch.setenv("PRIVATE_AGENT_STATE_PATH", str(tmp_path / "state.json"))
    monkeypatch.setattr(policy, "STATE_DIR", tmp_path / "sandbox")
    policy.reset_state_for_tests()
    bridge.reset_state_for_tests()
    bridge.register_private_handler(pagent.handle_private_turn)
    ptasks.reset_state_for_tests()
    ptasks.register_reporter(None, None)
    monkeypatch.setattr(ptasks, "_quiet", lambda _uid, _now: False)
    cfg = ptasks.private_agent_config
    monkeypatch.setattr(cfg, "private_agent_progress_min_gap_seconds", 0)
    monkeypatch.setattr(cfg, "private_agent_report_batch_seconds", 0)

    async def fake_health(*, refresh: bool = False):
        return HEALTHY, None

    for module in (policy, computer):
        monkeypatch.setattr(module, "cached_health", fake_health)
    yield
    for runner in list(ptasks._running.values()):
        runner.cancel()
    ptasks.reset_state_for_tests()
    ptasks.register_reporter(pagent.report_task, pagent.notify_progress)
    bridge.reset_state_for_tests()
    bridge.register_private_handler(pagent.handle_private_turn)


# ── 发送规划：节奏分条 + 媒体标记 ────────────────────────────────


def test_plan_splits_rhythm_and_media_markers() -> None:
    _plan, items = delivery.plan_private_reply(
        "【节奏:分条】图画好啦|||[图片:sandbox:out/chart.png]看看[文件:sandbox:out/表.xlsx]"
    )
    kinds = [(item.kind, item.value) for item in items]
    assert kinds == [
        ("text", "图画好啦"),
        ("image", "sandbox:out/chart.png"),
        ("text", "看看"),
        ("file", "sandbox:out/表.xlsx"),
    ]
    assert items[0].delay == 0.0
    assert all(item.delay > 0 for item in items[1:])
    assert "【节奏" not in delivery.plain_text("【节奏:一条】好的")
    assert delivery.plain_text("[图片:sandbox:a.png]") == "[图片]"


def test_plan_caps_message_count_and_keeps_code_blocks() -> None:
    _plan, items = delivery.plan_private_reply("|||".join(f"第{i}条" for i in range(10)))
    assert len(items) <= delivery.MAX_MESSAGES
    assert "第9条" in items[-1].value
    _plan, code = delivery.plan_private_reply("```\n[图片:sandbox:a.png]\n```")
    assert [item.kind for item in code] == ["text"]


# ── 发送：媒体失败降级成文字说明，发送失败就停 ───────────────────


class _Client:
    files: dict[str, tuple[bytes, str, str]] = {}
    calls: list[tuple[str, Any]] = []

    async def read_file(self, workspace, path, *, max_bytes=None):
        _Client.calls.append(("read_file", (workspace, path)))
        if path not in _Client.files:
            raise computer.SandboxError("not_found", "没有这个文件")
        return _Client.files[path]

    async def run(self, workspace, code, **kwargs):
        _Client.calls.append(("run", workspace))
        return {"status": "ok", "stdout": "42", "files": [{"path": "out/a.png", "kind": "image", "size": 3}]}

    async def write_file(self, workspace, path, data):
        _Client.calls.append(("write_file", (workspace, path)))


@pytest.fixture
def fake_client(monkeypatch):
    _Client.files = {}
    _Client.calls = []
    monkeypatch.setattr(computer, "SandboxClient", _Client)
    return _Client


@pytest.mark.asyncio
async def test_deliver_sends_images_and_degrades_failures(fake_client) -> None:
    fake_client.files = {"out/a.png": (b"png", "image/png", "image")}
    sent: list[str] = []
    uploads: list[tuple[str, bytes]] = []

    async def send(message):
        sent.append(str(message))

    async def upload(name, data):
        uploads.append((name, data))

    result = await delivery.deliver(
        USER,
        "好了|||[图片:sandbox:out/a.png]|||[图片:sandbox:out/missing.png]|||[文件:sandbox:out/a.png]",
        send,
        upload_file=upload,
        sleep=_no_sleep,
    )
    assert result.images == 1 and result.files == 1 and result.dropped == 1
    assert "图片没发出去" in result.text
    assert uploads == [("a.png", b"png")]
    # 只读他自己的工作区
    assert {call[1][0] for call in fake_client.calls if call[0] == "read_file"} == {f"p{USER}"}


@pytest.mark.asyncio
async def test_deliver_without_upload_api_and_stops_on_send_failure(fake_client) -> None:
    fake_client.files = {"a.txt": (b"x", "text/plain", "text")}
    sent: list[str] = []

    async def send(message):
        if len(sent) >= 1:
            raise RuntimeError("断了")
        sent.append(str(message))

    result = await delivery.deliver(
        USER, "[文件:sandbox:a.txt]|||第二条|||第三条", send, sleep=_no_sleep
    )
    assert result.delivered == ["（文件没发出去：当前协议端不支持私聊传文件）"]
    assert result.files == 0


async def _no_sleep(_seconds):
    return None


# ── 表情包闸门 ─────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_private_sticker_budget_and_counting(monkeypatch) -> None:
    settings = SimpleNamespace(
        enabled=True,
        private_enabled=True,
        private_daily_limit=2,
        private_cooldown_seconds=0,
        max_per_message=3,
    )
    monkeypatch.setattr(delivery, "load_sticker_settings", lambda: settings)
    today = "2026-11-01"
    assert delivery.private_sticker_left(USER, today) == 2

    async def segment(user_id, name, budget):
        if budget[0] <= 0:
            return None
        budget[0] -= 1
        return delivery.MessageSegment.text(f"<{name}>")

    monkeypatch.setattr(delivery, "_sticker_segment", segment)
    sent: list[str] = []

    async def send(message):
        sent.append(str(message))

    result = await delivery.deliver(
        USER, "[表情:开心]|||[表情:哭]|||[表情:笑]", send, today=today, sleep=_no_sleep
    )
    assert result.stickers == 2 and result.dropped == 1
    assert delivery.private_sticker_left(USER, today) == 0
    assert delivery.private_sticker_instruction(USER, today) == ""
    assert delivery.private_sticker_left(USER, "2026-11-02") == 2
    settings.private_enabled = False
    assert delivery.private_sticker_left(USER, "2026-11-02") == 0


# ── 虚拟电脑：每人工作区与每日次数 ─────────────────────────────


@pytest.mark.asyncio
async def test_computer_uses_user_workspace_and_private_quota(fake_client) -> None:
    policy.save_settings(policy.SandboxSettings(private_daily_runs=1))
    assert computer.workspace_for_user(USER) == f"p{USER}"
    result = await computer.run_code(USER, {"code": "print(42)", "language": "python"})
    assert ("run", f"p{USER}") in fake_client.calls
    assert result["runs_left_today"] == 0
    assert "[图片:sandbox:" in result["next"]
    assert policy.private_usage(USER)["used"] == 1
    assert policy.private_usage(USER + 1)["used"] == 0
    with pytest.raises(tool_args.ToolError):
        await computer.run_code(USER, {"code": "print(1)"}, rerun=True)


@pytest.mark.asyncio
async def test_computer_closed_when_private_disabled() -> None:
    policy.save_settings(policy.SandboxSettings(private_enabled=False))
    ready, reason = await computer.computer_status()
    assert not ready and reason == "sandbox_closed"
    names = {s["function"]["name"] for s in ptools.tool_schemas()}
    assert not names & ptools.COMPUTER_NAMES
    with_computer = {s["function"]["name"] for s in ptools.tool_schemas(computer=True, tasks=True)}
    assert with_computer >= ptools.COMPUTER_NAMES | ptools.TASK_NAMES


# ── 收媒体：只发图片也交给 Agent，连发合并保留媒体 ──────────────


@pytest.mark.asyncio
async def test_media_only_message_is_accepted(monkeypatch) -> None:
    from nonebot.adapters.onebot.v11 import Message, MessageSegment

    monkeypatch.setattr(bridge, "_paused_cache", None)
    monkeypatch.setattr(bridge.private_agent_config, "private_agent_auto_reply", True)
    event = _private_event("x")
    image = Message(MessageSegment.image("https://example.com/a.png"))
    event.message = image
    event.original_message = image
    assert bridge.event_media(event)[0]["type"] == "image"
    assert bridge.describe_media(bridge.event_media(event)) == "[图片]"
    assert await ai_chat._is_auto_reply_msg(event)
    monkeypatch.setattr(bridge.private_agent_config, "private_agent_accept_media", False)
    assert not await ai_chat._is_auto_reply_msg(event)


def test_burst_media_is_taken_once() -> None:
    chat_state._burst_media[USER] = [{"type": "image", "file": "a"}]
    assert chat_state.take_burst_media(USER) == [{"type": "image", "file": "a"}]
    assert chat_state.take_burst_media(USER) == []


# ── 后台小助手 ─────────────────────────────────────────────────


class _Recorder:
    def __init__(self) -> None:
        self.reports: list[dict[str, Any]] = []
        self.progress: list[str] = []

    async def report(self, bot, user_id, task) -> bool:
        self.reports.append(dict(task))
        return True

    async def notify(self, bot, user_id, text) -> bool:
        self.progress.append(text)
        return True


@pytest.fixture
def recorder():
    rec = _Recorder()
    ptasks.register_reporter(rec.report, rec.notify)
    return rec


def _worker_script(monkeypatch, replies: list[Any], seen: list[list[dict]] | None = None):
    queue = list(replies)

    async def fake(messages, tools, **kwargs):
        if seen is not None:
            seen.append([dict(m) for m in messages])
        item = queue.pop(0) if queue else None
        if callable(item):
            item = await item()
        return SimpleNamespace(message=item, outcome="success" if item else "error")

    monkeypatch.setattr(worker.llm, "complete_with_tools_result", fake)


def _start(title="查天气", tools=("web",), **extra):
    return ptasks.start_task(
        object(),
        USER,
        title=title,
        goal=f"{title}的目标",
        brief="用户想知道三地天气",
        toolsets=list(tools),
        save_to_notes=extra.get("save_to_notes", False),
        now=pagent.now_beijing(),
    )


async def _settle() -> None:
    for _ in range(50):
        runners = list(ptasks._running.values())
        if not runners:
            return
        await asyncio.gather(*runners, return_exceptions=True)


@pytest.mark.asyncio
async def test_delegate_progress_done_report(monkeypatch, recorder) -> None:
    seen: list[list[dict]] = []
    _worker_script(
        monkeypatch,
        [
            _message(calls=[("report_progress", {"note": "已查到北京", "step": 1, "total": 2})]),
            _message(calls=[("report_progress", {"note": "已查到上海", "step": 2, "total": 2})]),
            _message(calls=[("finish", {"summary": "北京晴，上海雨", "success": True})]),
        ],
        seen,
    )
    payload = _start(save_to_notes=True)
    assert payload["task_id"] == "t1" and payload["status"] == "已派出"
    await _settle()
    # 像本人随口报一声：没有「标题」前缀，计数放句尾
    assert recorder.progress == ["已查到北京（1/2）", "已查到上海（2/2）"]
    assert [r["status"] for r in recorder.reports] == ["done"]
    task = ptasks.get_task(USER, "t1")
    assert task["summary"] == "北京晴，上海雨" and task["reported_state"] == "done"
    assert task["saved_note"] is True
    assert any("北京晴" in n["content"] for n in store.load(USER)["notes"])
    # 子智能体只看到任务单，看不到聊天记录
    first = seen[0]
    assert [m["role"] for m in first] == ["system"] * (len(first) - 1) + ["user"]
    assert "用户想知道三地天气" in first[-1]["content"]
    digest = ptasks.context_digest(USER, pagent.now_beijing())
    assert digest and digest[0]["id"] == "t1"


@pytest.mark.asyncio
async def test_progress_suppressed_in_quiet_hours_and_report_held(monkeypatch, recorder) -> None:
    monkeypatch.setattr(ptasks, "_quiet", lambda _uid, _now: True)
    monkeypatch.setattr(ptasks, "QUIET_GRACE", timedelta(seconds=-1))
    _worker_script(
        monkeypatch,
        [
            _message(calls=[("report_progress", {"note": "做了一半"})]),
            _message(calls=[("finish", {"summary": "好了"})]),
        ],
    )
    _start()
    await _settle()
    assert recorder.progress == [] and recorder.reports == []
    assert ptasks.get_task(USER, "t1")["held"] is True
    monkeypatch.setattr(ptasks, "_quiet", lambda _uid, _now: False)
    assert await ptasks.flush_held(pagent.now_beijing()) == 1
    assert len(recorder.reports) == 1
    assert await ptasks.flush_held(pagent.now_beijing()) == 0


@pytest.mark.asyncio
async def test_ask_user_then_answer_resumes_with_context(monkeypatch, recorder) -> None:
    seen: list[list[dict]] = []
    _worker_script(
        monkeypatch,
        [
            _message(calls=[("ask_user", {"question": "要查哪三个城市？"})]),
            _message(calls=[("finish", {"summary": "查好了"})]),
        ],
        seen,
    )
    _start()
    await _settle()
    task = ptasks.get_task(USER, "t1")
    assert task["status"] == "blocked" and task["question"] == "要查哪三个城市？"
    assert recorder.reports[-1]["status"] == "blocked"
    with pytest.raises(tool_args.ToolError):
        ptasks.answer_task(object(), USER, "t9", "北京")
    ptasks.answer_task(object(), USER, "t1", "北京上海广州")
    await _settle()
    assert ptasks.get_task(USER, "t1")["status"] == "done"
    resumed = seen[-1]
    assert any("北京上海广州" in str(m.get("content")) for m in resumed)
    assert any(m.get("role") == "tool" for m in resumed)  # 带着上次的上下文
    assert [r["status"] for r in recorder.reports] == ["blocked", "done"]


@pytest.mark.asyncio
async def test_cancel_running_task(monkeypatch, recorder) -> None:
    gate = asyncio.Event()

    async def hang():
        await gate.wait()
        return _message("不会到这里")

    _worker_script(monkeypatch, [hang])
    _start()
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    assert ptasks.cancel_task(USER, "t1")["status"] == "已取消"
    await _settle()
    assert ptasks.get_task(USER, "t1")["status"] == "cancelled"
    assert recorder.reports == []
    assert ptasks.cancel_task(USER, "t1")["note"] == "任务已经结束了"


@pytest.mark.asyncio
async def test_timeout_marks_failed(monkeypatch, recorder) -> None:
    monkeypatch.setattr(
        ptasks.private_agent_config, "private_agent_task_timeout_seconds", 0.05
    )

    async def slow():
        await asyncio.sleep(5)

    _worker_script(monkeypatch, [slow])
    _start()
    await _settle()
    task = ptasks.get_task(USER, "t1")
    assert task["status"] == "failed" and "没做完" in task["error"]
    assert recorder.reports[-1]["status"] == "failed"


@pytest.mark.asyncio
async def test_slot_and_daily_limits(monkeypatch, recorder) -> None:
    gate = asyncio.Event()

    async def hang():
        await gate.wait()
        return _message(calls=[("finish", {"summary": "ok"})])

    _worker_script(monkeypatch, [hang] * 10)
    cfg = ptasks.private_agent_config
    monkeypatch.setattr(cfg, "private_agent_task_per_user", 1)
    monkeypatch.setattr(cfg, "private_agent_task_daily", 3)
    assert _start("一")["status"] == "已派出"
    assert _start("二")["status"] == "排队中"
    with pytest.raises(tool_args.ToolError, match="在做或在排队"):
        _start("三")
    gate.set()
    await _settle()
    assert _start("三")["task_id"] == "t3"
    with pytest.raises(tool_args.ToolError, match="用完"):
        _start("四")
    await _settle()


@pytest.mark.asyncio
async def test_tasks_disabled_rejects() -> None:
    from unittest import mock

    with mock.patch.object(
        ptasks.private_agent_config, "private_agent_tasks_enabled", False
    ), pytest.raises(tool_args.ToolError, match="没有开放"):
        _start()


def test_restart_recovery_marks_active_failed() -> None:
    def seed(record):
        record["tasks"] = [
            {"id": "t1", "title": "a", "status": "running", "created_at": "2026-11-01 10:00"},
            {"id": "t2", "title": "b", "status": "blocked", "created_at": "2026-11-01 10:00"},
            {"id": "t3", "title": "c", "status": "done", "created_at": "2026-11-01 10:00"},
        ]

    store.update(USER, seed)
    assert ptasks.recover_on_startup() == 1
    tasks = {t["id"]: t for t in store.load(USER)["tasks"]}
    assert tasks["t1"]["status"] == "failed" and "重启" in tasks["t1"]["error"]
    assert tasks["t2"]["status"] == "blocked" and tasks["t2"]["context_lost"]
    assert tasks["t3"]["status"] == "done"


@pytest.mark.asyncio
async def test_finish_drops_unseen_file_refs(monkeypatch, recorder, fake_client) -> None:
    _worker_script(
        monkeypatch,
        [
            _message(calls=[("run_code", {"code": "plot()", "language": "python"})]),
            _message(
                calls=[
                    (
                        "finish",
                        {
                            "summary": "画好了",
                            "files": ["sandbox:out/a.png", "sandbox:../../etc/passwd"],
                        },
                    )
                ]
            ),
        ],
    )
    _start("画图", tools=("computer",))
    await _settle()
    task = ptasks.get_task(USER, "t1")
    assert task["files"] == ["sandbox:out/a.png"]
    assert ("run", f"p{USER}") in fake_client.calls


@pytest.mark.asyncio
async def test_worker_rejects_tools_outside_toolset(monkeypatch) -> None:
    _worker_script(
        monkeypatch,
        [
            _message(calls=[("run_code", {"code": "1"})]),
            _message(calls=[("finish", {"summary": "ok"})]),
        ],
    )
    task = {"id": "t1", "title": "x", "goal": "y", "brief": "", "toolsets": ["web"]}
    outcome = await worker.run_worker(
        task, user_id=USER, now=pagent.now_beijing(), max_rounds=4
    )
    tool_msgs = [m for m in outcome.messages if m.get("role") == "tool"]
    assert "没有 run_code" in json.loads(tool_msgs[0]["content"])["error"]
    assert outcome.status == "done"


def test_fallback_report_and_claim_guard() -> None:
    done = {"title": "画图", "status": "done", "summary": "好了", "files": ["sandbox:out/a.png"]}
    assert "[图片:sandbox:out/a.png]" in pagent.fallback_report(done)
    blocked = {"title": "查天气", "status": "blocked", "question": "哪三个城市？", "id": "t1"}
    assert "哪三个城市" in pagent.fallback_report(blocked)
    assert "小助手" not in pagent.fallback_report(blocked)
    assert "「" not in pagent.fallback_report(done)
    assert pagent.report_payload(blocked)["status"] == "waiting_for_you"
    failed = {"title": "x", "status": "failed", "error": "超时"}
    assert pagent.report_payload(failed)["reason"] == "超时"


def test_private_data_wipe_cancels_tasks() -> None:
    def seed(record):
        record["tasks"] = [{"id": "t1", "title": "a", "status": "blocked"}]

    store.update(USER, seed)
    assert ptasks.cancel_all(USER) == 1
    assert ptasks.get_task(USER, "t1")["status"] == "cancelled"
