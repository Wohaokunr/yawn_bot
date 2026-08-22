# ruff: noqa: E501,F401,I001,TID252,PLR0912,PLR0913,PLR0917,C901,TRY003,TRY004,TRY300,TRY301,ASYNC240,UP035,PGH004,ANN001,ANN201,ANN202,ARG001,FBT001,FBT002,COM812,RUF001,RUF100,TC002,PLR2004,RUF022
"""群聊 Agent 配置和记忆管理命令。"""

import json

from nonebot import logger
from nonebot.adapters.onebot.v11 import GroupMessageEvent, Message
from nonebot.params import CommandArg
from nonebot.plugin import on_command
from nonebot_plugin_orm import async_scoped_session
from sqlalchemy import select

from ..permission import is_group_admin, require_feature
from ..data_models.agent_memory import AgentMemory, AgentPrivacy
from ..data_models.group_agent_config import GroupAgentConfig
from .context import now_beijing
from .log import dbg
from .memory import delete_group_memories, delete_member_memories, list_memories
from .persona import PERSONA_FIELDS, parse_persona_assignments, resolve_persona

# 命令元数据登记在包级 __init__.__plugin_meta__（help_panel 只扫描包级）。

agent_command = on_command("群聊Agent", aliases={"群AI"}, priority=5, block=True)
agent_settings = on_command("Agent设置", priority=5, block=True)
agent_status = on_command("Agent状态", priority=5, block=True)
agent_memory = on_command("Agent记忆", priority=5, block=True)
agent_profile = on_command("Agent画像", priority=5, block=True)
agent_clear = on_command("Agent清理", priority=5, block=True)
agent_export = on_command("Agent导出", priority=5, block=True)
agent_persona = on_command("Agent人设", priority=5, block=True)
agent_privacy = on_command("Agent隐私", priority=5, block=True)


async def _get_config(session: async_scoped_session, group_id: int) -> GroupAgentConfig:
    config = await session.get(GroupAgentConfig, group_id)
    if config is None:
        config = GroupAgentConfig(group_id=group_id)
        session.add(config)
        await session.flush()
    return config


@agent_command.handle()
async def handle_agent_command(
    event: GroupMessageEvent,
    session: async_scoped_session,
    args: Message = CommandArg(),
    _perm: None = require_feature("group_agent"),  # pyright: ignore[reportArgumentType]
) -> None:
    if not isinstance(event, GroupMessageEvent):
        await agent_command.finish("请在群聊中使用")
    dbg(
        f"群 {event.group_id} 命令 /群聊Agent: user={event.get_user_id()} "
        f"args={args.extract_plain_text()!r}"
    )
    if not is_group_admin(event):
        dbg(f"群 {event.group_id} /群聊Agent 拒绝: 非群管理")
        await agent_command.finish("群聊 Agent 仅限群主或管理员管理")
    config = await _get_config(session, int(event.group_id))
    text = args.extract_plain_text().strip()
    if text in {"开", "开启", "on"}:
        config.enabled = True
        await session.commit()
        dbg(f"群 {event.group_id} Agent 已开启")
        await agent_command.finish("群聊 Agent 已开启")
    if text in {"关", "关闭", "off"}:
        config.enabled = False
        await session.commit()
        dbg(f"群 {event.group_id} Agent 已关闭")
        await agent_command.finish("群聊 Agent 已关闭")
    dbg(f"群 {event.group_id} /群聊Agent 查询状态: enabled={config.enabled}")
    await agent_command.finish(
        f"群聊 Agent：{'开启' if config.enabled else '关闭'}\n主动概率：{config.proactive_probability:.0%}\n冷场阈值：{config.idle_threshold_minutes} 分钟\n每日上限：{config.daily_limit}"
    )


