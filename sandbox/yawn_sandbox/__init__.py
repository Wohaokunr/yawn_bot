"""YawnBot 代码沙箱执行服务（runner）。

只依赖标准库，运行在独立的、无网络的容器里，通过 Unix socket 接受 Bot 的
执行请求。每个工作区（通常一个群一个）映射到独立的系统 uid，用户代码以该
uid 在 rlimit 约束下运行，结束后整组进程连同该 uid 的残留进程一起清理。
"""

from __future__ import annotations

RUNNER_VERSION = "1.0.0"
API_VERSION = 1

__all__ = ["API_VERSION", "RUNNER_VERSION"]
