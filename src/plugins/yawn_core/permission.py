"""权限核心模块：功能注册表、权限解析链、Depends 依赖工厂。"""

from typing import Optional

from nonebot import logger
from nonebot.adapters.onebot.v11 import GroupMessageEvent, MessageEvent, MessageSegment
from nonebot.dependencies import Dependent
from nonebot.matcher import Matcher
from nonebot.params import Depends
from nonebot.permission import SUPERUSER
from nonebot_plugin_orm import async_scoped_session

from .data_models.global_user_feature import GlobalUserFeature
from .data_models.group_feature import GroupFeature
from .data_models.user_feature import UserFeature

__all__ = [
    "FEATURE_REGISTRY",
    "SUPERUSER",
    "check_feature_permission",
    "get_feature_display",
    "is_group_admin",
    "is_valid_feature",
    "list_features",
    "require_feature",
    "resolve_feature_key",
]

# ── 功能注册表 ──────────────────────────────────────────────
# 新功能只需在此注册即可自动获得权限管控能力。
# key = 功能标识（英文），value = 显示名称（中文）
FEATURE_REGISTRY: dict[str, str] = {
    "checkin": "签到",
    "info": "个人信息",
    "ai_chat": "Yawn对话",
}


def is_valid_feature(feature: str) -> bool:
    """判断功能标识是否已注册。"""
    return feature in FEATURE_REGISTRY


def get_feature_display(feature: str) -> str:
    """获取功能的中文显示名称。"""
    return FEATURE_REGISTRY.get(feature, feature)


def list_features() -> list[tuple[str, str]]:
    """列出所有已注册功能的 (key, display_name)。"""
    return list(FEATURE_REGISTRY.items())


def resolve_feature_key(name: str) -> Optional[str]:
    """将功能显示名（中文）或标识（英文）反查为功能 key。"""
    if name in FEATURE_REGISTRY:
        return name
    for key, display in FEATURE_REGISTRY.items():
        if display == name:
            return key
    return None


def is_group_admin(event: GroupMessageEvent) -> bool:
    """检查事件发送者是否为群主或群管理员。"""
    return event.sender.role in ("owner", "admin")


# ── 权限解析链 ──────────────────────────────────────────────


async def check_feature_permission(
    user_id: int,
    group_id: Optional[int],
    feature: str,
    session: async_scoped_session,
) -> bool:
    """检查用户是否有权使用指定功能。

    群聊解析链（优先级从高到低）：
      1. 超级管理员 → 始终放行
      2. UserFeature（群内用户级覆盖）→ 有记录则按其 enabled 值
      3. GroupFeature（群级别开关）→ 有记录则按其 enabled 值
      4. 无记录 → 默认放行

    私聊解析链：
      1. 超级管理员 → 始终放行
      2. GlobalUserFeature（全局用户开关）→ 有记录则按其 enabled 值
      3. 无记录 → 默认放行
    """
    from nonebot import get_driver

    # 超级管理员始终放行
    superusers = get_driver().config.superusers
    if str(user_id) in superusers:
        return True

    if group_id is not None:
        # 群聊：先查用户级覆盖
        user_feat = await session.get(
            UserFeature,
            {"group_id": group_id, "user_id": user_id, "feature": feature},
        )
        if user_feat is not None:
            return user_feat.enabled

        # 再查群级别开关
        group_feat = await session.get(
            GroupFeature,
            {"group_id": group_id, "feature": feature},
        )
        if group_feat is not None:
            return group_feat.enabled

        # 默认放行
        return True

    # 私聊：查全局用户功能开关
    global_feat = await session.get(
        GlobalUserFeature,
        {"user_id": user_id, "feature": feature},
    )
    if global_feat is not None:
        return global_feat.enabled

    # 默认放行
    return True


# ── Depends 依赖工厂 ────────────────────────────────────────


def require_feature(feature: str) -> Dependent:
    """创建功能权限检查依赖，用于 handler 参数注入。

    用法::

        @matcher.handle()
        async def handler(
            event: GroupMessageEvent,
            session: async_scoped_session,
            _=require_feature("checkin"),
        ) -> None:
            ...
    """

    async def _checker(
        event: MessageEvent,
        matcher: Matcher,
        session: async_scoped_session,
    ) -> None:
        user_id = int(event.get_user_id())
        group_id: Optional[int] = getattr(event, "group_id", None)
        if group_id is not None:
            group_id = int(group_id)

        allowed = await check_feature_permission(user_id, group_id, feature, session)
        if not allowed:
            display = get_feature_display(feature)
            logger.info(f"用户 {user_id} 尝试使用功能「{display}」但权限不足")
            await matcher.finish(MessageSegment.text(f"功能「{display}」当前未开启哦~"))

    return Depends(_checker)


# ── 权限状态查询（供管理命令使用）──────────────────────────


async def get_user_feature_status(
    user_id: int,
    group_id: Optional[int],
    session: async_scoped_session,
) -> list[tuple[str, str, bool, str]]:
    """查询用户在指定场景下所有功能的权限状态。

    返回 [(feature_key, display_name, enabled, source), ...]
    source 取值：用户覆盖 / 群设置 / 默认开启 / 全局设置
    """
    from nonebot import get_driver

    superusers = get_driver().config.superusers
    is_su = str(user_id) in superusers

    results: list[tuple[str, str, bool, str]] = []

    for feat_key, display in FEATURE_REGISTRY.items():
        if is_su:
            results.append((feat_key, display, True, "超级管理员"))
            continue

        source = "默认开启"
        enabled = True

        if group_id is not None:
            user_feat = await session.get(
                UserFeature,
                {
                    "group_id": group_id,
                    "user_id": user_id,
                    "feature": feat_key,
                },
            )
            if user_feat is not None:
                enabled = user_feat.enabled
                source = "用户覆盖"
            else:
                group_feat = await session.get(
                    GroupFeature,
                    {"group_id": group_id, "feature": feat_key},
                )
                if group_feat is not None:
                    enabled = group_feat.enabled
                    source = "群设置"
        else:
            global_feat = await session.get(
                GlobalUserFeature,
                {"user_id": user_id, "feature": feat_key},
            )
            if global_feat is not None:
                enabled = global_feat.enabled
                source = "全局设置"

        results.append((feat_key, display, enabled, source))

    return results
