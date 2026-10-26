# ruff: noqa: E501
"""网页搜索的开放策略、限额与每日用量（WebUI 可改，不落数据库）。

和代码沙箱一样，设置与用量是 ``data/agent_web_search/`` 下的小 JSON 文件，原子
写入；坏文件一律回落默认值，不能让 Agent 因为一个配置文件起不来。

一次 ``web_search`` / ``web_fetch`` 的授权顺序：总开关 → 密钥 → 群开放范围 →
仅管理员 → 本回合次数 → 成员冷却 → 成员/群每日次数 → 全站每日预算。次数在
请求前预扣；DeepSeek 只对成功且有结果的请求计费，所以失败、无结果和命中缓存
的调用都会退还，配额口径与账单一致。

最近调用记录只放进程内有界环形缓冲（给 WebUI 排障看），不落盘、重启即清。
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import threading
import time
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from .context import now_beijing
from .web_search_client import PRICE_PER_CALL_YUAN, find_api_key

PROJECT_ROOT = Path(__file__).resolve().parents[4]
STATE_DIR = PROJECT_ROOT / "data" / "agent_web_search"
MAX_COOLDOWN_WAIT = 10.0
RECENT_LIMIT = 200
_COOLDOWN_MEMORY = 2048
CallKind = Literal["search", "fetch"]


def normalize_domain(value: str) -> str:
    """``https://www.Example.com/x`` → ``example.com``；非法输入返回空串。"""

    text = str(value or "").strip().casefold()
    if "://" in text:
        text = text.split("://", 1)[1]
    text = text.split("/", 1)[0].split("?", 1)[0].split("#", 1)[0]
    text = text.rsplit("@", 1)[-1].split(":", 1)[0].strip(".")
    text = text.removeprefix("*.").removeprefix("www.")
    if not text or "." not in text or any(char.isspace() for char in text):
        return ""
    return text


def domain_matches(host: str, domains: list[str] | tuple[str, ...]) -> bool:
    """``host`` 是否等于或属于名单里的某个域名（子域名也算）。"""

    folded = normalize_domain(host)
    if not folded:
        return False
    return any(folded == item or folded.endswith(f".{item}") for item in domains)


class WebSearchSettings(BaseModel):
    """WebUI「网页搜索 → 策略与限额」的全部字段。"""

    model_config = ConfigDict(extra="ignore")

    enabled: bool = True
    group_mode: Literal["all", "allowlist"] = "all"
    allowed_groups: list[int] = Field(default_factory=list, max_length=500)
    blocked_groups: list[int] = Field(default_factory=list, max_length=500)
    admin_only: bool = False
    member_daily_calls: int = Field(default=30, ge=0, le=1000)
    group_daily_calls: int = Field(default=200, ge=0, le=10000)
    # 全站每天最多计费请求数（搜索 + 抓取），防止被刷爆账单；0 表示暂停。
    global_daily_calls: int = Field(default=1000, ge=0, le=100000)
    member_cooldown_seconds: int = Field(default=3, ge=0, le=600)
    max_calls_per_turn: int = Field(default=6, ge=1, le=20)
    default_count: int = Field(default=5, ge=1, le=20)
    max_count: int = Field(default=10, ge=1, le=50)
    # need_content=true 会把每条结果的整页正文带回来，很费上下文；默认只给摘要。
    allow_full_content: bool = False
    fetch_enabled: bool = True
    max_fetch_chars: int = Field(default=6000, ge=1000, le=20000)
    cache_ttl_minutes: int = Field(default=30, ge=0, le=1440)
    blocked_domains: list[str] = Field(default_factory=list, max_length=200)

    @field_validator("blocked_domains", mode="before")
    @classmethod
    def _clean_domains(cls, value: Any) -> list[str]:
        items = value if isinstance(value, list) else []
        cleaned: list[str] = []
        for item in items:
            domain = normalize_domain(str(item))
            if domain and domain not in cleaned:
                cleaned.append(domain)
        return cleaned

    def group_open(self, group_id: int) -> bool:
        if group_id in self.blocked_groups:
            return False
        if self.group_mode == "allowlist":
            return group_id in self.allowed_groups
        return True

    def clamp_count(self, count: int | None) -> int:
        requested = self.default_count if count is None else int(count)
        return max(1, min(requested, self.max_count))