@agent_settings.handle()
async def handle_agent_settings(
    event: GroupMessageEvent,
    session: async_scoped_session,
    args: Message = CommandArg(),
    _perm: None = require_feature("group_agent"),  # pyright: ignore[reportArgumentType]
) -> None:
    if not isinstance(event, GroupMessageEvent):
        await agent_settings.finish("请在群聊中使用")
    dbg(
        f"群 {event.group_id} 命令 /Agent设置: user={event.get_user_id()} "
        f"args={args.extract_plain_text()!r}"
    )
    if not is_group_admin(event):
        dbg(f"群 {event.group_id} /Agent设置 拒绝: 非群管理")
        await agent_settings.finish("群聊 Agent 设置仅限群主或管理员")
    config = await _get_config(session, int(event.group_id))
    parts = args.extract_plain_text().split()
    if len(parts) == 2 and parts[0] in {"概率", "probability"}:
        try:
            config.proactive_probability = max(0.0, min(float(parts[1]), 1.0))
        except ValueError:
            dbg(f"群 {event.group_id} /Agent设置 概率参数非法: {parts[1]!r}")
            await agent_settings.finish("概率需要 0 到 1 之间的数字")
        await session.commit()
        dbg(f"群 {event.group_id} 主动概率已更新为 {config.proactive_probability}")
        await agent_settings.finish("主动概率已更新")
    if len(parts) == 2 and parts[0] in {"媒体缓存", "media_cache"}:
        if parts[1].lower() in {"开", "开启", "on", "true"}:
            config.media_cache_enabled = True
            cache_enabled = True
        elif parts[1].lower() in {"关", "关闭", "off", "false"}:
            config.media_cache_enabled = False
            cache_enabled = False
        else:
            dbg(f"群 {event.group_id} /Agent设置 媒体缓存参数非法: {parts[1]!r}")
            await agent_settings.finish("媒体缓存参数需要 开 或 关")
        await session.commit()
        dbg(f"群 {event.group_id} 媒体缓存已{'开启' if cache_enabled else '关闭'}")
        # 提交后 config 属性已过期，回复文案用提交前的本地值。
        await agent_settings.finish(f"媒体缓存已{'开启' if cache_enabled else '关闭'}")
    dbg(f"群 {event.group_id} /Agent设置 参数无法识别,返回用法")
    await agent_settings.finish("用法：/Agent设置 概率 0.15；/Agent设置 媒体缓存 开|关")


@agent_memory.handle()
async def handle_agent_memory(
    event: GroupMessageEvent,
    session: async_scoped_session,
    _perm: None = require_feature("group_agent"),  # pyright: ignore[reportArgumentType]
) -> None:
    if not isinstance(event, GroupMessageEvent):
        await agent_memory.finish("请在群聊中使用")
    dbg(f"群 {event.group_id} 命令 /Agent记忆: user={event.get_user_id()}")
    rows = await list_memories(session, int(event.group_id))
    dbg(f"群 {event.group_id} /Agent记忆 查询到 {len(rows)} 条记忆")
    if not rows:
        await agent_memory.finish("当前群还没有沉淀记忆")
    await agent_memory.finish(
        "\n".join(f"- {row.memory_key}: {row.content[:120]}" for row in rows)
    )


@agent_status.handle()
async def handle_agent_status(
    event: GroupMessageEvent,
    session: async_scoped_session,
    _perm: None = require_feature("group_agent"),  # pyright: ignore[reportArgumentType]
) -> None:
    if not isinstance(event, GroupMessageEvent):
        await agent_status.finish("请在群聊中使用")
    dbg(f"群 {event.group_id} 命令 /Agent状态: user={event.get_user_id()}")
    config = await _get_config(session, int(event.group_id))
    dbg(
        f"群 {event.group_id} /Agent状态: enabled={config.enabled} "
        f"trigger_mode={config.trigger_mode!r} probability={config.proactive_probability} "
        f"media_cache={config.media_cache_enabled}"
    )
    await agent_status.finish(
        f"群聊 Agent：{'开启' if config.enabled else '关闭'}；触发：{config.trigger_mode}；主动概率：{config.proactive_probability:.0%}；媒体缓存：{'开' if config.media_cache_enabled else '关'}"
    )


