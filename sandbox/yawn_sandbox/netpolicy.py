"""出网策略：按工作区 uid 放行或拦截外网，并始终挡住内网地址。

容器接入一张独立的 bridge 网络（compose 的 ``sandbox-egress``，不和 Bot、NapCat
同网）后，用户代码就能 ``curl``/``pip`` 下载外网文件。runner 以 root 运行并带
``NET_ADMIN``，在容器自己的网络命名空间里维护两条 iptables 链：

- ``YAWN_SBX``：所有工作区 uid（``uid_base`` 起 ``uid_span`` 个）的出站流量都先经过它。
  回环（Docker 内置 DNS 127.0.0.11）放行；然后跳 ``YAWN_SBX_BLOCK``；最后拒绝
  内网/保留地址（宿主机、局域网、云厂商元数据 169.254.169.254 等）；其余放行。
- ``YAWN_SBX_BLOCK``：被关掉联网的工作区 uid 一条 REJECT。

每次执行前 ``apply(uid, allowed)`` 把该 uid 调成 Bot 要求的状态。用户代码以普通 uid、
无 capability、no-new-privileges 运行，改不了这些规则。

拿不到 iptables（老镜像、没给 NET_ADMIN）时 ``control`` 为 False：容器没网就一切照旧
（与 1.1 相同）；容器有网却管不住时，健康检查如实报出来，由 Bot/WebUI 决定是否继续用。
"""

from __future__ import annotations

import contextlib
import logging
import os
import shutil
import subprocess
import threading
from pathlib import Path
from typing import Any

logger = logging.getLogger("yawn_sandbox.net")

CHAIN = "YAWN_SBX"
BLOCK_CHAIN = "YAWN_SBX_BLOCK"
PRIVATE_V4 = (
    "0.0.0.0/8",
    "10.0.0.0/8",
    "100.64.0.0/10",
    "127.0.0.0/8",
    "169.254.0.0/16",
    "172.16.0.0/12",
    "192.0.0.0/24",
    "192.168.0.0/16",
    "198.18.0.0/15",
    "224.0.0.0/4",
    "240.0.0.0/4",
)
PRIVATE_V6 = ("::1/128", "fc00::/7", "fe80::/10", "ff00::/8", "::ffff:0:0/96")
_TIMEOUT = 5


def _env_bool(name: str, *, default: bool) -> bool:
    raw = os.environ.get(name, "").strip().lower()
    if not raw:
        return default
    return raw in {"1", "true", "yes", "on"}


def default_route() -> bool:
    """容器里有没有通往外面的默认路由（/proc/net/route 里目的地 00000000）。"""

    try:
        lines = Path("/proc/net/route").read_text(encoding="utf-8").splitlines()[1:]
    except OSError:
        return False
    for line in lines:
        fields = line.split()
        if len(fields) > 2 and fields[1] == "00000000" and fields[0] != "lo":
            return True
    return False


