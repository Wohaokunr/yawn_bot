"""ORM 会话过期 / 消息路由 / LLM 并发超时等回归测试。

nonebot_plugin_orm 的会话默认 ``expire_on_commit=True``：commit 后在异步代码里
访问任何 ORM 属性都会触发 MissingGreenlet。以下测试用真实的内存 SQLite 会话
（同样开启 expire_on_commit）复现这些路径。
"""

from __future__ import annotations

import asyncio
import contextlib
import importlib
import sys
from pathlib import Path
from typing import TYPE_CHECKING, Any

import nonebot
import pytest
from nonebot.exception import FinishedException, RejectedException
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

if TYPE_CHECKING:
    from typing_extensions import Self

try:
    nonebot.get_driver()
except ValueError:
    nonebot.init()

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))
if nonebot.get_plugin("yawn_core") is None:
    nonebot.load_from_toml("pyproject.toml")

checkin_module = importlib.import_module("src.plugins.yawn_core.checkin")
reminder_module = importlib.import_module("src.plugins.yawn_core.reminder")
panel_router = importlib.import_module("src.plugins.yawn_core.panel_router")
panel_menu = importlib.import_module("src.plugins.yawn_core.panel_menu")
ai_chat = importlib.import_module("src.plugins.yawn_core.ai_chat")
llm = importlib.import_module("src.plugins.yawn_core.llm")
fanqie_state = importlib.import_module("src.plugins.yawn_core.yawn_fanqie.state")

bot_group_models = importlib.import_module(
    "src.plugins.yawn_core.data_models.bot_group"
)
bot_user_models = importlib.import_module("src.plugins.yawn_core.data_models.bot_user")
user_group_models = importlib.import_module(
    "src.plugins.yawn_core.data_models.user_group"
)
checkin_record_models = importlib.import_module(
    "src.plugins.yawn_core.data_models.checkin_record"
)
checkin_user_models = importlib.import_module(
    "src.plugins.yawn_core.data_models.checkin_user"
)
reminder_models = importlib.import_module(
    "src.plugins.yawn_core.data_models.scheduled_reminder"
)
chat_session_models = importlib.import_module(
    "src.plugins.yawn_core.data_models.chat_session"
)
chat_message_models = importlib.import_module(
    "src.plugins.yawn_core.data_models.chat_message"
)


async def _make_factory() -> async_sessionmaker[AsyncSession]:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    tables = [
        bot_user_models.BotUser.__table__,
        bot_group_models.BotGroup.__table__,
        user_group_models.UserGroup.__table__,
        checkin_record_models.CheckinRecord.__table__,
        checkin_user_models.CheckinUser.__table__,
        reminder_models.ScheduledReminder.__table__,
        chat_session_models.ChatSession.__table__,
        chat_message_models.ChatMessage.__table__,
    ]
    async with engine.begin() as connection:
        await connection.run_sync(
            lambda sync_connection: bot_group_models.BotGroup.metadata.create_all(
                sync_connection, tables=tables
            )
        )
    # 与 nonebot_plugin_orm 一致：commit 后过期所有属性
    return async_sessionmaker(engine, expire_on_commit=True)


def _group_event(user_id: int = 10001, group_id: int = 20001) -> Any:
    from nonebot.adapters.onebot.v11 import GroupMessageEvent, Message

    return GroupMessageEvent.model_validate(
        {
            "time": 0,
            "self_id": 1,
            "post_type": "message",
            "sub_type": "normal",
            "user_id": user_id,
            "group_id": group_id,
            "message_type": "group",
            "message_id": 1,
            "message": Message("/签到"),
            "original_message": Message("/签到"),
            "raw_message": "/签到",
            "font": 0,
            "sender": {"user_id": user_id, "nickname": "测试"},
        }
    )


# ── 1. 首次签到 ─────────────────────────────────────────