@agent_profile.handle()
async def handle_agent_profile(
    event: GroupMessageEvent,
    session: async_scoped_session,
    args: Message = CommandArg(),
    _perm: None = require_feature("group_agent"),  # pyright: ignore[reportArgumentType]
) -> None:
    if not isinstance(event, GroupMessageEvent):
        await agent_profile.finish("请在群聊中使用")
    dbg(
        f"群 {event.group_id} 命令 /Agent画像: user={event.get_user_id()} "
        f"args={args.extract_plain_text()!r}"
    )
    raw_user_id = args.extract_plain_text().strip()
    if raw_user_id and not raw_user_id.isdigit():
        await agent_profile.finish(
            "Agent画像 需要 QQ 号作为参数，例如 /Agent画像 12345"
        )
    user_id = int(raw_user_id) if raw_user_id.isdigit() else int(event.get_user_id())
    now = now_beijing()
    stmt = (
        select(AgentMemory)
        .where(
            AgentMemory.group_id == int(event.group_id),
            AgentMemory.subject_user_id == user_id,
            AgentMemory.memory_type == "profile",
            AgentMemory.visibility.in_(("group", "public")),
            AgentMemory.expires_at.is_(None) | (AgentMemory.expires_at >= now),
        )
        .order_by(AgentMemory.salience.desc())
        .limit(10)
    )
    rows = (await session.execute(stmt)).scalars().all()
    dbg(f"群 {event.group_id} /Agent画像 查询成员 {user_id}: {len(rows)} 条画像")
    if not rows:
        await agent_profile.finish("暂无该成员的人物画像")
    await agent_profile.finish(
        "\n".join(f"- {row.memory_key}: {row.content[:160]}" for row in rows)
    )


@agent_clear.handle()
async def handle_agent_clear(
    event: GroupMessageEvent,
    session: async_scoped_session,
    _perm: None = require_feature("group_agent"),  # pyright: ignore[reportArgumentType]
) -> None:
    if not isinstance(event, GroupMessageEvent):
        await agent_clear.finish("请在群聊中使用")
    dbg(f"群 {event.group_id} 命令 /Agent清理: user={event.get_user_id()}")
    if not is_group_admin(event):
        dbg(f"群 {event.group_id} /Agent清理 拒绝: 非群管理")
        await agent_clear.finish("清理群记忆仅限群主或管理员")
    count = await delete_group_memories(session, int(event.group_id))
    logger.info("群 %s 清理 Agent 记忆 %s 条", event.group_id, count)
    dbg(f"群 {event.group_id} /Agent清理 完成: 共清除 {count} 条记录")
    await agent_clear.finish(f"已清理 {count} 条群聊 Agent 记忆")


@agent_export.handle()
async def handle_agent_export(
    event: GroupMessageEvent,
    session: async_scoped_session,
    _perm: None = require_feature("group_agent"),  # pyright: ignore[reportArgumentType]
) -> None:
    if not isinstance(event, GroupMessageEvent):
        await agent_export.finish("请在群聊中使用")
    dbg(f"群 {event.group_id} 命令 /Agent导出: user={event.get_user_id()}")
    if not is_group_admin(event):
        dbg(f"群 {event.group_id} /Agent导出 拒绝: 非群管理")
        await agent_export.finish("导出群聊 Agent 数据仅限群主或管理员")
    now = now_beijing()
    stmt = (
        select(AgentMemory)
        .where(
            AgentMemory.group_id == int(event.group_id),
            AgentMemory.visibility.in_(("group", "public")),
            AgentMemory.expires_at.is_(None) | (AgentMemory.expires_at >= now),
        )
        .order_by(AgentMemory.id.asc())
        .limit(200)
    )
    rows = (await session.execute(stmt)).scalars().all()
    payload = [
        {
            "type": row.memory_type,
            "key": row.memory_key,
            "subject_user_id": row.subject_user_id,
            "content": row.content,
            "visibility": row.visibility,
        }
        for row in rows
    ]
    text = json.dumps(
        {"group_id": int(event.group_id), "memories": payload}, ensure_ascii=False
    )
    dbg(
        f"群 {event.group_id} /Agent导出: 记忆 {len(payload)} 条, JSON {len(text)} 字节"
    )
    # 群消息有单条长度上限，超长的导出分块发送，避免静默失败。
    chunks = [text[i : i + 3000] for i in range(0, len(text), 3000)] or ["{}"]
    for chunk in chunks[:-1]:
        await agent_export.send(Message(chunk))
    await agent_export.finish(Message(chunks[-1]))


