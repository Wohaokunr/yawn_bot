# ruff: noqa: E501, RUF001, PLR2004, PLR0913, TID252
"""私聊分条的三件事：私聊专用的节奏说明、条数分布追踪、分条发到一半被打断。

1. **节奏说明**（``private_rhythm_instruction``）：私聊大多数回复一条就够，两条其次，
   三条以上只在情绪真的上来时用。以前的说明把“分条”写成平常闲聊的默认、写“2~3 条”、
   示例里连发正好三条，模型几乎每次都写 ``xxx|||xxx|||xxx``。这里的示例覆盖一条、两条、
   四条连发、蹦字、补一句、长内容分段、表情包，没有一个是三条，并写明“条数不是固定格式”。
   只随人设变（表现力/详略），放在 system#1（前缀缓存），不含任何每轮会变的内容。
2. **条数分布**（``note_delivery`` / ``mix_note``）：按人记最近几次实际发了几条（进程内、
   有界）。最近几次条数一模一样（比如连着三次三条）或者每次都分条时，在本轮资料（system#3）
   里提醒一句“这次按内容来，一条也行”。只看条数这个结构事实，不按关键词猜内容。
3. **打断**（``InterruptWatch`` / ``note_cut`` / ``take_cut``）：分条逐条发的时候他又发来
   新消息（对话队列的到达计数变了），剩下没发的文字和表情包就不发了；没发出去的不写进
   历史，只在下一轮的本轮资料里告诉模型“上一条说到一半被打断了，第几条之后他没看到”。
   虚拟电脑里的图片/文件是他要的东西，默认照发（``PRIVATE_AGENT_INTERRUPT_KEEP_MEDIA``）。

全部状态都在进程内、按人有界，重启就清零（只影响提醒，不影响对话）。
"""

from __future__ import annotations

import time
from collections import Counter, deque
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from .log import dbg_exc
from .speech_rhythm import (
    MAX_SPLIT_PARTS,
    RHYTHM_BLOCK,
    RHYTHM_LABELS,
    RhythmSettings,
    rhythm_settings,
)

# ── 配置 ───────────────────────────────────────────────────────────────────

DEFAULT_PRIVATE_MAX_PARTS = 4
# 分条之间的停顿被切成这么长的小段，每段之后看一眼有没有新消息（打断能在半秒内生效）。
POLL_SECONDS = 0.25
# 被打断的记录多久以内还告诉下一轮（秒）。太久以后他已经不记得那条了。
CUT_TTL_SECONDS = 600.0
# 每人记最近几次发送的形状；全站最多记多少人（超出时丢最久没说话的）。
RECENT_SHAPES = 8
MAX_TRACKED_USERS = 4096
# 最近连着这么多次条数完全一样（且都 ≥2 条）就提醒一句。
SAME_COUNT_STREAK = 3
# 最近连着这么多次都分了条（≥2 条）就提醒一句。
SPLIT_STREAK = 4
# 冷启动时（进程刚重启）用历史里最近几条回复的行数估一下条数。
SEED_FROM_HISTORY = 4


def _config() -> Any:
    try:
        from ..private_chat_bridge import private_agent_config
    except Exception:  # noqa: BLE001
        return None
    return private_agent_config


def interrupt_enabled() -> bool:
    config = _config()
    return bool(getattr(config, "private_agent_interrupt", True)) if config else True


def keep_media_on_interrupt() -> bool:
    config = _config()
    return bool(getattr(config, "private_agent_interrupt_keep_media", True)) if config else True


def mix_hint_enabled() -> bool:
    config = _config()
    return bool(getattr(config, "private_agent_rhythm_mix_hint", True)) if config else True


def private_max_parts() -> int:
    """私聊“分条”最多几条（PRIVATE_AGENT_REPLY_MAX_PARTS，1~4）。

    以前私聊共用群聊的 AGENT_REPLY_MAX_PARTS=3：模型写了四条也会被并成三条，
    条数天然往 3 堆。私聊单独一个上限，默认 4。
    """

    config = _config()
    raw = getattr(config, "private_agent_reply_max_parts", DEFAULT_PRIVATE_MAX_PARTS) if config else DEFAULT_PRIVATE_MAX_PARTS
    try:
        value = int(raw)
    except (TypeError, ValueError):
        value = DEFAULT_PRIVATE_MAX_PARTS
    return max(1, min(value, MAX_SPLIT_PARTS))


def private_rhythm_settings() -> RhythmSettings:
    """群聊那套节奏设置（开关、快慢、总停顿上限），只把条数上限换成私聊自己的。"""

    base = rhythm_settings()
    return RhythmSettings(
        mode=base.mode,
        max_parts=private_max_parts(),
        pace=base.pace,
        max_spread_seconds=base.max_spread_seconds,
    )


