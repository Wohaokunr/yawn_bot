# ruff: noqa: E501,PLR0911
"""表情包的开放策略、发送频率与用量（WebUI 可改，不落数据库）。

设置与用量是 ``data/agent_stickers/`` 下的小 JSON 文件，原子写入；坏文件一律
回落默认值。频率闸门有两层：

1. **提示词层**：冷却中、今日额度用完、群没开放时，本轮根本不给模型看表情包
   菜单（``exposure``），模型自然不会发；
2. **发送层**：模型仍然写了表情标记时，``sticker_send`` 再检查一次，超出就
   去掉那一条，不让整句失败。

“这张发过了”只在进程内记（每群最近几张），用于轮换，避免老发同一张；累计
使用次数落在 ``stats.json`` 给 WebUI 看。
"""

from __future__ import annotations

import contextlib
import json
import random
import threading
import time
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from .context import now_beijing
from .sticker_library import Sticker, available_stickers

PROJECT_ROOT = Path(__file__).resolve().parents[4]
STATE_DIR = PROJECT_ROOT / "data" / "agent_stickers"
RECENT_PER_GROUP = 6
_GROUP_MEMORY = 1024

Frequency = Literal["rare", "normal", "often"]
FREQUENCY_LABELS: dict[str, str] = {"rare": "偶尔", "normal": "适中", "often": "爱发"}


class StickerSettings(BaseModel):
    """WebUI「表情包 → 策略」的全部字段。"""

    model_config = ConfigDict(extra="ignore")

    enabled: bool = True
    group_mode: Literal["all", "allowlist"] = "all"
    allowed_groups: list[int] = Field(default_factory=list, max_length=500)
    blocked_groups: list[int] = Field(default_factory=list, max_length=500)
    # 发表情包的“性格”：只影响提示词里怎么说，硬上限看下面几个数字。
    frequency: Frequency = "normal"
    # 同一个群两次表情包之间至少隔多久（秒）；0 = 不限。
    group_cooldown_seconds: int = Field(default=60, ge=0, le=86400)
    group_daily_limit: int = Field(default=60, ge=0, le=10000)
    max_per_message: int = Field(default=1, ge=1, le=3)
    menu_size: int = Field(default=12, ge=3, le=40)
    # 主动插话、暖场、续聊里也能发表情包（仍受人设“接梗”倾向约束）。
    allow_proactive: bool = True
    # save_sticker：off=不让成员收藏；review=进待审核；auto=直接可用。
    collect_mode: Literal["off", "review", "auto"] = "review"
    collect_admin_only: bool = False
    member_daily_collect: int = Field(default=10, ge=0, le=200)
    max_bytes: int = Field(default=3 * 1024 * 1024, ge=64 * 1024, le=10 * 1024 * 1024)
    # 私聊里也能发表情包（只用“所有群可用”的那部分库）；每人每天上限与冷却。
    private_enabled: bool = True
    private_daily_limit: int = Field(default=20, ge=0, le=1000)
    private_cooldown_seconds: int = Field(default=20, ge=0, le=86400)

    def group_open(self, group_id: int) -> bool:
        if group_id in self.blocked_groups:
            return False
        if self.group_mode == "allowlist":
            return group_id in self.allowed_groups
        return True


def _empty_usage(day: str) -> dict[str, Any]:
    return {"day": day, "groups": {}, "collects": {}, "totals": {"sent": 0, "dropped": 0, "collected": 0}}


