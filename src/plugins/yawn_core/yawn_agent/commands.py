# ruff: noqa: E501,F401,I001,TID252,PLR0911,PLR0912,PLR0913,PLR0915,PLR0917,C901,TRY003,TRY004,TRY300,TRY301,ASYNC240,UP035,PGH004,ANN001,ANN201,ANN202,ARG001,FBT001,FBT002,COM812,RUF001,RUF100,TC002,PLR2004,RUF022
"""群聊 Agent 配置和记忆管理命令。"""

import json
import math

from nonebot import logger
from nonebot.adapters.onebot.v11 import GroupMessageEvent, Message
from nonebot.matcher import Matcher
from nonebot.params import ArgPlainText, CommandArg
from nonebot_plugin_orm import async_scoped_session
from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError

from ..command_definition import build_matcher  # noqa: TID252
from ..command_ux import (
    permission_required,
    scope_required,
    temporary_failure,
    validation_failed,
)
from .command_definitions import COMMAND_BY_NAME
from ..permission import is_group_admin, require_feature
from ..session_interaction import (
    SessionChoice,
    SessionIntent,
    confirmation_matches,
    format_change_preview,
    is_cancel,
    pass_through_new_command,
    parse_toggle,
    resolve_choice,
    resolve_session_intent,
)
from ..data_models.agent_memory import AgentMemory, AgentPrivacy
from ..data_models.group_agent_config import GroupAgentConfig
from .config_store import (
    agent_runtime_enabled,
    get_or_create_config,
    set_agent_runtime_enabled,
)
from .conversation import close_group_conversations
from .context import now_beijing
from .log import dbg
from .memory import delete_group_memories, delete_member_memories, list_memories
from .persona import (
    MAX_FIELD_LENGTH,
    PERSONA_FIELDS,
    parse_persona_assignments,
    resolve_persona,
)

# 命令元数据由包级 __init__ 注册到 command_catalog。

agent_command = build_matcher(COMMAND_BY_NAME["群聊Agent"])
agent_settings = build_matcher(COMMAND_BY_NAME["Agent设置"])
agent_status = build_matcher(COMMAND_BY_NAME["Agent状态"])
agent_memory = build_matcher(COMMAND_BY_NAME["Agent记忆"])
agent_profile = build_matcher(COMMAND_BY_NAME["Agent画像"])
agent_clear = build_matcher(COMMAND_BY_NAME["Agent清理"])
agent_export = build_matcher(COMMAND_BY_NAME["Agent导出"])
agent_persona = build_matcher(COMMAND_BY_NAME["Agent人设"])
agent_privacy = build_matcher(COMMAND_BY_NAME["Agent隐私"])

# /Agent设置 的交互字段。业务边界留在命令模块，共享 helper 只负责会话输入约定。
_AGENT_SETTING_CHOICES = (
    SessionChoice(
        "explicit_wakeup_enabled",
        "叫名唤醒",
        ("叫名", "唤醒", "wake"),
    ),
    SessionChoice(
        "reply_trigger_enabled",
        "回复触发",
        ("回复", "回复bot", "reply"),
    ),
    SessionChoice(
        "short_conversation_enabled",
        "自然续聊",
        ("续聊", "短会话", "short_conversation"),
    ),
    SessionChoice(
        "proactive_enabled",
        "主动参与",
        ("主动", "参与", "proactive"),
    ),
    SessionChoice(
        "participation_intensity",
        "参与强度",
        ("强度", "积极程度", "intensity"),
    ),
    SessionChoice(
        "proactive_active_enabled",
        "活跃聊天加入",
        ("插话", "活跃参与", "active"),
    ),
    SessionChoice(
        "idle_threshold_minutes",
        "冷场判定",
        ("冷场", "idle", "idle_threshold"),
    ),
    SessionChoice(
        "cooldown_minutes",
        "参与冷却",
        ("冷却", "cooldown"),
    ),
    SessionChoice(
        "daily_limit",
        "每日参与上限",
        ("日上限", "daily"),
    ),
    SessionChoice(
        "media_cache_enabled",
        "媒体缓存",
        ("media_cache",),
    ),
)
# Kept for direct advanced commands and backward compatibility, but hidden from the menu.
_ADVANCED_AGENT_SETTING_CHOICES = (
    SessionChoice(
        "proactive_probability",
        "暖场基础概率",
        ("暖场概率", "probability"),
    ),
    SessionChoice(
        "proactive_active_probability",
        "加入话题概率",
        ("插话概率", "active_probability"),
    ),
    SessionChoice(
        "proactive_active_window_minutes",
        "活跃话题窗口",
        ("活跃窗口", "active_window"),
    ),
)
_ALL_AGENT_SETTING_CHOICES = _AGENT_SETTING_CHOICES + _ADVANCED_AGENT_SETTING_CHOICES
_AGENT_SETTING_LABELS = {choice.key: choice.label for choice in _ALL_AGENT_SETTING_CHOICES}
_BOOL_AGENT_SETTINGS = {
    "explicit_wakeup_enabled",
    "reply_trigger_enabled",
    "short_conversation_enabled",
    "proactive_enabled",
    "proactive_active_enabled",
    "media_cache_enabled",
}
_FLOAT_AGENT_SETTINGS = {
    "proactive_probability": (0.0, 1.0),
    "proactive_active_probability": (0.0, 1.0),
}
_INT_AGENT_SETTINGS = {
    "idle_threshold_minutes": (0, 10080),
    "cooldown_minutes": (0, 10080),
    "daily_limit": (0, 10000),
    "proactive_active_window_minutes": (1, 1440),
}
_PARTICIPATION_PRESETS = {
    "克制": (0.18, 0.10),
    "平衡": (0.35, 0.25),
    "活跃": (0.55, 0.45),
}