def _empty_usage(day: str) -> dict[str, Any]:
    return {
        "day": day,
        "groups": {},
        "members": {},
        "totals": {"search": 0, "fetch": 0, "billed": 0, "empty": 0, "failed": 0, "cached": 0},
    }


class _Store:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._settings: WebSearchSettings | None = None
        self._settings_mtime: float | None = None
        self._last_call: dict[str, float] = {}
        self.recent: deque[dict[str, Any]] = deque(maxlen=RECENT_LIMIT)

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

    def settings(self) -> WebSearchSettings:
        path = self.settings_path()
        try:
            mtime = path.stat().st_mtime
        except OSError:
            mtime = None
        with self._lock:
            if self._settings is not None and mtime == self._settings_mtime:
                return self._settings
            settings = WebSearchSettings()
            if mtime is not None:
                with contextlib.suppress(OSError, ValueError, ValidationError):
                    settings = WebSearchSettings.model_validate(
                        json.loads(path.read_text(encoding="utf-8"))
                    )
            self._settings = settings
            self._settings_mtime = mtime
            return settings

    def save_settings(self, settings: WebSearchSettings) -> WebSearchSettings:
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
            return _empty_usage(today)
        for key in ("groups", "members"):
            if not isinstance(data.get(key), dict):
                data[key] = {}
        totals = data.get("totals")
        base = _empty_usage(today)["totals"]
        data["totals"] = {**base, **(totals if isinstance(totals, dict) else {})}
        return data

    def usage(self) -> dict[str, Any]:
        with self._lock:
            return self._load_usage()

    def adjust(
        self,
        group_id: int,
        user_id: int | None,
        delta: int,
        *,
        totals: dict[str, int] | None = None,
    ) -> dict[str, Any]:
        with self._lock:
            data = self._load_usage()
            if delta:
                group_key = str(group_id)
                data["groups"][group_key] = max(int(data["groups"].get(group_key, 0)) + delta, 0)
                if user_id is not None:
                    member_key = f"{group_id}:{user_id}"
                    data["members"][member_key] = max(
                        int(data["members"].get(member_key, 0)) + delta, 0
                    )
            for key, value in (totals or {}).items():
                data["totals"][key] = max(int(data["totals"].get(key, 0)) + value, 0)
            with contextlib.suppress(OSError):
                self._write_json(self.usage_path(), data)
            return data

    def cooldown_remaining(self, key: str, seconds: int) -> float:
        if seconds <= 0:
            return 0.0
        last = self._last_call.get(key)
        if last is None:
            return 0.0
        return max(seconds - (time.monotonic() - last), 0.0)

    def mark_call(self, key: str) -> float | None:
        previous = self._last_call.get(key)
        if len(self._last_call) >= _COOLDOWN_MEMORY:
            self._last_call.clear()
        self._last_call[key] = time.monotonic()
        return previous

    def restore_call(self, key: str, previous: float | None) -> None:
        if previous is None:
            self._last_call.pop(key, None)
        else:
            self._last_call[key] = previous


_store = _Store()


def load_settings() -> WebSearchSettings:
    return _store.settings()


def save_settings(settings: WebSearchSettings) -> WebSearchSettings:
    return _store.save_settings(settings)


def usage_snapshot() -> dict[str, Any]:
    return _store.usage()


def estimated_cost_yuan(billed: int) -> float:
    return round(max(billed, 0) * PRICE_PER_CALL_YUAN, 4)