# ── 节奏说明（system#1，只随人设变）────────────────────────────────────────


def private_rhythm_instruction(
    *,
    expressiveness: int = 1,
    verbosity: int = 1,
    settings: RhythmSettings | None = None,
) -> str:
    """私聊的发送节奏说明。输出只取决于人设档位和配置，同一个人每轮逐字节一样。"""

    resolved = settings or private_rhythm_settings()
    if not resolved.enabled:
        return ""
    cap = resolved.max_parts
    lines = [
        "发送节奏（每次都要选）：回复最前面先写【节奏:整段/分条/连发/蹦字】，标签不会发出去；"
        "||| 隔开的每段是一条 QQ 消息，没写 ||| 就是一条。",
        "条数跟着内容和心情走，不是固定格式：私聊里大多数回复一条就够——接一句话、回答一个问题、"
        "给个反应、认真讲道理，都用【节奏:整段】一条发完；有自然的停顿（先反应再说事、说完想补一句）才分两条；"
        "三条及以上只在情绪真的上来（激动、震惊、连环吐槽）或者要一下下蹦字时用。"
        "别每次都分成同样的几条，上一条怎么发的这条不用照着来。",
        "可以这样发（照抄格式，内容换成你的）：",
        "一条：【节奏:整段】好耶 那我等你消息；",
        "两条：【节奏:分条】刚看到|||笑死 这也太巧了；",
        "说完补一句：【节奏:分条】这题我会|||略略略～；",
        "连发（几个字一条，四五条也行）：【节奏:连发】不是吧|||真的假的|||你骗我|||我不信；",
        "蹦字（8 字以内一句，会一个字一个字蹦出去）：【节奏:蹦字】你在干嘛呀；",
        "长内容（讲步骤、认真聊、一百多字以上）：【节奏:整段】写成一条，用空行分好段，会按段落分几条发；",
        "表情包、图片、文件各自单独成一条，可以只发一个表情包，也可以放在文字前后，例如【节奏:分条】行吧|||[表情:名字]。",
        f"分条最多 {cap} 条（连发、蹦字另算）；几个字又没想好怎么断就【节奏:整段】，系统不会替你硬拆。",
        "节奏不是多说话的理由：分几条，加起来的字数也和发一条时一样，守住上面的字数目标；"
        "连发和蹦字通常比一条更短。代码、列表、链接放同一条。",
        "你分条发的时候他要是插话，后面没发出去的就不发了，所以最要紧的话放第一条。",
    ]
    if verbosity <= 0:
        lines.append("你话少：基本都是一条，几个字的连发偶尔用。")
    if expressiveness >= 3:
        lines.append("你的表现力较强，情绪明显时可以更放开地用连发和蹦字，平常照样一条为主。")
    elif expressiveness <= 0:
        lines.append("你比较收着，很少连发和蹦字。")
    return "".join(lines)


# ── 条数分布 ───────────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class Shape:
    parts: int  # 文字条数（表情包/图片/文件不算）
    rhythm: str
    media: int = 0
    interrupted: bool = False
    at: float = 0.0


@dataclass(slots=True)
class _Stats:
    deliveries: int = 0
    interrupted: int = 0
    skipped_items: int = 0
    by_parts: Counter[str] = field(default_factory=Counter)
    by_rhythm: Counter[str] = field(default_factory=Counter)
    with_media: int = 0
    hints: int = 0
    cut_notes: int = 0

    def as_dict(self) -> dict[str, Any]:
        total = self.deliveries or 1
        return {
            "deliveries": self.deliveries,
            "interrupted": self.interrupted,
            "skippedItems": self.skipped_items,
            "byParts": {key: self.by_parts.get(key, 0) for key in ("1", "2", "3", "4", "5+")},
            "partsShare": {
                key: round(self.by_parts.get(key, 0) / total, 3) for key in ("1", "2", "3", "4", "5+")
            },
            "byRhythm": {
                RHYTHM_LABELS.get(key, key): count for key, count in sorted(self.by_rhythm.items())
            },
            "withMedia": self.with_media,
            "mixHints": self.hints,
            "cutNotes": self.cut_notes,
        }


_recent: dict[int, deque[Shape]] = {}
_stats = _Stats()


def _bucket(parts: int) -> str:
    return "5+" if parts >= 5 else str(max(parts, 1))


