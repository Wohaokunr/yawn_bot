# ruff: noqa: E501, PLR0911, C901, FBT001, PLR2004, TRY003
"""表情包的开放策略、发送频率与用量（WebUI 可改，不落数据库）。

设置与用量是 ``data/agent_stickers/`` 下的小 JSON 文件，原子写入；坏文件一律
回落默认值。频率闸门有两层：

1. **提示词层**：冷却中、今日额度用完、群没开放时，本轮根本不给模型看表情包
   菜单（``exposure``），模型自然不会发；
2. **发送层**：模型仍然写了表情标记时，``sticker_send`` 再检查一次，超出就
   去掉那一条，不让整句失败。

“这张发过了”只在进程内记（每群最近几张 + 每张上次在本群发出的时间），用于轮换，
避免老发同一张；累计使用次数（含分群）落在 ``stats.json``，每天的合计归档进
``history.json``（最近 120 天），模型写了但库里对不上的名字记在 ``misses.json``，
都只给 WebUI 看，不进提示词。

3.0 起每个群可以单独覆盖一部分设置（``group_overrides``），所有闸门都先取
``effective_settings(群号)`` 再判断；另有安静时段、每小时上限、两次表情包之间至少
隔几条文字、同一张图的间隔、单张每日上限这几道只看计数和时间的闸门。
"""

from __future__ import annotations

import contextlib
import json
import random
import re
import threading
import time
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from .context import now_beijing
from .sticker_library import Sticker, available_stickers

PROJECT_ROOT = Path(__file__).resolve().parents[4]
STATE_DIR = PROJECT_ROOT / "data" / "agent_stickers"
RECENT_PER_GROUP = 6
_GROUP_MEMORY = 1024
_STICKER_MEMORY = 4096
HISTORY_DAYS = 120
MAX_MISSES = 200
MAX_SEND_LOG = 300
MAX_GROUP_OVERRIDES = 200
MAX_STAT_GROUPS = 12
TOTAL_KEYS = ("sent", "dropped", "collected", "stolen", "labeled", "private_sent")
PRIVATE_STAT_KEY = "private"

Frequency = Literal["rare", "normal", "often"]
FREQUENCY_LABELS: dict[str, str] = {"rare": "偶尔", "normal": "适中", "often": "爱发"}
MenuStrategy = Literal["fresh", "popular", "explore", "balanced"]
MENU_STRATEGY_LABELS: dict[str, str] = {
    "fresh": "轮着来（每小时换一批）",
    "popular": "常用的优先",
    "explore": "没怎么用过的优先",
    "balanced": "常用和冷门各一半",
}
_CLOCK_RE = re.compile(r"^([01]\d|2[0-3]):([0-5]\d)$")


