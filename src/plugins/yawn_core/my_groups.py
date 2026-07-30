"""私聊群聊面板模块。

用户通过私聊命令查看与机器人共同存在的群聊列表，
管理员可进一步查看功能面板、管理面板、监测面板，
并可交互式切换群功能开关。
"""

from datetime import datetime
from typing import TYPE_CHECKING, Optional

from nonebot import logger
from nonebot.adapters import Bot
from nonebot.adapters.onebot.v11 import (
    GroupMessageEvent,
    Message,
    MessageEvent,
)
from nonebot.matcher import Matcher
from nonebot.params import ArgPlainText, CommandArg
from nonebot.plugin import on_command
from nonebot_plugin_orm import async_scoped_session
from sqlalchemy import func, select
from sqlalchemy.orm import selectinload

from .data_models.group_feature import GroupFeature
from .data_models.user_group import UserGroup
from .permission import list_features

if TYPE_CHECKING:
    from .data_models.bot_group import BotGroup

logger.info("私聊群聊面板模块已加载")

# ── 命令匹配器 ──────────────────────────────────────────────

my_groups_cmd = on_command(
    "我的群聊",
    aliases={"群列表", "我的群"},
    priority=5,
    block=True,
)


# ── 辅助函数 ──────────────────────────────────────────────


def _fmt_time(dt: Optional[datetime]) -> str:
    """格式化时间为字符串。"""
    if dt is None:
        return "暂无"
    return f"{dt:%Y-%m-%d %H:%M}"


async def _get_user_role_in_group(
    bot: Bot,
    group_id: int,
    user_id: int,
) -> str:
    """获取用户在群中的角色，失败时返回 member。"""
    try:
        info = await bot.call_api(
            "get_group_member_info",
            group_id=group_id,
            user_id=user_id,
        )
        return info.get("role", "member")
    except Exception:
        logger.warning(
            f"获取用户 {user_id} 在群 {group_id} 的角色失败"
        )
        return "member"


def _build_group_list_text(
    groups: list[dict],
) -> str:
    """构建群聊编号列表文本。"""
    lines = ["═══ 我的群聊 ═══"]
    for i, g in enumerate(groups, 1):
        tag = "[管理员] " if g["is_admin"] else ""
        name = g["group_name"] or "未知群聊"
        lines.append(f"{i}. {tag}{name} ({g['group_id']})")
    return "\n".join(lines)


async def _build_admin_panel(
    session: async_scoped_session,
    bot: Bot,
    group: dict,
) -> str:
    """构建管理员三大面板文本。"""
    gid = group["group_id"]
    lines: list[str] = []

    # ── 功能面板 ──
    lines.append("═══ 功能面板 ═══")
    features = list_features()
    for i, (key, display) in enumerate(features, 1):
        gf = await session.get(
            GroupFeature,
            {"group_id": gid, "feature": key},
        )
        enabled = gf.enabled if gf is not None else True
        icon = "开" if enabled else "关"
        lines.append(f"  {i}. [{icon}] {display}")

    # ── 管理面板 ──
    lines.append("═══ 管理面板 ═══")
    lines.append(f"  群名: {group['group_name'] or '未知'}")
    lines.append(f"  群号: {gid}")

    member_count: Optional[int] = None
    try:
        ginfo = await bot.call_api(
            "get_group_info", group_id=gid
        )
        member_count = ginfo.get("member_count")
    except Exception:
        pass
    if member_count is not None:
        lines.append(f"  群成员数: {member_count}")

    tracked = await session.scalar(
        select(func.count()).select_from(UserGroup).where(
            UserGroup.group_id == gid
        )
    )
    lines.append(f"  已追踪用户: {tracked or 0}")

    # ── 监测面板 ──
    lines.append("═══ 监测面板 ═══")
    lines.append(
        f"  群最后活跃: {_fmt_time(group['last_active_at'])}"
    )
    lines.append(
        f"  我的最后发言: {_fmt_time(group['last_seen_at'])}"
    )

    feat_count = await session.scalar(
        select(func.count()).select_from(GroupFeature).where(
            GroupFeature.group_id == gid
        )
    )
    lines.append(f"  功能变更数: {feat_count or 0}")

    return "\n".join(lines)


# ── 事件处理 ──────────────────────────────────────────────


