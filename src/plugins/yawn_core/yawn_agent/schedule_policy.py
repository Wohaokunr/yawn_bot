# ruff: noqa: E501
"""Agent 定时任务的开放策略、限额与每日用量（WebUI 可改，不落数据库）。

和代码沙箱一样，设置与用量是 ``data/agent_schedule/`` 下的小 JSON 文件，原子
写入；坏文件一律回落默认值。任务本身在数据表 ``AgentScheduledTask`` 里。

策略只收窄能力：总开关、群开放范围、谁能建任务、每群/每人同时有效的任务数、
最小触发间隔、最远可以排到多久以后、每群每天最多执行几次、错过触发怎么补、
连续失败几次自动暂停、夜间免打扰。
"""

from __future__ import annotations

import contextlib
import json
import threading
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from .context import now_beijing

PROJECT_ROOT = Path(__file__).resolve().parents[4]
STATE_DIR = PROJECT_ROOT / "data" / "agent_schedule"
_CLOCK_PATTERN = r"^([01]\d|2[0-3]):[0-5]\d$"


class ScheduleSettings(BaseModel):
    """WebUI「定时任务 → 策略与限额」的全部字段。"""

    model_config = ConfigDict(extra="ignore")

    enabled: bool = True
    group_mode: Literal["all", "allowlist"] = "all"
    allowed_groups: list[int] = Field(default_factory=list, max_length=500)
    blocked_groups: list[int] = Field(default_factory=list, max_length=500)
    # members：任何群成员都能让 Bot 设定时任务；admins：仅群主/管理员。
    who_can_create: Literal["members", "admins"] = "members"
    # 关掉后只能建 notify（到点直接发提醒，不调模型、不用工具）。
    allow_agent_mode: bool = True
    max_active_per_group: int = Field(default=20, ge=1, le=500)
    max_active_per_member: int = Field(default=5, ge=1, le=100)
    # 循环任务两次触发之间至少隔多久；群管理员建的任务也受它约束。
    min_interval_minutes: int = Field(default=10, ge=1, le=1440)
    max_horizon_days: int = Field(default=366, ge=1, le=730)
    group_daily_runs: int = Field(default=60, ge=1, le=2000)
    # 进程停机/繁忙导致超过宽限还没执行：skip 直接跳到下一次；run_once 补跑一次。
    misfire_grace_minutes: int = Field(default=10, ge=1, le=1440)
    missed_policy: Literal["skip", "run_once"] = "run_once"
    # 补跑最多补多久以前错过的触发；更早的直接跳过，免得半夜醒来补一堆早安。
    catch_up_max_hours: int = Field(default=6, ge=0, le=72)
    max_consecutive_failures: int = Field(default=3, ge=1, le=20)
    quiet_hours_enabled: bool = False
    quiet_start: str = Field(default="01:00", pattern=_CLOCK_PATTERN)
    quiet_end: str = Field(default="07:00", pattern=_CLOCK_PATTERN)
    # 落在免打扰时段的触发：defer 推迟到时段结束；skip 直接跳过这一次。
    quiet_policy: Literal["defer", "skip"] = "defer"
    # 同一次 tick 最多并行执行几个群的任务（同一个群总是串行，共用群锁）。
    concurrency: int = Field(default=2, ge=1, le=8)
    # 单次 Agent 唤醒的硬时限（秒）；对话自己的回合时限通常更短。
    run_timeout_seconds: int = Field(default=300, ge=30, le=1800)
    notify_prefix: str = Field(default="⏰ ", max_length=8)
    history_limit: int = Field(default=20, ge=5, le=100)

    @field_validator("allowed_groups", "blocked_groups")
    @classmethod
    def _dedupe(cls, value: list[int]) -> list[int]:
        return list(dict.fromkeys(int(item) for item in value if int(item) > 0))

    def group_open(self, group_id: int) -> bool:
        if group_id in self.blocked_groups:
            return False
        if self.group_mode == "allowlist":
            return group_id in self.allowed_groups
        return True

    def in_quiet_hours(self, clock: str) -> bool:
        if not self.quiet_hours_enabled or self.quiet_start == self.quiet_end:
            return False
        if self.quiet_start < self.quiet_end:
            return self.quiet_start <= clock < self.quiet_end
        return clock >= self.quiet_start or clock < self.quiet_end


