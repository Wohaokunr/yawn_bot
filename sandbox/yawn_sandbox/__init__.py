"""YawnBot 代码沙箱执行服务（runner）。

只依赖标准库，运行在独立的容器里（1.2 起可接一张只出不进、挡住内网的网络），通过 Unix socket 接受 Bot 的
执行请求。每个工作区（通常一个群一个）映射到独立的系统 uid，用户代码以该
uid 在 rlimit 约束下运行，结束后整组进程连同该 uid 的残留进程一起清理。
"""

from __future__ import annotations

RUNNER_VERSION = "1.2.0"
API_VERSION = 1
# health 里报出的可选能力；Bot/WebUI 据此决定显示哪些操作（旧 runner 没有）。
FEATURES = (
    "shell",
    "upload_guard",
    "rename",
    "mkdir",
    "dir_summary",
    # 1.2：流式大文件上传、按工作区放行/拦截外网、能力包 yawnkit.caps
    "stream_upload",
    "network_policy",
    "capabilities",
)

__all__ = ["API_VERSION", "FEATURES", "RUNNER_VERSION"]
