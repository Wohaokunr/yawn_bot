# ruff: noqa: E501,F401,I001,TID252,PLR0911,PLR0912,PLR0913,PLR0915,PLR0917,C901,TRY003,TRY004,TRY300,TRY301,ASYNC240,UP035,PGH004,ANN001,ANN201,ANN202,ARG001,FBT001,FBT002,COM812,RUF001,RUF100,TC002,PLR2004,RUF022
"""群聊 Agent 配置和记忆管理命令。"""

import json
import math

from nonebot import logger
from nonebot.adapters.onebot.v11 import GroupMessageEvent, Message, MessageSegment
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
from .emotion import emotion_public_state
from .log import dbg
from .memory import delete_group_memories, delete_member_memories, list_memories
from .persona import (
    MAX_ALIAS_LENGTH,
    MAX_CATCHPHRASES,
    MAX_EXAMPLES,
    MAX_IDENTITY_LENGTH,
    MAX_LONG_TEXT_LENGTH,
    MAX_NAME_LENGTH,
    MAX_TOPICS,
    PERSONA_PRESETS,
    PERSONA_TRAIT_FIELDS,
    PersonaCard,
    apply_persona_card,
    group_persona_card,
    persona_apply_preset,
    persona_behavior,
    persona_card_summary,
    persona_override_reason,
    persona_source_label,
    persona_summary,
    persona_trait_label,
    persona_trait_name,
    reset_persona,
    resolve_persona_source,
    resolve_preset_id,
)
from .persona_library import find_template_by_name, list_templates, template_card

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

_PERSONA_MENU_CHOICES = (
    SessionChoice("preset", "切换角色模板", ("模板", "角色模板")),
    SessionChoice("field", "改文字设定", ("文字", "设定", "名字", "自称", "称呼", "口癖", "性格")),
    SessionChoice("trait", "调档位", ("档位", "风格", "说话风格", "社交", "社交倾向")),
    SessionChoice("library", "使用人设库", ("人设库", "库")),
    SessionChoice("trial", "试演当前人设", ("试演", "预览")),
    SessionChoice("reset", "恢复全局人设", ("重置", "恢复默认", "reset")),
)
# 文字字段：key、菜单名、上限说明、是否列表。
_PERSONA_TEXT_FIELDS: dict[str, tuple[str, str, bool]] = {
    "name": ("名字", f"最多 {MAX_NAME_LENGTH} 字", False),
    "self_name": ("自称", f"例如“本喵”，最多 {MAX_ALIAS_LENGTH} 字", False),
    "user_title": ("对成员的称呼", f"例如“杂鱼”，最多 {MAX_ALIAS_LENGTH} 字", False),
    "identity": ("一句话身份", f"最多 {MAX_IDENTITY_LENGTH} 字", False),
    "personality": ("性格", f"最多 {MAX_LONG_TEXT_LENGTH} 字", False),
    "speaking_style": ("说话方式", f"最多 {MAX_LONG_TEXT_LENGTH} 字", False),
    "catchphrases": ("口癖", f"用顿号/逗号分隔，最多 {MAX_CATCHPHRASES} 条", True),
    "likes": ("喜欢的话题", f"用顿号/逗号分隔，最多 {MAX_TOPICS} 条", True),
    "avoid_topics": ("避开的话题", f"用顿号/逗号分隔，最多 {MAX_TOPICS} 条", True),
    "examples": ("示例台词", f"一行一句，最多 {MAX_EXAMPLES} 句", True),
}
_PERSONA_FIELD_CHOICES = tuple(
    SessionChoice(key, label, ()) for key, (label, _hint, _is_list) in _PERSONA_TEXT_FIELDS.items()
)
_PERSONA_TRAIT_CHOICES = tuple(
    SessionChoice(key, persona_trait_name(key), ()) for key in PERSONA_TRAIT_FIELDS
)
_PERSONA_TRAIT_LABELS = {choice.key: choice.label for choice in _PERSONA_TRAIT_CHOICES}
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


def _persona_field_display(card: PersonaCard, key: str) -> str:
    value = getattr(card, key)
    if isinstance(value, list):
        return "、".join(value) if value else "（无）"
    return str(value or "（无）")