@my_groups_cmd.handle()
async def handle_list_groups(
    bot: Bot,
    event: MessageEvent,
    matcher: Matcher,
    session: async_scoped_session,
    args: Message = CommandArg(),
) -> None:
    """列出用户与机器人共同存在的群聊。"""
    if isinstance(event, GroupMessageEvent):
        await my_groups_cmd.finish("请私聊我使用此命令哦~")

    user_id = int(event.get_user_id())

    stmt = (
        select(UserGroup)
        .options(selectinload(UserGroup.group))
        .where(UserGroup.user_id == user_id)
        .order_by(UserGroup.group_id)
    )
    result = await session.execute(stmt)
    user_groups = result.scalars().all()

    if not user_groups:
        await my_groups_cmd.finish(
            "你还没有和我共同存在的群聊哦~"
        )

    groups: list[dict] = []
    for ug in user_groups:
        grp: "BotGroup" = ug.group
        role = await _get_user_role_in_group(
            bot, grp.group_id, user_id
        )
        groups.append(
            {
                "group_id": grp.group_id,
                "group_name": grp.group_name,
                "is_admin": role in ("owner", "admin"),
                "last_active_at": grp.last_active_at,
                "first_seen_at": ug.first_seen_at,
                "last_seen_at": ug.last_seen_at,
            }
        )

    matcher.state["groups"] = groups
    list_text = _build_group_list_text(groups)
    await my_groups_cmd.send(list_text)

    # 若命令自带序号参数，跳过询问
    if args.extract_plain_text().strip():
        matcher.set_arg("group_choice", args)


@my_groups_cmd.got(
    "group_choice",
    prompt="请输入群序号查看详情，发送「取消」退出",
)
async def handle_group_detail(
    bot: Bot,
    matcher: Matcher,
    session: async_scoped_session,
    group_choice: str = ArgPlainText("group_choice"),
) -> None:
    """处理群选择，展示群详情或管理员面板。"""
    text = group_choice.strip()

    if text in ("取消", "退出", "q"):
        await my_groups_cmd.finish("已退出，下次再见~")

    if not text.isdigit():
        await my_groups_cmd.reject(
            "请输入有效的群序号，或发送「取消」退出"
        )

    idx = int(text)
    groups: list[dict] = matcher.state["groups"]

    if idx < 1 or idx > len(groups):
        await my_groups_cmd.reject(
            f"序号超出范围（1-{len(groups)}），请重新输入"
        )

    selected = groups[idx - 1]
    matcher.state["selected_group"] = selected

    if not selected["is_admin"]:
        # 非管理员：展示基础群信息
        name = selected["group_name"] or "未知群聊"
        lines = [
            f"═══ {name} ═══",
            f"  群号: {selected['group_id']}",
            f"  首次加入: {_fmt_time(selected['first_seen_at'])}",
            f"  群最后活跃: "
            f"{_fmt_time(selected['last_active_at'])}",
            f"  我的最后发言: "
            f"{_fmt_time(selected['last_seen_at'])}",
        ]
        await my_groups_cmd.finish("\n".join(lines))

    # 管理员：构建并发送三大面板
    matcher.state["is_admin"] = True
    panel = await _build_admin_panel(
        session, bot, selected
    )
    await my_groups_cmd.send(panel)


@my_groups_cmd.got(
    "feature_choice",
    prompt="输入功能序号切换开关，发送「退出」结束管理",
)
async def handle_feature_toggle(
    bot: Bot,
    matcher: Matcher,
    session: async_scoped_session,
    feature_choice: str = ArgPlainText("feature_choice"),
) -> None:
    """管理员切换群功能开关（循环交互）。"""
    if not matcher.state.get("is_admin"):
        await my_groups_cmd.finish()

    text = feature_choice.strip()

    if text in ("退出", "取消", "q"):
        await my_groups_cmd.finish("已退出管理面板")

    if not text.isdigit():
        await my_groups_cmd.reject_arg(
            "feature_choice",
            "请输入有效的功能序号，或发送「退出」结束",
        )

    idx = int(text)
    features = list_features()

    if idx < 1 or idx > len(features):
        await my_groups_cmd.reject_arg(
            "feature_choice",
            f"序号超出范围（1-{len(features)}），请重新输入",
        )

    feat_key, display = features[idx - 1]
    group: dict = matcher.state["selected_group"]
    gid = group["group_id"]

    # 切换功能开关
    gf = await session.get(
        GroupFeature,
        {"group_id": gid, "feature": feat_key},
    )
    if gf is None:
        # 默认开启 → 关闭需新建记录
        new_enabled = False
        gf = GroupFeature(
            group_id=gid,
            feature=feat_key,
            enabled=False,
        )
        session.add(gf)
    else:
        new_enabled = not gf.enabled
        gf.enabled = new_enabled

    await session.commit()

    status_text = "开启" if new_enabled else "关闭"
    group_name = group["group_name"] or str(gid)
    logger.info(
        f"管理员在群 {gid} {status_text}了功能「{display}」"
    )

    # 刷新面板
    panel = await _build_admin_panel(session, bot, group)
    await my_groups_cmd.reject_arg(
        "feature_choice",
        f"已为群「{group_name}」{status_text}功能"
        f"「{display}」\n\n{panel}",
    )