@pytest.mark.asyncio
async def test_first_checkin_initialises_counters(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    factory = await _make_factory()
    sent: list[str] = []

    async def fake_finish(message: object = None, **_kwargs: object) -> None:
        sent.append(str(message))
        raise FinishedException

    monkeypatch.setattr(checkin_module.checkin, "finish", fake_finish)
    monkeypatch.setattr(checkin_module, "randint", lambda _a, _b: 7)

    async with factory() as session:
        with pytest.raises(FinishedException):
            await checkin_module.handle_checkin(_group_event(), session, None)

    assert "签到成功" in sent[-1]
    async with factory() as session:
        row = await session.get(
            checkin_user_models.CheckinUser,
            {"group_id": 20001, "user_id": 10001},
        )
        assert row is not None
        assert (row.total_days, row.streak_days, row.points) == (1, 1, 7)


# ── 2/3. 定时提醒 commit 后访问过期属性 ─────────────────


async def _seed_reminder(factory: async_sessionmaker[AsyncSession]) -> int:
    async with factory() as session:
        session.add(bot_group_models.BotGroup(group_id=20001, group_name="群"))
        reminder = reminder_models.ScheduledReminder(
            group_id=20001,
            creator_user_id=10001,
            name="早安",
            schedule_type="recurring",
            cron_expression="0 7 * * *",
            target_type="group",
            target_user_id=None,
            message_segments=[{"type": "text", "data": {"text": "早"}}],
            enabled=True,
        )
        session.add(reminder)
        await session.flush()
        reminder_id = reminder.id
        await session.commit()
    return reminder_id


@pytest.mark.asyncio
async def test_update_reminder_after_commit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    factory = await _make_factory()
    reminder_id = await _seed_reminder(factory)
    scheduled: list[int] = []
    monkeypatch.setattr(
        reminder_module,
        "_schedule_reminder_job",
        lambda rid, _schedule: scheduled.append(rid),
    )

    async with factory() as session:
        reminder = await session.get(reminder_models.ScheduledReminder, reminder_id)
        draft = reminder_module._draft_from_reminder(reminder)
        draft.name = "午安"
        updated = await reminder_module._update_reminder(session, reminder, draft)
        # 调用方随后读取 id / 详情
        assert updated.id == reminder_id
        assert updated.name == "午安"

    assert scheduled == [reminder_id]


@pytest.mark.asyncio
async def test_disable_action_after_commit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    factory = await _make_factory()
    reminder_id = await _seed_reminder(factory)
    shown: list[tuple[int, str | None]] = []
    monkeypatch.setattr(reminder_module, "_remove_reminder_job", lambda _rid: True)

    async def fake_show_detail(
        _session: object,
        _matcher: object,
        rid: int,
        *,
        prefix: str | None = None,
    ) -> None:
        shown.append((rid, prefix))

    monkeypatch.setattr(reminder_module, "_show_detail", fake_show_detail)

    async with factory() as session:
        reminder = await session.get(reminder_models.ScheduledReminder, reminder_id)
        await reminder_module._send_action_result(
            session, object(), reminder, "disable"
        )

    assert shown == [(reminder_id, "已停用。")]
    async with factory() as session:
        row = await session.get(reminder_models.ScheduledReminder, reminder_id)
        assert row is not None
        assert row.enabled is False


@pytest.mark.asyncio
async def test_ensure_group_new_group_name_readable() -> None:
    factory = await _make_factory()

    class FakeBot:
        async def get_group_info(self, *, group_id: int) -> dict[str, Any]:
            return {"group_id": group_id, "group_name": "新群"}

    async with factory() as session:
        group = await reminder_module._ensure_group(FakeBot(), session, 30001)
        assert group.group_name == "新群"


# ── 7. 私聊目标提醒也受所属群功能开关约束 ───────────────


@pytest.mark.asyncio
async def test_private_reminder_respects_group_feature_gate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakeReminder:
        enabled = True
        creator_user_id = 10001
        group_id = 20001
        target_type = "private"
        target_user_id = 10002
        message_segments = ()

    class FakeSession:
        async def __aenter__(self) -> Self:
            return self

        async def __aexit__(self, *_args: object) -> None:
            return None

        async def get(self, *_args: object) -> FakeReminder:
            return FakeReminder()

    checks: list[tuple[int, int | None]] = []

    async def fake_check(
        user_id: int, group_id: int | None, _feature: str, _session: object
    ) -> bool:
        checks.append((user_id, group_id))
        return group_id is None  # 群关闭，私聊个人开关开启

    def fail_get_bot() -> None:
        raise AssertionError("功能关闭时不应发送")

    monkeypatch.setattr(reminder_module, "get_session", FakeSession)
    monkeypatch.setattr(reminder_module, "check_feature_permission", fake_check)
    monkeypatch.setattr(reminder_module, "get_bot", fail_get_bot)

    status = await reminder_module._deliver_reminder(1, force=False)

    assert status == "feature_disabled"
    assert (10001, 20001) in checks


# ── 4. 面板删除对话需在当前会话重新取行 ───────────────


@pytest.mark.asyncio
async def test_panel_delete_chat_session_persists() -> None:
    factory = await _make_factory()
    async with factory() as session:
        chat = chat_session_models.ChatSession(user_id=10001, title="旧对话")
        session.add(chat)
        await session.commit()
    # 模拟上一个事件：由已关闭的会话加载 flow.sessions
    async with factory() as old_session:
        sessions = await panel_router.get_user_sessions(old_session, 10001)
        flow = panel_menu.PanelFlow(
            user_id=10001,
            mode=panel_menu.PanelMode.PRIVATE,
            view=panel_menu.PanelView.CHAT_LIST,
            sessions=sessions,
        )

    class FakeMatcher:
        async def reject_arg(self, _key: str, _prompt: object = None) -> None:
            raise RejectedException

    async with factory() as session:
        with pytest.raises(RejectedException):
            await panel_router._route_chat_list(FakeMatcher(), session, flow, "删除 1")

    async with factory() as session:
        row = await session.get(chat_session_models.ChatSession, sessions[0].id)
        assert row is not None
        assert row.is_deleted is True
    assert flow.sessions == []


# ── 6. 对话模式不吞掉会话向导的回复 ───────────────────


@pytest.mark.asyncio
async def test_chat_mode_listener_yields_to_waiting_session_matcher(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import nonebot.message as nb_message
    from nonebot.adapters.onebot.v11 import Adapter, Bot, Message, PrivateMessageEvent
    from nonebot.dependencies import Dependent
    from nonebot.matcher import Matcher
    from nonebot.permission import USER
    from nonebot.rule import Rule

    user_id = 40001
    event = PrivateMessageEvent.model_validate(
        {
            "time": 0,
            "self_id": 1,
            "post_type": "message",
            "sub_type": "friend",
            "user_id": user_id,
            "message_type": "private",
            "message_id": 1,
            "message": Message("1"),
            "original_message": Message("1"),
            "raw_message": "1",
            "font": 0,
            "sender": {"user_id": user_id, "nickname": "测试"},
        }
    )
    bot = Bot(Adapter(nonebot.get_driver()), "1")

    monkeypatch.setattr(nb_message, "_event_preprocessors", set())
    monkeypatch.setattr(nb_message, "_run_preprocessors", set())
    monkeypatch.setattr(ai_chat, "is_in_mode", lambda uid: uid == user_id)

    calls: list[str] = []

    async def chat_handler() -> None:
        calls.append("chat")

    async def wizard_handler() -> None:
        calls.append("wizard")

    monkeypatch.setattr(
        ai_chat.chat_mode_listener,
        "handlers",
        [
            Dependent[Any].parse(
                call=chat_handler, allow_types=Matcher.HANDLER_PARAM_TYPES
            )
        ],
    )

    # 对话模式下无等待中的向导：消息照常进入对话模式
    await nb_message.handle_event(bot, event)
    assert calls == ["chat"]

    # 与 got/reject 产生的会话临时 matcher 相同的参数
    temp = Matcher.new(
        "message",
        Rule(),
        USER(event.get_session_id()),
        [wizard_handler],
        temp=True,
        priority=0,
        block=True,
    )
    calls.clear()
    try:
        await nb_message.handle_event(bot, event)
    finally:
        # 临时 matcher 运行后会自行销毁；仅在未运行时清理
        with contextlib.suppress(ValueError):
            temp.destroy()
    assert calls == ["wizard"]


# ── 8. LLM 并发名额的排队时间计入调用方超时 ─────────────


@pytest.mark.asyncio
async def test_llm_timeout_covers_semaphore_wait(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakeCompletions:
        async def create(self, **_kwargs: object) -> None:
            raise AssertionError("排队超时时不应发起请求")

    class FakeClient:
        class chat:  # noqa: N801
            completions = FakeCompletions()

    monkeypatch.setattr(llm, "get_client", lambda *_a, **_k: FakeClient())
    semaphore = asyncio.Semaphore(1)
    monkeypatch.setattr(llm, "_COMPLETION_CONCURRENCY", semaphore)
    await semaphore.acquire()
    try:
        result = await asyncio.wait_for(
            llm.complete([{"role": "user", "content": "hi"}], timeout=0.05),
            timeout=2,
        )
        assert result is None
        tool_result = await asyncio.wait_for(
            llm.complete_with_tools_result(
                [{"role": "user", "content": "hi"}], [], timeout=0.05
            ),
            timeout=2,
        )
        assert tool_result.outcome == "timeout"
        assert tool_result.message is None
    finally:
        semaphore.release()
    # 排队超时不得泄漏名额
    assert not semaphore.locked()


# ── 9. 番茄过期文件定时清理 ───────────────────────────


@pytest.mark.asyncio
async def test_fanqie_cleanup_scheduled_hourly(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, Any] = {}

    def add_job(func: object, trigger: str, **kwargs: Any) -> None:
        captured.update(kwargs, func=func, trigger=trigger)

    monkeypatch.setattr(fanqie_state.scheduler, "add_job", add_job)
    await fanqie_state._schedule_file_cleanup()

    assert captured["trigger"] == "interval"
    assert captured["hours"] == 1
    assert captured["replace_existing"] is True

    calls: list[bool] = []
    monkeypatch.setattr(
        fanqie_state, "cleanup_expired_files", lambda: calls.append(True) or 0
    )
    await captured["func"]()
    assert calls == [True]