class WebSearchPolicyError(PermissionError):
    """策略拒绝；``reason`` 是给发现/前端用的机器可读原因。"""

    def __init__(self, reason: str, message: str) -> None:
        super().__init__(message)
        self.reason = reason


def exposure_reason(group_id: int, *, actor_is_admin: bool) -> str | None:
    """工具发现阶段：web 工具族当前能不能用；None 表示可用。"""

    settings = load_settings()
    if not settings.enabled or not settings.group_open(group_id):
        return "web_closed"
    if find_api_key() is None:
        return "web_no_key"
    if settings.admin_only and not actor_is_admin:
        return "actor_not_admin"
    if settings.global_daily_calls <= 0:
        return "web_paused"
    return None


@dataclass(slots=True)
class CallReservation:
    group_id: int
    user_id: int | None
    kind: str
    settings: WebSearchSettings
    previous_call: float | None = None
    waited_seconds: float = 0.0
    settled: bool = False

    def settle(self, outcome: Literal["billed", "empty", "failed", "cached"]) -> None:
        """结算一次预扣：只有 ``billed`` 保留次数，其余都退还（不计费）。"""

        if self.settled:
            return
        self.settled = True
        delta = 0 if outcome == "billed" else -1
        totals = {self.kind: 1, outcome: 1}
        _store.adjust(self.group_id, self.user_id, delta, totals=totals)
        if outcome == "failed" and self.user_id is not None:
            _store.restore_call(f"{self.group_id}:{self.user_id}", self.previous_call)


def check_access(
    group_id: int,
    user_id: int | None,
    *,
    kind: CallKind,
    actor_is_admin: bool,
    turn_calls: int = 0,
) -> WebSearchSettings:
    """不计次数的准入检查（命中缓存时也要过）；不满足抛 ``WebSearchPolicyError``。"""

    settings = load_settings()
    if not settings.enabled:
        raise WebSearchPolicyError("web_closed", "网页搜索已被管理员关闭")
    if not settings.group_open(group_id):
        raise WebSearchPolicyError("web_closed", "本群没有开放网页搜索")
    if kind == "fetch" and not settings.fetch_enabled:
        raise WebSearchPolicyError("fetch_closed", "管理员关闭了网页正文抓取")
    if user_id is None:
        raise WebSearchPolicyError("no_actor", "网页搜索需要明确的群成员调用者")
    if settings.admin_only and not actor_is_admin:
        raise WebSearchPolicyError("actor_not_admin", "本群的网页搜索仅限群主或管理员使用")
    if turn_calls >= settings.max_calls_per_turn:
        raise WebSearchPolicyError(
            "turn_limit", f"这一轮已经搜了 {turn_calls} 次（上限 {settings.max_calls_per_turn}）"
        )
    return settings


def record_cached(kind: CallKind) -> None:
    """命中缓存：不花钱、不占配额，只记进当日统计。"""

    _store.adjust(0, None, 0, totals={kind: 1, "cached": 1})


def record_admin_call(kind: CallKind, outcome: Literal["billed", "empty", "failed"]) -> None:
    """WebUI 试搜/试抓：计入全站当日计费统计，但不占任何群或成员配额。"""

    _store.adjust(0, None, 0, totals={kind: 1, outcome: 1})


