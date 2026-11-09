# ruff: noqa: E501
"""好感度玩法的全站策略：每个等级给什么权益、礼物和商店、每日任务、衰减。

原来好感只是一个数加一个等级名，涨到“挚友”和“陌生”在功能上没有任何区别。这里定义
“每一级到底有什么用”，由 :mod:`affinity_perks` 在各个真正生效的地方读取：

- **额度加成**：私聊每天能聊的轮数、私聊联网次数、私聊表情包、群里代码沙箱/网页搜索/
  收藏表情包的个人每日额度、私聊人设每天能自己改几次；
- **签到加成**：签到积分按等级多给一成到三成；
- **解锁**：互动动作（击掌/摸头/抱抱/贴贴/举高高）、让 Bot 按你定的称呼叫你、私聊
  主动提议、久别问候、升级礼包；
- **说话分寸**：每级一句相处提示和一句“可以做什么”，给群聊/私聊 Agent 的发言人层；
- **玩法**：用签到积分送礼物（每天有一样 Bot 最喜欢的，效果翻倍）、商店兑换道具
  （补签卡、好感护盾、签到暴击卡、任务刷新券）、每日三个小任务、长期不来往的衰减。

策略存在 ``data/affinity/policy.json``（WebUI「好感度」页可改），坏文件或缺字段一律
回落默认值；不建表、不需要迁移。
"""

from __future__ import annotations

import contextlib
import json
import os
import threading
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from .affinity import LEVEL_TONE, LEVELS

LEVEL_NAMES: tuple[str, ...] = tuple(name for _floor, name in LEVELS)

# ── 解锁项 ────────────────────────────────────────────────────────────
# key → (展示名, 说明)。新增解锁项要在 affinity_perks 里有真正读取它的地方。
UNLOCKS: dict[str, tuple[str, str]] = {
    "interact.highfive": ("击掌", "/击掌 和 Bot 击个掌"),
    "interact.pat": ("摸头", "/摸头 摸摸 Bot 的头"),
    "interact.hug": ("抱抱", "/抱抱 给 Bot 一个拥抱"),
    "interact.cuddle": ("贴贴", "/贴贴 和 Bot 贴贴"),
    "interact.lift": ("举高高", "/举高高 把 Bot 举起来"),
    "nickname": ("专属称呼", "/叫我 <称呼>：群里和私聊的 Bot 都按你定的称呼叫你"),
    "offers": ("主动提议", "私聊里 Bot 会隔一阵主动问你要不要帮忙做点事"),
    "care": ("久别问候", "好几天没聊时，Bot 会在私聊里主动问候你一句"),
    "levelup_gift": ("升级礼包", "每升一级送一份道具礼包"),
}
INTERACT_KEYS: tuple[str, ...] = tuple(key for key in UNLOCKS if key.startswith("interact."))


class LevelPerks(BaseModel):
    """一个等级的权益。数值都是“在基础额度上加多少”，基础额度为 0（被关掉）时不加。"""

    model_config = ConfigDict(extra="ignore")

    level: str
    # 一句相处提示（替代 affinity.LEVEL_TONE，给模型看）
    tone: str = Field(default="", max_length=80)
    # 这一级 Bot 可以做什么（给模型看的分寸，如“可以撒娇、翻旧梗”）
    behavior: str = Field(default="", max_length=120)
    # 额度加成
    private_turns_pct: int = Field(default=0, ge=0, le=500)
    private_web_bonus: int = Field(default=0, ge=0, le=200)
    private_sticker_bonus: int = Field(default=0, ge=0, le=200)
    web_search_bonus: int = Field(default=0, ge=0, le=500)
    sandbox_bonus: int = Field(default=0, ge=0, le=500)
    sticker_collect_bonus: int = Field(default=0, ge=0, le=200)
    persona_edit_bonus: int = Field(default=0, ge=0, le=100)
    memory_bonus: int = Field(default=0, ge=0, le=30)
    # 签到积分加成（百分比）
    checkin_points_pct: int = Field(default=0, ge=0, le=300)
    # 每天最多送几次礼物、商店折扣
    gift_daily: int = Field(default=3, ge=0, le=100)
    shop_discount_pct: int = Field(default=0, ge=0, le=90)
    # 解锁项（UNLOCKS 的 key）
    unlocks: list[str] = Field(default_factory=list, max_length=40)
    # 升到这一级时送的道具 {道具 key: 数量}
    levelup_items: dict[str, int] = Field(default_factory=dict)

    @field_validator("unlocks")
    @classmethod
    def _known_unlocks(cls, value: list[str]) -> list[str]:
        seen: list[str] = []
        for key in value:
            if key in UNLOCKS and key not in seen:
                seen.append(key)
        return seen

    @field_validator("levelup_items")
    @classmethod
    def _clean_items(cls, value: dict[str, int]) -> dict[str, int]:
        return {str(k): max(0, min(int(v), 99)) for k, v in value.items() if int(v) > 0}