_PERSONA_CHOICES = (
    SessionChoice("name", "名称", ("名字",)),
    SessionChoice("identity", "身份定位", ("身份",)),
    SessionChoice("role", "群内角色", ("角色",)),
    SessionChoice("tone", "语气", ()),
    SessionChoice("speech_style", "表达风格", ("风格", "style")),
    SessionChoice("values", "价值观", ()),
    SessionChoice("knowledge_boundary", "知识边界", ("知识",)),
    SessionChoice("emotion_baseline", "情绪基线", ("情绪",)),
    SessionChoice("response_length", "回复长度", ("长度", "length")),
    SessionChoice("privacy_boundary", "隐私边界", ("隐私",)),
    SessionChoice("reset", "重置为全局默认", ("重置", "reset")),
)
_PERSONA_LABELS = {choice.key: choice.label for choice in _PERSONA_CHOICES}
_PERSONA_CONFIRM_PHRASE = "确认保存"
_PRIVACY_CONFIRM_PHRASE = "确认退出并删除"
_CLEAR_CONFIRM_PHRASE = "确认清理"


def _participation_intensity(config: GroupAgentConfig) -> str:
    warmup = float(config.proactive_probability)
    interject = float(config.proactive_active_probability)
    for label, (expected_warmup, expected_interject) in _PARTICIPATION_PRESETS.items():
        if (
            abs(warmup - expected_warmup) < 0.001
            and abs(interject - expected_interject) < 0.001
        ):
            return label
    return "自定义"


def _get_agent_setting_value(
    config: GroupAgentConfig, key: str
) -> bool | float | int | str:
    if key == "participation_intensity":
        return _participation_intensity(config)
    return getattr(config, key)


def _set_agent_setting_value(
    config: GroupAgentConfig, key: str, value: bool | float | str
) -> None:
    if key == "participation_intensity":
        warmup, interject = _PARTICIPATION_PRESETS[str(value)]
        config.proactive_probability = warmup
        config.proactive_active_probability = interject
        return
    setattr(config, key, value)


def _format_agent_setting_value(
    key: str, value: bool | float | str
) -> str:
    if key in _BOOL_AGENT_SETTINGS:
        return "开启" if bool(value) else "关闭"
    if key == "participation_intensity":
        return str(value)
    if key in _FLOAT_AGENT_SETTINGS:
        return f"{float(value):.0%}"
    if key in {
        "idle_threshold_minutes",
        "cooldown_minutes",
        "proactive_active_window_minutes",
    }:
        return f"{int(value)} 分钟"
    if key == "daily_limit":
        return f"{int(value)} 次/日"
    return str(value)


def _parse_agent_setting_value(key: str, raw: str) -> bool | float | int | str:
    if key in _BOOL_AGENT_SETTINGS:
        parsed = parse_toggle(raw)
        if parsed is None:
            raise ValueError("请输入 开 或 关")
        return parsed
    if key == "participation_intensity":
        aliases = {
            "克制": "克制",
            "低": "克制",
            "low": "克制",
            "restrained": "克制",
            "平衡": "平衡",
            "中": "平衡",
            "medium": "平衡",
            "balanced": "平衡",
            "活跃": "活跃",
            "高": "活跃",
            "high": "活跃",
            "active": "活跃",
        }
        value = aliases.get(raw.strip().casefold())
        if value is None:
            raise ValueError("请输入 克制、平衡 或 活跃")
        return value
    if key in _FLOAT_AGENT_SETTINGS:
        try:
            value = float(raw)
        except ValueError as exc:
            raise ValueError("请输入 0 到 1 之间的数字，例如 0.35") from exc
        if not math.isfinite(value):
            raise ValueError("请输入有限数字")
        minimum, maximum = _FLOAT_AGENT_SETTINGS[key]
        if not minimum <= value <= maximum:
            raise ValueError(f"请输入 {minimum:g} 到 {maximum:g} 之间的数字")
        return value
    if key in _INT_AGENT_SETTINGS:
        try:
            value = int(raw)
        except ValueError as exc:
            raise ValueError("请输入整数") from exc
        minimum, maximum = _INT_AGENT_SETTINGS[key]
        if not minimum <= value <= maximum:
            raise ValueError(f"请输入 {minimum} 到 {maximum} 之间的整数")
        return value
    raise ValueError("不支持的设置项")


def _agent_setting_value_prompt(key: str) -> str:
    if key in _BOOL_AGENT_SETTINGS:
        return "请输入 开 或 关"
    if key == "participation_intensity":
        return "请输入 克制、平衡 或 活跃"
    if key in _FLOAT_AGENT_SETTINGS:
        return "请输入 0 到 1 之间的数字，例如 0.35"
    if key in {
        "idle_threshold_minutes",
        "cooldown_minutes",
        "proactive_active_window_minutes",
    }:
        return "请输入分钟数"
    if key == "daily_limit":
        return "请输入每日次数上限（0-10000）"
    return "请输入新的值"