async def reserve_call(  # noqa: PLR0913
    group_id: int,
    user_id: int | None,
    *,
    kind: CallKind,
    actor_is_admin: bool,
    turn_calls: int = 0,
    wait_cooldown: bool = True,
) -> CallReservation:
    """检查策略并预扣一次调用；任何不满足都抛 ``WebSearchPolicyError``。

    ``turn_calls``：本回合已经发出的搜索/抓取次数。同一回合的第二次起不受冷却
    限制（“搜一下 → 打开第一条”是一件事），但受 ``max_calls_per_turn`` 约束。
    """

    settings = check_access(
        group_id, user_id, kind=kind, actor_is_admin=actor_is_admin, turn_calls=turn_calls
    )
    if find_api_key() is None:
        raise WebSearchPolicyError("web_no_key", "没有配置 DeepSeek API 密钥")
    cooldown_key = f"{group_id}:{user_id}"
    waited = 0.0
    remaining = (
        0.0
        if turn_calls > 0
        else _store.cooldown_remaining(cooldown_key, settings.member_cooldown_seconds)
    )
    if remaining > 0 and wait_cooldown and remaining <= MAX_COOLDOWN_WAIT:
        await asyncio.sleep(remaining)
        waited, remaining = remaining, 0.0
    if remaining > 0:
        raise WebSearchPolicyError("cooldown", f"搜得太频繁了，请 {remaining:.0f} 秒后再试")
    usage = _store.usage()
    used_member = int(usage["members"].get(cooldown_key, 0))
    used_group = int(usage["groups"].get(str(group_id), 0))
    billed_today = int(usage["totals"].get("billed", 0))
    if used_member >= settings.member_daily_calls:
        raise WebSearchPolicyError(
            "member_quota", f"你今天的网页搜索次数已用完（配额 {settings.member_daily_calls} 次）"
        )
    if used_group >= settings.group_daily_calls:
        raise WebSearchPolicyError(
            "group_quota", f"本群今天的网页搜索次数已用完（配额 {settings.group_daily_calls} 次）"
        )
    if billed_today >= settings.global_daily_calls:
        raise WebSearchPolicyError(
            "global_quota", "今天的网页搜索总预算已经用完，明天再来"
        )
    _store.adjust(group_id, user_id, 1)
    previous = _store.mark_call(cooldown_key)
    return CallReservation(
        group_id, user_id, kind, settings, previous_call=previous, waited_seconds=waited
    )


def quota_status(group_id: int, user_id: int | None) -> dict[str, Any]:
    settings = load_settings()
    usage = _store.usage()
    used_group = int(usage["groups"].get(str(group_id), 0))
    status: dict[str, Any] = {
        "group_used": used_group,
        "group_limit": settings.group_daily_calls,
    }
    if user_id is not None:
        used_member = int(usage["members"].get(f"{group_id}:{user_id}", 0))
        status.update(
            member_used=used_member,
            member_limit=settings.member_daily_calls,
            member_left=max(settings.member_daily_calls - used_member, 0),
        )
    return status


def record_recent(entry: dict[str, Any]) -> None:
    """记一条最近调用（进程内、有界）；``entry`` 已由调用方去掉正文。"""

    row = {"at": time.time(), **entry}
    with _store._lock:
        _store.recent.appendleft(row)


def recent_calls(limit: int = 50, *, group_id: int | None = None) -> list[dict[str, Any]]:
    with _store._lock:
        rows = list(_store.recent)
    if group_id is not None:
        rows = [row for row in rows if row.get("group_id") == group_id]
    return rows[: max(1, min(limit, RECENT_LIMIT))]


def forget_member_recent(group_id: int, user_id: int) -> int:
    """成员退出隐私/删除记忆时，把他触发的最近调用（含搜索词）一起抹掉。"""

    with _store._lock:
        kept = [
            row
            for row in _store.recent
            if not (row.get("group_id") == group_id and row.get("user_id") == user_id)
        ]
        removed = len(_store.recent) - len(kept)
        _store.recent.clear()
        _store.recent.extend(kept)
    return removed


def reset_state_for_tests() -> None:
    global _store  # noqa: PLW0603
    _store = _Store()


__all__ = [
    "MAX_COOLDOWN_WAIT",
    "STATE_DIR",
    "CallReservation",
    "WebSearchPolicyError",
    "WebSearchSettings",
    "check_access",
    "domain_matches",
    "estimated_cost_yuan",
    "exposure_reason",
    "forget_member_recent",
    "load_settings",
    "normalize_domain",
    "quota_status",
    "recent_calls",
    "record_admin_call",
    "record_cached",
    "record_recent",
    "reserve_call",
    "save_settings",
    "usage_snapshot",
]
