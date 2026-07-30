"""权限管理命令模块。

普通用户：/功能列表、/开启、/关闭
群主/群管：/群功能、/群用户功能
超级管理员：/全局群功能、/全局用户功能、/权限查询
"""

from typing import Optional

from nonebot import logger
from nonebot.adapters.onebot.v11 import (
    GroupMessageEvent,
    Message,
    MessageEvent,
    MessageSegment,
)
from nonebot.params import CommandArg
from nonebot.permission import SUPERUSER
from nonebot.plugin import on_command
from nonebot_plugin_orm import async_scoped_session

from .data_models.global_user_feature import GlobalUserFeature
from .data_models.group_feature import GroupFeature
from .data_models.user_feature import UserFeature
from .data_models.user_group import UserGroup
from .permission import (
    get_feature_display,
    get_user_feature_status,
    is_group_admin,
    list_features,
    resolve_feature_key,
)

logger.info("权限管理模块已加载")

# 命令参数解析所需的最小 part 数量
_MIN_ACTION_PARTS = 2  # 开启/关闭 + 功能名
_MIN_GLOBAL_CMD_PARTS = 3  # 目标ID + 开启/关闭 + 功能名

# ── 命令匹配器 ──────────────────────────────────────────────

# 普通用户（群聊）
feature_list_cmd = on_command("功能列表", aliases={"功能"}, priority=5, block=True)
enable_cmd = on_command("开启", priority=5, block=True)
disable_cmd = on_command("关闭", priority=5, block=True)

# 群主/群管（群聊）
group_feature_cmd = on_command("群功能", priority=3, block=True)
group_user_feature_cmd = on_command("群用户功能", priority=3, block=True)

# 超级管理员
global_group_feature_cmd = on_command(
    "全局群功能", permission=SUPERUSER, priority=2, block=True
)
global_user_feature_cmd = on_command(
    "全局用户功能", permission=SUPERUSER, priority=2, block=True
)
perm_query_cmd = on_command("权限查询", permission=SUPERUSER, priority=2, block=True)


# ── 辅助函数 ──────────────────────────────────────────────


def _parse_action_and_feature(
    parts: list[str],
) -> tuple[Optional[bool], Optional[str]]:
    """从参数列表中解析 (动作, 功能key)。

    返回 (None, None) 表示解析失败。
    """
    if len(parts) < _MIN_ACTION_PARTS:
        return None, None
    action_str, feature_str = parts[0], parts[1]
    if action_str not in ("开启", "关闭"):
        return None, None
    feature_key = resolve_feature_key(feature_str)
    if feature_key is None:
        return None, None
    return action_str == "开启", feature_key


def _extract_target_user_id(args: Message) -> Optional[int]:
    """从消息参数中提取目标用户 ID（支持 @提及 和纯数字）。"""
    for seg in args:
        if seg.type == "at":
            return int(seg.data.get("qq", 0))
    text = args.extract_plain_text().strip()
    parts = text.split()
    if parts and parts[0].isdigit():
        return int(parts[0])
    return None


def _build_feature_list_text(
    statuses: list[tuple[str, str, bool, str]],
) -> str:
    """将功能状态列表格式化为文本。"""
    lines: list[str] = []
    for _key, display, enabled, source in statuses:
        icon = "✓" if enabled else "✗"
        lines.append(f"  {icon} {display}（{source}）")
    return "\n".join(lines)


# ── 普通用户命令 ──────────────────────────────────────────


@feature_list_cmd.handle()
async def handle_feature_list(
    event: GroupMessageEvent,
    session: async_scoped_session,
) -> None:
    """列出所有功能及自己在当前群的开关状态。"""
    statuses = await get_user_feature_status(event.user_id, event.group_id, session)
    lines = ["═══ 功能列表 ═══"]
    lines.append(_build_feature_list_text(statuses))
    lines.append("使用 /开启 <功能名> 或 /关闭 <功能名> 管理个人功能")
    await feature_list_cmd.finish("\n".join(lines))