def _build_agent_settings_menu(config: GroupAgentConfig) -> str:
    lines = [
        "Agent 设置",
        "@ Agent 始终属于明确呼叫；下面只调整额外的参与能力。",
    ]
    for index, choice in enumerate(_AGENT_SETTING_CHOICES, start=1):
        value = _get_agent_setting_value(config, choice.key)
        lines.append(
            f"{index}. {choice.label}：{_format_agent_setting_value(choice.key, value)}"
        )
    lines.extend(
        (
            "",
            "回复序号或名称选择；「菜单」重新显示，「取消」退出。",
        )
    )
    return "\n".join(lines)


def _format_agent_runtime_summary(
    config: GroupAgentConfig, runtime_enabled: bool
) -> str:
    now = now_beijing()
    proactive_today = (
        int(config.proactive_count)
        if config.proactive_day == now.strftime("%Y-%m-%d")
        else 0
    )
    active_participation = (
        "活跃聊天也可加入" if config.proactive_active_enabled else "仅在冷场时参与"
    )
    return (
        f"群聊 Agent：{'开启' if runtime_enabled else '关闭'}\n"
        f"明确呼叫：@ 始终响应；回复 Agent：{'开' if config.reply_trigger_enabled else '关'}；"
        f"叫名唤醒：{'开' if config.explicit_wakeup_enabled else '关'}\n"
        f"自然续聊：{'开' if config.short_conversation_enabled else '关'}；"
        f"主动参与：{'开' if config.proactive_enabled else '关'}"
        f"{f'（{active_participation}）' if config.proactive_enabled else ''}\n"
        f"参与强度：{_participation_intensity(config)}；"
        f"今日主动：{proactive_today}/{config.daily_limit}；"
        f"参与冷却：{config.cooldown_minutes} 分钟"
    )


def _build_persona_text(config: GroupAgentConfig) -> str:
    persona = resolve_persona(config)
    lines = ["Agent 人设", "当前生效内容："]
    for choice in _PERSONA_CHOICES:
        if choice.key == "reset":
            continue
        lines.append(f"- {choice.label}：{persona[choice.key]}")
    return "\n".join(lines)


def _build_persona_menu(config: GroupAgentConfig) -> str:
    lines = [_build_persona_text(config), "", "选择要编辑的字段："]
    for index, choice in enumerate(_PERSONA_CHOICES, start=1):
        lines.append(f"{index}. {choice.label}")
    lines.append("回复序号或字段名称；「菜单」重新显示，「取消」退出。")
    return "\n".join(lines)


async def _commit(session: async_scoped_session) -> bool:
    """提交命令产生的状态变更；失败时回滚并提示稍后重试。"""

    try:
        await session.commit()
    except SQLAlchemyError:
        # SQLite busy 等瞬时错误不能上抛毒化 NoneBot 处理器。
        logger.warning("群聊 Agent 命令状态提交失败,已回滚")
        await session.rollback()
        return False
    return True


@agent_command.handle()
async def handle_agent_command(
    event: GroupMessageEvent,
    session: async_scoped_session,
    args: Message = CommandArg(),
) -> None:
    if not isinstance(event, GroupMessageEvent):
        await agent_command.finish(
            scope_required("群聊 Agent 管理", "群聊", "请在目标群发送 /群聊Agent")
        )
    dbg(
        f"群 {event.group_id} 命令 /群聊Agent: user={event.get_user_id()} "
        f"args={args.extract_plain_text()!r}"
    )
    if not is_group_admin(event):
        dbg(f"群 {event.group_id} /群聊Agent 拒绝: 非群管理")
        await agent_command.finish(
            permission_required(
                "群聊 Agent 管理", "群主或群管理员", "请群管理员执行此操作"
            )
        )
    config = await get_or_create_config(session, int(event.group_id))
    if config is None:
        # 并发创建竞态的输方在对方事务未提交时可能读到空。
        await agent_command.finish("Agent 配置暂时不可用，请稍后重试")
    text = args.extract_plain_text().strip()
    if text in {"开", "开启", "on"}:
        await set_agent_runtime_enabled(
            session, int(event.group_id), enabled=True, config=config
        )
        if not await _commit(session):
            await agent_command.finish("操作失败，请稍后重试")
        dbg(f"群 {event.group_id} Agent 已开启")
        await agent_command.finish("群聊 Agent 已开启")
    if text in {"关", "关闭", "off"}:
        await set_agent_runtime_enabled(
            session, int(event.group_id), enabled=False, config=config
        )
        if not await _commit(session):
            await agent_command.finish("操作失败，请稍后重试")
        close_group_conversations(int(event.group_id), reason="命令关闭 Agent 总开关")
        dbg(f"群 {event.group_id} Agent 已关闭")
        await agent_command.finish("群聊 Agent 已关闭")
    runtime_enabled = await agent_runtime_enabled(
        session, int(event.group_id), config=config
    )
    dbg(f"群 {event.group_id} /群聊Agent 查询状态: enabled={runtime_enabled}")
    await agent_command.finish(_format_agent_runtime_summary(config, runtime_enabled))


