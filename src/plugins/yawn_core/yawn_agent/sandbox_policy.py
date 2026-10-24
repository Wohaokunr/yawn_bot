# ruff: noqa: E501
"""代码沙箱的开放策略、限额与每日用量（WebUI 可改，不落数据库）。

设置与用量都是 ``data/agent_sandbox/`` 下的小 JSON 文件，原子写入；坏文件
一律回落默认值，不能让 Agent 因为一个配置文件起不来。

一次 ``run_python`` 的授权顺序：总开关 → 群开放范围 → 仅管理员 → 沙箱在线
与隔离自检 → 成员冷却 → 成员/群每日次数。次数在执行前预扣，执行没有真正
发生（沙箱繁忙、离线）时退还，和特权工具的配额语义一致。
"""

from __future__ import annotations

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
    disk_quota_mb: int = Field(default=200, ge=10, le=4096)
    max_output_chars: int = Field(default=3000, ge=500, le=12000)
    max_share_mb: int = Field(default=20, ge=1, le=32)
    allow_unisolated: bool = False

    def limits(self, *, timeout_seconds: int | None = None) -> dict[str, int]:
        timeout = self.timeout_seconds
        if timeout_seconds is not None:
            timeout = max(1, min(int(timeout_seconds), self.timeout_seconds))
        return {
            "timeout_seconds": timeout,
            "memory_mb": self.memory_mb,
            "disk_quota_mb": self.disk_quota_mb,
        }

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
            return {"day": today, "groups": {}, "members": {}}
        data.setdefault("groups", {})
        data.setdefault("members", {})
        return data

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

    def cooldown_remaining(self, key: str, seconds: int) -> float:
        if seconds <= 0:
            return 0.0
        last = self._last_run.get(key)
        if last is None:
            return 0.0
        return max(seconds - (time.monotonic() - last), 0.0)

    def mark_run(self, key: str) -> None:
        if len(self._last_run) >= _COOLDOWN_MEMORY:
            self._last_run.clear()
        self._last_run[key] = time.monotonic()


_store = _Store()


def load_settings() -> SandboxSettings:
    return _store.settings()


def save_settings(settings: SandboxSettings) -> SandboxSettings:
    return _store.save_settings(settings)


def usage_snapshot() -> dict[str, Any]:
    return _store.usage()


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

    def refund(self) -> None:
        if self.refunded:
            return
        self.refunded = True
        _store.adjust(self.group_id, self.user_id, -1)


async def reserve_run(  # noqa: C901
    group_id: int, user_id: int | None, *, actor_is_admin: bool
) -> RunReservation:
    """检查策略并预扣一次执行；任何不满足都抛 ``SandboxPolicyError``。"""

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
    remaining = _store.cooldown_remaining(cooldown_key, settings.member_cooldown_seconds)
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
    _store.mark_run(cooldown_key)
    return RunReservation(group_id, user_id, settings)


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


def reset_state_for_tests() -> None:
    global _store  # noqa: PLW0603
    _store = _Store()


__all__ = [
    "STATE_DIR",
    "RunReservation",
    "SandboxPolicyError",
    "SandboxSettings",
    "exposure_reason",
    "group_usage",
    "load_settings",
    "member_usage",
    "quota_status",
    "reserve_run",
    "save_settings",
    "usage_snapshot",
]