class GroupOverride(BaseModel):
    """某个群单独的设置；None = 跟全局一样。"""

    model_config = ConfigDict(extra="ignore")

    enabled: bool | None = None
    frequency: Frequency | None = None
    group_cooldown_seconds: int | None = Field(default=None, ge=0, le=86400)
    group_daily_limit: int | None = Field(default=None, ge=0, le=10000)
    group_hourly_limit: int | None = Field(default=None, ge=0, le=1000)
    max_per_message: int | None = Field(default=None, ge=1, le=3)
    menu_size: int | None = Field(default=None, ge=3, le=40)
    allow_proactive: bool | None = None
    steal_mode: Literal["off", "review", "auto"] | None = None
    collect_mode: Literal["off", "review", "auto"] | None = None
    note: str = Field(default="", max_length=60)

    def changes(self) -> dict[str, Any]:
        return {key: value for key, value in self.model_dump(exclude={"enabled", "note"}).items() if value is not None}

    @property
    def empty(self) -> bool:
        return self.enabled is None and not self.changes()


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
    # ── AI 标注（识图模型起名、打标签、写什么时候发、认图上的字）──
    ai_label_enabled: bool = True
    # WebUI 上传时没填名字/标签的，交给 AI 补；关掉就退回文件名。
    ai_label_on_upload: bool = True
    # 群友收藏（save_sticker）和 Bot 偷图时，模型没给名字/标签就让识图模型补。
    ai_label_on_collect: bool = True
    # 管理员的命名要求，追加在标注提示词后面（如“名字用网络梗、四个字以内”）。
    ai_label_style: str = Field(default="", max_length=300)
    # 识图模型判断“不适合当表情包”（色情/血腥/隐私）的图：收藏/偷图一律进待审核。
    ai_label_review_unsafe: bool = True
    # ── Bot 偷图（steal_sticker / 私聊 stickers.steal）──
    # off=不偷；review=偷来先进待审核；auto=直接能用（AI 判断不安全的仍进待审核）。
    steal_mode: Literal["off", "review", "auto"] = "auto"
    # 只偷 QQ 标成表情包的图（sub_type=1）；打开后普通照片/截图也能偷。
    steal_photos: bool = False
    group_daily_steals: int = Field(default=5, ge=0, le=200)
    steal_cooldown_seconds: int = Field(default=300, ge=0, le=86400)
    private_steal_enabled: bool = True
    private_daily_steals: int = Field(default=5, ge=0, le=200)
    # 这些成员发的图不偷（他们说过“别偷我的图”）。
    steal_protected_users: list[int] = Field(default_factory=list, max_length=500)
    # ── 3.0：发送节奏（都只看计数和时间，不看聊天内容）──
    # 同一个群每小时最多发几张；0 = 不限。
    group_hourly_limit: int = Field(default=0, ge=0, le=1000)
    # 两次表情包之间 Bot 至少先说几条文字；0 = 不限。
    min_messages_between: int = Field(default=0, ge=0, le=20)
    # 同一张图在同一个群里至少隔多少分钟再发；0 = 只靠“最近几张轮换”。
    sticker_cooldown_minutes: int = Field(default=0, ge=0, le=1440)
    # 安静时段：这段时间所有群都不发表情包（文字照常）。
    quiet_enabled: bool = False
    quiet_start: str = "00:00"
    quiet_end: str = "07:00"
    # ── 3.0：菜单怎么挑 ──
    menu_strategy: MenuStrategy = "fresh"
    # 新入库的 N 天内优先进菜单（让新图有机会被用上）；0 = 不照顾。
    new_boost_days: int = Field(default=3, ge=0, le=30)
    # 菜单里每张后面的说明最多几个字；0 = 只给名字（最省 token）。
    menu_hint_chars: int = Field(default=14, ge=0, le=30)
    # 菜单后面要不要附“按情绪写 [表情:情绪]”那一行。
    menu_show_tags: bool = True
    # ── 3.0：偷图 / 收藏进阶 ──
    # 偷来 / 收藏来的图默认在哪些群用：all=所有群；origin=只在来源群（管理员可以再放开）。
    steal_scope: Literal["all", "origin"] = "all"
    collect_scope: Literal["all", "origin"] = "all"
    # 动图（GIF 等）偷不偷。
    steal_animated: bool = True
    # 短边小于这么多像素的不偷（糊成一团的小图）；0 = 不限。
    steal_min_side: int = Field(default=32, ge=0, le=512)
    # 同一个人一天最多被偷几张；0 = 不限。
    steal_sender_daily: int = Field(default=3, ge=0, le=100)
    # 这些群里不偷图（发还是照常发）。
    steal_blocked_groups: list[int] = Field(default_factory=list, max_length=500)
    # 识图模型把握低于这个值的偷图/收藏先进待审核；0 = 不看把握。
    review_below_confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    # 成员可以自己说“别偷我的图”（my_stickers.protect）。
    member_self_protect: bool = True
    # 成员可以改名 / 删掉自己收藏的表情包（my_stickers）。
    member_manage_own: bool = True
    # ── 3.0：AI 标注进阶 ──
    # 识图模型返回的不是 JSON / 空回复时再试一次。
    ai_label_retry: bool = True
    # 批量标注同时请求几张。
    ai_label_concurrency: int = Field(default=2, ge=1, le=4)
    # 让识图模型顺便给 1~2 个别名（模型写别名也能发出这张）。
    ai_label_aliases: bool = True
    # ── 3.0：回收站 ──
    # 删除的表情包在回收站留几天；0 = 直接删、不进回收站。
    trash_days: int = Field(default=14, ge=0, le=90)
    # 每个群单独的设置（键是群号）。
    group_overrides: dict[str, GroupOverride] = Field(default_factory=dict)

    @field_validator("quiet_start", "quiet_end", mode="before")
    @classmethod
    def _clock(cls, value: object) -> str:
        text = str(value or "").strip()
        if not _CLOCK_RE.fullmatch(text):
            raise ValueError("时间要写成 HH:MM")
        return text

    @field_validator("group_overrides", mode="before")
    @classmethod
    def _overrides(cls, value: object) -> dict[str, Any]:
        output: dict[str, Any] = {}
        for key, item in (value.items() if isinstance(value, dict) else []):
            text = str(key).strip()
            if not text.isdigit() or not 4 <= len(text) <= 12:
                continue
            output[text] = item
            if len(output) >= MAX_GROUP_OVERRIDES:
                break
        return output

    def override_for(self, group_id: int | None) -> GroupOverride | None:
        return self.group_overrides.get(str(int(group_id))) if group_id else None

    def group_open(self, group_id: int) -> bool:
        override = self.override_for(group_id)
        if override is not None and override.enabled is not None:
            return override.enabled
        if group_id in self.blocked_groups:
            return False
        if self.group_mode == "allowlist":
            return group_id in self.allowed_groups
        return True

    def for_group(self, group_id: int | None) -> StickerSettings:
        """这个群实际生效的设置（套上它的单独设置）。"""

        override = self.override_for(group_id)
        changes = override.changes() if override is not None else {}
        return self.model_copy(update=changes) if changes else self

    def steal_open(self, group_id: int) -> bool:
        return self.steal_mode != "off" and int(group_id) not in self.steal_blocked_groups


