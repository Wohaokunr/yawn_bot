# ruff: noqa: E501
"""私聊主动提议的全站策略与每日用量（WebUI 可改，不落数据库）。

和定时任务策略一样是 ``data/private_agent/offers/`` 下的小 JSON 文件，原子写入，
坏文件一律回落默认值；改了文件（mtime 变化）下一次读取自动生效，不用重启。

策略只管“多久问一次、什么时候不问、问几次没人理就停、哪些事可以拿来问”；
每位用户自己的开关、间隔、静音的种类在他的个人数据 ``offers`` 键里（``private_offers``）。
环境变量 ``PRIVATE_AGENT_OFFERS=false`` 是总闸，关掉时这里的设置保留但不生效。
"""

from __future__ import annotations

import contextlib
import json
import os
import threading
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from .context import now_beijing

PROJECT_ROOT = Path(__file__).resolve().parents[4]
_CLOCK_PATTERN = r"^([01]\d|2[0-3]):[0-5]\d$"

# 能拿来提议的事（顺序就是 WebUI 展示顺序）：键 → (名字, 说明)。
KIND_LABELS: dict[str, tuple[str, str]] = {
    "task_blocked": ("后台的事在等他回答", "小助手做到一半卡住、等他回答的问题"),
    "todo_overdue": ("逾期的待办", "过了截止日还没做完的待办：问要不要帮他拆开、改期或者现在就动手"),
    "todo_today": ("今天到期的待办", "今天截止的待办：问要不要帮他准备、查资料或者到点提醒"),
    "task_failed": ("没做成的后台任务", "最近一天内小助手没做成的事：问要不要换个办法再试"),
    "open_thread": ("还没下文的事", "对话回顾里他提过、还没结果的事（面试、快递、考试…）：问一句要不要帮忙"),
    "handoff_open": ("群里转来没办完的事", "群里答应了私聊接着办、还没办完的事"),
    "reminder_prep": ("快到点的提醒", "三小时内要到点的提醒：问要不要提前帮他准备"),
    "reminder_broken": ("出问题的提醒", "发送失败或者规则坏了的提醒：问要不要帮他修好"),
    "countdown_near": ("临近的倒数日", "三天内到的倒数日：问要不要帮他准备"),
    "mood_care": ("最近心情不好", "最近几次记的心情偏低：轻轻问一句要不要聊聊或帮他做点什么"),
    "checkin": ("今天还没签到的群", "傍晚以后还有群没签：问要不要我帮他签"),
    "day_plan": ("排一下今天", "早上、他有好几件待办时：问要不要帮他排今天先做什么"),
    "weekly_plan": ("排一下这周", "周日晚上或周一上午、他有待办时：问要不要帮他排这周"),
    "todo_pile": ("待办堆起来了", "没做完的待办很多：问要不要帮他排优先级、清掉过时的"),
    "todo_stale": ("放了很久的待办", "一周以上没动、也没截止日的待办：问还要不要做、要不要拆小"),
    "notes_digest": ("最近记的笔记", "最近一周记了不少笔记：问要不要帮他整理成一份摘要"),
}
ALL_KINDS: tuple[str, ...] = tuple(KIND_LABELS)
REASON_LABELS: dict[str, str] = {
    "env_off": "总闸 PRIVATE_AGENT_OFFERS 关着",
    "agent_off": "私聊 Agent 或主动消息关着",
    "policy_off": "策略里关掉了主动提议",
    "not_listed": "不在开放名单里",
    "blocked": "在屏蔽名单里",
    "user_off": "他自己关掉了主动提议",
    "paused": "他用 /退出 暂停了私聊 Agent",
    "not_friend": "不是好友",
    "affinity_level": "好感等级还没解锁主动提议",
    "quiet": "夜间不打扰时段",
    "dnd": "他设的免打扰时段",
    "snoozed": "他让我先别问（暂停中）",
    "inactive": "很久没在私聊里说话了",
    "no_history": "还没在私聊里聊过",
    "chatting": "刚刚还在聊，不打断",
    "unanswered": "上几次问了都没回，等他先开口",
    "daily_cap": "今天问的次数到上限了",
    "not_due": "还没到下一次的时间",
    "backoff": "上次没发出去，过一会儿再试",
    "busy": "他这会儿正在和我说话",
    "no_candidates": "没找到值得问的事",
    "ok": "可以问",
}