@enable_cmd.handle()
async def handle_enable(
    event: GroupMessageEvent,
    session: async_scoped_session,
    args: Message = CommandArg(),
) -> None:
    """普通用户开启自己的某功能。"""
    await _set_user_own_feature(event, session, args, enabled=True)


@disable_cmd.handle()
async def handle_disable(
    event: GroupMessageEvent,
    session: async_scoped_session,
    args: Message = CommandArg(),
) -> None:
    """普通用户关闭自己的某功能。"""
    await _set_user_own_feature(event, session, args, enabled=False)


async def _set_user_own_feature(
    event: GroupMessageEvent,
    session: async_scoped_session,
    args: Message,
    *,
    enabled: bool,
) -> None:
    """设置用户自己在当前群的功能开关。"""
    text = args.extract_plain_text().strip()
    feature_key = resolve_feature_key(text)
    if feature_key is None:
        names = "、".join(d for _, d in list_features())
        await enable_cmd.finish(f"未知功能「{text}」，可用功能：{names}")

    record = await session.get(
        UserFeature,
        {
            "group_id": event.group_id,
            "user_id": event.user_id,
            "feature": feature_key,
        },
    )
    if record is None:
        record = UserFeature(
            group_id=event.group_id,
            user_id=event.user_id,
            feature=feature_key,
            enabled=enabled,
        )
        session.add(record)
    else:
        record.enabled = enabled

    await session.commit()
    display = get_feature_display(feature_key)
    status_text = "开启" if enabled else "关闭"
    logger.info(
        f"用户 {event.user_id} 在群 {event.group_id} {status_text}了功能「{display}」"
    )
    await enable_cmd.finish(f"已为你{status_text}功能「{display}」")


# ── 群主/群管命令 ─────────────────────────────────────────


@group_feature_cmd.handle()
async def handle_group_feature(
    event: GroupMessageEvent,
    session: async_scoped_session,
    args: Message = CommandArg(),
) -> None:
    """群管查看/修改本群功能开关。"""
    if not is_group_admin(event):
        await group_feature_cmd.finish("仅群主或群管理员可以管理群功能")

    text = args.extract_plain_text().strip()

    # 无参数 → 列出本群功能状态
    if not text:
        lines = ["═══ 本群功能状态 ═══"]
        for feat_key, display in list_features():
            gf = await session.get(
                GroupFeature,
                {"group_id": event.group_id, "feature": feat_key},
            )
            enabled = gf.enabled if gf is not None else True
            icon = "✓" if enabled else "✗"
            lines.append(f"  {icon} {display}")
        lines.append("使用 /群功能 开启/关闭 <功能名> 管理")
        await group_feature_cmd.finish("\n".join(lines))

    # 有参数 → 修改
    parts = text.split()
    action_enabled, feature_key = _parse_action_and_feature(parts)
    if action_enabled is None or feature_key is None:
        await group_feature_cmd.finish("格式：/群功能 开启/关闭 <功能名>")

    gf = await session.get(
        GroupFeature,
        {"group_id": event.group_id, "feature": feature_key},
    )
    if gf is None:
        gf = GroupFeature(
            group_id=event.group_id,
            feature=feature_key,
            enabled=action_enabled,
        )
        session.add(gf)
    else:
        gf.enabled = action_enabled

    await session.commit()
    display = get_feature_display(feature_key)
    status_text = "开启" if action_enabled else "关闭"
    logger.info(
        f"群 {event.group_id} 管理员 {event.user_id} {status_text}了群功能「{display}」"
    )
    await group_feature_cmd.finish(f"已为本群{status_text}功能「{display}」")


