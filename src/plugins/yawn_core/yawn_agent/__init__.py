# ruff: noqa: E501,F401,I001,TID252,PLR0912,PLR0913,PLR0917,C901,TRY003,TRY004,TRY300,TRY301,ASYNC240,UP035,PGH004,ANN001,ANN201,ANN202,ARG001,FBT001,FBT002,COM812,RUF001,RUF100
"""QQ 群聊 Agent 子插件。

该包把 OneBot V11 的消息解析、群聊上下文、记忆和工具调用收敛为独立
子插件；所有持久化仍使用 yawn_core 的 ORM bind。
"""

from . import agent, capabilities, collector, commands, context, media, memory, message_parser, persona, proactive, prompt, tools

__all__ = [
    "agent",
    "capabilities",
    "collector",
    "commands",
    "context",
    "media",
    "memory",
    "message_parser",
    "persona",
    "proactive",
    "prompt",
    "tools",
]