@agent_settings.handle()
async def handle_agent_settings(
    event: GroupMessageEvent,
    matcher: Matcher,
    session: async_scoped_session,
    args: Message = CommandArg(),
    _perm: None = require_feature("group_agent"),  # pyright: ignore[reportArgumentType]
) -> None:
    if not isinstance(event, GroupMessageEvent):
        await agent_settings.finish(
            scope_required("Agent 设置", "群聊", "请在目标群发送 /Agent设置")
        )
    dbg(
        f"群 {event.group_id} 命令 /Agent设置: user={event.get_user_id()} "
        f"args={args.extract_plain_text()!r}"
    )
    if not is_group_admin(event):
        dbg(f"群 {event.group_id} /Agent设置 拒绝: 非群管理")
        await agent_settings.finish(
            permission_required(
                "Agent 设置", "群主或群管理员", "请群管理员执行此操作"
            )
        )
    config = await get_or_create_config(session, int(event.group_id))
    if config is None:
        await agent_settings.finish("Agent 配置暂时不可用，请稍后重试")

    text = args.extract_plain_text().strip()
    parts = text.split()
    if len(parts) == 2:
        key = resolve_choice(parts[0], _ALL_AGENT_SETTING_CHOICES)
        if key is not None:
            try:
                value = _parse_agent_setting_value(key, parts[1])
            except ValueError as exc:
                await agent_settings.finish(str(exc))
            before = _get_agent_setting_value(config, key)
            _set_agent_setting_value(config, key, value)
            config.updated_by = int(event.get_user_id())
            before_text = _format_agent_setting_value(key, before)
            after = _get_agent_setting_value(config, key)
            after_text = _format_agent_setting_value(key, after)
            if not await _commit(session):
                await agent_settings.finish("操作失败，请稍后重试")
            dbg(
                f"群 {event.group_id} /Agent设置 快捷设置 "
                f"{key}={value!r} by={event.get_user_id()}"
            )
            await agent_settings.finish(
                format_change_preview(
                    _AGENT_SETTING_LABELS[key], before_text, after_text
                )
                + "\n已保存。"
            )

    if not text:
        matcher.state["agent_settings_step"] = "select"
        await agent_settings.send(_build_agent_settings_menu(config))
        return

    key = resolve_choice(text, _ALL_AGENT_SETTING_CHOICES)
    if key is not None:
        matcher.state["agent_settings_step"] = "value"
        matcher.state["agent_settings_key"] = key
        current = _format_agent_setting_value(
            key, _get_agent_setting_value(config, key)
        )
        await agent_settings.send(
            f"{_AGENT_SETTING_LABELS[key]}\n当前：{current}\n"
            f"{_agent_setting_value_prompt(key)}；发送「返回」回菜单，「取消」退出。"
        )
        return

    await agent_settings.finish(
        "快捷用法：/Agent设置 主动参与 开；/Agent设置 强度 平衡；"
        "/Agent设置 叫名 开；/Agent设置 冷却 8\n"
        "不带参数可进入交互设置。"
    )


@agent_settings.got("agent_settings_input")
async def handle_agent_settings_input(
    event: GroupMessageEvent,
    matcher: Matcher,
    session: async_scoped_session,
    choice: str = ArgPlainText("agent_settings_input"),
) -> None:
    intent = resolve_session_intent(choice)
    if intent is SessionIntent.NEW_COMMAND:
        await pass_through_new_command(matcher, choice)
    if not isinstance(event, GroupMessageEvent) or not is_group_admin(event):
        await agent_settings.finish(
            permission_required(
                "Agent 设置", "群主或群管理员", "重新进入时会再次检查权限"
            )
        )
    if intent is SessionIntent.EXIT:
        await agent_settings.finish("已取消 Agent 设置。")

    config = await get_or_create_config(session, int(event.group_id))
    if config is None:
        await agent_settings.finish("Agent 配置暂时不可用，请稍后重试")

    step = str(matcher.state.get("agent_settings_step") or "select")
    if intent is SessionIntent.MENU:
        await agent_settings.reject_arg(
            "agent_settings_input", _build_agent_settings_menu(config)
        )
    if intent is SessionIntent.BACK:
        if step == "select":
            await agent_settings.finish("已退出 Agent 设置。")
        matcher.state["agent_settings_step"] = "select"
        matcher.state.pop("agent_settings_key", None)
        await agent_settings.reject_arg(
            "agent_settings_input", _build_agent_settings_menu(config)
        )
    if step == "select":
        key = resolve_choice(choice, _AGENT_SETTING_CHOICES)
        if key is None:
            await agent_settings.reject_arg(
                "agent_settings_input",
                validation_failed("没有这个设置项", "回复菜单中的序号或名称")
                + "\n\n"
                + _build_agent_settings_menu(config),
            )
            return
        matcher.state["agent_settings_key"] = key
        matcher.state["agent_settings_step"] = "value"
        current = _format_agent_setting_value(
            key, _get_agent_setting_value(config, key)
        )
        await agent_settings.reject_arg(
            "agent_settings_input",
            f"已选择：{_AGENT_SETTING_LABELS[key]}\n当前：{current}\n"
            f"{_agent_setting_value_prompt(key)}；发送「返回」回菜单，「取消」退出。",
        )
        return

    key = str(matcher.state.get("agent_settings_key") or "")
    if key not in _AGENT_SETTING_LABELS:
        matcher.state["agent_settings_step"] = "select"
        await agent_settings.reject_arg(
            "agent_settings_input",
            "会话状态已刷新，请重新选择。\n\n" + _build_agent_settings_menu(config),
        )
        return
    try:
        value = _parse_agent_setting_value(key, choice.strip())
    except ValueError as exc:
        await agent_settings.reject_arg(
            "agent_settings_input",
            validation_failed(str(exc), _agent_setting_value_prompt(key)),
        )
        return

    before = _get_agent_setting_value(config, key)
    _set_agent_setting_value(config, key, value)
    config.updated_by = int(event.get_user_id())
    before_text = _format_agent_setting_value(key, before)
    after = _get_agent_setting_value(config, key)
    after_text = _format_agent_setting_value(key, after)
    if not await _commit(session):
        await agent_settings.finish("操作失败，请稍后重试")
    dbg(
        f"群 {event.group_id} /Agent设置 会话设置 "
        f"{key}={value!r} by={event.get_user_id()}"
    )
    await agent_settings.finish(
        format_change_preview(_AGENT_SETTING_LABELS[key], before_text, after_text)
        + "\n已保存。"
    )