def _minutes(clock: str) -> int:
    hour, minute = clock.split(":")
    return int(hour) * 60 + int(minute)


def in_quiet_hours(settings: StickerSettings, now: datetime | None = None) -> bool:
    """现在是不是安静时段（支持跨零点，如 23:00 到 07:00；起止相同 = 全天）。纯函数。"""

    if not settings.quiet_enabled:
        return False
    moment = now or now_beijing()
    current = moment.hour * 60 + moment.minute
    start, end = _minutes(settings.quiet_start), _minutes(settings.quiet_end)
    if start == end:
        return True
    if start < end:
        return start <= current < end
    return current >= start or current < end


def _empty_usage(day: str) -> dict[str, Any]:
    return {
        "day": day,
        "groups": {},
        "collects": {},
        "steals": {},
        "hours": {},
        "stickers": {},
        "sender_steals": {},
        "drops": {},
        "totals": dict.fromkeys(TOTAL_KEYS, 0),
    }


_USAGE_MAPS = ("groups", "collects", "steals", "hours", "stickers", "sender_steals", "drops")


class _Store:
    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._settings: StickerSettings | None = None
        self._settings_mtime: float | None = None
        self._last_sent: dict[int, float] = {}
        self._last_steal: dict[str, float] = {}
        self._recent: dict[int, deque[str]] = {}
        self._plain_since: dict[int, int] = {}
        self._sticker_last: dict[tuple[int, str], float] = {}
        self._send_log: deque[dict[str, Any]] = deque(maxlen=MAX_SEND_LOG)

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
    def history_path() -> Path:
        return STATE_DIR / "history.json"

    @staticmethod
    def misses_path() -> Path:
        return STATE_DIR / "misses.json"

    @staticmethod
    def _write_json(path: Path, data: dict[str, Any]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(".tmp")
        temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        temporary.replace(path)

    @staticmethod
    def _read_json(path: Path) -> dict[str, Any]:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        return data if isinstance(data, dict) else {}

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
            # 空的群覆盖不落盘，免得列表里堆一堆“什么都没改”的群。
            cleaned = {key: value for key, value in settings.group_overrides.items() if not value.empty}
            if len(cleaned) != len(settings.group_overrides):
                settings = settings.model_copy(update={"group_overrides": cleaned})
            self._write_json(self.settings_path(), settings.model_dump())
            self._settings = settings
            with contextlib.suppress(OSError):
                self._settings_mtime = self.settings_path().stat().st_mtime
        return settings

    def _archive(self, data: dict[str, Any]) -> None:
        """跨天时把昨天（或更早那天）的合计归档进 history.json；同一天只归档一次。"""

        day = str(data.get("day") or "")
        totals = data.get("totals")
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", day) or not isinstance(totals, dict):
            return
        row = {key: int(totals.get(key, 0) or 0) for key in TOTAL_KEYS}
        if not any(row.values()):
            return
        history = self._read_json(self.history_path())
        raw_days = history.get("days")
        days: dict[str, Any] = dict(raw_days) if isinstance(raw_days, dict) else {}
        if day in days:
            return
        raw_groups = data.get("groups")
        groups: dict[str, Any] = raw_groups if isinstance(raw_groups, dict) else {}
        row["groups"] = sum(1 for value in groups.values() if int(value or 0) > 0)
        days[day] = row
        for old in sorted(days)[:-HISTORY_DAYS]:
            days.pop(old, None)
        with contextlib.suppress(OSError):
            self._write_json(self.history_path(), {"days": days})

    def _load_usage(self) -> dict[str, Any]:
        today = now_beijing().strftime("%Y-%m-%d")
        data = self._read_json(self.usage_path())
        if data.get("day") != today:
            if data:
                self._archive(data)
            return _empty_usage(today)
        base = _empty_usage(today)
        for key in _USAGE_MAPS:
            if not isinstance(data.get(key), dict):
                data[key] = {}
        totals = data.get("totals")
        data["totals"] = {**base["totals"], **(totals if isinstance(totals, dict) else {})}
        return data

    def usage(self) -> dict[str, Any]:
        with self._lock:
            return self._load_usage()

    def history(self) -> dict[str, dict[str, int]]:
        with self._lock:
            days = self._read_json(self.history_path()).get("days")
            return {str(key): dict(value) for key, value in days.items() if isinstance(value, dict)} if isinstance(days, dict) else {}

    def _load_stats(self) -> dict[str, Any]:
        return self._read_json(self.stats_path())

    def stats(self) -> dict[str, Any]:
        with self._lock:
            return self._load_stats()

    def _save_usage(self, usage: dict[str, Any]) -> None:
        with contextlib.suppress(OSError):
            self._write_json(self.usage_path(), usage)

    def note_sent(self, group_id: int, sticker_ids: list[str], *, private: bool = False) -> None:
        """``private`` 为真时 ``group_id`` 是私聊的轮换键（负的 QQ 号）：只记轮换、单张次数和
        使用统计，不占任何群的额度，统计里归到“私聊”，不留是谁。"""

        if not sticker_ids:
            return
        moment = now_beijing()
        stamp = moment.strftime("%Y-%m-%d %H:%M:%S")
        hour = moment.strftime("%Y%m%d%H")
        with self._lock:
            if len(self._last_sent) >= _GROUP_MEMORY:
                self._last_sent.clear()
                self._recent.clear()
                self._plain_since.clear()
            if len(self._sticker_last) >= _STICKER_MEMORY:
                self._sticker_last.clear()
            now = time.monotonic()
            self._last_sent[group_id] = now
            self._plain_since[group_id] = 0
            recent = self._recent.setdefault(group_id, deque(maxlen=RECENT_PER_GROUP))
            for item in sticker_ids:
                recent.append(item)
                self._sticker_last[group_id, item] = now
                self._send_log.append({"at": stamp, "groupId": None if private else group_id, "stickerId": item})
            usage = self._load_usage()
            key = PRIVATE_STAT_KEY if private else str(group_id)
            total_key = "private_sent" if private else "sent"
            if not private:
                usage["groups"][key] = int(usage["groups"].get(key, 0)) + len(sticker_ids)
                bucket = usage["hours"].get(key)
                count = int(bucket.get("n", 0)) if isinstance(bucket, dict) and bucket.get("h") == hour else 0
                usage["hours"][key] = {"h": hour, "n": count + len(sticker_ids)}
            usage["totals"][total_key] = int(usage["totals"].get(total_key, 0)) + len(sticker_ids)
            stats = self._load_stats()
            for item in sticker_ids:
                usage["stickers"][item] = int(usage["stickers"].get(item, 0)) + 1
                previous = stats.get(item)
                row: dict[str, Any] = previous if isinstance(previous, dict) else {}
                raw_groups = row.get("groups")
                groups: dict[str, Any] = dict(raw_groups) if isinstance(raw_groups, dict) else {}
                groups[key] = int(groups.get(key, 0)) + 1
                if len(groups) > MAX_STAT_GROUPS:
                    groups = dict(sorted(groups.items(), key=lambda pair: -int(pair[1]))[:MAX_STAT_GROUPS])
                stats[item] = {
                    "uses": int(row.get("uses", 0)) + 1,
                    "last_used": stamp,
                    "first_used": row.get("first_used") or stamp,
                    "groups": groups,
                }
            self._save_usage(usage)
            with contextlib.suppress(OSError):
                self._write_json(self.stats_path(), stats)

    def note_plain(self, group_id: int) -> None:
        """Bot 在这个群发了一条不带表情包的消息（“隔几条文字”闸门用，进程内）。"""

        with self._lock:
            if group_id in self._last_sent:
                self._plain_since[group_id] = self._plain_since.get(group_id, 0) + 1

    def plain_since(self, group_id: int) -> int | None:
        """上次表情包之后说了几条文字；本进程还没在这个群发过表情包时返回 None。"""

        return self._plain_since.get(group_id) if group_id in self._last_sent else None

    def note_dropped(self, count: int, reasons: dict[str, int] | None = None) -> None:
        if count <= 0:
            return
        with self._lock:
            usage = self._load_usage()
            usage["totals"]["dropped"] = int(usage["totals"].get("dropped", 0)) + count
            for reason, number in (reasons or {}).items():
                usage["drops"][reason] = int(usage["drops"].get(reason, 0)) + int(number)
            self._save_usage(usage)

    def note_collect(self, group_id: int, user_id: int) -> None:
        with self._lock:
            usage = self._load_usage()
            key = f"{group_id}:{user_id}"
            usage["collects"][key] = int(usage["collects"].get(key, 0)) + 1
            usage["totals"]["collected"] = int(usage["totals"].get("collected", 0)) + 1
            self._save_usage(usage)

    def note_steal(self, key: str, sender_id: int | None = None) -> None:
        with self._lock:
            if len(self._last_steal) >= _GROUP_MEMORY:
                self._last_steal.clear()
            self._last_steal[key] = time.monotonic()
            usage = self._load_usage()
            usage["steals"][key] = int(usage["steals"].get(key, 0)) + 1
            if sender_id:
                sender = str(int(sender_id))
                usage["sender_steals"][sender] = int(usage["sender_steals"].get(sender, 0)) + 1
            usage["totals"]["stolen"] = int(usage["totals"].get("stolen", 0)) + 1
            self._save_usage(usage)

    def note_labeled(self, count: int) -> None:
        if count <= 0:
            return
        with self._lock:
            usage = self._load_usage()
            usage["totals"]["labeled"] = int(usage["totals"].get("labeled", 0)) + count
            self._save_usage(usage)

    def steals_today(self, key: str) -> int:
        return int(self.usage().get("steals", {}).get(key, 0))

    def sender_steals_today(self, user_id: int) -> int:
        return int(self.usage().get("sender_steals", {}).get(str(int(user_id)), 0))

    def forget_sender(self, user_id: int) -> None:
        with self._lock:
            usage = self._load_usage()
            if usage["sender_steals"].pop(str(int(user_id)), None) is not None:
                self._save_usage(usage)

    def steal_cooldown(self, key: str, seconds: int) -> float:
        if seconds <= 0:
            return 0.0
        last = self._last_steal.get(key)
        if last is None:
            return 0.0
        return max(seconds - (time.monotonic() - last), 0.0)

    def collects_today(self, group_id: int, user_id: int) -> int:
        return int(self.usage().get("collects", {}).get(f"{group_id}:{user_id}", 0))

    def sent_today(self, group_id: int) -> int:
        return int(self.usage().get("groups", {}).get(str(group_id), 0))

    def sent_this_hour(self, group_id: int) -> int:
        bucket = self.usage().get("hours", {}).get(str(group_id))
        if not isinstance(bucket, dict) or bucket.get("h") != now_beijing().strftime("%Y%m%d%H"):
            return 0
        return int(bucket.get("n", 0))

    def sticker_sent_today(self) -> dict[str, int]:
        return {str(key): int(value) for key, value in self.usage().get("stickers", {}).items()}

    def cooldown_remaining(self, group_id: int, seconds: int) -> float:
        if seconds <= 0:
            return 0.0
        last = self._last_sent.get(group_id)
        if last is None:
            return 0.0
        return max(seconds - (time.monotonic() - last), 0.0)

    def sticker_cooling(self, group_id: int, sticker_id: str, minutes: int) -> bool:
        if minutes <= 0:
            return False
        last = self._sticker_last.get((group_id, sticker_id))
        return last is not None and time.monotonic() - last < minutes * 60

    def recent(self, group_id: int) -> tuple[str, ...]:
        return tuple(self._recent.get(group_id, ()))

    def send_log(self) -> list[dict[str, Any]]:
        with self._lock:
            return list(self._send_log)

    def forget_stats(self, sticker_id: str) -> None:
        with self._lock:
            stats = self._load_stats()
            if stats.pop(sticker_id, None) is not None:
                with contextlib.suppress(OSError):
                    self._write_json(self.stats_path(), stats)

    # ── 没对上的名字（模型写了 [表情:xxx] 但库里没有）──

    def _load_misses(self) -> dict[str, dict[str, Any]]:
        items = self._read_json(self.misses_path()).get("items")
        return {str(key): dict(value) for key, value in items.items() if isinstance(value, dict)} if isinstance(items, dict) else {}

    def note_miss(self, name: str, group_id: int | None) -> None:
        text = " ".join(str(name or "").split())[:24]
        key = "".join(text.split()).casefold()
        if not key:
            return
        with self._lock:
            items = self._load_misses()
            row = items.get(key) or {"name": text, "count": 0, "groups": []}
            row["count"] = int(row.get("count", 0)) + 1
            row["last"] = now_beijing().strftime("%Y-%m-%d %H:%M:%S")
            groups = [str(item) for item in row.get("groups") or []]
            if group_id is not None and str(int(group_id)) not in groups:
                groups = [*groups, str(int(group_id))][-5:]
            row["groups"] = groups
            items[key] = row
            if len(items) > MAX_MISSES:
                # 满了：去掉出现次数最少、最久没再出现的。
                for old in sorted(items, key=lambda item: (int(items[item].get("count", 0)), str(items[item].get("last") or "")))[: len(items) - MAX_MISSES]:
                    items.pop(old, None)
            with contextlib.suppress(OSError):
                self._write_json(self.misses_path(), {"items": items})

    def misses(self) -> list[dict[str, Any]]:
        with self._lock:
            items = self._load_misses()
        rows = [{"key": key, **value} for key, value in items.items()]
        return sorted(rows, key=lambda row: (-int(row.get("count", 0)), str(row.get("name") or "")))

    def dismiss_miss(self, name: str | None) -> int:
        """去掉一条（``name`` 为 None 时清空）；返回去掉了几条。"""

        with self._lock:
            items = self._load_misses()
            if name is None:
                removed = len(items)
                items = {}
            else:
                removed = 1 if items.pop("".join(str(name).split()).casefold(), None) is not None else 0
            if removed:
                with contextlib.suppress(OSError):
                    self._write_json(self.misses_path(), {"items": items})
            return removed

    def reset_runtime(self) -> None:
        with self._lock:
            self._last_sent.clear()
            self._last_steal.clear()
            self._recent.clear()
            self._plain_since.clear()
            self._sticker_last.clear()
            self._send_log.clear()
            self._settings = None
            self._settings_mtime = None


_store = _Store()


def load_settings() -> StickerSettings:
    return _store.settings()


def effective_settings(group_id: int | None) -> StickerSettings:
    """这个群实际生效的设置；私聊 / 没有群号时就是全局设置。"""

    return _store.settings().for_group(group_id)


def save_settings(settings: StickerSettings) -> StickerSettings:
    return _store.save_settings(settings)


def usage_snapshot() -> dict[str, Any]:
    return _store.usage()


def stats_snapshot() -> dict[str, Any]:
    return _store.stats()


def history_snapshot(days: int = 30) -> list[dict[str, Any]]:
    """最近 ``days`` 天每天的合计（含今天），没有记录的日子补 0，按日期升序。"""

    days = min(max(int(days), 1), HISTORY_DAYS)
    history = _store.history()
    usage = _store.usage()
    today = now_beijing().date()
    rows: list[dict[str, Any]] = []
    for offset in range(days - 1, -1, -1):
        day = (today - timedelta(days=offset)).strftime("%Y-%m-%d")
        source = usage.get("totals", {}) if day == usage.get("day") else history.get(day, {})
        rows.append({"day": day, **{key: int(source.get(key, 0) or 0) for key in TOTAL_KEYS}})
    return rows


def note_sent(group_id: int, sticker_ids: list[str]) -> None:
    _store.note_sent(int(group_id), [str(item) for item in sticker_ids if item])


def private_scope(user_id: int) -> int:
    """私聊的轮换键：负的 QQ 号，和任何群号都不会撞。只在进程内用，不落盘。"""

    return -abs(int(user_id))


def note_private_sent(user_id: int, sticker_ids: list[str]) -> None:
    """私聊里真发出去的表情包：计入使用次数和“这张刚发过”，不占群额度。"""

    _store.note_sent(private_scope(user_id), [str(item) for item in sticker_ids if item], private=True)


def note_plain(group_id: int) -> None:
    _store.note_plain(int(group_id))


def note_dropped(count: int, reasons: dict[str, int] | None = None) -> None:
    _store.note_dropped(count, reasons)


def note_miss(name: str, group_id: int | None) -> None:
    _store.note_miss(name, group_id)


def misses_snapshot() -> list[dict[str, Any]]:
    return _store.misses()


def dismiss_miss(name: str | None) -> int:
    return _store.dismiss_miss(name)


def send_log() -> list[dict[str, Any]]:
    return _store.send_log()


def note_collect(group_id: int, user_id: int) -> None:
    _store.note_collect(int(group_id), int(user_id))


def steal_key(group_id: int | None, user_id: int | None = None) -> str:
    """偷图计数的键：群里按群（``g<群号>``），私聊按人（``p<QQ>``）。"""

    return f"g{int(group_id)}" if group_id else f"p{int(user_id or 0)}"


def note_steal(key: str, sender_id: int | None = None) -> None:
    _store.note_steal(key, sender_id)


def note_labeled(count: int) -> None:
    _store.note_labeled(int(count))


def steals_today(key: str) -> int:
    return _store.steals_today(key)


def sender_steals_today(user_id: int) -> int:
    return _store.sender_steals_today(user_id)


def forget_member_usage(user_id: int) -> None:
    """成员要求忘掉自己时：今天“被偷了几张”的计数也抹掉（键里有他的号码）。"""

    _store.forget_sender(user_id)


def steal_cooldown_remaining(key: str) -> float:
    return _store.steal_cooldown(key, load_settings().steal_cooldown_seconds)


def collects_today(group_id: int, user_id: int) -> int:
    return _store.collects_today(int(group_id), int(user_id))


def forget_stats(sticker_id: str) -> None:
    _store.forget_stats(sticker_id)


def recent_sent(group_id: int) -> tuple[str, ...]:
    return _store.recent(int(group_id))


def cooldown_remaining(group_id: int) -> float:
    return _store.cooldown_remaining(int(group_id), effective_settings(group_id).group_cooldown_seconds)


def set_protected(user_id: int, protected: bool) -> bool:
    """把成员加进 / 移出“别偷我的图”名单；返回名单有没有变。"""

    settings = load_settings()
    users = list(settings.steal_protected_users)
    target = int(user_id)
    if protected == (target in users):
        return False
    if protected:
        if len(users) >= 500:
            return False
        users.append(target)
    else:
        users.remove(target)
    save_settings(settings.model_copy(update={"steal_protected_users": users}))
    return True


def reset_for_tests() -> None:
    _store.reset_runtime()


@dataclass(frozen=True, slots=True)
class StickerExposure:
    """本轮能不能发表情包；``reason`` 为 None 表示可以。``settings`` 是本群生效的设置。"""

    reason: str | None
    settings: StickerSettings
    stickers: tuple[Sticker, ...] = ()
    cooldown_seconds: float = 0.0
    left_today: int = 0
    left_hour: int | None = None

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
            **({"left_hour": self.left_hour} if self.left_hour is not None else {}),
        }