def note_delivery(
    user_id: int,
    *,
    parts: int,
    rhythm: str,
    media: int = 0,
    interrupted: bool = False,
    skipped: int = 0,
    now: float | None = None,
) -> None:
    """记一次实际发出去的形状（只算送达的部分）。"""

    if parts <= 0 and media <= 0:
        return
    moment = time.time() if now is None else now
    uid = int(user_id)
    shapes = _recent.get(uid)
    if shapes is None:
        if len(_recent) >= MAX_TRACKED_USERS:
            oldest = min(_recent, key=lambda key: _recent[key][-1].at if _recent[key] else 0.0)
            _recent.pop(oldest, None)
        shapes = deque(maxlen=RECENT_SHAPES)
        _recent[uid] = shapes
    shapes.append(Shape(max(parts, 0), rhythm or RHYTHM_BLOCK, media, interrupted, moment))
    _stats.deliveries += 1
    if parts > 0:
        _stats.by_parts[_bucket(parts)] += 1
    _stats.by_rhythm[rhythm or RHYTHM_BLOCK] += 1
    if media:
        _stats.with_media += 1
    if interrupted:
        _stats.interrupted += 1
        _stats.skipped_items += max(skipped, 0)


def _history_counts(history: list[dict[str, Any]] | None) -> list[int]:
    """冷启动估计：历史里 Bot 的回复是逐条用换行拼起来存的，行数≈条数（粗略，只用来提醒）。"""

    counts: list[int] = []
    for message in reversed(history or []):
        if message.get("role") != "assistant":
            continue
        content = str(message.get("content") or "")
        if not content.strip():
            continue
        lines = [line for line in content.split("\n") if line.strip()]
        counts.append(min(len(lines), 8))
        if len(counts) >= SEED_FROM_HISTORY:
            break
    counts.reverse()
    return counts


def recent_counts(user_id: int, history: list[dict[str, Any]] | None = None) -> list[int]:
    """最近几次回复的文字条数（旧→新）。进程里没有记录时用历史估一下。"""

    shapes = _recent.get(int(user_id))
    if shapes:
        return [shape.parts for shape in shapes if shape.parts > 0]
    return _history_counts(history)


def mix_streak(counts: list[int]) -> str:
    """最近的条数是不是“套模板”了：``same``=连着几次条数一样（≥2 条）；
    ``always_split``=连着几次都分了条；都不是返回空串。"""

    if len(counts) >= SAME_COUNT_STREAK:
        tail = counts[-SAME_COUNT_STREAK:]
        if tail[0] >= 2 and len(set(tail)) == 1:
            return "same"
    if len(counts) >= SPLIT_STREAK and all(item >= 2 for item in counts[-SPLIT_STREAK:]):
        return "always_split"
    return ""


def mix_note(user_id: int, history: list[dict[str, Any]] | None = None) -> dict[str, Any] | None:
    """需要提醒时返回放进本轮资料的 ``recent_rhythm``；不需要返回 None。"""

    if not mix_hint_enabled():
        return None
    counts = recent_counts(user_id, history)
    streak = mix_streak(counts)
    if not streak:
        return None
    _stats.hints += 1
    return {"last_reply_parts": counts[-max(SAME_COUNT_STREAK, SPLIT_STREAK):], "pattern": streak}


# ── 打断 ───────────────────────────────────────────────────────────────────


def _arrivals(user_id: int) -> int:
    try:
        from ..chat_state import arrival_count

        return arrival_count(user_id)
    except Exception:  # noqa: BLE001
        return 0


Sleep = Callable[[float], Awaitable[Any]]


@dataclass(slots=True)
class InterruptWatch:
    """分条发送期间看他有没有发新消息。``since`` 是开始发送那一刻的到达计数。

    只看对话队列的到达计数（结构事实），不看内容；一旦触发就一直是触发状态。
    """

    user_id: int
    since: int
    enabled: bool = True
    fired: bool = False
    counter: Callable[[int], int] = _arrivals

    @classmethod
    def start(cls, user_id: int, *, enabled: bool | None = None, counter: Callable[[int], int] | None = None) -> InterruptWatch:
        read = counter or _arrivals
        return cls(
            int(user_id),
            read(int(user_id)),
            enabled=interrupt_enabled() if enabled is None else enabled,
            fired=False,
            counter=read,
        )

    def check(self) -> bool:
        if not self.enabled:
            return False
        if not self.fired:
            try:
                self.fired = self.counter(self.user_id) != self.since
            except Exception:  # noqa: BLE001
                dbg_exc("私聊打断检测失败(忽略)")
        return self.fired

    async def pause(self, seconds: float, sleep: Sleep) -> bool:
        """停 ``seconds`` 秒；中途他发了新消息就提前结束并返回 True。"""

        if self.check():
            return True
        if seconds <= 0:
            return False
        if not self.enabled:
            await sleep(seconds)
            return False
        left = float(seconds)
        while left > 1e-6:
            step = min(POLL_SECONDS, left)
            await sleep(step)
            left -= step
            if self.check():
                return True
        return False