def _build_persona_text(config: GroupAgentConfig) -> str:
    resolved = resolve_persona_source(config)
    card = resolved.card
    behavior = persona_behavior(config)
    followup_text = (
        "首轮后结束"
        if behavior.max_followup_bot_turns <= 1
        else f"最多再续 {behavior.max_followup_bot_turns - 1} 次"
    )
    reaction_text = "允许" if behavior.allow_spontaneous_reaction else "关闭"
    emotion = emotion_public_state(
        config.emotion_state if isinstance(config.emotion_state, dict) else {},
        expressiveness=card.expressiveness,
    )
    emotion_text = (
        f"{emotion['displayLabel']} · 强度 {int(float(emotion['intensity']) * 100)}%"
        if emotion["updatedAt"] is not None
        else "平静 · 暂无近期情绪事件"
    )
    lines = [
        "Agent 人设",
        f"来源：{persona_source_label(resolved)}",
        f"概览：{persona_card_summary(card)}",
        f"身份：{card.identity or '（无）'}",
    ]
    if card.self_name or card.user_title:
        lines.append(
            f"自称：{card.self_name or '（无）'}；称呼成员：{card.user_title or '（无）'}"
        )
    if card.catchphrases:
        lines.append(f"口癖：{'、'.join(card.catchphrases)}")
    if card.personality:
        lines.append(f"性格：{card.personality}")
    if card.speaking_style:
        lines.append(f"说话方式：{card.speaking_style}")
    lines.extend(
        [
            (
                f"实际行为：主动候选 ×{behavior.active_probability_scale:.2f}；"
                f"自动续聊 {followup_text}；主动 reaction {reaction_text}"
            ),
            f"动态情绪：{emotion_text}",
        ]
    )
    return "\n".join(lines)


def _build_persona_menu(config: GroupAgentConfig) -> str:
    lines = [_build_persona_text(config), "", "选择要做什么："]
    for index, choice in enumerate(_PERSONA_MENU_CHOICES, start=1):
        lines.append(f"{index}. {choice.label}")
    lines.append("回复序号或名称；「菜单」重新显示，「取消」退出。")
    return "\n".join(lines)


def _build_persona_preset_menu() -> str:
    lines = ["选择角色模板（会替换文字设定和档位，保留名字）："]
    for index, preset in enumerate(PERSONA_PRESETS.values(), start=1):
        lines.append(f"{index}. {preset.label} — {preset.description}")
    lines.append("回复序号或模板名称；「返回」回上一级。")
    return "\n".join(lines)


def _resolve_persona_preset(value: str) -> str | None:
    return resolve_preset_id(value)


def _build_persona_field_menu(card: PersonaCard) -> str:
    lines = ["选择要改的文字设定："]
    for index, choice in enumerate(_PERSONA_FIELD_CHOICES, start=1):
        current = _persona_field_display(card, choice.key)
        if len(current) > 24:
            current = current[:24] + "…"
        lines.append(f"{index}. {choice.label}（当前：{current}）")
    lines.append("回复序号或名称；「返回」回上一级。")
    return "\n".join(lines)


def _build_persona_trait_menu(card: PersonaCard | None = None) -> str:
    lines = ["选择要调整的档位："]
    for index, choice in enumerate(_PERSONA_TRAIT_CHOICES, start=1):
        suffix = ""
        if card is not None:
            level = int(getattr(card, choice.key))
            suffix = f"（当前 {level}/4 {persona_trait_label(choice.key, level)}）"
        lines.append(f"{index}. {choice.label}{suffix}")
    lines.append("回复序号或名称；「返回」回上一级。")
    return "\n".join(lines)


def _build_persona_trait_value_menu(key: str, current: int) -> str:
    lines = [
        f"{_PERSONA_TRAIT_LABELS[key]}：当前 {current}/4（{persona_trait_label(key, current)}）",
        "请选择 0-4：",
    ]
    lines.extend(
        f"{value}. {persona_trait_label(key, value)}" for value in range(5)
    )
    return "\n".join(lines)


def _persona_trial_text(config: GroupAgentConfig) -> str:
    return (
        "人设试演入口\n"
        f"当前：{persona_summary(config)}\n"
        "WebUI → Agent → 人设 可以用未保存草稿直接做真实模型试演；"
        "试演不会写数据库、不会发群消息、不会执行工具。"
    )