REASON_TEXT: dict[str, str] = {
    "closed": "表情包功能被管理员关了",
    "group_closed": "本群没有开放表情包",
    "empty": "本群还没有可用的表情包",
    "cooldown": "刚发过表情包，还在冷却",
    "daily_limit": "本群今天的表情包额度用完了",
    "hourly_limit": "本群这个小时的表情包额度用完了",
    "spacing": "上一张表情包之后还没说够几条文字",
    "quiet": "现在是安静时段，不发表情包",
    "proactive_off": "主动发言里不发表情包",
    "persona_off": "人设的接梗倾向太低，不主动发表情包",
    "too_many": "一条发言里的表情包超过了上限",
    "unknown": "模型写的名字在本群可用的库里对不上",
    "repeat": "这张刚在本群发过，或今天发够了",
}


def exposure(group_id: int | None, *, proactive: bool = False) -> StickerExposure:
    """这一轮给不给模型看表情包菜单。不做任何关键词判断，只看开关、库存、频率和时间。"""

    base = load_settings()
    if not base.enabled:
        return StickerExposure("closed", base)
    if group_id is None or not base.group_open(int(group_id)):
        return StickerExposure("group_closed", base)
    gid = int(group_id)
    settings = base.for_group(gid)
    if proactive and not settings.allow_proactive:
        return StickerExposure("proactive_off", settings)
    stickers = tuple(available_stickers(gid))
    if not stickers:
        return StickerExposure("empty", settings)
    if in_quiet_hours(settings):
        return StickerExposure("quiet", settings, stickers)
    left = max(settings.group_daily_limit - _store.sent_today(gid), 0)
    if left <= 0:
        return StickerExposure("daily_limit", settings, stickers, left_today=0)
    left_hour: int | None = None
    if settings.group_hourly_limit > 0:
        left_hour = max(settings.group_hourly_limit - _store.sent_this_hour(gid), 0)
        if left_hour <= 0:
            return StickerExposure("hourly_limit", settings, stickers, left_today=left, left_hour=0)
    remaining = _store.cooldown_remaining(gid, settings.group_cooldown_seconds)
    if remaining > 0:
        return StickerExposure("cooldown", settings, stickers, cooldown_seconds=remaining, left_today=left, left_hour=left_hour)
    if settings.min_messages_between > 0:
        spoken = _store.plain_since(gid)
        if spoken is not None and spoken < settings.min_messages_between:
            return StickerExposure("spacing", settings, stickers, left_today=left, left_hour=left_hour)
    return StickerExposure(None, settings, stickers, left_today=left, left_hour=left_hour)