class Gift(BaseModel):
    model_config = ConfigDict(extra="ignore")

    key: str = Field(min_length=1, max_length=24, pattern=r"^[a-z0-9_]+$")
    name: str = Field(min_length=1, max_length=12)
    emoji: str = Field(default="🎁", max_length=8)
    price: int = Field(ge=0, le=100000)
    affinity: int = Field(ge=1, le=100)
    enabled: bool = True


class ShopItem(BaseModel):
    model_config = ConfigDict(extra="ignore")

    key: str = Field(min_length=1, max_length=24, pattern=r"^[a-z0-9_]+$")
    name: str = Field(min_length=1, max_length=12)
    price: int = Field(ge=0, le=100000)
    description: str = Field(default="", max_length=80)
    min_level: str = "陌生"
    # 背包里最多存几个
    max_hold: int = Field(default=5, ge=1, le=99)
    enabled: bool = True

    @field_validator("min_level")
    @classmethod
    def _level(cls, value: str) -> str:
        return value if value in LEVEL_NAMES else "陌生"


# 道具效果：只有这几种由代码实现；商店条目的 key 必须是其中之一。
ITEM_EFFECTS: dict[str, str] = {
    "repair_card": "补签卡：连签断了 7 天内，在某个群续上（把断掉的那几天算作签过）",
    "shield": "好感护盾：14 天内长时间不来往也不会掉好感",
    "lucky_card": "签到暴击卡：今天下一次签到积分翻倍",
    "quest_refresh": "任务刷新券：把今天没完成的任务换一批",
}


class QuestPolicy(BaseModel):
    model_config = ConfigDict(extra="ignore")

    enabled: bool = True
    per_day: int = Field(default=3, ge=1, le=5)
    reward_affinity: int = Field(default=2, ge=0, le=20)
    # 三个全做完再送一张道具（空字符串不送）
    all_done_item: str = "lucky_card"


class DecayPolicy(BaseModel):
    model_config = ConfigDict(extra="ignore")

    enabled: bool = True
    # 多少天没有任何正向来往开始掉
    idle_days: int = Field(default=14, ge=3, le=365)
    per_day: int = Field(default=1, ge=1, le=50)
    # 只掉到当前等级的下限，不会因为不来往掉级
    keep_level: bool = True


class CarePolicy(BaseModel):
    model_config = ConfigDict(extra="ignore")

    # 多少天没私聊算“久别”
    idle_days: int = Field(default=4, ge=1, le=60)
    # 超过这么多天就不再问候（人可能已经不用了）
    give_up_days: int = Field(default=45, ge=2, le=365)


