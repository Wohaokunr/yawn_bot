# ruff: noqa: TID252, E501, TRY003
"""人设库：可复用的人设卡 + 全局默认人设。

运行时热路径只读 ``persona.global_persona()`` 的进程内缓存；这里负责在启动时
和每次写入后刷新缓存。表尚未迁移（用户还没执行 ``nb orm upgrade``）时所有读取
都降级为“没有人设库、使用内置默认”，不影响对话。
"""

from __future__ import annotations

from typing import Any

from nonebot import get_driver, logger
from nonebot_plugin_orm import get_session
from sqlalchemy import func, select, update
from sqlalchemy.exc import SQLAlchemyError

from ..data_models.agent_persona_template import AgentPersonaTemplate
from .log import dbg
from .persona import (
    PersonaCard,
    parse_persona_card,
    persona_card_summary,
    set_global_persona_card,
)

MAX_LIBRARY_ITEMS = 50


class PersonaLibraryError(ValueError):
    """人设库写入被拒绝（重名、超量、不存在、版本冲突）。"""

    def __init__(self, message: str, *, status: int = 400) -> None:
        super().__init__(message)
        self.status = status


def template_card(row: AgentPersonaTemplate) -> PersonaCard:
    return parse_persona_card(row.card) or PersonaCard()


def serialize_template(row: AgentPersonaTemplate) -> dict[str, Any]:
    card = template_card(row)
    return {
        "id": str(row.id),
        "name": row.name,
        "isGlobal": bool(row.is_global),
        "card": card.api_dict(),
        "summary": persona_card_summary(card),
        "version": str(int(row.revision or 1)),
        "updatedAt": row.updated_at.isoformat() if row.updated_at else None,
    }


async def refresh_global_persona(session: Any) -> None:
    """从人设库读取全局默认并刷新进程缓存；表缺失时回退内置默认。"""

    try:
        row = await session.scalar(
            select(AgentPersonaTemplate)
            .where(AgentPersonaTemplate.is_global.is_(True))
            .order_by(AgentPersonaTemplate.id)
            .limit(1)
        )
    except SQLAlchemyError:
        dbg("人设库表不可用（可能尚未迁移），全局人设使用内置默认")
        set_global_persona_card(None)
        return
    if row is None:
        set_global_persona_card(None)
        return
    set_global_persona_card(template_card(row), name=row.name)


async def list_templates(session: Any) -> list[AgentPersonaTemplate]:
    return list(
        (
            await session.execute(
                select(AgentPersonaTemplate).order_by(
                    AgentPersonaTemplate.is_global.desc(), AgentPersonaTemplate.name
                )
            )
        )
        .scalars()
        .all()
    )


async def _name_taken(session: Any, name: str, *, exclude_id: int | None = None) -> bool:
    stmt = select(func.count()).select_from(AgentPersonaTemplate).where(
        func.lower(AgentPersonaTemplate.name) == name.casefold()
    )
    if exclude_id is not None:
        stmt = stmt.where(AgentPersonaTemplate.id != exclude_id)
    return int(await session.scalar(stmt) or 0) > 0


def _clean_name(name: str) -> str:
    cleaned = " ".join(str(name or "").split())[:32]
    if not cleaned:
        raise PersonaLibraryError("人设名称不能为空")
    return cleaned


async def _clear_global(session: Any, *, keep_id: int | None = None) -> None:
    stmt = update(AgentPersonaTemplate).where(AgentPersonaTemplate.is_global.is_(True))
    if keep_id is not None:
        stmt = stmt.where(AgentPersonaTemplate.id != keep_id)
    await session.execute(stmt.values(is_global=False))


async def create_template(
    session: Any, *, name: str, card: PersonaCard, is_global: bool = False
) -> AgentPersonaTemplate:
    cleaned = _clean_name(name)
    total = int(
        await session.scalar(select(func.count()).select_from(AgentPersonaTemplate))
        or 0
    )
    if total >= MAX_LIBRARY_ITEMS:
        raise PersonaLibraryError(f"人设库最多保存 {MAX_LIBRARY_ITEMS} 个人设")
    if await _name_taken(session, cleaned):
        raise PersonaLibraryError(f"人设库里已经有「{cleaned}」了", status=409)
    if is_global:
        await _clear_global(session)
    row = AgentPersonaTemplate(
        name=cleaned, card=card.storage_dict(), is_global=is_global, revision=1
    )
    session.add(row)
    await session.flush()
    return row


async def get_template(session: Any, template_id: int) -> AgentPersonaTemplate:
    row = await session.get(AgentPersonaTemplate, template_id)
    if row is None:
        raise PersonaLibraryError("人设库里没有这个人设", status=404)
    return row


def check_template_version(row: AgentPersonaTemplate, version: str | None) -> None:
    if version is not None and str(version) != str(int(row.revision or 1)):
        raise PersonaLibraryError("这个人设已被其他页面修改，请刷新后再保存", status=409)


async def update_template(  # noqa: PLR0913
    session: Any,
    template_id: int,
    *,
    version: str | None,
    name: str | None = None,
    card: PersonaCard | None = None,
    is_global: bool | None = None,
) -> AgentPersonaTemplate:
    row = await get_template(session, template_id)
    check_template_version(row, version)
    changed = False
    if name is not None:
        cleaned = _clean_name(name)
        if cleaned != row.name:
            if await _name_taken(session, cleaned, exclude_id=row.id):
                raise PersonaLibraryError(f"人设库里已经有「{cleaned}」了", status=409)
            row.name = cleaned
            changed = True
    if card is not None and card.storage_dict() != row.card:
        row.card = card.storage_dict()
        changed = True
    if is_global is not None and bool(is_global) != bool(row.is_global):
        if is_global:
            await _clear_global(session, keep_id=row.id)
        row.is_global = bool(is_global)
        changed = True
    if changed:
        row.revision = int(row.revision or 1) + 1
        await session.flush()
    return row


async def delete_template(
    session: Any, template_id: int, *, version: str | None
) -> bool:
    """删除人设；返回它是否曾是全局默认（调用方据此提示已回退内置默认）。"""

    row = await get_template(session, template_id)
    check_template_version(row, version)
    was_global = bool(row.is_global)
    await session.delete(row)
    await session.flush()
    return was_global


async def find_template_by_name(session: Any, name: str) -> AgentPersonaTemplate | None:
    cleaned = " ".join(str(name or "").split())
    if not cleaned:
        return None
    rows = await list_templates(session)
    if cleaned.isdigit():
        index = int(cleaned) - 1
        if 0 <= index < len(rows):
            return rows[index]
    for row in rows:
        if row.name.casefold() == cleaned.casefold():
            return row
    return None


@get_driver().on_startup
async def _load_global_persona() -> None:
    try:
        async with get_session() as session:
            await refresh_global_persona(session)
    except Exception:  # noqa: BLE001
        logger.warning("加载全局人设失败，使用内置默认", exc_info=True)
        set_global_persona_card(None)


__all__ = [
    "MAX_LIBRARY_ITEMS",
    "PersonaLibraryError",
    "check_template_version",
    "create_template",
    "delete_template",
    "find_template_by_name",
    "get_template",
    "list_templates",
    "refresh_global_persona",
    "serialize_template",
    "template_card",
    "update_template",
]