async def _build_persona_library_menu(session: async_scoped_session) -> str | None:
    try:
        rows = await list_templates(session)
    except SQLAlchemyError:
        return None
    if not rows:
        return "人设库还是空的。可以在 WebUI → Agent → 人设 里把人设存进人设库。"
    lines = ["选择要应用到本群的人设（复制一份，之后互不影响）："]
    for index, row in enumerate(rows, start=1):
        flag = "【全局】" if row.is_global else ""
        lines.append(f"{index}. {flag}{row.name} — {persona_card_summary(template_card(row))}")
    lines.append("回复序号或名称；「返回」回上一级。")
    return "\n".join(lines)


def _pending_preview(label: str, before: str, after: str, config: GroupAgentConfig) -> str:
    return (
        format_change_preview(label, before, after)
        + ("\n保存后将切换为当前群自定义人设。" if not config.persona_enabled else "")
        + f"\n\n回复「{_PERSONA_CONFIRM_PHRASE}」保存，或发送「取消」。"
    )


def _plain_message(text: str) -> Message:
    """把记忆/模型产出的文本包装为纯文本段，避免 CQ 码被解析成真实消息段。"""

    return Message(MessageSegment.text(text))


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
        _plain_message(
            "\n".join(f"- {row.memory_key}: {row.content[:120]}" for row in rows)
        )
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
        _plain_message(
            "\n".join(f"- {row.memory_key}: {row.content[:160]}" for row in rows)
        )
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
        await agent_export.send(_plain_message(chunk))
    await agent_export.finish(_plain_message(chunks[-1]))


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
            "直接发送 /Agent人设，可以切换模板、改名字/自称/称呼/口癖/性格、调档位或使用人设库。"
        )
    if action in {"试演", "preview", "trial"}:
        await agent_persona.finish(_persona_trial_text(config))
    if action in {"人设库", "library"} and len(parts) == 1:
        menu = await _build_persona_library_menu(session)
        await agent_persona.finish(menu or "人设库暂时不可用（可能还没执行数据库迁移）。")
    if action in {"模板", "preset", "使用", "use"} and len(parts) >= 2:
        target = " ".join(parts[1:])
        before = persona_summary(config)
        if action in {"模板", "preset"}:
            preset_id = _resolve_persona_preset(target)
            if preset_id is None:
                await agent_persona.finish(
                    "没有这个角色模板。\n" + _build_persona_preset_menu()
                )
            card = persona_apply_preset(group_persona_card(config), preset_id)
            label = "角色模板"
        else:
            try:
                row = await find_template_by_name(session, target)
            except SQLAlchemyError:
                row = None
            if row is None:
                await agent_persona.finish("人设库里没有这个人设。发送 /Agent人设 人设库 查看列表。")
            card = template_card(row)
            label = f"人设库「{row.name}」"
        mutation = apply_persona_card(config, card, enabled=True)
        if mutation.semantic_changed:
            config.persona_version += 1
            config.updated_by = int(event.get_user_id())
        if mutation.storage_changed and not await _commit(session):
            await agent_persona.finish("操作失败，请稍后重试")
        if not mutation.semantic_changed:
            await agent_persona.finish("当前已经是这个人设。")
        await agent_persona.finish(
            format_change_preview(label, before, persona_card_summary(card))
            + "\n已启用当前群自定义人设。"
        )
    if action in {"重置", "reset"}:
        mutation = reset_persona(config)
        if mutation.semantic_changed:
            config.persona_version += 1
            config.updated_by = int(event.get_user_id())
        persona_version = config.persona_version
        if mutation.storage_changed and not await _commit(session):
            await agent_persona.finish("操作失败，请稍后重试")
        if not mutation.storage_changed:
            await agent_persona.finish("当前已经是全局默认人设")
        dbg(f"群 {event.group_id} 人设已重置,version={persona_version}")
        await agent_persona.finish("已重置为全局默认人设")
    if raw:
        await agent_persona.finish(
            "用法：/Agent人设；/Agent人设 查看；/Agent人设 模板 <名称>；"
            "/Agent人设 人设库；/Agent人设 使用 <人设库名称>；"
            "/Agent人设 试演；/Agent人设 重置"
        )

    matcher.state["agent_persona_step"] = "select"
    await agent_persona.send(_build_persona_menu(config))