def _default_levels() -> list[LevelPerks]:
    rows: list[dict[str, Any]] = [
        {
            "behavior": "先礼貌友好，别开过头的玩笑，也别装熟",
            "gift_daily": 3,
            "unlocks": ["interact.highfive"],
        },
        {
            "behavior": "可以随意一点，偶尔接个梗",
            "private_turns_pct": 10,
            "private_web_bonus": 1,
            "private_sticker_bonus": 2,
            "web_search_bonus": 2,
            "sandbox_bonus": 2,
            "sticker_collect_bonus": 2,
            "checkin_points_pct": 5,
            "gift_daily": 4,
            "unlocks": ["interact.highfive", "interact.pat", "offers", "levelup_gift"],
            "levelup_items": {"repair_card": 1},
        },
        {
            "behavior": "可以开玩笑、吐槽，记得他说过的事并自然提起",
            "private_turns_pct": 25,
            "private_web_bonus": 2,
            "private_sticker_bonus": 4,
            "web_search_bonus": 4,
            "sandbox_bonus": 4,
            "sticker_collect_bonus": 4,
            "persona_edit_bonus": 2,
            "memory_bonus": 2,
            "checkin_points_pct": 10,
            "gift_daily": 5,
            "shop_discount_pct": 5,
            "unlocks": ["interact.highfive", "interact.pat", "interact.hug", "offers", "nickname", "levelup_gift"],
            "levelup_items": {"shield": 1, "lucky_card": 1},
        },
        {
            "behavior": "像朋友一样说话，会关心他的近况，帮忙时更上心",
            "private_turns_pct": 50,
            "private_web_bonus": 4,
            "private_sticker_bonus": 6,
            "web_search_bonus": 8,
            "sandbox_bonus": 8,
            "sticker_collect_bonus": 6,
            "persona_edit_bonus": 4,
            "memory_bonus": 4,
            "checkin_points_pct": 15,
            "gift_daily": 6,
            "shop_discount_pct": 10,
            "unlocks": ["interact.highfive", "interact.pat", "interact.hug", "interact.cuddle", "offers", "nickname", "care", "levelup_gift"],
            "levelup_items": {"repair_card": 1, "lucky_card": 2},
        },
        {
            "behavior": "可以更亲昵、偶尔撒娇，翻翻你们的旧梗，他心情不好时多陪陪",
            "private_turns_pct": 75,
            "private_web_bonus": 6,
            "private_sticker_bonus": 8,
            "web_search_bonus": 12,
            "sandbox_bonus": 12,
            "sticker_collect_bonus": 8,
            "persona_edit_bonus": 6,
            "memory_bonus": 6,
            "checkin_points_pct": 20,
            "gift_daily": 8,
            "shop_discount_pct": 15,
            "unlocks": list(UNLOCKS),
            "levelup_items": {"shield": 1, "repair_card": 1, "quest_refresh": 1},
        },
        {
            "behavior": "像多年老友一样相处，彼此都很了解，有默契，说话不用客套",
            "private_turns_pct": 100,
            "private_web_bonus": 10,
            "private_sticker_bonus": 10,
            "web_search_bonus": 20,
            "sandbox_bonus": 20,
            "sticker_collect_bonus": 10,
            "persona_edit_bonus": 10,
            "memory_bonus": 8,
            "checkin_points_pct": 30,
            "gift_daily": 10,
            "shop_discount_pct": 20,
            "unlocks": list(UNLOCKS),
            "levelup_items": {"shield": 2, "repair_card": 2, "lucky_card": 2},
        },
    ]
    return [
        LevelPerks(level=name, tone=LEVEL_TONE.get(name, ""), **row)
        for name, row in zip(LEVEL_NAMES, rows, strict=True)
    ]


def _default_gifts() -> list[Gift]:
    return [
        Gift(key="candy", name="糖果", emoji="🍬", price=10, affinity=1),
        Gift(key="milk_tea", name="奶茶", emoji="🧋", price=30, affinity=2),
        Gift(key="flower", name="鲜花", emoji="💐", price=60, affinity=3),
        Gift(key="cake", name="蛋糕", emoji="🍰", price=120, affinity=5),
        Gift(key="star", name="星星灯", emoji="🌟", price=300, affinity=10),
    ]


def _default_shop() -> list[ShopItem]:
    return [
        ShopItem(key="repair_card", name="补签卡", price=80, description=ITEM_EFFECTS["repair_card"], max_hold=3),
        ShopItem(key="shield", name="好感护盾", price=60, description=ITEM_EFFECTS["shield"], min_level="面熟", max_hold=3),
        ShopItem(key="lucky_card", name="签到暴击卡", price=40, description=ITEM_EFFECTS["lucky_card"], max_hold=5),
        ShopItem(key="quest_refresh", name="任务刷新券", price=20, description=ITEM_EFFECTS["quest_refresh"], max_hold=5),
    ]