class _Store:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._settings: StickerSettings | None = None
        self._settings_mtime: float | None = None
        self._last_sent: dict[int, float] = {}
        self._recent: dict[int, deque[str]] = {}

    @staticmethod
    def settings_path() -> Path:
        return STATE_DIR / "settings.json"

    @staticmethod
    def usage_path() -> Path:
        return STATE_DIR / "usage.json"

    @staticmethod
    def stats_path() -> Path:
        return STATE_DIR / "stats.json"

    @staticmethod
    def _write_json(path: Path, data: dict[str, Any]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(".tmp")
        temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        temporary.replace(path)

    def settings(self) -> StickerSettings:
        path = self.settings_path()
        try:
            mtime: float | None = path.stat().st_mtime
        except OSError:
            mtime = None
        with self._lock:
            if self._settings is not None and mtime == self._settings_mtime:
                return self._settings
            settings = StickerSettings()
            if mtime is not None:
                with contextlib.suppress(OSError, ValueError, ValidationError):
                    settings = StickerSettings.model_validate(json.loads(path.read_text(encoding="utf-8")))
            self._settings = settings
            self._settings_mtime = mtime
            return settings

    def save_settings(self, settings: StickerSettings) -> StickerSettings:
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
        base = _empty_usage(today)
        for key in ("groups", "collects"):
            if not isinstance(data.get(key), dict):
                data[key] = {}
        totals = data.get("totals")
        data["totals"] = {**base["totals"], **(totals if isinstance(totals, dict) else {})}
        return data

    def usage(self) -> dict[str, Any]:
        with self._lock:
            return self._load_usage()

    def _load_stats(self) -> dict[str, Any]:
        try:
            data = json.loads(self.stats_path().read_text(encoding="utf-8"))
        except (OSError, ValueError):
            data = {}
        return data if isinstance(data, dict) else {}

    def stats(self) -> dict[str, Any]:
        with self._lock:
            return self._load_stats()

    def note_sent(self, group_id: int, sticker_ids: list[str]) -> None:
        if not sticker_ids:
            return
        stamp = now_beijing().strftime("%Y-%m-%d %H:%M:%S")
        with self._lock:
            if len(self._last_sent) >= _GROUP_MEMORY:
                self._last_sent.clear()
                self._recent.clear()
            self._last_sent[group_id] = time.monotonic()
            recent = self._recent.setdefault(group_id, deque(maxlen=RECENT_PER_GROUP))
            for item in sticker_ids:
                recent.append(item)
            usage = self._load_usage()
            key = str(group_id)
            usage["groups"][key] = int(usage["groups"].get(key, 0)) + len(sticker_ids)
            usage["totals"]["sent"] = int(usage["totals"].get("sent", 0)) + len(sticker_ids)
            stats = self._load_stats()
            for item in sticker_ids:
                previous = stats.get(item)
                row: dict[str, Any] = previous if isinstance(previous, dict) else {}
                stats[item] = {"uses": int(row.get("uses", 0)) + 1, "last_used": stamp}
            with contextlib.suppress(OSError):
                self._write_json(self.usage_path(), usage)
                self._write_json(self.stats_path(), stats)

    def note_dropped(self, count: int) -> None:
        if count <= 0:
            return
        with self._lock:
            usage = self._load_usage()
            usage["totals"]["dropped"] = int(usage["totals"].get("dropped", 0)) + count
            with contextlib.suppress(OSError):
                self._write_json(self.usage_path(), usage)

    def note_collect(self, group_id: int, user_id: int) -> None:
        with self._lock:
            usage = self._load_usage()
            key = f"{group_id}:{user_id}"
            usage["collects"][key] = int(usage["collects"].get(key, 0)) + 1
            usage["totals"]["collected"] = int(usage["totals"].get("collected", 0)) + 1
            with contextlib.suppress(OSError):
                self._write_json(self.usage_path(), usage)

    def collects_today(self, group_id: int, user_id: int) -> int:
        return int(self.usage().get("collects", {}).get(f"{group_id}:{user_id}", 0))

    def sent_today(self, group_id: int) -> int:
        return int(self.usage().get("groups", {}).get(str(group_id), 0))

    def cooldown_remaining(self, group_id: int, seconds: int) -> float:
        if seconds <= 0:
            return 0.0
        last = self._last_sent.get(group_id)
        if last is None:
            return 0.0
        return max(seconds - (time.monotonic() - last), 0.0)

    def recent(self, group_id: int) -> tuple[str, ...]:
        return tuple(self._recent.get(group_id, ()))

    def forget_stats(self, sticker_id: str) -> None:
        with self._lock:
            stats = self._load_stats()
            if stats.pop(sticker_id, None) is not None:
                with contextlib.suppress(OSError):
                    self._write_json(self.stats_path(), stats)

    def reset_runtime(self) -> None:
        with self._lock:
            self._last_sent.clear()
            self._recent.clear()
            self._settings = None
            self._settings_mtime = None


_store = _Store()


def load_settings() -> StickerSettings:
    return _store.settings()


def save_settings(settings: StickerSettings) -> StickerSettings:
    return _store.save_settings(settings)


def usage_snapshot() -> dict[str, Any]:
    return _store.usage()


def stats_snapshot() -> dict[str, Any]:
    return _store.stats()


def note_sent(group_id: int, sticker_ids: list[str]) -> None:
    _store.note_sent(int(group_id), [str(item) for item in sticker_ids if item])


def note_dropped(count: int) -> None:
    _store.note_dropped(count)


def note_collect(group_id: int, user_id: int) -> None:
    _store.note_collect(int(group_id), int(user_id))


def collects_today(group_id: int, user_id: int) -> int:
    return _store.collects_today(int(group_id), int(user_id))


def forget_stats(sticker_id: str) -> None:
    _store.forget_stats(sticker_id)


def recent_sent(group_id: int) -> tuple[str, ...]:
    return _store.recent(int(group_id))


def cooldown_remaining(group_id: int) -> float:
    return _store.cooldown_remaining(int(group_id), load_settings().group_cooldown_seconds)


def reset_for_tests() -> None:
    _store.reset_runtime()


@dataclass(frozen=True, slots=True)
class StickerExposure:
    """本轮能不能发表情包；``reason`` 为 None 表示可以。"""

    reason: str | None
    settings: StickerSettings
    stickers: tuple[Sticker, ...] = ()
    cooldown_seconds: float = 0.0
    left_today: int = 0

    @property
    def open(self) -> bool:
        return self.reason is None

    def as_dict(self) -> dict[str, Any]:
        return {
            "open": self.open,
            "reason": self.reason,
            "available": len(self.stickers),
            "cooldown_seconds": round(self.cooldown_seconds, 1),
            "left_today": self.left_today,
        }


REASON_TEXT: dict[str, str] = {
    "closed": "表情包功能被管理员关了",
    "group_closed": "本群没有开放表情包",
    "empty": "本群还没有可用的表情包",
    "cooldown": "刚发过表情包，还在冷却",
    "daily_limit": "本群今天的表情包额度用完了",
    "proactive_off": "主动发言里不发表情包",
    "persona_off": "人设的接梗倾向太低，不主动发表情包",
}


def exposure(group_id: int | None, *, proactive: bool = False) -> StickerExposure:
    """这一轮给不给模型看表情包菜单。不做任何关键词判断，只看开关、库存和频率。"""

    settings = load_settings()
    if not settings.enabled:
        return StickerExposure("closed", settings)
    if group_id is None or not settings.group_open(int(group_id)):
        return StickerExposure("group_closed", settings)
    if proactive and not settings.allow_proactive:
        return StickerExposure("proactive_off", settings)
    stickers = tuple(available_stickers(int(group_id)))
    if not stickers:
        return StickerExposure("empty", settings)
    left = max(settings.group_daily_limit - _store.sent_today(int(group_id)), 0)
    if left <= 0:
        return StickerExposure("daily_limit", settings, stickers, left_today=0)
    remaining = _store.cooldown_remaining(int(group_id), settings.group_cooldown_seconds)
    if remaining > 0:
        return StickerExposure("cooldown", settings, stickers, cooldown_seconds=remaining, left_today=left)
    return StickerExposure(None, settings, stickers, left_today=left)


def pick_sticker(group_id: int, candidates: list[Sticker], *, rng: random.Random | None = None) -> Sticker | None:
    """同一个名字/情绪有好几张时挑一张：避开本群最近发过的，再在用得少的里随机。"""

    if not candidates:
        return None
    if len(candidates) == 1:
        return candidates[0]
    recent = set(recent_sent(group_id))
    fresh = [item for item in candidates if item.sticker_id not in recent] or list(candidates)
    stats = stats_snapshot()

    def uses(item: Sticker) -> int:
        row = stats.get(item.sticker_id)
        return int(row.get("uses", 0)) if isinstance(row, dict) else 0

    fewest = min(uses(item) for item in fresh)
    pool = [item for item in fresh if uses(item) <= fewest + 2]
    return (rng or random).choice(pool)


def menu_entries(
    group_id: int, stickers: tuple[Sticker, ...] | list[Sticker], *, size: int, seed: str = ""
) -> list[tuple[str, str]]:
    """给模型看的菜单：同名合并，先放本群最近没发过的，再按“每小时换一批”的
    稳定随机顺序截断到 ``size`` 项。只看库存与发送记录，不看聊天内容。"""

    recent = set(recent_sent(group_id))
    hour = now_beijing().strftime("%Y%m%d%H")
    rng = random.Random(f"{group_id}:{hour}:{seed}")
    shuffled = list(stickers)
    rng.shuffle(shuffled)
    shuffled.sort(key=lambda item: item.sticker_id in recent)
    seen: set[str] = set()
    menu: list[tuple[str, str]] = []
    for item in shuffled:
        key = "".join(item.name.split()).casefold()
        if not key or key in seen:
            continue
        seen.add(key)
        hint = item.description or "、".join(tag for tag in item.tags if tag != item.name)
        menu.append((item.name, hint[:14]))
        if len(menu) >= size:
            break
    return menu


__all__ = [
    "FREQUENCY_LABELS",
    "REASON_TEXT",
    "StickerExposure",
    "StickerSettings",
    "collects_today",
    "cooldown_remaining",
    "exposure",
    "forget_stats",
    "load_settings",
    "menu_entries",
    "note_collect",
    "note_dropped",
    "note_sent",
    "pick_sticker",
    "recent_sent",
    "reset_for_tests",
    "save_settings",
    "stats_snapshot",
    "usage_snapshot",
]
