from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

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
    reminder,
)

__all__ = [
    "SubPluginLoadStatus",
    "ai_chat",
    "checkin",
    "friend_approve",
    "get_sub_plugin_load_report",
    "help_panel",
    "panel",
    "permission",
    "presence",
    "reminder",
]


SubPluginLoadState = Literal["loaded", "missing", "failed"]


@dataclass(frozen=True, slots=True)
class SubPluginLoadStatus:
    """一次可选子插件加载尝试的结果。"""

    module_name: str
    label: str
    state: SubPluginLoadState
    detail: str | None = None


_SUB_PLUGIN_SPECS = (
    ("yawn_werewolf", "狼人杀"),
    ("yawn_rpg", "跑团"),
)
_SUB_PLUGIN_LOAD_REPORT: tuple[SubPluginLoadStatus, ...] = ()


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
    except Exception:  # noqa: BLE001
        logger.warning("启用 SQLite WAL 模式失败，使用默认配置")


def _load_sub_plugins(
    *,
    load_plugin: Callable[[str], object | None] | None = None,
    package_dir: Path | None = None,
) -> tuple[SubPluginLoadStatus, ...]:
    """加载可选业务子插件；子插件缺失或加载失败不影响 yawn_core。"""
    import nonebot

    package_dir = package_dir or Path(__file__).parent
    load_plugin = load_plugin or nonebot.load_plugin
    report: list[SubPluginLoadStatus] = []

    # 用 __name__ 推导模块路径：nonebot 以 CWD 相对路径注册插件
    # （如 src.plugins.yawn_core），硬编码包名会找不到模块。
    # 子插件逐个隔离加载，某个目录缺失或导入失败不能阻断其他玩法。
    for dirname, label in _SUB_PLUGIN_SPECS:
        module_name = f"{__name__}.{dirname}"
        if not (package_dir / dirname).is_dir():
            report.append(
                SubPluginLoadStatus(module_name, label, "missing", "目录不存在")
            )
            continue
        try:
            plugin = load_plugin(module_name)
        except Exception as exc:  # noqa: BLE001
            detail = f"{type(exc).__name__}: {exc}"
            report.append(SubPluginLoadStatus(module_name, label, "failed", detail))
            logger.warning(f"{label}子插件加载失败，已跳过：{detail}", exc_info=True)
            continue
        if plugin is None:
            detail = "NoneBot 未返回已注册插件"
            report.append(SubPluginLoadStatus(module_name, label, "failed", detail))
            logger.warning(f"{label}子插件未能注册，已跳过：{detail}")
            continue
        report.append(SubPluginLoadStatus(module_name, label, "loaded"))

    return tuple(report)


def get_sub_plugin_load_report() -> tuple[SubPluginLoadStatus, ...]:
    """返回启动时记录的子插件加载报告，供诊断面板和健康检查使用。"""

    return _SUB_PLUGIN_LOAD_REPORT


@get_driver().on_startup
async def _report_sub_plugin_status() -> None:
    """在启动日志中汇总可选子插件状态，避免失败只表现为功能缺失。"""

    loaded = [item.label for item in _SUB_PLUGIN_LOAD_REPORT if item.state == "loaded"]
    missing = [
        item.label for item in _SUB_PLUGIN_LOAD_REPORT if item.state == "missing"
    ]
    failed = [item.label for item in _SUB_PLUGIN_LOAD_REPORT if item.state == "failed"]
    details = [
        f"{item.label}（{item.detail}）"
        for item in _SUB_PLUGIN_LOAD_REPORT
        if item.state == "failed" and item.detail
    ]
    summary = (
        f"已加载={','.join(loaded) or '无'}；"
        f"缺失={','.join(missing) or '无'}；"
        f"失败={','.join(failed) or '无'}"
    )
    if failed:
        logger.error(f"yawn_core 子插件启动报告：{summary}。详情：{'；'.join(details)}")
    else:
        logger.info(f"yawn_core 子插件启动报告：{summary}")


_SUB_PLUGIN_LOAD_REPORT = _load_sub_plugins()