@group_user_feature_cmd.handle()
async def handle_group_user_feature(
    event: GroupMessageEvent,
    session: async_scoped_session,
    args: Message = CommandArg(),
) -> None:
    """群管管理本群特定用户的功能。"""
    if not is_group_admin(event):
        await group_user_feature_cmd.finish("仅群主或群管理员可以管理用户功能")

    # 提取目标用户（@提及 或 QQ号）
    target_user_id = _extract_target_user_id(args)
    if target_user_id is None:
        await group_user_feature_cmd.finish(
            "格式：/群用户功能 @用户 开启/关闭 <功能名>\n"
            "或：/群用户功能 <QQ号> 开启/关闭 <功能名>"
        )

    # 解析动作和功能
    text = args.extract_plain_text().strip()
    parts = text.split()
    # 去掉开头的 QQ 号（如果有）
    if parts and parts[0].isdigit():
        parts = parts[1:]

    action_enabled, feature_key = _parse_action_and_feature(parts)
    if action_enabled is None or feature_key is None:
        await group_user_feature_cmd.finish(
            "格式：/群用户功能 @用户 开启/关闭 <功能名>"
        )

    # 确保目标用户的 UserGroup 存在（FK 约束）
    user_group = await session.get(
        UserGroup,
        {"group_id": event.group_id, "user_id": target_user_id},
    )
    if user_group is None:
        user_group = UserGroup(
            group_id=event.group_id,
            user_id=target_user_id,
        )
        session.add(user_group)
        await session.flush()

    record = await session.get(
        UserFeature,
        {
            "group_id": event.group_id,
            "user_id": target_user_id,
            "feature": feature_key,
        },
    )
    if record is None:
        record = UserFeature(
            group_id=event.group_id,
            user_id=target_user_id,
            feature=feature_key,
            enabled=action_enabled,
        )
        session.add(record)
    else:
        record.enabled = action_enabled

    await session.commit()
    display = get_feature_display(feature_key)
    status_text = "开启" if action_enabled else "关闭"
    logger.info(
        f"群 {event.group_id} 管理员 {event.user_id} "
        f"为用户 {target_user_id} {status_text}了功能「{display}」"
    )
    await group_user_feature_cmd.finish(
        MessageSegment.at(target_user_id) + f" 的功能「{display}」已{status_text}"
    )


# ── 超级管理员命令 ────────────────────────────────────────


@global_group_feature_cmd.handle()
async def handle_global_group_feature(
    event: MessageEvent,
    session: async_scoped_session,
    args: Message = CommandArg(),
) -> None:
    """超管管理任意群的功能开关。"""
    text = args.extract_plain_text().strip()
    parts = text.split()
    if len(parts) < _MIN_GLOBAL_CMD_PARTS or not parts[0].isdigit():
        await global_group_feature_cmd.finish(
            "格式：/全局群功能 <群号> 开启/关闭 <功能名>"
        )

    group_id = int(parts[0])
    action_enabled, feature_key = _parse_action_and_feature(parts[1:])
    if action_enabled is None or feature_key is None:
        await global_group_feature_cmd.finish(
            "格式：/全局群功能 <群号> 开启/关闭 <功能名>"
        )

    gf = await session.get(
        GroupFeature,
        {"group_id": group_id, "feature": feature_key},
    )
    if gf is None:
        gf = GroupFeature(
            group_id=group_id,
            feature=feature_key,
            enabled=action_enabled,
        )
        session.add(gf)
    else:
        gf.enabled = action_enabled

    await session.commit()
    display = get_feature_display(feature_key)
    status_text = "开启" if action_enabled else "关闭"
    logger.info(
        f"超级管理员 {event.user_id} 为群 {group_id} {status_text}了功能「{display}」"
    )
    await global_group_feature_cmd.finish(
        f"已为群 {group_id} {status_text}功能「{display}」"
    )