class OfferSettings(BaseModel):
    """WebUI「私聊 Agent → 主动提议」的全部策略字段。"""

    model_config = ConfigDict(extra="ignore")

    enabled: bool = True
    # 新用户默认开（on）还是要他自己说“可以主动问我”才开（off）
    default_on: bool = True
    # 两次提议至少隔多久（小时）；从上一次提议和上一次聊天里更晚的那个算起
    interval_hours: float = Field(default=3.0, ge=0.5, le=48)
    # 他可以自己调的间隔下限（防止有人设成每半小时）
    user_min_interval_hours: float = Field(default=1.0, ge=0.5, le=48)
    # 每次加上的 0~N 分钟随机偏移（按人固定），免得所有人同一分钟被问
    jitter_minutes: int = Field(default=15, ge=0, le=120)
    # 连着几次没回就停，直到他先开口；每多一次没回，间隔翻倍（上限 max_backoff_hours）
    max_unanswered: int = Field(default=2, ge=1, le=10)
    backoff_factor: float = Field(default=2.0, ge=1.0, le=4.0)
    max_backoff_hours: float = Field(default=24.0, ge=1, le=168)
    daily_max: int = Field(default=3, ge=1, le=12)
    # 全站夜间不打扰（他自己的免打扰时段另算，两个都要躲）
    quiet_hours_enabled: bool = True
    quiet_start: str = Field(default="22:30", pattern=_CLOCK_PATTERN)
    quiet_end: str = Field(default="09:00", pattern=_CLOCK_PATTERN)
    # 只问最近 N 天在私聊里说过话的人（再久就别打扰了）
    active_within_days: int = Field(default=7, ge=1, le=90)
    # 他（或我）最近 N 分钟内说过话就不插话
    min_idle_minutes: int = Field(default=30, ge=5, le=720)
    # 同一件事问过之后多少小时内不再拿来问；他说不用的那一类事静音多少天
    item_cooldown_hours: int = Field(default=24, ge=1, le=336)
    decline_mute_days: int = Field(default=3, ge=0, le=60)
    # 给模型看的候选最多几件、一次最多问几件
    max_candidates: int = Field(default=4, ge=1, le=8)
    max_items_per_offer: int = Field(default=2, ge=1, le=3)
    # 候选分数低于这个就不问（0~100）
    min_score: int = Field(default=30, ge=0, le=100)
    # 允许模型看完候选后决定这次不问（【不回】）
    allow_model_skip: bool = True
    only_friends: bool = True
    # 每次扫描最多给几个人跑提议回合（一个人跑完再下一个，避免同时占满模型）
    per_tick: int = Field(default=2, ge=1, le=10)
    # 发出去之后多少小时还没回就算“没回”；他回了话算“回了”
    answer_window_hours: float = Field(default=12.0, ge=1, le=72)
    user_mode: Literal["all", "allowlist"] = "all"
    allowed_users: list[int] = Field(default_factory=list, max_length=500)
    blocked_users: list[int] = Field(default_factory=list, max_length=500)
    kinds: dict[str, bool] = Field(default_factory=lambda: dict.fromkeys(ALL_KINDS, True))

    @field_validator("allowed_users", "blocked_users")
    @classmethod
    def _dedupe(cls, value: list[int]) -> list[int]:
        return list(dict.fromkeys(int(item) for item in value if int(item) > 0))

    @field_validator("kinds")
    @classmethod
    def _known_kinds(cls, value: dict[str, bool]) -> dict[str, bool]:
        merged = dict.fromkeys(ALL_KINDS, True)
        merged.update({key: bool(flag) for key, flag in value.items() if key in KIND_LABELS})
        return merged

    def user_open(self, user_id: int) -> str | None:
        if int(user_id) in self.blocked_users:
            return "blocked"
        if self.user_mode == "allowlist" and int(user_id) not in self.allowed_users:
            return "not_listed"
        return None

    def in_quiet_hours(self, clock: str) -> bool:
        if not self.quiet_hours_enabled or self.quiet_start == self.quiet_end:
            return False
        if self.quiet_start < self.quiet_end:
            return self.quiet_start <= clock < self.quiet_end
        return clock >= self.quiet_start or clock < self.quiet_end

    def kind_on(self, kind: str) -> bool:
        return bool(self.kinds.get(kind, True))


