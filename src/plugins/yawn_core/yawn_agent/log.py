"""yawn_agent 调试日志开关。

设置环境变量 ``AGENT_DEBUG_LOG=true`` 后,本子插件会输出海量调试日志。
由于 nonebot 的 logger 按全局 ``LOG_LEVEL`` 过滤,``logger.debug`` 在默认
INFO 级别下不会显示,因此这里统一以 INFO 级别输出并带 ``[agent-debug]``
前缀,单一开关即可生效,也方便 grep。

注意:开关打开时日志会包含完整群消息内容与 LLM 回复,仅供本地排查使用。
"""

import os

from nonebot import get_driver, logger

_TRUE_VALUES = frozenset({"1", "true", "yes", "on"})


def _as_bool(value: object) -> bool:
    if isinstance(value, bool):
        return value
    return str(value or "").strip().lower() in _TRUE_VALUES


def _debug_enabled() -> bool:
    """Read the switch from NoneBot config, with an early-import fallback."""
    try:
        configured = getattr(get_driver().config, "agent_debug_log", None)
    except ValueError:
        configured = None
    if configured is None:
        configured = os.environ.get("AGENT_DEBUG_LOG")
    return _as_bool(configured)


def dbg(message: str) -> None:
    """开关打开时输出调试日志(INFO 级别,带 [agent-debug] 前缀)。"""
    if _debug_enabled():
        logger.info(f"[agent-debug] {message}")


def dbg_exc(message: str) -> None:
    """同 dbg,附带当前异常栈。"""
    if _debug_enabled():
        logger.opt(exception=True).info(f"[agent-debug] {message}")
