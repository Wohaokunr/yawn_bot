# ruff: noqa: PLR2004
from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from typing import TYPE_CHECKING, Any

import nonebot
import pytest
import pytest_asyncio
from nonebot.adapters.onebot.v11 import (
    Adapter,
    Bot,
    GroupMessageEvent,
    Message,
    PrivateMessageEvent,
)
from nonebot.adapters.onebot.v11.event import Sender
from nonebot.internal.matcher import current_event, current_matcher
from sqlalchemy.exc import OperationalError
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_scoped_session,
    async_sessionmaker,
    create_async_engine,
)

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
try:
    nonebot.get_driver()
except ValueError:
    nonebot.init()
if nonebot.get_plugin("yawn_core") is None:
    nonebot.load_from_toml("pyproject.toml")

import nonebot.message as dispatch
import nonebot_plugin_orm as orm

from src.plugins.yawn_core import presence
from src.plugins.yawn_core.data_models.bot_user import BotUser
from src.plugins.yawn_core.data_models.group_agent_config import GroupAgentConfig
from src.plugins.yawn_core.data_models.group_agent_message import GroupAgentMessage
from src.plugins.yawn_core.yawn_agent import agent, collector, dialogue


def group_event(user_id: int) -> GroupMessageEvent:
    return GroupMessageEvent(
        time=1,
        self_id=900,
        post_type="message",
        sub_type="normal",
        user_id=user_id,
        message_type="group",
        message_id=user_id,
        group_id=100,
        message=Message("请解释为什么天空是蓝色的"),
        original_message=Message("[CQ:at,qq=900]请解释为什么天空是蓝色的"),
        raw_message="请解释为什么天空是蓝色的",
        font=0,
        sender=Sender(user_id=user_id, nickname="测试成员", role="member"),
        to_me=True,
    )


@pytest_asyncio.fixture
async def database(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'test.db'}")
    async with engine.begin() as connection:
        await connection.run_sync(orm.Model.metadata.create_all)
    factory = async_sessionmaker(engine)
    scoped = async_scoped_session(
        factory,
        lambda: (id(current_event.get(None)), current_matcher.get(None)),
    )
    monkeypatch.setattr(orm, "_session_factory", factory, raising=False)
    monkeypatch.setattr(orm, "_scoped_sessions", scoped, raising=False)
    collector.reset_for_tests()
    yield factory
    tasks = list(collector._workers.values())
    collector.reset_for_tests()
    await asyncio.gather(*tasks, return_exceptions=True)
    await scoped.remove()
    await engine.dispose()


