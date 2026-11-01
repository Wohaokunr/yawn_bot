# ruff: noqa: E501
"""代码沙箱的开放策略、限额与每日用量（WebUI 可改，不落数据库）。

设置与用量都是 ``data/agent_sandbox/`` 下的小 JSON 文件，原子写入；坏文件
一律回落默认值，不能让 Agent 因为一个配置文件起不来。

一次 ``run_python`` 的授权顺序：总开关 → 群开放范围 → 仅管理员 → 沙箱在线
与隔离自检 → 成员冷却 → 成员/群每日次数。次数在执行前预扣，执行没有真正
发生（沙箱繁忙、离线）时退还，和特权工具的配额语义一致。

冷却只用来防刷，不该让一次正常的“报错 → 改代码 → 重跑”失败：同一回合里的
重跑不受冷却限制；跨回合撞上冷却时，剩余时间在 ``MAX_COOLDOWN_WAIT`` 秒以内
就等一会儿再跑，而不是把“运行得太频繁了”甩给模型。
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from .context import now_beijing
from .sandbox_client import SandboxError, cached_health

PROJECT_ROOT = Path(__file__).resolve().parents[4]
STATE_DIR = PROJECT_ROOT / "data" / "agent_sandbox"
_COOLDOWN_MEMORY = 2048
INBOX_MEDIA_TYPES = ("image", "file", "record", "video")
InboxAutoImport = Literal["always", "sandbox_turns", "off"]
# 撞上冷却时最多替模型等多久；更长的冷却（管理员调大了）才报错。
MAX_COOLDOWN_WAIT = 10.0


class SandboxSettings(BaseModel):
    """WebUI「代码沙箱 → 策略与限额」的全部字段。"""

    model_config = ConfigDict(extra="ignore")

    enabled: bool = True
    group_mode: Literal["all", "allowlist"] = "all"
    allowed_groups: list[int] = Field(default_factory=list, max_length=500)
    blocked_groups: list[int] = Field(default_factory=list, max_length=500)
    admin_only: bool = False
    member_daily_runs: int = Field(default=20, ge=0, le=1000)
    group_daily_runs: int = Field(default=120, ge=0, le=10000)
    member_cooldown_seconds: int = Field(default=5, ge=0, le=600)
    timeout_seconds: int = Field(default=30, ge=5, le=90)
    memory_mb: int = Field(default=1024, ge=128, le=4096)
    disk_quota_mb: int = Field(default=200, ge=10, le=65536)
    max_output_chars: int = Field(default=6000, ge=500, le=12000)
    max_share_mb: int = Field(default=20, ge=1, le=32)
    allow_unisolated: bool = False
    # ── 群聊附件入箱（图片/文件/语音/视频 → 工作区 inbox/）──────────────────
    inbox_enabled: bool = True
    # always：沙箱开放的群里每个明确触发的回合都先入箱；sandbox_turns：本轮
    # 装载了沙箱工具才入箱（默认）；off：只在模型调用 import_attachments 时入箱。
    inbox_auto_import: InboxAutoImport = "sandbox_turns"
    inbox_types: list[Literal["image", "file", "record", "video"]] = Field(
        default_factory=lambda: list(INBOX_MEDIA_TYPES)
    )
    inbox_max_file_mb: int = Field(default=20, ge=1, le=4096)
    inbox_max_per_turn: int = Field(default=6, ge=1, le=20)
    inbox_history_minutes: int = Field(default=180, ge=0, le=2880)
    inbox_group_daily_mb: int = Field(default=500, ge=0, le=20000)
    # ── 私聊虚拟电脑（每位用户一个独立工作区 p<QQ>）──────────────────────────
    private_enabled: bool = True
    private_daily_runs: int = Field(default=500, ge=0, le=10000)
    # 私聊发来的图片/文件自动放进他的电脑 inbox/
    private_inbox: bool = True
    private_inbox_daily_mb: int = Field(default=4096, ge=0, le=100000)
    # 私聊单个附件入箱上限（MB）：APK、安装包、压缩包这类大文件也能放进去。
    private_inbox_max_file_mb: int = Field(default=1024, ge=1, le=4096)
    # 每人工作区总空间（MB）与用户代码能写出的单个文件大小（MB）。
    private_disk_quota_mb: int = Field(default=5120, ge=50, le=65536)
    private_file_mb: int = Field(default=1024, ge=16, le=4096)
    # ── 虚拟电脑联网（容器接了出网网络时生效；内网地址始终挡住）──────────────
    # 私聊每人的电脑默认能上网下载文件；群聊工作区是群成员共用的，默认不放开。
    private_network: bool = True
    group_network: bool = False

    def limits(self, *, timeout_seconds: int | None = None) -> dict[str, int]:
        timeout = self.timeout_seconds
        if timeout_seconds is not None:
            timeout = max(1, min(int(timeout_seconds), self.timeout_seconds))
        return {
            "timeout_seconds": timeout,
            "memory_mb": self.memory_mb,
            "disk_quota_mb": self.disk_quota_mb,
        }

    def private_limits(self, *, timeout_seconds: int | None = None) -> dict[str, int]:
        limits = self.limits(timeout_seconds=timeout_seconds)
        limits["disk_quota_mb"] = self.private_disk_quota_mb
        limits["file_mb"] = self.private_file_mb
        return limits

    def inbox_allows(self, media_type: str) -> bool:
        return self.inbox_enabled and media_type in self.inbox_types

    def group_open(self, group_id: int) -> bool:
        if group_id in self.blocked_groups:
            return False
        if self.group_mode == "allowlist":
            return group_id in self.allowed_groups
        return True


class _Store:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._settings: SandboxSettings | None = None
        self._settings_mtime: float | None = None
        self._last_run: dict[str, float] = {}

    # 路径每次现算，测试可以 monkeypatch STATE_DIR。
    @staticmethod
    def settings_path() -> Path:
        return STATE_DIR / "settings.json"

    @staticmethod
    def usage_path() -> Path:
        return STATE_DIR / "usage.json"

    @staticmethod
    def _write_json(path: Path, data: dict[str, Any]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(".tmp")
        temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        temporary.replace(path)

    def settings(self) -> SandboxSettings:
        path = self.settings_path()
        try:
            mtime = path.stat().st_mtime
        except OSError:
            mtime = None
        with self._lock:
            if self._settings is not None and mtime == self._settings_mtime:
                return self._settings
            settings = SandboxSettings()
            if mtime is not None:
                with contextlib.suppress(OSError, ValueError, ValidationError):
                    settings = SandboxSettings.model_validate(
                        json.loads(path.read_text(encoding="utf-8"))
                    )
            self._settings = settings
            self._settings_mtime = mtime
            return settings

    def save_settings(self, settings: SandboxSettings) -> SandboxSettings:
        with self._lock:
            self._write_json(self.settings_path(), settings.model_dump())
            self._settings = settings
            with contextlib.suppress(OSError):
                self._settings_mtime = self.settings_path().stat().st_mtime
        return settings

    def _load_usage(self) -> dict[str, Any]:
        today = now_beijing().strftime("%Y-%m-%d")
        try:
            data = json.loads(self.usage_path().read_text(encoding="utf-8"))
        except (OSError, ValueError):
            data = {}
        if not isinstance(data, dict) or data.get("day") != today:
            return {
                "day": today,
                "groups": {},
                "members": {},
                "inbox": {},
                "private": {},
                "private_inbox": {},
            }
        data.setdefault("groups", {})
        data.setdefault("members", {})
        for key in ("private", "private_inbox"):
            if not isinstance(data.get(key), dict):
                data[key] = {}
        if not isinstance(data.get("inbox"), dict):
            data["inbox"] = {}
        return data

    def add_inbox(self, group_id: int, *, files: int, size: int) -> dict[str, int]:
        """记一笔入箱用量（文件数与字节数），按北京时间自然日清零。"""

        with self._lock:
            data = self._load_usage()
            row = data["inbox"].get(str(group_id))
            row = dict(row) if isinstance(row, dict) else {}
            row["files"] = max(int(row.get("files") or 0) + files, 0)
            row["bytes"] = max(int(row.get("bytes") or 0) + size, 0)
            data["inbox"][str(group_id)] = row
            with contextlib.suppress(OSError):
                self._write_json(self.usage_path(), data)
            return row

    def usage(self) -> dict[str, Any]:
        with self._lock:
            return self._load_usage()

    def adjust(self, group_id: int, user_id: int | None, delta: int) -> dict[str, Any]:
        with self._lock:
            data = self._load_usage()
            group_key = str(group_id)
            data["groups"][group_key] = max(int(data["groups"].get(group_key, 0)) + delta, 0)
            if user_id is not None:
                member_key = f"{group_id}:{user_id}"
                data["members"][member_key] = max(
                    int(data["members"].get(member_key, 0)) + delta, 0
                )
            with contextlib.suppress(OSError):
                self._write_json(self.usage_path(), data)
            return data

    def adjust_private(self, user_id: int, delta: int) -> int:
        with self._lock:
            data = self._load_usage()
            key = str(int(user_id))
            data["private"][key] = max(int(data["private"].get(key, 0)) + delta, 0)
            with contextlib.suppress(OSError):
                self._write_json(self.usage_path(), data)
            return int(data["private"][key])

    def add_private_inbox(self, user_id: int, size: int) -> int:
        with self._lock:
            data = self._load_usage()
            key = str(int(user_id))
            data["private_inbox"][key] = max(
                int(data["private_inbox"].get(key, 0)) + size, 0
            )
            with contextlib.suppress(OSError):
                self._write_json(self.usage_path(), data)
            return int(data["private_inbox"][key])

    def cooldown_remaining(self, key: str, seconds: int) -> float:
        if seconds <= 0:
            return 0.0
        last = self._last_run.get(key)
        if last is None:
            return 0.0
        return max(seconds - (time.monotonic() - last), 0.0)

    def mark_run(self, key: str) -> float | None:
        """记录一次运行；返回之前的时间戳，退还时用来恢复。"""

        previous = self._last_run.get(key)
        if len(self._last_run) >= _COOLDOWN_MEMORY:
            self._last_run.clear()
        self._last_run[key] = time.monotonic()
        return previous

    def restore_run(self, key: str, previous: float | None) -> None:
        if previous is None:
            self._last_run.pop(key, None)
        else:
            self._last_run[key] = previous


_store = _Store()


def load_settings() -> SandboxSettings:
    return _store.settings()


def save_settings(settings: SandboxSettings) -> SandboxSettings:
    return _store.save_settings(settings)


def usage_snapshot() -> dict[str, Any]:
    return _store.usage()


def inbox_usage(group_id: int) -> dict[str, int]:
    row = _store.usage()["inbox"].get(str(group_id))
    if not isinstance(row, dict):
        return {"files": 0, "bytes": 0}
    return {"files": int(row.get("files") or 0), "bytes": int(row.get("bytes") or 0)}


def record_inbox_usage(group_id: int, *, files: int, size: int) -> dict[str, int]:
    return _store.add_inbox(group_id, files=files, size=size)


def group_usage(group_id: int) -> int:
    return int(_store.usage()["groups"].get(str(group_id), 0))


def member_usage(group_id: int, user_id: int) -> int:
    return int(_store.usage()["members"].get(f"{group_id}:{user_id}", 0))


class SandboxPolicyError(PermissionError):
    """策略拒绝；``reason`` 是给发现/前端用的机器可读原因。"""

    def __init__(self, reason: str, message: str) -> None:
        super().__init__(message)
        self.reason = reason


def exposure_reason(
    group_id: int,
    *,
    actor_is_admin: bool,
    health: dict[str, Any] | None,
    health_error: SandboxError | None,
) -> str | None:
    """工具发现阶段：sandbox 工具族当前能不能用；None 表示可用。"""

    settings = load_settings()
    if not settings.enabled or not settings.group_open(group_id):
        return "sandbox_closed"
    if settings.admin_only and not actor_is_admin:
        return "actor_not_admin"
    if health_error is not None or health is None:
        return "sandbox_offline"
    if not settings.allow_unisolated and not _isolated(health):
        return "sandbox_unsafe"
    return None


def _isolated(health: dict[str, Any]) -> bool:
    isolation = health.get("isolation") if isinstance(health, dict) else None
    return bool(isinstance(isolation, dict) and isolation.get("isolated"))


@dataclass(slots=True)
class RunReservation:
    group_id: int
    user_id: int | None
    settings: SandboxSettings
    refunded: bool = False
    previous_run: float | None = None
    waited_seconds: float = 0.0

    def refund(self) -> None:
        """代码根本没跑：退还次数，冷却也恢复成这次之前的状态。"""

        if self.refunded:
            return
        self.refunded = True
        _store.adjust(self.group_id, self.user_id, -1)
        if self.user_id is not None:
            _store.restore_run(f"{self.group_id}:{self.user_id}", self.previous_run)


async def reserve_run(  # noqa: C901
    group_id: int,
    user_id: int | None,
    *,
    actor_is_admin: bool,
    skip_cooldown: bool = False,
    wait_cooldown: bool = True,
) -> RunReservation:
    """检查策略并预扣一次执行；任何不满足都抛 ``SandboxPolicyError``。

    ``skip_cooldown``：同一回合里的重跑（调用方据回合账本判断）。
    ``wait_cooldown``：剩余冷却不超过 ``MAX_COOLDOWN_WAIT`` 时等它结束。
    """

    settings = load_settings()
    if not settings.enabled:
        raise SandboxPolicyError("sandbox_closed", "代码沙箱已被管理员关闭")
    if not settings.group_open(group_id):
        raise SandboxPolicyError("sandbox_closed", "本群没有开放代码沙箱")
    if user_id is None:
        raise SandboxPolicyError("no_actor", "代码沙箱需要明确的群成员调用者")
    if settings.admin_only and not actor_is_admin:
        raise SandboxPolicyError("actor_not_admin", "本群的代码沙箱仅限群主或管理员使用")
    health, error = await cached_health()
    if error is not None or health is None:
        raise SandboxPolicyError(
            "sandbox_offline", f"代码沙箱当前不可用：{error or '未知原因'}"
        )
    if not settings.allow_unisolated and not _isolated(health):
        raise SandboxPolicyError(
            "sandbox_unsafe", "代码沙箱的隔离自检没有通过，管理员需要先检查部署"
        )
    if not health.get("accepting_runs", True):
        raise SandboxPolicyError("sandbox_unsafe", "代码沙箱当前拒绝执行，请联系管理员")
    cooldown_key = f"{group_id}:{user_id}"
    waited = 0.0
    remaining = (
        0.0
        if skip_cooldown
        else _store.cooldown_remaining(cooldown_key, settings.member_cooldown_seconds)
    )
    if remaining > 0 and wait_cooldown and remaining <= MAX_COOLDOWN_WAIT:
        await asyncio.sleep(remaining)
        waited = remaining
        remaining = 0.0
    if remaining > 0:
        raise SandboxPolicyError(
            "cooldown", f"运行得太频繁了，请 {remaining:.0f} 秒后再试（配额冷却）"
        )
    usage = _store.usage()
    used_member = int(usage["members"].get(cooldown_key, 0))
    used_group = int(usage["groups"].get(str(group_id), 0))
    if used_member >= settings.member_daily_runs:
        raise SandboxPolicyError(
            "member_quota",
            f"你今天的代码沙箱次数已用完（配额 {settings.member_daily_runs} 次）",
        )
    if used_group >= settings.group_daily_runs:
        raise SandboxPolicyError(
            "group_quota",
            f"本群今天的代码沙箱次数已用完（配额 {settings.group_daily_runs} 次）",
        )
    _store.adjust(group_id, user_id, 1)
    previous = _store.mark_run(cooldown_key)
    return RunReservation(
        group_id, user_id, settings, previous_run=previous, waited_seconds=waited
    )


def quota_status(group_id: int, user_id: int | None) -> dict[str, Any]:
    settings = load_settings()
    usage = _store.usage()
    used_group = int(usage["groups"].get(str(group_id), 0))
    status: dict[str, Any] = {
        "group_used": used_group,
        "group_limit": settings.group_daily_runs,
    }
    if user_id is not None:
        used_member = int(usage["members"].get(f"{group_id}:{user_id}", 0))
        status.update(
            member_used=used_member,
            member_limit=settings.member_daily_runs,
            member_left=max(settings.member_daily_runs - used_member, 0),
        )
    return status


# ── 私聊虚拟电脑 ────────────────────────────────────────────────────────


def private_exposure_reason(
    health: dict[str, Any] | None, health_error: SandboxError | None
) -> str | None:
    """私聊里虚拟电脑当前能不能用；None 表示可用。"""

    settings = load_settings()
    if (
        not settings.enabled
        or not settings.private_enabled
        or settings.private_daily_runs <= 0
    ):
        return "sandbox_closed"
    if health_error is not None or health is None:
        return "sandbox_offline"
    if not settings.allow_unisolated and not _isolated(health):
        return "sandbox_unsafe"
    return None


@dataclass(slots=True)
class PrivateRunReservation:
    user_id: int
    settings: SandboxSettings
    refunded: bool = False
    previous_run: float | None = None

    def refund(self) -> None:
        if self.refunded:
            return
        self.refunded = True
        _store.adjust_private(self.user_id, -1)
        _store.restore_run(f"p:{self.user_id}", self.previous_run)


async def reserve_private_run(
    user_id: int, *, skip_cooldown: bool = False
) -> PrivateRunReservation:
    """私聊运行一次代码：总开关 → 私聊开关 → 在线与隔离 → 冷却 → 每人每日次数。"""

    settings = load_settings()
    if not settings.enabled or not settings.private_enabled:
        raise SandboxPolicyError("sandbox_closed", "管理员没有开放私聊里的虚拟电脑")
    health, error = await cached_health()
    if error is not None or health is None:
        raise SandboxPolicyError(
            "sandbox_offline", f"代码沙箱当前不可用：{error or '未知原因'}"
        )
    if not settings.allow_unisolated and not _isolated(health):
        raise SandboxPolicyError(
            "sandbox_unsafe", "代码沙箱的隔离自检没有通过，管理员需要先检查部署"
        )
    if not health.get("accepting_runs", True):
        raise SandboxPolicyError("sandbox_unsafe", "代码沙箱当前拒绝执行，请联系管理员")
    key = f"p:{user_id}"
    remaining = (
        0.0
        if skip_cooldown
        else _store.cooldown_remaining(key, settings.member_cooldown_seconds)
    )
    if 0 < remaining <= MAX_COOLDOWN_WAIT:
        await asyncio.sleep(remaining)
        remaining = 0.0
    if remaining > 0:
        raise SandboxPolicyError(
            "cooldown", f"运行得太频繁了，请 {remaining:.0f} 秒后再试（配额冷却）"
        )
    used = int(_store.usage()["private"].get(str(int(user_id)), 0))
    if used >= settings.private_daily_runs:
        raise SandboxPolicyError(
            "member_quota",
            f"你今天在私聊里的虚拟电脑次数已用完（配额 {settings.private_daily_runs} 次）",
        )
    _store.adjust_private(user_id, 1)
    previous = _store.mark_run(key)
    return PrivateRunReservation(int(user_id), settings, previous_run=previous)


def private_usage(user_id: int) -> dict[str, int]:
    settings = load_settings()
    usage = _store.usage()
    used = int(usage["private"].get(str(int(user_id)), 0))
    return {
        "used": used,
        "limit": settings.private_daily_runs,
        "left": max(settings.private_daily_runs - used, 0),
        "inbox_bytes": int(usage["private_inbox"].get(str(int(user_id)), 0)),
    }


def record_private_inbox(user_id: int, size: int) -> int:
    return _store.add_private_inbox(user_id, size)


def reset_state_for_tests() -> None:
    global _store  # noqa: PLW0603
    _store = _Store()


__all__ = [
    "INBOX_MEDIA_TYPES",
    "MAX_COOLDOWN_WAIT",
    "STATE_DIR",
    "PrivateRunReservation",
    "RunReservation",
    "SandboxPolicyError",
    "SandboxSettings",
    "exposure_reason",
    "group_usage",
    "inbox_usage",
    "load_settings",
    "member_usage",
    "private_exposure_reason",
    "private_usage",
    "quota_status",
    "record_inbox_usage",
    "record_private_inbox",
    "reserve_private_run",
    "reserve_run",
    "save_settings",
    "usage_snapshot",
]