def _uses(stats: dict[str, Any], item: Sticker) -> int:
    row = stats.get(item.sticker_id)
    return int(row.get("uses", 0)) if isinstance(row, dict) else 0


def sendable_now(group_id: int, stickers: list[Sticker] | tuple[Sticker, ...], settings: StickerSettings | None = None) -> list[Sticker]:
    """去掉“这张刚在本群发过”（同图间隔）和“这张今天发够了”（单张每日上限）的。"""

    settings = settings or effective_settings(group_id or None)
    capped = [item for item in stickers if item.daily_cap > 0]
    today = _store.sticker_sent_today() if capped else {}
    output: list[Sticker] = []
    for item in stickers:
        if item.daily_cap > 0 and today.get(item.sticker_id, 0) >= item.daily_cap:
            continue
        if _store.sticker_cooling(int(group_id), item.sticker_id, settings.sticker_cooldown_minutes):
            continue
        output.append(item)
    return output


def pick_sticker(
    group_id: int,
    candidates: list[Sticker],
    *,
    rng: random.Random | None = None,
    settings: StickerSettings | None = None,
    strict: bool = False,
) -> Sticker | None:
    """同一个名字/情绪有好几张时挑一张：避开本群最近发过的，再在用得少的里按权重随机。

    ``strict`` 为真时，候选全在“同图间隔 / 单张每日上限”里就返回 None（发送层据此去掉这个
    标记）；否则只在有得选的时候才避开它们。
    """

    if not candidates:
        return None
    allowed = sendable_now(group_id, candidates, settings)
    if not allowed:
        if strict:
            return None
        allowed = list(candidates)
    if len(allowed) == 1:
        return allowed[0]
    # 权重 0 的只有模型点名（没有别的候选）时才用。
    weighted = [item for item in allowed if item.weight > 0] or allowed
    recent = set(recent_sent(group_id))
    fresh = [item for item in weighted if item.sticker_id not in recent] or weighted
    stats = stats_snapshot()
    fewest = min(_uses(stats, item) for item in fresh)
    pool = [item for item in fresh if _uses(stats, item) <= fewest + 2]
    weights = [max(item.weight, 1) for item in pool]
    chooser = rng or random
    return chooser.choices(pool, weights=weights, k=1)[0]


