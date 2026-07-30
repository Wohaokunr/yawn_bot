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
                def _set_sqlite_pragma(dbapi_conn, _record):  # noqa: ANN001
                    cursor = dbapi_conn.cursor()
                    cursor.execute("PRAGMA journal_mode=WAL")
                    cursor.execute("PRAGMA busy_timeout=30000")
                    cursor.close()

                logger.debug("SQLite WAL 模式已启用")
    except Exception:
        logger.warning("启用 SQLite WAL 模式失败，使用默认配置")


def _load_sub_plugins() -> None:
    """加载可选业务子插件；子插件缺失或加载失败不影响 yawn_core。"""
    from pathlib import Path

    import nonebot

    if not (Path(__file__).parent / "yawn_werewolf").is_dir():
        return
    try:
        # 用 __name__ 推导模块路径：nonebot 以 CWD 相对路径注册插件
        # （如 src.plugins.yawn_core），硬编码包名会找不到模块
        plugin = nonebot.load_plugin(f"{__name__}.yawn_werewolf")
    except Exception:  # noqa: BLE001
        logger.warning("狼人杀子插件加载失败，已跳过")
        return
    if plugin is None:
        logger.warning("狼人杀子插件未能注册，已跳过")


_load_sub_plugins()