@agent_memory.handle()
async def handle_agent_memory(
    event: GroupMessageEvent,
    session: async_scoped_session,
    _perm: None = require_feature("group_agent"),  # pyright: ignore[reportArgumentType]
) -> None:
    if not isinstance(event, GroupMessageEvent):
        await agent_memory.finish(scope_required("Agent 记忆查看", "群聊", "请在目标群发送 /Agent记忆"))
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
        await agent_status.finish(
            scope_required("Agent 状态查看", "群聊", "请在目标群发送 /Agent状态")
        )
    dbg(f"群 {event.group_id} 命令 /Agent状态: user={event.get_user_id()}")
    config = await get_or_create_config(session, int(event.group_id))
    if config is None:
        await agent_status.finish("Agent 配置暂时不可用，请稍后重试")
    runtime_enabled = await agent_runtime_enabled(
        session, int(event.group_id), config=config
    )
    dbg(
        f"群 {event.group_id} /Agent状态: enabled={runtime_enabled} "
        f"reply_trigger={config.reply_trigger_enabled} wakeup={config.explicit_wakeup_enabled} "
        f"conversation={config.short_conversation_enabled} proactive={config.proactive_enabled} "
        f"daily_limit={config.daily_limit}"
    )
    await agent_status.finish(_format_agent_runtime_summary(config, runtime_enabled))