def _created_within(item: Sticker, days: int, now: datetime) -> bool:
    if days <= 0 or not item.created_at:
        return False
    try:
        created = datetime.strptime(item.created_at, "%Y-%m-%d %H:%M:%S")  # noqa: DTZ007
    except ValueError:
        return False
    return now.replace(tzinfo=None) - created <= timedelta(days=days)


def _strategy_order(items: list[Sticker], strategy: str, stats: dict[str, Any], recent: set[str]) -> list[Sticker]:
    """``items`` 已经按“每小时换一批”洗过牌；这里按策略重排（稳定排序，洗牌顺序做并列时的次序）。"""

    if strategy == "popular":
        ordered = sorted(items, key=lambda item: -_uses(stats, item))
    elif strategy == "explore":
        ordered = sorted(items, key=lambda item: _uses(stats, item))
    elif strategy == "balanced":
        hot = sorted(items, key=lambda item: -_uses(stats, item))
        cold = sorted(items, key=lambda item: _uses(stats, item))
        ordered = []
        seen: set[str] = set()
        for pair in zip(hot, cold, strict=True):
            for item in pair:
                if item.sticker_id not in seen:
                    seen.add(item.sticker_id)
                    ordered.append(item)
    else:
        ordered = list(items)
    # 权重“多用”的往前挪一档；最近刚发过的放最后（各策略都一样）。
    ordered.sort(key=lambda item: item.weight < 2)
    ordered.sort(key=lambda item: item.sticker_id in recent)
    return ordered


