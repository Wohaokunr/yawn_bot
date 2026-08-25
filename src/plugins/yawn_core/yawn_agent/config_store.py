# ruff: noqa: E501,F401,I001,TID252,TRY003,TRY300,TRY301,UP035,PGH004,ANN001,ANN201,ANN202,ARG001,COM812,RUF001,RUF100
"""群级 Agent 配置的 get-or-create 共享入口。"""

from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from ..data_models.group_agent_config import GroupAgentConfig
from ..data_models.group_feature import GroupFeature
from .log import dbg


async def get_or_create_config(session: Any, group_id: int) -> GroupAgentConfig | None:
    record = await session.get(GroupAgentConfig, group_id)
    if record is None:
        record = GroupAgentConfig(group_id=group_id)
        session.add(record)
        try:
            await session.flush()
        except IntegrityError:
            # block=False 监听器下同一群可能并发创建；输的一方重新读取。
            dbg(f"群 {group_id} Agent 配置并发创建竞态,回滚后重新读取")
            await session.rollback()
            record = await session.get(GroupAgentConfig, group_id)
        else:
            dbg(f"群 {group_id} 新建 Agent 配置记录")
    return record


async def list_agent_group_ids(session: Any) -> list[int]:
    """列出所有存在 Agent 配置的群号（含未启用群：过期数据同样需要清理）。"""

    rows = (
        await session.execute(
            select(GroupAgentConfig.group_id).order_by(GroupAgentConfig.group_id)
        )
    ).scalars()
    return [int(value) for value in rows]


async def agent_runtime_enabled(
    session: Any,
    group_id: int,
    *,
    config: GroupAgentConfig | None = None,
) -> bool:
    """Agent 群级总开关。

    运行态必须同时满足专用 ``GroupAgentConfig.enabled`` 与通用群功能
    ``group_agent`` 开关。后者默认开启；任一处显式关闭都视为整个 Agent
    子系统停用，后台主动发言、短会话和记忆整理都必须遵守这一口径。
    """

    if config is None:
        config = await session.get(GroupAgentConfig, group_id)
    # 尚未创建专用配置时等价于模型默认值 enabled=True；这样 WebUI 的
    # “实际生效”与首次收到消息后 get-or-create 的运行结果保持一致。
    if config is not None and not bool(config.enabled):
        return False
    feature = await session.get(
        GroupFeature,
        {"group_id": group_id, "feature": "group_agent"},
    )
    return feature is None or bool(feature.enabled)


async def set_agent_runtime_enabled(
    session: Any,
    group_id: int,
    *,
    enabled: bool,
    config: GroupAgentConfig | None = None,
) -> GroupAgentConfig | None:
    """同步写入 Agent 专用配置和通用 ``group_agent`` 群功能开关。"""

    if config is None:
        config = await get_or_create_config(session, group_id)
    if config is None:
        return None
    config.enabled = bool(enabled)
    feature = await session.get(
        GroupFeature,
        {"group_id": group_id, "feature": "group_agent"},
    )
    if feature is None:
        session.add(
            GroupFeature(
                group_id=group_id,
                feature="group_agent",
                enabled=bool(enabled),
            )
        )
    else:
        feature.enabled = bool(enabled)
    return config