@agent_profile.handle()
async def handle_agent_profile(
    event: GroupMessageEvent,
    session: async_scoped_session,
    args: Message = CommandArg(),
    _perm: None = require_feature("group_agent"),  # pyright: ignore[reportArgumentType]
) -> None:
    if not isinstance(event, GroupMessageEvent):
        await agent_profile.finish(scope_required("Agent 画像查看", "群聊", "请在目标群发送 /Agent画像"))
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
    privacy = await session.get(AgentPrivacy, (int(event.group_id), user_id))
    if privacy is not None and privacy.opted_out:
        await agent_profile.finish("该成员已退出 Agent 记忆")
    now = now_beijing()
    stmt = (
        select(AgentMemory)
        .where(
            AgentMemory.group_id == int(event.group_id),
            AgentMemory.subject_user_id == user_id,
            AgentMemory.memory_type.in_(("core", "profile")),
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
    matcher: Matcher,
    session: async_scoped_session,
    args: Message = CommandArg(),
    _perm: None = require_feature("group_agent"),  # pyright: ignore[reportArgumentType]
) -> None:
    if not isinstance(event, GroupMessageEvent):
        await agent_clear.finish(scope_required("Agent 记忆清理", "群聊", "请在目标群发送 /Agent清理"))
    dbg(
        f"群 {event.group_id} 命令 /Agent清理: user={event.get_user_id()} "
        f"args={args.extract_plain_text()!r}"
    )
    if not is_group_admin(event):
        dbg(f"群 {event.group_id} /Agent清理 拒绝: 非群管理")
        await agent_clear.finish("清理群记忆仅限群主或管理员")

    # 即便用户把确认短语直接写在命令后面，也不在同一条消息中执行删除。
    matcher.state["agent_clear_waiting_confirm"] = True
    await agent_clear.send(
        "危险操作：这会清理本群 Agent 的记忆、关系和相关缓存数据，且不可撤销。\n"
        f"如确认继续，请单独回复「{_CLEAR_CONFIRM_PHRASE}」；发送「取消」退出。"
    )


@agent_clear.got("agent_clear_confirm")
async def handle_agent_clear_confirm(
    event: GroupMessageEvent,
    session: async_scoped_session,
    confirm: str = ArgPlainText("agent_clear_confirm"),
) -> None:
    if not isinstance(event, GroupMessageEvent) or not is_group_admin(event):
        await agent_clear.finish("当前已无权继续清理群记忆")
    if is_cancel(confirm):
        await agent_clear.finish("已取消清理。")
    if not confirmation_matches(confirm, _CLEAR_CONFIRM_PHRASE):
        await agent_clear.reject_arg(
            "agent_clear_confirm",
            f"为避免误删，请完整输入「{_CLEAR_CONFIRM_PHRASE}」，或发送「取消」。",
        )
        return
    count = await delete_group_memories(session, int(event.group_id))
    logger.info("群 %s 清理 Agent 记忆 %s 条", event.group_id, count)
    dbg(f"群 {event.group_id} /Agent清理 完成: 共清除 {count} 条记录")
    await agent_clear.finish(f"已清理 {count} 条群聊 Agent 数据。")


@agent_export.handle()
async def handle_agent_export(
    event: GroupMessageEvent,
    session: async_scoped_session,
    _perm: None = require_feature("group_agent"),  # pyright: ignore[reportArgumentType]
) -> None:
    if not isinstance(event, GroupMessageEvent):
        await agent_export.finish(scope_required("Agent 记忆导出", "群聊", "请在目标群发送 /Agent导出"))
    dbg(f"群 {event.group_id} 命令 /Agent导出: user={event.get_user_id()}")
    if not is_group_admin(event):
        dbg(f"群 {event.group_id} /Agent导出 拒绝: 非群管理")
        await agent_export.finish("导出群聊 Agent 数据仅限群主或管理员")
    rows = await list_memories(session, int(event.group_id), limit=200)
    payload = [
        {
            "type": row.memory_type,
            "key": row.memory_key,
            "subject_user_id": int(row.subject_user_id or 0) or None,
            "content": row.content,
            "visibility": row.visibility,
            "source_kind": row.source_kind,
            "related_user_ids": list(row.related_user_ids or []),
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
    matcher: Matcher,
    session: async_scoped_session,
    args: Message = CommandArg(),
    _perm: None = require_feature("group_agent"),  # pyright: ignore[reportArgumentType]
) -> None:
    if not isinstance(event, GroupMessageEvent):
        await agent_persona.finish(
            scope_required("Agent 人设管理", "群聊", "请在目标群发送 /Agent人设")
        )
    dbg(
        f"群 {event.group_id} 命令 /Agent人设: user={event.get_user_id()} "
        f"args={args.extract_plain_text()!r}"
    )
    if not is_group_admin(event):
        dbg(f"群 {event.group_id} /Agent人设 拒绝: 非群管理")
        await agent_persona.finish(
            permission_required(
                "Agent 人设管理", "群主或群管理员", "请群管理员执行此操作"
            )
        )
    config = await get_or_create_config(session, int(event.group_id))
    if config is None:
        await agent_persona.finish("Agent 配置暂时不可用，请稍后重试")

    raw = args.extract_plain_text().strip()
    parts = raw.split()
    action = parts[0].lower() if parts else ""
    if action in {"查看", "show"}:
        await agent_persona.finish(_build_persona_text(config))
    if action in {"示例", "example"}:
        await agent_persona.finish(
            "高级语法：/Agent人设 设置 name=Yawn tone=温和 "
            "style=短句为主 response_length=通常1到3句\n"
            "普通使用直接发送 /Agent人设 进入字段选择。"
        )
    if action in {"重置", "reset"}:
        config.persona_override = {}
        config.persona_enabled = True
        config.persona_version += 1
        config.updated_by = int(event.get_user_id())
        persona_version = config.persona_version
        if not await _commit(session):
            await agent_persona.finish("操作失败，请稍后重试")
        dbg(f"群 {event.group_id} 人设已重置,version={persona_version}")
        await agent_persona.finish("已重置为全局默认人设")
    if action in {"设置", "set"}:
        try:
            updates = parse_persona_assignments(parts[1:])
        except ValueError as exc:
            dbg(f"群 {event.group_id} /Agent人设 解析失败: {exc}")
            await agent_persona.finish(str(exc))
        if not updates:
            await agent_persona.finish("至少需要一个 key=value 人设字段")
        before = resolve_persona(config)
        override = dict(config.persona_override or {})
        override.update(updates)
        config.persona_override = {
            key: override[key] for key in PERSONA_FIELDS if key in override
        }
        config.persona_enabled = True
        config.persona_version += 1
        config.updated_by = int(event.get_user_id())
        persona_version = config.persona_version
        if not await _commit(session):
            await agent_persona.finish("操作失败，请稍后重试")
        dbg(
            f"群 {event.group_id} 人设已更新: fields={sorted(updates)} "
            f"version={persona_version}"
        )
        preview = ["群级人设已更新："]
        for key, value in updates.items():
            label = _PERSONA_LABELS.get(key, key)
            preview.append(format_change_preview(label, before[key], value))
        await agent_persona.finish("\n\n".join(preview))
    if raw:
        key = resolve_choice(raw, _PERSONA_CHOICES)
        if key is not None:
            matcher.state["agent_persona_step"] = (
                "confirm" if key == "reset" else "value"
            )
            matcher.state["agent_persona_key"] = key
            if key == "reset":
                await agent_persona.send(
                    "预览：将删除本群全部人设覆盖，恢复全局默认。\n"
                    f"回复「{_PERSONA_CONFIRM_PHRASE}」保存，或发送「取消」。"
                )
            else:
                current = resolve_persona(config)[key]
                await agent_persona.send(
                    f"已选择：{_PERSONA_LABELS[key]}\n当前：{current}\n"
                    f"请输入新的内容（最多 {MAX_FIELD_LENGTH} 字符）；发送「返回」回菜单，「取消」退出。"
                )
            return
        await agent_persona.finish(
            "用法：/Agent人设；/Agent人设 查看；"
            "/Agent人设 设置 key=value ...；/Agent人设 重置"
        )

    matcher.state["agent_persona_step"] = "select"
    await agent_persona.send(_build_persona_menu(config))


@agent_persona.got("agent_persona_input")
async def handle_agent_persona_input(
    event: GroupMessageEvent,
    matcher: Matcher,
    session: async_scoped_session,
    value: str = ArgPlainText("agent_persona_input"),
) -> None:
    intent = resolve_session_intent(value)
    if intent is SessionIntent.NEW_COMMAND:
        await pass_through_new_command(matcher, value)
    if not isinstance(event, GroupMessageEvent) or not is_group_admin(event):
        await agent_persona.finish(
            permission_required(
                "Agent 人设修改", "群主或群管理员", "重新进入时会再次检查权限"
            )
        )
    if intent is SessionIntent.EXIT:
        await agent_persona.finish("已取消 Agent 人设修改。")

    config = await get_or_create_config(session, int(event.group_id))
    if config is None:
        await agent_persona.finish("Agent 配置暂时不可用，请稍后重试")
    step = str(matcher.state.get("agent_persona_step") or "select")
    if intent is SessionIntent.MENU:
        await agent_persona.reject_arg(
            "agent_persona_input", _build_persona_menu(config)
        )
    if intent is SessionIntent.BACK:
        if step == "select":
            await agent_persona.finish("已退出 Agent 人设修改。")
        matcher.state["agent_persona_step"] = "select"
        matcher.state.pop("agent_persona_key", None)
        matcher.state.pop("agent_persona_pending", None)
        matcher.state.pop("agent_persona_before", None)
        await agent_persona.reject_arg(
            "agent_persona_input", _build_persona_menu(config)
        )

    if step == "select":
        key = resolve_choice(value, _PERSONA_CHOICES)
        if key is None:
            await agent_persona.reject_arg(
                "agent_persona_input",
                validation_failed("没有这个字段", "回复菜单中的序号或字段名称")
                + "\n\n"
                + _build_persona_menu(config),
            )
            return
        matcher.state["agent_persona_key"] = key
        if key == "reset":
            matcher.state["agent_persona_step"] = "confirm"
            matcher.state["agent_persona_pending"] = "__reset__"
            await agent_persona.reject_arg(
                "agent_persona_input",
                "预览：将删除本群全部人设覆盖，恢复全局默认。\n"
                f"回复「{_PERSONA_CONFIRM_PHRASE}」保存，或发送「取消」。",
            )
            return
        matcher.state["agent_persona_step"] = "value"
        current = resolve_persona(config)[key]
        await agent_persona.reject_arg(
            "agent_persona_input",
            f"已选择：{_PERSONA_LABELS[key]}\n当前：{current}\n"
            f"请输入新的内容（最多 {MAX_FIELD_LENGTH} 字符）；发送「返回」回菜单，「取消」退出。",
        )
        return

    key = str(matcher.state.get("agent_persona_key") or "")
    if step == "value":
        cleaned_source = " ".join(value.strip().split())
        if not cleaned_source:
            await agent_persona.reject_arg(
                "agent_persona_input",
                validation_failed("内容不能为空", "输入人设内容"),
            )
            return
        if len(cleaned_source) > MAX_FIELD_LENGTH:
            await agent_persona.reject_arg(
                "agent_persona_input",
                validation_failed(
                    f"内容超过 {MAX_FIELD_LENGTH} 字符",
                    f"缩短到 {MAX_FIELD_LENGTH} 字符以内",
                ),
            )
            return
        try:
            pending = parse_persona_assignments([f"{key}={cleaned_source}"])[key]
        except (KeyError, ValueError) as exc:
            await agent_persona.reject_arg("agent_persona_input", str(exc))
            return
        before = resolve_persona(config)[key]
        matcher.state["agent_persona_pending"] = pending
        matcher.state["agent_persona_before"] = before
        matcher.state["agent_persona_step"] = "confirm"
        await agent_persona.reject_arg(
            "agent_persona_input",
            format_change_preview(_PERSONA_LABELS[key], before, pending)
            + f"\n\n回复「{_PERSONA_CONFIRM_PHRASE}」保存，或发送「取消」。",
        )
        return

    if step != "confirm" or key not in {*PERSONA_FIELDS, "reset"}:
        matcher.state["agent_persona_step"] = "select"
        await agent_persona.reject_arg(
            "agent_persona_input",
            "会话状态已刷新，请重新选择。\n\n" + _build_persona_menu(config),
        )
        return
    if not confirmation_matches(value, _PERSONA_CONFIRM_PHRASE):
        await agent_persona.reject_arg(
            "agent_persona_input",
            validation_failed(
                "确认短语不匹配", f"完整输入「{_PERSONA_CONFIRM_PHRASE}」"
            ),
        )
        return

    if key == "reset":
        config.persona_override = {}
        final_message = "已重置为全局默认人设。"
    else:
        pending = str(matcher.state.get("agent_persona_pending") or "")
        if not pending:
            matcher.state["agent_persona_step"] = "value"
            await agent_persona.reject_arg(
                "agent_persona_input", "待保存内容已失效，请重新输入。"
            )
            return
        override = dict(config.persona_override or {})
        override[key] = pending
        config.persona_override = {
            field: override[field] for field in PERSONA_FIELDS if field in override
        }
        before = str(matcher.state.get("agent_persona_before") or "")
        final_message = (
            format_change_preview(_PERSONA_LABELS[key], before, pending) + "\n已保存。"
        )
    config.persona_enabled = True
    config.persona_version += 1
    config.updated_by = int(event.get_user_id())
    persona_version = config.persona_version
    if not await _commit(session):
        await agent_persona.finish("操作失败，请稍后重试")
    dbg(f"群 {event.group_id} /Agent人设 会话保存 key={key} version={persona_version}")
    await agent_persona.finish(final_message)


@agent_privacy.handle()
async def handle_agent_privacy(
    event: GroupMessageEvent,
    matcher: Matcher,
    session: async_scoped_session,
    args: Message = CommandArg(),
    _perm: None = require_feature("group_agent"),  # pyright: ignore[reportArgumentType]
) -> None:
    if not isinstance(event, GroupMessageEvent):
        await agent_privacy.finish(scope_required("Agent 隐私设置", "群聊", "请在目标群发送 /Agent隐私"))
    action = args.extract_plain_text().strip().lower()
    dbg(
        f"群 {event.group_id} 命令 /Agent隐私: user={event.get_user_id()} action={action!r}"
    )
    privacy = await session.get(
        AgentPrivacy, (int(event.group_id), int(event.get_user_id()))
    )
    opted_out = bool(privacy and privacy.opted_out)
    status = "已退出（不保存后续消息）" if opted_out else "参与中"

    if action in {"查看", "show", "status"}:
        await agent_privacy.finish(f"本群 Agent 记忆隐私状态：{status}")
    if action in {"恢复", "optin", "on"}:
        if privacy is None:
            privacy = AgentPrivacy(
                group_id=int(event.group_id), user_id=int(event.get_user_id())
            )
            session.add(privacy)
        privacy.opted_out = False
        if not await _commit(session):
            await agent_privacy.finish("操作失败，请稍后重试")
        dbg(f"群 {event.group_id} 用户 {event.get_user_id()} 已恢复 Agent 记忆")
        await agent_privacy.finish("已恢复本群 Agent 记忆。")
    if action in {"退出", "optout", "off"}:
        matcher.state["agent_privacy_step"] = "confirm_exit"
        await agent_privacy.send(
            f"当前状态：{status}\n"
            "退出后，后续消息不再进入本群 Agent 记忆；同时会删除与你相关的已沉淀记忆、关系和原始消息。\n"
            f"如确认继续，请回复「{_PRIVACY_CONFIRM_PHRASE}」；发送「取消」退出。"
        )
        return
    if action:
        await agent_privacy.finish("用法：/Agent隐私；/Agent隐私 查看|退出|恢复")

    matcher.state["agent_privacy_step"] = "select"
    await agent_privacy.send(
        f"本群 Agent 记忆隐私状态：{status}\n"
        "回复「退出」停止记忆并删除与你相关的已沉淀数据；"
        "回复「恢复」重新参与；发送「取消」退出。"
    )


@agent_privacy.got("agent_privacy_input")
async def handle_agent_privacy_input(
    event: GroupMessageEvent,
    matcher: Matcher,
    session: async_scoped_session,
    value: str = ArgPlainText("agent_privacy_input"),
) -> None:
    if not isinstance(event, GroupMessageEvent):
        await agent_privacy.finish(scope_required("Agent 隐私设置", "群聊", "请在目标群发送 /Agent隐私"))
    if is_cancel(value):
        await agent_privacy.finish("已取消隐私设置。")

    group_id = int(event.group_id)
    user_id = int(event.get_user_id())
    privacy = await session.get(AgentPrivacy, (group_id, user_id))
    step = str(matcher.state.get("agent_privacy_step") or "select")

    if step == "select":
        choice = value.strip().lower()
        if choice in {"恢复", "optin", "on"}:
            if privacy is None:
                privacy = AgentPrivacy(group_id=group_id, user_id=user_id)
                session.add(privacy)
            privacy.opted_out = False
            if not await _commit(session):
                await agent_privacy.finish("操作失败，请稍后重试")
            dbg(f"群 {group_id} 用户 {user_id} 已恢复 Agent 记忆")
            await agent_privacy.finish("已恢复本群 Agent 记忆。")
        if choice not in {"退出", "optout", "off"}:
            await agent_privacy.reject_arg(
                "agent_privacy_input",
                "请输入「退出」或「恢复」，或发送「取消」。",
            )
            return
        matcher.state["agent_privacy_step"] = "confirm_exit"
        await agent_privacy.reject_arg(
            "agent_privacy_input",
            "退出会删除与你相关的已沉淀记忆、关系和原始消息，且不可撤销。\n"
            f"如确认继续，请回复「{_PRIVACY_CONFIRM_PHRASE}」；发送「取消」退出。",
        )
        return

    if step != "confirm_exit":
        matcher.state["agent_privacy_step"] = "select"
        await agent_privacy.reject_arg(
            "agent_privacy_input", "会话状态已刷新，请重新选择「退出」或「恢复」。"
        )
        return
    if not confirmation_matches(value, _PRIVACY_CONFIRM_PHRASE):
        await agent_privacy.reject_arg(
            "agent_privacy_input",
            f"为避免误删，请完整输入「{_PRIVACY_CONFIRM_PHRASE}」，或发送「取消」。",
        )
        return

    if privacy is None:
        privacy = AgentPrivacy(group_id=group_id, user_id=user_id)
        session.add(privacy)
    privacy.opted_out = True
    count = await delete_member_memories(session, group_id, user_id)
    dbg(f"群 {group_id} 用户 {user_id} 已隐私退出并清除 {count} 条相关数据")
    await agent_privacy.finish(
        f"已退出本群 Agent 记忆；后续消息不会被保存。已删除 {count} 条相关数据。"
    )


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
