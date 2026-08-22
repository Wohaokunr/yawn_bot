# ruff: noqa: E501,F401,I001,TID252,TRY003,TRY300,TRY301,UP035,PGH004,ANN001,ANN201,ANN202,ARG001,COM812,RUF001,RUF100
"""群级 Agent 配置的 get-or-create 共享入口。"""

from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from ..data_models.group_agent_config import GroupAgentConfig
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