def state_dir() -> Path:
    raw = os.environ.get("PRIVATE_AGENT_OFFER_DIR", "").strip()
    return Path(raw) if raw else PROJECT_ROOT / "data" / "private_agent" / "offers"


class _Store:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._settings: OfferSettings | None = None
        self._key: tuple[str, float | None] | None = None

    @staticmethod
    def settings_path() -> Path:
        return state_dir() / "settings.json"

    @staticmethod
    def usage_path() -> Path:
        return state_dir() / "usage.json"

    @staticmethod
    def _write(path: Path, data: dict[str, Any]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(".tmp")
        temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        temporary.replace(path)

    def settings(self) -> OfferSettings:
        path = self.settings_path()
        try:
            mtime: float | None = path.stat().st_mtime
        except OSError:
            mtime = None
        key = (str(path), mtime)
        with self._lock:
            if self._settings is not None and self._key == key:
                return self._settings
            settings = OfferSettings()
            if mtime is not None:
                with contextlib.suppress(OSError, ValueError, ValidationError):
                    settings = OfferSettings.model_validate(json.loads(path.read_text(encoding="utf-8")))
            self._settings, self._key = settings, key
            return settings

    def save(self, settings: OfferSettings) -> OfferSettings:
        path = self.settings_path()
        with self._lock:
            self._write(path, settings.model_dump())
            self._settings = settings
            with contextlib.suppress(OSError):
                self._key = (str(path), path.stat().st_mtime)
        return settings

    def reset(self) -> None:
        with self._lock:
            self._settings = None
            self._key = None

    def _usage(self) -> dict[str, Any]:
        today = now_beijing().strftime("%Y-%m-%d")
        try:
            data = json.loads(self.usage_path().read_text(encoding="utf-8"))
        except (OSError, ValueError):
            data = {}
        if not isinstance(data, dict) or data.get("day") != today:
            return {"day": today, "results": {}, "kinds": {}}
        for key in ("results", "kinds"):
            if not isinstance(data.get(key), dict):
                data[key] = {}
        return data

    def usage(self) -> dict[str, Any]:
        with self._lock:
            return self._usage()

    def record(self, result: str, kinds: list[str] | tuple[str, ...] = ()) -> None:
        with self._lock:
            data = self._usage()
            data["results"][result] = int(data["results"].get(result, 0)) + 1
            for kind in kinds:
                data["kinds"][kind] = int(data["kinds"].get(kind, 0)) + 1
            with contextlib.suppress(OSError):
                self._write(self.usage_path(), data)


_store = _Store()


def load_settings() -> OfferSettings:
    return _store.settings()


def save_settings(settings: OfferSettings) -> OfferSettings:
    return _store.save(settings)


def reset_settings_cache() -> None:
    _store.reset()


def usage_snapshot() -> dict[str, Any]:
    return _store.usage()


def record_usage(result: str, kinds: list[str] | tuple[str, ...] = ()) -> None:
    """记一次结果（sent / skipped / user_spoke / failed / accepted / declined / later …）。"""

    _store.record(result, kinds)


def kind_catalog() -> list[dict[str, str]]:
    return [{"kind": kind, "label": label, "description": desc} for kind, (label, desc) in KIND_LABELS.items()]


__all__ = [
    "ALL_KINDS",
    "KIND_LABELS",
    "REASON_LABELS",
    "OfferSettings",
    "kind_catalog",
    "load_settings",
    "record_usage",
    "reset_settings_cache",
    "save_settings",
    "state_dir",
    "usage_snapshot",
]