async def _persona_stage(
    matcher: Matcher, config: GroupAgentConfig, pending: PersonaCard, preview: str
) -> None:
    """暂存待保存的人设卡并要求确认；人设内容先过安全检查。"""

    reason = persona_override_reason(pending)
    if reason is not None:
        await agent_persona.reject_arg(
            "agent_persona_input",
            validation_failed(reason, "人设只描述角色，不要写覆盖系统规则的内容")
            + "\n\n"
            + _build_persona_menu(config),
        )
        return
    matcher.state["agent_persona_pending_profile"] = pending
    matcher.state["agent_persona_key"] = "profile"
    matcher.state["agent_persona_step"] = "confirm"
    await agent_persona.reject_arg("agent_persona_input", preview)


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
        matcher.state["agent_persona_step"] = "select"
        await agent_persona.reject_arg(
            "agent_persona_input", _build_persona_menu(config)
        )
        return
    if intent is SessionIntent.BACK:
        if step == "select":
            await agent_persona.finish("已退出 Agent 人设修改。")
        matcher.state["agent_persona_step"] = "select"
        matcher.state.pop("agent_persona_key", None)
        matcher.state.pop("agent_persona_pending_profile", None)
        await agent_persona.reject_arg(
            "agent_persona_input", _build_persona_menu(config)
        )
        return

    current = group_persona_card(config)
    if step == "select":
        key = resolve_choice(value, _PERSONA_MENU_CHOICES)
        if key is None:
            await agent_persona.reject_arg(
                "agent_persona_input",
                validation_failed("没有这个选项", "回复菜单中的序号或名称")
                + "\n\n"
                + _build_persona_menu(config),
            )
            return
        if key == "preset":
            matcher.state["agent_persona_step"] = "preset"
            await agent_persona.reject_arg(
                "agent_persona_input", _build_persona_preset_menu()
            )
            return
        if key == "field":
            matcher.state["agent_persona_step"] = "field"
            await agent_persona.reject_arg(
                "agent_persona_input", _build_persona_field_menu(current)
            )
            return
        if key == "trait":
            matcher.state["agent_persona_step"] = "trait"
            await agent_persona.reject_arg(
                "agent_persona_input", _build_persona_trait_menu(current)
            )
            return
        if key == "library":
            menu = await _build_persona_library_menu(session)
            if menu is None:
                await agent_persona.reject_arg(
                    "agent_persona_input",
                    "人设库暂时不可用（可能还没执行数据库迁移）。\n\n"
                    + _build_persona_menu(config),
                )
                return
            matcher.state["agent_persona_step"] = "library"
            await agent_persona.reject_arg("agent_persona_input", menu)
            return
        if key == "trial":
            await agent_persona.reject_arg(
                "agent_persona_input",
                _persona_trial_text(config) + "\n\n" + _build_persona_menu(config),
            )
            return
        if key == "reset":
            matcher.state["agent_persona_key"] = "reset"
            matcher.state["agent_persona_step"] = "confirm"
            await agent_persona.reject_arg(
                "agent_persona_input",
                "预览：将删除本群全部人设覆盖，恢复全局默认。\n"
                f"回复「{_PERSONA_CONFIRM_PHRASE}」保存，或发送「取消」。",
            )
            return

    if step == "preset":
        preset_id = _resolve_persona_preset(value)
        if preset_id is None:
            await agent_persona.reject_arg(
                "agent_persona_input",
                "没有这个角色模板。\n\n" + _build_persona_preset_menu(),
            )
            return
        pending = persona_apply_preset(current, preset_id)
        await _persona_stage(
            matcher,
            config,
            pending,
            _pending_preview(
                "角色模板",
                persona_card_summary(current),
                persona_card_summary(pending),
                config,
            ),
        )
        return

    if step == "library":
        try:
            row = await find_template_by_name(session, value)
        except SQLAlchemyError:
            row = None
        if row is None:
            menu = await _build_persona_library_menu(session)
            await agent_persona.reject_arg(
                "agent_persona_input",
                "人设库里没有这个人设。\n\n" + (menu or _build_persona_menu(config)),
            )
            return
        pending = template_card(row)
        await _persona_stage(
            matcher,
            config,
            pending,
            _pending_preview(
                f"人设库「{row.name}」",
                persona_card_summary(current),
                persona_card_summary(pending),
                config,
            ),
        )
        return

    if step == "field":
        key = resolve_choice(value, _PERSONA_FIELD_CHOICES)
        if key is None:
            await agent_persona.reject_arg(
                "agent_persona_input",
                validation_failed("没有这个设定", "回复当前菜单中的序号或名称")
                + "\n\n"
                + _build_persona_field_menu(current),
            )
            return
        label, hint, _is_list = _PERSONA_TEXT_FIELDS[key]
        matcher.state["agent_persona_key"] = key
        matcher.state["agent_persona_step"] = "field_value"
        await agent_persona.reject_arg(
            "agent_persona_input",
            f"{label}：当前 {_persona_field_display(current, key)}\n"
            f"请输入新的{label}（{hint}）；输入「清空」删除。",
        )
        return

    if step == "field_value":
        key = str(matcher.state.get("agent_persona_key") or "")
        if key not in _PERSONA_TEXT_FIELDS:
            matcher.state["agent_persona_step"] = "select"
            await agent_persona.reject_arg(
                "agent_persona_input",
                "会话状态已刷新，请重新选择。\n\n" + _build_persona_menu(config),
            )
            return
        label, _hint, is_list = _PERSONA_TEXT_FIELDS[key]
        source = value.strip()
        if source == "清空":
            if key == "name":
                await agent_persona.reject_arg(
                    "agent_persona_input",
                    validation_failed("名字不能清空", "输入一个新名字"),
                )
                return
            source = ""
        del is_list  # 列表字段由 PersonaCard 校验器统一按分隔符拆分
        pending = PersonaCard.model_validate({**current.model_dump(), key: source})
        await _persona_stage(
            matcher,
            config,
            pending,
            _pending_preview(
                label,
                _persona_field_display(current, key),
                _persona_field_display(pending, key),
                config,
            ),
        )
        return

    if step == "trait":
        key = resolve_choice(value, _PERSONA_TRAIT_CHOICES)
        if key is None:
            await agent_persona.reject_arg(
                "agent_persona_input",
                validation_failed("没有这个档位", "回复当前菜单中的序号或名称")
                + "\n\n"
                + _build_persona_trait_menu(current),
            )
            return
        matcher.state["agent_persona_key"] = key
        matcher.state["agent_persona_step"] = "trait_value"
        await agent_persona.reject_arg(
            "agent_persona_input",
            _build_persona_trait_value_menu(key, int(getattr(current, key))),
        )
        return

    if step == "trait_value":
        key = str(matcher.state.get("agent_persona_key") or "")
        cleaned = value.strip()
        if key not in _PERSONA_TRAIT_LABELS or not cleaned.isdigit():
            await agent_persona.reject_arg(
                "agent_persona_input",
                validation_failed("档位无效", "输入 0、1、2、3 或 4"),
            )
            return
        level = int(cleaned)
        if not 0 <= level <= 4:
            await agent_persona.reject_arg(
                "agent_persona_input", validation_failed("档位超出范围", "输入 0-4")
            )
            return
        previous = int(getattr(current, key))
        pending = current.model_copy(update={key: level})
        await _persona_stage(
            matcher,
            config,
            pending,
            _pending_preview(
                _PERSONA_TRAIT_LABELS[key],
                f"{previous}/4（{persona_trait_label(key, previous)}）",
                f"{level}/4（{persona_trait_label(key, level)}）",
                config,
            ),
        )
        return

    key = str(matcher.state.get("agent_persona_key") or "")
    if step != "confirm" or key not in {"profile", "reset"}:
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
        mutation = reset_persona(config)
        final_message = "已重置为全局默认人设。"
    else:
        pending = matcher.state.get("agent_persona_pending_profile")
        if not isinstance(pending, PersonaCard):
            matcher.state["agent_persona_step"] = "select"
            await agent_persona.reject_arg(
                "agent_persona_input",
                "待保存人设已失效，请重新选择。\n\n" + _build_persona_menu(config),
            )
            return
        mutation = apply_persona_card(config, pending, enabled=True)
        final_message = f"已保存：{persona_card_summary(pending)}"
    if mutation.semantic_changed:
        config.persona_version += 1
        config.updated_by = int(event.get_user_id())
    persona_version = config.persona_version
    if mutation.storage_changed and not await _commit(session):
        await agent_persona.finish("操作失败，请稍后重试")
    if not mutation.semantic_changed:
        await agent_persona.finish("人设内容没有变化。")
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