class NetPolicy:
    def __init__(
        self,
        *,
        uid_base: int,
        uid_span: int,
        enabled: bool | None = None,
        runner: Any = None,
    ) -> None:
        self.uid_base = uid_base
        self.uid_span = uid_span
        # SANDBOX_NETWORK_DEFAULT：Bot 没说（老版本 Bot）时是否放行外网。
        self.default_allowed = _env_bool("SANDBOX_NETWORK_DEFAULT", default=True)
        self.block_private = _env_bool("SANDBOX_BLOCK_PRIVATE", default=True)
        self._enabled = (
            enabled
            if enabled is not None
            else _env_bool("SANDBOX_NETWORK_CONTROL", default=True)
        )
        self._run = runner or self._exec
        self._lock = threading.Lock()
        self._blocked: set[int] = set()
        self.control = False
        self.error: str | None = None
        self.ipv6 = False

    # ── iptables ─────────────────────────────────────────────────────────

    @staticmethod
    def _exec(binary: str, args: list[str]) -> tuple[int, str]:
        path = shutil.which(binary)
        if path is None:
            return 127, f"{binary} 不存在"
        try:
            done = subprocess.run(
                [path, "-w", str(_TIMEOUT), *args],
                capture_output=True,
                text=True,
                timeout=_TIMEOUT + 5,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            return 1, str(exc)
        return done.returncode, (done.stderr or done.stdout or "").strip()

    def _both(self, args: list[str], *, v6_args: list[str] | None = None) -> bool:
        code, message = self._run("iptables", args)
        if code != 0:
            self.error = message[:200]
            return False
        if self.ipv6:
            self._run("ip6tables", v6_args if v6_args is not None else args)
        return True

    def setup(self) -> bool:
        """建链；失败（没权限/没 iptables）时 ``control=False`` 并记下原因。"""

        if not self._enabled:
            self.error = "已用 SANDBOX_NETWORK_CONTROL=false 关闭"
            return False
        if os.geteuid() != 0:
            self.error = "runner 不是 root，不能管理防火墙"
            return False
        code, message = self._run("iptables", ["-S", "OUTPUT"])
        if code != 0:
            self.error = message[:200] or "iptables 不可用"
            return False
        self.ipv6 = self._run("ip6tables", ["-S", "OUTPUT"])[0] == 0
        uid_range = f"{self.uid_base}-{self.uid_base + self.uid_span - 1}"
        for binary, ranges in (("iptables", PRIVATE_V4), ("ip6tables", PRIVATE_V6)):
            if binary == "ip6tables" and not self.ipv6:
                continue
            for chain in (CHAIN, BLOCK_CHAIN):
                if self._run(binary, ["-N", chain])[0] != 0:
                    self._run(binary, ["-F", chain])
            self._run(binary, ["-A", CHAIN, "-o", "lo", "-j", "RETURN"])
            self._run(binary, ["-A", CHAIN, "-j", BLOCK_CHAIN])
            if self.block_private:
                for cidr in ranges:
                    self._run(binary, ["-A", CHAIN, "-d", cidr, "-j", "REJECT"])
            self._run(binary, ["-A", CHAIN, "-j", "RETURN"])
            jump = ["OUTPUT", "-m", "owner", "--uid-owner", uid_range, "-j", CHAIN]
            if self._run(binary, ["-C", *jump])[0] != 0:
                code, message = self._run(binary, ["-I", *jump])
                if code != 0 and binary == "iptables":
                    self.error = message[:200]
                    return False
        self.control = True
        self.error = None
        self._blocked.clear()
        logger.info(
            "出网策略已就绪（内网拦截=%s，默认放行=%s）",
            self.block_private,
            self.default_allowed,
        )
        return True

    def apply(self, uid: int | None, allowed: bool | None) -> bool:  # noqa: FBT001
        """把某个工作区 uid 调成放行/拦截；返回策略是否真的生效（或无需生效）。"""

        want = self.default_allowed if allowed is None else bool(allowed)
        if uid is None:
            return want or not self.attached()
        if not self.control:
            # 管不住：容器本来就没网时，“不许联网”自然成立。
            return want or not self.attached()
        with self._lock:
            blocked = uid in self._blocked
            rule = [BLOCK_CHAIN, "-m", "owner", "--uid-owner", str(uid), "-j", "REJECT"]
            if want and blocked:
                with contextlib.suppress(Exception):
                    self._both(["-D", *rule])
                self._blocked.discard(uid)
            elif not want and not blocked:
                if not self._both(["-A", *rule]):
                    return False
                self._blocked.add(uid)
        return True

    @staticmethod
    def attached() -> bool:
        from .system import network_interfaces

        return bool(network_interfaces()) and default_route()

    def guarded(self) -> bool:
        """容器没网，或有网但内网地址被挡住（runner 管得住防火墙）。"""

        return not self.attached() or (self.control and self.block_private)

    def report(self) -> dict[str, Any]:
        return {
            "attached": self.attached(),
            "control": self.control,
            "block_private": self.block_private,
            "default_allowed": self.default_allowed,
            "guarded": self.guarded(),
            "ipv6": self.ipv6,
            "blocked_workspaces": len(self._blocked),
            "error": self.error,
        }


__all__ = ["BLOCK_CHAIN", "CHAIN", "NetPolicy", "default_route"]
