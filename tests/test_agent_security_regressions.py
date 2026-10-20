# ruff: noqa: PLR2004
"""Agent 安全回归：CQ 注入、critical 意图、actor-vs-target、隐私过滤与提交失败。"""

from __future__ import annotations

import importlib
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import nonebot
import pytest
from sqlalchemy import select
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
    commands,
    dialogue_runtime_support,
    message_parser,
    outbound,
    tools,
)

memory = importlib.import_module("src.plugins.yawn_core.yawn_agent.memory")
models = importlib.import_module("src.plugins.yawn_core.data_models.agent_memory")
message_models = importlib.import_module(
    "src.plugins.yawn_core.data_models.group_agent_message"
)
config_models = importlib.import_module(
    "src.plugins.yawn_core.data_models.group_agent_config"
)

BOT_ID = 100
OWNER_ID = 201
ADMIN_ID = 200
OTHER_ADMIN_ID = 202
MEMBER_ID = 300
OPTED_OUT_ID = 400
CQ_PAYLOAD = "[CQ:at,qq=all][CQ:image,file=file:///etc/passwd]"


# ── 1. CQ 码注入 ────────────────────────────────────────────


class _ForwardSession:
    async def get(self, _model: object, _key: object) -> object:
        return SimpleNamespace(user_id=MEMBER_ID, group_nickname="成员")


@pytest.mark.asyncio
async def test_forward_custom_node_content_is_plain_text() -> None:
    prepared = await outbound.prepare_forward_message(
        [{"type": "custom", "user_id": MEMBER_ID, "content": CQ_PAYLOAD}],
        session=_ForwardSession(),
        group_id=1,
    )

    content = prepared.nodes[0].data["content"]
    assert [segment.type for segment in content] == ["text"]
    assert content[0].data["text"] == CQ_PAYLOAD


def test_command_memory_output_does_not_parse_cq_codes() -> None:
    message = commands._plain_message(f"- key: {CQ_PAYLOAD}")

    assert [segment.type for segment in message] == ["text"]
    assert CQ_PAYLOAD in message[0].data["text"]


# ── 2. critical 工具显式意图 ────────────────────────────────


def test_critical_tools_are_never_discoverable() -> None:
    for query in ("踢人", "全员禁言", "设置管理员", "删除群文件", "*"):
        ranked = tools.rank_discoverable_tools(
            query, list(tools._TOOL_DEFINITIONS), limit=50
        )
        assert not {item.name for item in ranked} & tools._CRITICAL_TOOLS


def test_critical_intent_comes_only_from_current_text() -> None:
    assert "kick_member" in tools.select_critical_tool_names("把他移出群")
    assert "set_whole_group_mute" in tools.select_critical_tool_names("开启全员禁言")
    assert "set_group_admin" in tools.select_critical_tool_names("设为管理员")
    assert "delete_group_file" in tools.select_critical_tool_names("删掉那个文件")
    assert tools.select_critical_tool_names("今天天气不错") == frozenset()
    assert tools.select_critical_tool_names(None) == frozenset()