@agent_persona.handle()
async def handle_agent_persona(
    event: GroupMessageEvent,
    session: async_scoped_session,
    args: Message = CommandArg(),
    _perm: None = require_feature("group_agent"),  # pyright: ignore[reportArgumentType]
) -> None:
    if not isinstance(event, GroupMessageEvent):
        await agent_persona.finish("请在群聊中使用")
    dbg(
        f"群 {event.group_id} 命令 /Agent人设: user={event.get_user_id()} "
        f"args={args.extract_plain_text()!r}"
    )
    if not is_group_admin(event):
        dbg(f"群 {event.group_id} /Agent人设 拒绝: 非群管理")
        await agent_persona.finish("Agent 人设仅限群主或管理员管理")
    config = await _get_config(session, int(event.group_id))
    parts = args.extract_plain_text().strip().split()
    action = parts[0].lower() if parts else "查看"
    if action in {"查看", "show"}:
        await agent_persona.finish(
            "\n".join(
                f"{key}={value}" for key, value in resolve_persona(config).items()
            )
        )
    if action in {"示例", "example"}:
        await agent_persona.finish(
            "/Agent人设 设置 name=Yawn tone=温和 style=短句为主 response_length=通常1到3句"
        )
    if action in {"重置", "reset"}:
        config.persona_override = {}
        config.persona_enabled = True
        config.persona_version += 1
        await session.commit()
        dbg(f"群 {event.group_id} 人设已重置,version={config.persona_version}")
        await agent_persona.finish("已重置为全局默认人设")
    if action in {"设置", "set"}:
        try:
            updates = parse_persona_assignments(parts[1:])
        except ValueError as exc:
            dbg(f"群 {event.group_id} /Agent人设 解析失败: {exc}")
            await agent_persona.finish(str(exc))
        override = dict(config.persona_override or {})
        override.update(updates)
        config.persona_override = {
            key: override[key] for key in PERSONA_FIELDS if key in override
        }
        config.persona_enabled = True
        config.persona_version += 1
        await session.commit()
        dbg(
            f"群 {event.group_id} 人设已更新: fields={sorted(updates)} "
            f"version={config.persona_version}"
        )
        await agent_persona.finish("群级人设已更新")
    await agent_persona.finish("用法：/Agent人设 查看|设置 key=value ...|重置|示例")


@agent_privacy.handle()
async def handle_agent_privacy(
    event: GroupMessageEvent,
    session: async_scoped_session,
    args: Message = CommandArg(),
    _perm: None = require_feature("group_agent"),  # pyright: ignore[reportArgumentType]
) -> None:
    if not isinstance(event, GroupMessageEvent):
        await agent_privacy.finish("请在群聊中使用")
    action = args.extract_plain_text().strip().lower()
    dbg(
        f"群 {event.group_id} 命令 /Agent隐私: user={event.get_user_id()} action={action!r}"
    )
    if action not in {"退出", "optout", "off", "恢复", "optin", "on"}:
        await agent_privacy.finish("用法：/Agent隐私 退出|恢复")
    privacy = await session.get(
        AgentPrivacy, (int(event.group_id), int(event.get_user_id()))
    )
    if privacy is None:
        privacy = AgentPrivacy(
            group_id=int(event.group_id), user_id=int(event.get_user_id())
        )
        session.add(privacy)
    if action in {"退出", "optout", "off"}:
        privacy.opted_out = True
        await delete_member_memories(
            session, int(event.group_id), int(event.get_user_id())
        )
        await session.commit()
        dbg(f"群 {event.group_id} 用户 {event.get_user_id()} 已隐私退出并清除其记忆")
        await agent_privacy.finish("已退出本群 Agent 记忆；后续消息不会被保存")
    privacy.opted_out = False
    await session.commit()
    dbg(f"群 {event.group_id} 用户 {event.get_user_id()} 已恢复 Agent 记忆")
    await agent_privacy.finish("已恢复本群 Agent 记忆")


__all__ = [
    "agent_command",
    "agent_settings",
    "agent_status",
    "agent_memory",
    "agent_profile",
    "agent_clear",
    "agent_export",
    "agent_persona",
    "agent_privacy",
]