@dataclass(frozen=True, slots=True)
class Cut:
    at: float
    sent_parts: int
    unsent_parts: int
    unsent_media: int = 0
    pipeline: str = "turn"


_cuts: dict[int, Cut] = {}


def note_cut(
    user_id: int,
    *,
    sent_parts: int,
    unsent_parts: int,
    unsent_media: int = 0,
    pipeline: str = "turn",
    now: float | None = None,
) -> None:
    """记下“上一条说到一半被打断了”（只记条数，不记没发出去的内容）。"""

    if unsent_parts <= 0 and unsent_media <= 0:
        return
    if len(_cuts) >= MAX_TRACKED_USERS:
        _cuts.pop(min(_cuts, key=lambda key: _cuts[key].at), None)
    _cuts[int(user_id)] = Cut(
        time.time() if now is None else now, sent_parts, unsent_parts, unsent_media, pipeline
    )


def cut_note(user_id: int, *, now: float | None = None) -> dict[str, Any] | None:
    """上一条被打断的记录（不取走），放进本轮资料的 ``last_reply_cut``；过期的顺手清掉。

    这一轮真的开口时再 ``clear_cut``：这一轮如果选择不说话/让给下一轮，下一轮还要知道。
    """

    cut = _cuts.get(int(user_id))
    if cut is None:
        return None
    moment = time.time() if now is None else now
    age = moment - cut.at
    if age < 0 or age > CUT_TTL_SECONDS:
        _cuts.pop(int(user_id), None)
        return None
    payload: dict[str, Any] = {
        "sent_parts": cut.sent_parts,
        "unsent_parts": cut.unsent_parts,
        "seconds_ago": round(age),
    }
    if cut.unsent_media:
        payload["unsent_stickers"] = cut.unsent_media
    if cut.pipeline != "turn":
        payload["was"] = {"report": "告诉他后台结果", "coordinate": "回小助手的话"}.get(cut.pipeline, cut.pipeline)
    return payload


def clear_cut(user_id: int) -> bool:
    """这一轮开口了：上一条被打断的记录用过了。返回之前有没有记录。"""

    had = _cuts.pop(int(user_id), None) is not None
    if had:
        _stats.cut_notes += 1
    return had


def take_cut(user_id: int, *, now: float | None = None) -> dict[str, Any] | None:
    """取走（只给一次）上一条被打断的记录。"""

    payload = cut_note(user_id, now=now)
    if payload is not None:
        clear_cut(user_id)
    return payload


def pending_cut(user_id: int) -> bool:
    return int(user_id) in _cuts


# ── WebUI / 测试 ───────────────────────────────────────────────────────────


def snapshot(user_id: int | None = None) -> dict[str, Any]:
    """WebUI 用：全站条数分布、打断次数；给了用户时附他最近几次的形状。"""

    data: dict[str, Any] = {
        "settings": {
            "interrupt": interrupt_enabled(),
            "keepMedia": keep_media_on_interrupt(),
            "mixHint": mix_hint_enabled(),
            "maxParts": private_max_parts(),
        },
        "stats": _stats.as_dict(),
        "trackedUsers": len(_recent),
    }
    if user_id is not None:
        shapes = list(_recent.get(int(user_id), ()))
        data["recent"] = [
            {
                "parts": shape.parts,
                "rhythm": RHYTHM_LABELS.get(shape.rhythm, shape.rhythm),
                "media": shape.media,
                "interrupted": shape.interrupted,
                "at": round(shape.at, 3),
            }
            for shape in shapes
        ]
        data["pattern"] = mix_streak([shape.parts for shape in shapes if shape.parts > 0])
        data["pendingCut"] = pending_cut(int(user_id))
    return data


def reset() -> None:
    global _stats  # noqa: PLW0603
    _recent.clear()
    _cuts.clear()
    _stats = _Stats()


__all__ = [
    "CUT_TTL_SECONDS",
    "DEFAULT_PRIVATE_MAX_PARTS",
    "POLL_SECONDS",
    "Cut",
    "InterruptWatch",
    "Shape",
    "clear_cut",
    "cut_note",
    "interrupt_enabled",
    "keep_media_on_interrupt",
    "mix_hint_enabled",
    "mix_note",
    "mix_streak",
    "note_cut",
    "note_delivery",
    "pending_cut",
    "private_max_parts",
    "private_rhythm_instruction",
    "private_rhythm_settings",
    "recent_counts",
    "reset",
    "snapshot",
    "take_cut",
]