@pytest.mark.asyncio
async def test_preprocessors_own_concurrent_sessions(
    database: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sessions: list[AsyncSession] = []
    closed: list[AsyncSession] = []
    ready = asyncio.Event()

    class TrackedSession(AsyncSession):
        async def __aenter__(self) -> AsyncSession:
            sessions.append(self)
            if len(sessions) == 2:
                ready.set()
            await asyncio.wait_for(ready.wait(), 2)
            return self

        async def close(self) -> None:
            closed.append(self)
            await super().close()

    monkeypatch.setattr(
        presence,
        "get_session",
        async_sessionmaker(database.kw["bind"], class_=TrackedSession),
    )
    bot = Bot(Adapter(nonebot.get_driver()), "900")

    async def api(_bot: Bot, _action: str, **_kwargs: Any) -> dict[str, str]:
        return {"group_name": "测试群"}

    monkeypatch.setattr(Bot, "call_api", api)
    monkeypatch.setattr(presence, "get_bot", lambda: bot)
    results = await asyncio.gather(
        *(
            dispatch._apply_event_preprocessors(bot, group_event(user), {})
            for user in (10, 11)
        )
    )
    assert results == [True, True]
    assert len(sessions) == len(closed) == 2
    assert sessions[0] is not sessions[1]
    async with database() as session:
        assert await session.get(BotUser, 10) is not None
        assert await session.get(BotUser, 11) is not None


@pytest.mark.asyncio
async def test_failed_event_session_does_not_poison_next_event(
    database: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sessions: list[AsyncSession] = []
    rollbacks: list[AsyncSession] = []
    closed: list[AsyncSession] = []

    class FailingSession(AsyncSession):
        async def __aenter__(self) -> AsyncSession:
            sessions.append(self)
            return self

        async def commit(self) -> None:
            if self is sessions[0]:
                raise OperationalError("commit", {}, Exception("database is locked"))
            await super().commit()

        async def rollback(self) -> None:
            rollbacks.append(self)
            await super().rollback()

        async def close(self) -> None:
            closed.append(self)
            await super().close()

    monkeypatch.setattr(
        presence,
        "get_session",
        async_sessionmaker(database.kw["bind"], class_=FailingSession),
    )
    for user in (10, 11):
        event = PrivateMessageEvent.model_validate(
            {
                **group_event(user).model_dump(),
                "message_type": "private",
                "sub_type": "friend",
            }
        )
        await presence.track_user(event)
    assert len(rollbacks) == presence._MAX_COMMIT_ATTEMPTS
    assert closed == sessions
    async with database() as session:
        assert await session.get(BotUser, 10) is None
        assert await session.get(BotUser, 11) is not None


@pytest.mark.asyncio
async def test_group_dispatch_reaches_model_and_reply(
    database: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from openai.types.chat import ChatCompletionMessage

    from src.plugins.yawn_core.llm import LLMToolCompletionResult

    bot = Bot(Adapter(nonebot.get_driver()), "900")
    model_calls: list[str] = []
    sent: list[str] = []

    async def api(_bot: Bot, action: str, **kwargs: Any) -> Any:
        if action == "get_group_info":
            return {"group_id": 100, "group_name": "测试群", "member_count": 2}
        if action == "get_group_member_info":
            return {
                "user_id": kwargs["user_id"],
                "nickname": "测试成员",
                "role": "member",
            }
        if action == "get_group_member_list":
            return [{"user_id": 10, "nickname": "测试成员", "role": "member"}]
        if action == "get_supported_actions":
            return [
                "send_group_msg",
                "get_group_info",
                "get_group_member_info",
                "get_group_member_list",
            ]
        if action == "send_group_msg":
            sent.append(str(kwargs["message"]))
            return {"message_id": 1000}
        raise AssertionError(action)

    async def complete(*_args: Any, **kwargs: Any) -> LLMToolCompletionResult:
        model_calls.append(kwargs["task"])
        return LLMToolCompletionResult(
            message=ChatCompletionMessage(
                role="assistant", content="因为空气分子更容易散射蓝色光。"
            ),
            finish_reason="stop",
            prompt_tokens=10,
            completion_tokens=10,
            cached_tokens=0,
            cache_miss_tokens=10,
            outcome="success",
            duration_ms=1,
        )

    monkeypatch.setattr(Bot, "call_api", api)
    monkeypatch.setattr(presence, "get_bot", lambda: bot)
    monkeypatch.setattr(dialogue, "complete_with_tools_result", complete)
    monkeypatch.setattr(collector, "DEBOUNCE_SECONDS", 0)
    # Isolate only matcher selection; execute the real preprocessor, dependency
    # injection, Agent listener, queue worker, dialogue and outbound code.
    monkeypatch.setattr(dispatch, "matchers", {8: [agent.agent_listener]})
    async with database() as session:
        session.add(
            GroupAgentConfig(
                group_id=100, enabled=True, short_conversation_enabled=False
            )
        )
        await session.commit()
    await dispatch.handle_event(bot, group_event(10))
    queue = collector._queues.get((900, 100))
    assert queue is not None, "Message did not reach the Agent queue"
    await asyncio.wait_for(queue.join(), 10)
    assert model_calls == ["agent_dialogue"]
    assert len(sent) == 1
    assert "空气分子" in sent[0]
    async with database() as session:
        assert await session.get(BotUser, 10) is not None
        from sqlalchemy import select

        rows = (await session.scalars(select(GroupAgentMessage))).all()
        assert {row.user_id for row in rows} == {10, 900}