class AffinityPolicy(BaseModel):
    """WebUI「好感度 → 策略」的全部字段。"""

    model_config = ConfigDict(extra="ignore")

    enabled: bool = True
    levels: list[LevelPerks] = Field(default_factory=_default_levels)
    gifts: list[Gift] = Field(default_factory=_default_gifts, max_length=20)
    # 礼物来源每天最多加多少好感（跨所有礼物）
    gift_affinity_daily_cap: int = Field(default=10, ge=0, le=500)
    # 今日心仪礼物效果倍数
    favorite_multiplier: int = Field(default=2, ge=1, le=5)
    shop: list[ShopItem] = Field(default_factory=_default_shop, max_length=20)
    quests: QuestPolicy = Field(default_factory=QuestPolicy)
    decay: DecayPolicy = Field(default_factory=DecayPolicy)
    care: CarePolicy = Field(default_factory=CarePolicy)
    # 互动动作每天最多加多少好感
    interact_affinity_daily_cap: int = Field(default=3, ge=0, le=50)
    # 群聊/私聊 Agent 会不会在升级后自然提一句
    announce_levelup: bool = True

    @field_validator("levels")
    @classmethod
    def _complete_levels(cls, value: list[LevelPerks]) -> list[LevelPerks]:
        """补齐/排序到 LEVELS 的六个等级；多余或未知等级丢弃。"""

        by_name = {row.level: row for row in value if row.level in LEVEL_NAMES}
        defaults = {row.level: row for row in _default_levels()}
        return [by_name.get(name, defaults[name]) for name in LEVEL_NAMES]

    @field_validator("gifts")
    @classmethod
    def _unique_gifts(cls, value: list[Gift]) -> list[Gift]:
        seen: set[str] = set()
        out: list[Gift] = []
        for gift in value:
            if gift.key not in seen:
                seen.add(gift.key)
                out.append(gift)
        return out

    @field_validator("shop")
    @classmethod
    def _known_items(cls, value: list[ShopItem]) -> list[ShopItem]:
        seen: set[str] = set()
        out: list[ShopItem] = []
        for item in value:
            if item.key in ITEM_EFFECTS and item.key not in seen:
                seen.add(item.key)
                out.append(item)
        return out

    def perks(self, level_name: str) -> LevelPerks:
        for row in self.levels:
            if row.level == level_name:
                return row
        return self.levels[0]

    def gift(self, key_or_name: str) -> Gift | None:
        text = str(key_or_name or "").strip()
        for gift in self.gifts:
            if gift.enabled and text in {gift.key, gift.name, gift.emoji}:
                return gift
        for gift in self.gifts:
            if gift.enabled and text and text in gift.name:
                return gift
        return None

    def item(self, key_or_name: str) -> ShopItem | None:
        text = str(key_or_name or "").strip()
        for item in self.shop:
            if text in {item.key, item.name}:
                return item
        for item in self.shop:
            if text and text in item.name:
                return item
        return None

    def item_name(self, key: str) -> str:
        found = self.item(key)
        if found is not None:
            return found.name
        return {"repair_card": "补签卡", "shield": "好感护盾", "lucky_card": "签到暴击卡", "quest_refresh": "任务刷新券"}.get(key, key)

    def unlock_level(self, unlock: str) -> str | None:
        """最低哪一级解锁了这一项；都没有返回 None。"""

        for row in self.levels:
            if unlock in row.unlocks:
                return row.level
        return None


# ── 读写 ────────────────────────────────────────────────────────────

_LOCK = threading.RLock()
_cache: tuple[float, AffinityPolicy] | None = None


def data_root() -> Path:
    raw = os.environ.get("AFFINITY_DATA_DIR", "").strip()
    return Path(raw) if raw else Path("data") / "affinity"


def _policy_path() -> Path:
    return data_root() / "policy.json"


def load_policy() -> AffinityPolicy:
    """读策略；文件不存在/损坏/字段非法时回落默认值。按文件修改时间缓存。"""

    global _cache  # noqa: PLW0603
    path = _policy_path()
    with _LOCK:
        try:
            mtime = path.stat().st_mtime
        except OSError:
            mtime = -1.0
        if _cache is not None and _cache[0] == mtime and mtime >= 0:
            return _cache[1]
        policy = AffinityPolicy()
        if mtime >= 0:
            try:
                policy = AffinityPolicy.model_validate(json.loads(path.read_text(encoding="utf-8")))
            except (OSError, ValueError, ValidationError):
                policy = AffinityPolicy()
        _cache = (mtime, policy)
        return policy


def save_policy(policy: AffinityPolicy | dict[str, Any]) -> AffinityPolicy:
    """校验后原子写入；非法输入抛 ``ValidationError``。"""

    global _cache  # noqa: PLW0603
    value = policy if isinstance(policy, AffinityPolicy) else AffinityPolicy.model_validate(policy)
    path = _policy_path()
    with _LOCK:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(value.model_dump_json(indent=1), encoding="utf-8")
        tmp.replace(path)
        _cache = None
    return value


def reset_policy() -> AffinityPolicy:
    global _cache  # noqa: PLW0603
    with _LOCK, contextlib.suppress(OSError):
        _policy_path().unlink()
    _cache = None
    return load_policy()


def clear_cache() -> None:
    """测试用。"""

    global _cache  # noqa: PLW0603
    _cache = None


__all__ = [
    "INTERACT_KEYS",
    "ITEM_EFFECTS",
    "LEVEL_NAMES",
    "UNLOCKS",
    "AffinityPolicy",
    "CarePolicy",
    "DecayPolicy",
    "Gift",
    "LevelPerks",
    "QuestPolicy",
    "ShopItem",
    "clear_cache",
    "data_root",
    "load_policy",
    "reset_policy",
    "save_policy",
]