@global_user_feature_cmd.handle()
async def handle_global_user_feature(
    event: MessageEvent,
    session: async_scoped_session,
    args: Message = CommandArg(),
) -> None:
    """超管管理任意用户的功能开关。

    带群号 → 写入 UserFeature（群内用户级覆盖）
    不带群号 → 写入 GlobalUserFeature（全局用户开关）
    """
    text = args.extract_plain_text().strip()
    parts = text.split()

    if len(parts) < _MIN_GLOBAL_CMD_PARTS or not parts[0].isdigit():
        await global_user_feature_cmd.finish(
            "格式：/全局用户功能 <QQ号> <群号> 开启/关闭 <功能名>\n"
            "或：/全局用户功能 <QQ号> 开启/关闭 <功能名>（全局生效）"
        )

    target_user_id = int(parts[0])

    # 判断第二个参数是群号还是动作
    group_id: Optional[int] = None
    rest_parts: list[str] = parts[1:]
    if parts[1].isdigit():
        group_id = int(parts[1])
        rest_parts = parts[2:]

    action_enabled, feature_key = _parse_action_and_feature(rest_parts)
    if action_enabled is None or feature_key is None:
        await global_user_feature_cmd.finish(
            "格式：/全局用户功能 <QQ号> [群号] 开启/关闭 <功能名>"
        )

    display = get_feature_display(feature_key)
    status_text = "开启" if action_enabled else "关闭"

    if group_id is not None:
        await _set_user_feature(
            session,
            target_user_id,
            group_id,
            feature_key,
            enabled=action_enabled,
        )
        scope_text = f"群 {group_id} 内"
    else:
        await _set_global_user_feature(
            session,
            target_user_id,
            feature_key,
            enabled=action_enabled,
        )
        scope_text = "全局"

    await session.commit()
    logger.info(
        f"超级管理员 {event.user_id} 为用户 {target_user_id} "
        f"{scope_text}{status_text}了功能「{display}」"
    )
    await global_user_feature_cmd.finish(
        f"已为用户 {target_user_id} {scope_text}{status_text}功能「{display}」"
    )


async def _set_user_feature(
    session: async_scoped_session,
    user_id: int,
    group_id: int,
    feature: str,
    *,
    enabled: bool,
) -> None:
    """写入群内用户级功能覆盖（自动创建 UserGroup 若不存在）。"""
    user_group = await session.get(
        UserGroup,
        {"group_id": group_id, "user_id": user_id},
    )
    if user_group is None:
        user_group = UserGroup(group_id=group_id, user_id=user_id)
        session.add(user_group)
        await session.flush()

    record = await session.get(
        UserFeature,
        {"group_id": group_id, "user_id": user_id, "feature": feature},
    )
    if record is None:
        record = UserFeature(
            group_id=group_id,
            user_id=user_id,
            feature=feature,
            enabled=enabled,
        )
        session.add(record)
    else:
        record.enabled = enabled


async def _set_global_user_feature(
    session: async_scoped_session,
    user_id: int,
    feature: str,
    *,
    enabled: bool,
) -> None:
    """写入全局用户功能开关。"""
    record = await session.get(
        GlobalUserFeature,
        {"user_id": user_id, "feature": feature},
    )
    if record is None:
        record = GlobalUserFeature(
            user_id=user_id,
            feature=feature,
            enabled=enabled,
        )
        session.add(record)
    else:
        record.enabled = enabled


@perm_query_cmd.handle()
async def handle_perm_query(
    event: MessageEvent,  # noqa: ARG001
    session: async_scoped_session,
    args: Message = CommandArg(),
) -> None:
    """超管查询某用户的完整权限状态。"""
    text = args.extract_plain_text().strip()
    parts = text.split()

    if not parts or not parts[0].isdigit():
        await perm_query_cmd.finish("格式：/权限查询 <QQ号> [群号]")

    target_user_id = int(parts[0])
    group_id: Optional[int] = None
    if len(parts) > 1 and parts[1].isdigit():
        group_id = int(parts[1])

    statuses = await get_user_feature_status(target_user_id, group_id, session)

    if group_id is not None:
        header = f"═══ 用户 {target_user_id} 在群 {group_id} 的权限 ═══"
    else:
        header = f"═══ 用户 {target_user_id} 的全局权限（私聊）═══"

    lines = [header, _build_feature_list_text(statuses)]
    await perm_query_cmd.finish("\n".join(lines))