class _Store:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._settings: ScheduleSettings | None = None
        self._settings_mtime: float | None = None

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

    def settings(self) -> ScheduleSettings:
        path = self.settings_path()
        try:
            mtime = path.stat().st_mtime
        except OSError:
            mtime = None
        with self._lock:
            if self._settings is not None and mtime == self._settings_mtime:
                return self._settings
            settings = ScheduleSettings()
            if mtime is not None:
                with contextlib.suppress(OSError, ValueError, ValidationError):
                    settings = ScheduleSettings.model_validate(
                        json.loads(path.read_text(encoding="utf-8"))
                    )
            self._settings = settings
            self._settings_mtime = mtime
            return settings

    def save_settings(self, settings: ScheduleSettings) -> ScheduleSettings:
        with self._lock:
            self._write_json(self.settings_path(), settings.model_dump())
            self._settings = settings
            with contextlib.suppress(OSError):
                self._settings_mtime = self.settings_path().stat().st_mtime
        return settings

    def reset_cache(self) -> None:
        with self._lock:
            self._settings = None
            self._settings_mtime = None

    def _load_usage(self) -> dict[str, Any]:
        today = now_beijing().strftime("%Y-%m-%d")
        try:
            data = json.loads(self.usage_path().read_text(encoding="utf-8"))
        except (OSError, ValueError):
            data = {}
        if not isinstance(data, dict) or data.get("day") != today:
            return {"day": today, "groups": {}, "results": {}}
        if not isinstance(data.get("groups"), dict):
            data["groups"] = {}
        if not isinstance(data.get("results"), dict):
            data["results"] = {}
        return data

    def usage(self) -> dict[str, Any]:
        with self._lock:
            return self._load_usage()

    def record(self, group_id: int, result: str, *, counts: bool) -> None:
        with self._lock:
            data = self._load_usage()
            if counts:
                key = str(group_id)
                data["groups"][key] = int(data["groups"].get(key, 0)) + 1
            data["results"][result] = int(data["results"].get(result, 0)) + 1
            with contextlib.suppress(OSError):
                self._write_json(self.usage_path(), data)


_store = _Store()


def load_settings() -> ScheduleSettings:
    return _store.settings()


def save_settings(settings: ScheduleSettings) -> ScheduleSettings:
    return _store.save_settings(settings)


def reset_settings_cache() -> None:
    _store.reset_cache()


def usage_snapshot() -> dict[str, Any]:
    return _store.usage()


def group_runs_today(group_id: int) -> int:
    return int(_store.usage()["groups"].get(str(group_id), 0))


def record_run_usage(group_id: int, result: str, *, counts: bool) -> None:
    """记一次执行结果；``counts`` 为真时计入本群今日执行次数。"""

    _store.record(group_id, result, counts=counts)


def exposure_reason(group_id: int, *, actor_is_admin: bool) -> str | None:
    """工具发现阶段：定时任务工具族能不能用；None 表示可用。

    “仅管理员可建”不在这里挡：普通成员仍然可以查看本群任务、取消自己的
    任务，创建时由 handler 给出明确原因。
    """

    del actor_is_admin
    settings = load_settings()
    if not settings.enabled or not settings.group_open(group_id):
        return "schedule_closed"
    return None


__all__ = [
    "STATE_DIR",
    "ScheduleSettings",
    "exposure_reason",
    "group_runs_today",
    "load_settings",
    "record_run_usage",
    "reset_settings_cache",
    "save_settings",
    "usage_snapshot",
]