@pytest.mark.asyncio
async def test_explicit_critical_intent_loads_schema_for_admin_actor(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    owner_caps = capabilities.BotGroupCapabilities(
        role="owner", can_manage=True, actions=frozenset({"set_group_kick"})
    )
    probed: list[int] = []

    async def can_manage(_bot: Any, _group_id: int, user_id: int) -> bool:
        probed.append(user_id)
        return user_id == ADMIN_ID

    async def probe(*_args: Any, **_kwargs: Any) -> Any:
        return owner_caps

    monkeypatch.setattr(capabilities, "user_can_manage_group", can_manage)
    monkeypatch.setattr(capabilities, "probe_group_capabilities", probe)
    bot = SimpleNamespace(self_id=str(BOT_ID))

    (
        caps,
        allow_admin,
        names,
        source,
    ) = await dialogue_runtime_support.resolve_turn_tool_capabilities(
        bot,
        1,
        ADMIN_ID,
        "把300踢出群",
        has_reply=False,
        has_mentions=False,
        has_media=False,
    )
    assert source == "explicit_critical_intent"
    assert allow_admin is True
    assert caps is owner_caps
    assert "kick_member" in names

    (
        _caps,
        allow_admin,
        names,
        source,
    ) = await dialogue_runtime_support.resolve_turn_tool_capabilities(
        bot,
        1,
        MEMBER_ID,
        "把300踢出群",
        has_reply=False,
        has_mentions=False,
        has_media=False,
    )
    assert source == "progressive_bootstrap"
    assert allow_admin is False
    assert "kick_member" not in names


# ── 3. actor-vs-target ──────────────────────────────────────


def _owner_bot_fixture() -> tuple[Any, Any, Any]:
    roles = {
        BOT_ID: "owner",
        OWNER_ID: "owner",
        ADMIN_ID: "admin",
        OTHER_ADMIN_ID: "admin",
        MEMBER_ID: "member",
    }

    class Config:
        tool_allowlist = ("kick_member", "set_group_admin", "mute_member")
        tool_day = tools.now_beijing().strftime("%Y-%m-%d")
        admin_tool_count = 0
        admin_tool_daily_limit = 30
        critical_tool_count = 0
        critical_tool_daily_limit = 30

    config = Config()

    class Session:
        async def get(self, _model: object, _key: object) -> Any:
            return config

        async def flush(self) -> None:
            return None

        async def rollback(self) -> None:
            return None

    class Bot:
        self_id = str(BOT_ID)

        def __init__(self) -> None:
            self.calls: list[str] = []

        async def call_api(self, action: str, **params: Any) -> dict[str, Any]:
            if action == "get_group_member_info":
                user_id = int(params["user_id"])
                return {"user_id": user_id, "role": roles[user_id]}
            self.calls.append(action)
            return {}

    return Bot(), Session(), config


@pytest.fixture
def owner_caps(monkeypatch: pytest.MonkeyPatch) -> Any:
    current = capabilities.BotGroupCapabilities(
        role="owner",
        can_manage=True,
        actions=frozenset({"set_group_kick", "set_group_admin", "set_group_ban"}),
    )

    async def probe(*_args: Any, **_kwargs: Any) -> Any:
        return current

    async def no_audit(*_args: Any, **_kwargs: Any) -> None:
        return None

    monkeypatch.setattr(tools, "probe_group_capabilities", probe)
    monkeypatch.setattr(tools, "_audit", no_audit)
    return current


@pytest.mark.asyncio
async def test_admin_actor_cannot_kick_or_mute_other_admin(owner_caps: Any) -> None:
    bot, session, _config = _owner_bot_fixture()

    kick = await tools.execute_tool(
        "kick_member",
        {"user_id": OTHER_ADMIN_ID},
        bot=bot,
        group_id=1,
        actor_user_id=ADMIN_ID,
        session=session,
        capabilities=owner_caps,
        current_intent_text="把他踢出群",
    )
    mute = await tools.execute_tool(
        "mute_member",
        {"user_id": OTHER_ADMIN_ID, "duration": 60},
        bot=bot,
        group_id=1,
        actor_user_id=ADMIN_ID,
        session=session,
        capabilities=owner_caps,
    )
    assert kick["ok"] is False
    assert "调用者无权" in kick["error"]
    assert mute["ok"] is False
    assert "调用者无权" in mute["error"]
    assert bot.calls == []

    member_kick = await tools.execute_tool(
        "kick_member",
        {"user_id": MEMBER_ID},
        bot=bot,
        group_id=1,
        actor_user_id=ADMIN_ID,
        session=session,
        capabilities=owner_caps,
        current_intent_text="把他踢出群",
    )
    owner_kick = await tools.execute_tool(
        "kick_member",
        {"user_id": OTHER_ADMIN_ID},
        bot=bot,
        group_id=1,
        actor_user_id=OWNER_ID,
        session=session,
        capabilities=owner_caps,
        current_intent_text="把他踢出群",
    )
    assert member_kick["ok"] is True
    assert owner_kick["ok"] is True
    assert bot.calls == ["set_group_kick", "set_group_kick"]


@pytest.mark.asyncio
async def test_only_group_owner_can_change_admins(owner_caps: Any) -> None:
    bot, session, _config = _owner_bot_fixture()

    by_admin = await tools.execute_tool(
        "set_group_admin",
        {"user_id": MEMBER_ID, "enable": True},
        bot=bot,
        group_id=1,
        actor_user_id=ADMIN_ID,
        session=session,
        capabilities=owner_caps,
        current_intent_text="把他设为管理员",
    )
    assert by_admin["ok"] is False
    assert "只有群主" in by_admin["error"]
    assert bot.calls == []

    by_owner = await tools.execute_tool(
        "set_group_admin",
        {"user_id": MEMBER_ID, "enable": True},
        bot=bot,
        group_id=1,
        actor_user_id=OWNER_ID,
        session=session,
        capabilities=owner_caps,
        current_intent_text="把他设为管理员",
    )
    assert by_owner["ok"] is True
    assert bot.calls == ["set_group_admin"]


# ── 4. get_recent_group_messages 隐私过滤 ───────────────────


@pytest.mark.asyncio
async def test_recent_group_messages_skip_opted_out_senders(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    current = capabilities.BotGroupCapabilities(
        role="member", can_manage=False, actions=frozenset({"get_group_msg_history"})
    )

    async def no_audit(*_args: Any, **_kwargs: Any) -> None:
        return None

    monkeypatch.setattr(tools, "_audit", no_audit)

    class Result:
        def scalars(self) -> list[int]:
            return [OPTED_OUT_ID]

    class Session:
        async def execute(self, _stmt: object) -> Result:
            return Result()

    class Bot:
        async def call_api(self, action: str, **_params: Any) -> dict[str, Any]:
            assert action == "get_group_msg_history"
            return {
                "messages": [
                    {
                        "message_id": 1,
                        "user_id": OPTED_OUT_ID,
                        "sender": {"nickname": "退出者"},
                        "message": [{"type": "text", "data": {"text": "隐私内容"}}],
                    },
                    {
                        "message_id": 2,
                        "user_id": MEMBER_ID,
                        "sender": {"nickname": "成员"},
                        "message": [{"type": "text", "data": {"text": "公开内容"}}],
                    },
                ]
            }

    result = await tools.execute_tool(
        "get_recent_group_messages",
        {"count": 10},
        bot=Bot(),
        group_id=1,
        session=Session(),
        capabilities=current,
    )

    assert result["ok"] is True
    assert [item["user_id"] for item in result["result"]["items"]] == [MEMBER_ID]
    assert "隐私内容" not in str(result)


# ── 5. 引用链隐私 ───────────────────────────────────────────


def test_reply_chain_from_opted_out_member_is_replaced() -> None:
    normalized = message_parser.NormalizedMessage(
        plain_text="这个呢",
        segments=[],
        reply_chain=[
            {
                "message_id": 9,
                "user_id": OPTED_OUT_ID,
                "nickname": "退出者",
                "text": "隐私原文",
                "media_types": ["image"],
            },
            {"message_id": 8, "user_id": MEMBER_ID, "nickname": "成员", "text": "ok"},
        ],
        media_refs=[
            {"type": "image", "source": "reply", "source_user_id": OPTED_OUT_ID},
            {"type": "image", "source": "current"},
        ],
    )

    assert message_parser.redact_opted_out_replies(normalized, {OPTED_OUT_ID})
    first = normalized.reply_chain[0]
    assert first["message_id"] == 9
    assert first["user_id"] is None
    assert "隐私原文" not in normalized.prompt_text()
    assert "退出者" not in normalized.prompt_text()
    assert normalized.reply_chain[1]["text"] == "ok"
    assert normalized.media_refs == [{"type": "image", "source": "current"}]


@pytest.mark.asyncio
async def test_member_delete_redacts_other_rows_reply_chain() -> None:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    tables = [
        config_models.GroupAgentConfig.__table__,
        message_models.GroupAgentMessage.__table__,
        models.AgentMemory.__table__,
        models.AgentRelation.__table__,
        models.AgentPrivacy.__table__,
    ]
    async with engine.begin() as connection:
        await connection.run_sync(
            lambda sync_connection: models.AgentMemory.metadata.create_all(
                sync_connection, tables=tables
            )
        )
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    async with session_factory() as session:
        session.add(config_models.GroupAgentConfig(group_id=100))
        session.add(
            message_models.GroupAgentMessage(
                bot_id=9,
                message_id=2,
                group_id=100,
                user_id=MEMBER_ID,
                normalized_text="回复一下",
                reply_chain=[
                    {
                        "message_id": 1,
                        "user_id": OPTED_OUT_ID,
                        "nickname": "退出者",
                        "text": "隐私原文",
                        "media_types": ["image"],
                    }
                ],
                media_refs=[
                    {"type": "image", "source": "reply", "source_user_id": OPTED_OUT_ID}
                ],
            )
        )
        await session.commit()

        await memory.delete_member_memories(session, 100, OPTED_OUT_ID)

        row = (
            await session.execute(select(message_models.GroupAgentMessage))
        ).scalar_one()
        await session.refresh(row)
        assert "隐私原文" not in str(row.reply_chain)
        assert "退出者" not in str(row.reply_chain)
        assert row.reply_chain[0]["message_id"] == 1
        assert row.media_refs == []
    await engine.dispose()


# ── 6. 工具批次提交失败 ─────────────────────────────────────


@pytest.mark.asyncio
async def test_commit_tool_batch_failure_reloads_config_and_returns_false() -> None:
    class Session:
        def __init__(self) -> None:
            self.rollbacks = 0
            self.refreshes = 0

        async def commit(self) -> None:
            raise OperationalError("COMMIT", {}, Exception("locked"))

        async def rollback(self) -> None:
            self.rollbacks += 1

        async def refresh(self, _config: object) -> None:
            self.refreshes += 1

    session = Session()
    ok = await dialogue_runtime_support.commit_tool_batch(
        session, SimpleNamespace(), 1, 1, ["mute_member"]
    )

    assert ok is False
    assert session.rollbacks == 1
    assert session.refreshes == 1


@pytest.mark.asyncio
async def test_commit_failure_abort_sends_notice_only_when_nothing_sent() -> None:
    sent: list[str] = []

    async def fake_send(
        _bot: Any, _group_id: int, message: str, _enqueued: Any, **_kwargs: Any
    ) -> None:
        sent.append(message)

    await dialogue_runtime_support.abort_after_commit_failure(
        fake_send, "收尾", object(), 1, enqueued_at=None, message_id=5, sent=True
    )
    assert sent == []
    await dialogue_runtime_support.abort_after_commit_failure(
        fake_send, "收尾", object(), 1, enqueued_at=None, message_id=5, sent=False
    )
    assert sent == ["收尾"]
