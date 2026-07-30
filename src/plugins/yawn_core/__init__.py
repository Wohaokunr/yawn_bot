from nonebot import get_driver, logger
from sqlalchemy import event

from . import (
    ai_chat,
    checkin,
    friend_approve,
    help_panel,
    panel,
    permission,
    presence,
)

__all__ = [
    "ai_chat",
    "checkin",
    "friend_approve",
    "help_panel",
    "panel",
    "permission",
    "presence",
]


@get_driver().on_startup
async def _enable_sqlite_wal() -> None:
    """启用 SQLite WAL 模式，提升并发读写性能。"""
    try:
        import nonebot_plugin_orm

        for eng in nonebot_plugin_orm._engines.values():
            if "sqlite" in str(eng.url):

                @event.listens_for(eng.sync_engine, "connect")
                def _set_sqlite_pragma(dbapi_conn, _record):  # noqa: ANN001, ANN202
                    cursor = dbapi_conn.cursor()
                    cursor.execute("PRAGMA journal_mode=WAL")
                    cursor.execute("PRAGMA busy_timeout=30000")
                    cursor.close()

                logger.debug("SQLite WAL 模式已启用")
    except Exception:
        logger.warning("启用 SQLite WAL 模式失败，使用默认配置")