def menu_plan(
    group_id: int,
    stickers: tuple[Sticker, ...] | list[Sticker],
    *,
    size: int,
    seed: str = "",
    settings: StickerSettings | None = None,
) -> list[dict[str, Any]]:
    """菜单每一项和它为什么在这（置顶 / 新图 / 最爱 / 轮到）；WebUI 预览用，``menu_entries`` 取其中的名字和说明。

    顺序：置顶 → 新入库的和“最爱”（最多占三分之一）→ 其余按策略；同名合并；同一个主标签
    先最多占四分之一，位置没填满再放开。只看库存、发送记录和时间，不看聊天内容。
    """

    settings = settings or effective_settings(group_id or None)
    now = now_beijing()
    recent = set(recent_sent(group_id))
    rng = random.Random(f"{group_id}:{now.strftime('%Y%m%d%H')}:{seed}")
    pool = [item for item in sendable_now(group_id, stickers, settings) if item.weight > 0]
    rng.shuffle(pool)
    stats = stats_snapshot()
    pinned = [item for item in pool if item.pinned]
    boosted = [item for item in pool if not item.pinned and (item.weight >= 3 or _created_within(item, settings.new_boost_days, now))]
    boosted.sort(key=lambda item: item.sticker_id in recent)
    boosted_ids = {item.sticker_id for item in boosted[: max(size // 3, 1)]}
    rest = _strategy_order([item for item in pool if not item.pinned and item.sticker_id not in boosted_ids], settings.menu_strategy, stats, recent)
    ordered = [(item, "pinned") for item in pinned]
    ordered += [(item, "favorite" if item.weight >= 3 else "new") for item in boosted if item.sticker_id in boosted_ids]
    ordered += [(item, settings.menu_strategy) for item in rest]
    per_tag = max(size // 4, 2)
    tag_used: dict[str, int] = {}
    seen: set[str] = set()
    chosen: list[dict[str, Any]] = []
    held_back: list[tuple[Sticker, str]] = []

    def take(item: Sticker, why: str) -> None:
        hint = item.description or "、".join(tag for tag in item.tags if tag != item.name)
        chosen.append({"id": item.sticker_id, "name": item.name, "hint": hint[: settings.menu_hint_chars], "why": why})

    for item, why in ordered:
        if len(chosen) >= size:
            break
        key = "".join(item.name.split()).casefold()
        if not key or key in seen:
            continue
        primary = "".join(item.tags[0].split()).casefold() if item.tags else ""
        if why != "pinned" and primary and tag_used.get(primary, 0) >= per_tag:
            held_back.append((item, why))
            continue
        seen.add(key)
        if primary:
            tag_used[primary] = tag_used.get(primary, 0) + 1
        take(item, why)
    for item, why in held_back:
        if len(chosen) >= size:
            break
        key = "".join(item.name.split()).casefold()
        if key in seen:
            continue
        seen.add(key)
        take(item, why)
    return chosen


def menu_entries(
    group_id: int,
    stickers: tuple[Sticker, ...] | list[Sticker],
    *,
    size: int,
    seed: str = "",
    settings: StickerSettings | None = None,
) -> list[tuple[str, str]]:
    """给模型看的菜单 ``[(名字, 一句说明)]``；挑选规则见 :func:`menu_plan`。"""

    return [(row["name"], row["hint"]) for row in menu_plan(group_id, stickers, size=size, seed=seed, settings=settings)]


__all__ = [
    "FREQUENCY_LABELS",
    "MENU_STRATEGY_LABELS",
    "REASON_TEXT",
    "GroupOverride",
    "StickerExposure",
    "StickerSettings",
    "collects_today",
    "cooldown_remaining",
    "dismiss_miss",
    "effective_settings",
    "exposure",
    "forget_member_usage",
    "forget_stats",
    "history_snapshot",
    "in_quiet_hours",
    "load_settings",
    "menu_entries",
    "menu_plan",
    "misses_snapshot",
    "note_collect",
    "note_dropped",
    "note_labeled",
    "note_miss",
    "note_plain",
    "note_private_sent",
    "note_sent",
    "note_steal",
    "pick_sticker",
    "private_scope",
    "recent_sent",
    "reset_for_tests",
    "save_settings",
    "send_log",
    "sendable_now",
    "sender_steals_today",
    "set_protected",
    "stats_snapshot",
    "steal_cooldown_remaining",
    "steal_key",
    "steals_today",
    "usage_snapshot",
]
