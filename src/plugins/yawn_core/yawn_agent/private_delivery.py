# ruff: noqa: E501, C901, PLR0912, PLR0913, PLR0915, PLR2004, PERF401, PLW2901, ARG001, TRY301
"""私聊回复的发送：节奏分条 + 表情包 + 虚拟电脑里的图片/文件。

与群聊同一套发言节奏（``speech_rhythm``）：模型开头写【节奏:整段/分条/连发/蹦字】，
用 ``|||`` 分条；这里只解析标记、按节奏算停顿，不按关键词猜情绪。

正文里还可以写三种标记，每个都单独成一条 QQ 消息：

- ``[表情:名字]``：表情包，只从“所有群可用”的库里挑（``available_stickers(None)``），
  受 ``StickerSettings.private_*`` 的开关、每人每日上限和冷却约束；对不上就去掉；
- ``[图片:sandbox:路径]``：他虚拟电脑里的图片，以 base64 发出；
- ``[文件:sandbox:路径]``：他虚拟电脑里的其他文件，``upload_private_file`` 上传。

发不出去的媒体退回成一句文字说明（“图表没发出去：…”），不让整轮沉默，也不假装发了。
返回实际送达部分的文字（媒体写成 ``[图片]`` 之类的占位），由调用方写进历史。
"""

from __future__ import annotations

import asyncio
import base64
import re
import time
from dataclasses import dataclass
from typing import Any

from nonebot.adapters.onebot.v11 import Message, MessageSegment

from . import private_store as store
from .log import dbg, dbg_exc
from .speech_format import normalize_qq_text
from .speech_rhythm import RhythmPlan, parse_rhythm_markup, plan_text_rhythm
from .sticker_library import (
    available_stickers,
    match_stickers,
    sticker_path,
    tag_counts,
)
from .sticker_markup import _MARKER_RE as _STICKER_RE
from .sticker_markup import sticker_instruction
from .sticker_policy import load_settings as load_sticker_settings
from .sticker_policy import menu_entries, pick_sticker

MAX_MESSAGES = 6
MEDIA_PAUSE_SECONDS = 0.6
MAX_IMAGE_BYTES = 10 * 1024 * 1024
MAX_FILE_BYTES = 30 * 1024 * 1024
# 私聊表情包的发送记录与“最近发过”共用群聊的统计，用 0 作为私聊的伪群号。
PRIVATE_STICKER_SCOPE = 0
_MEDIA_RE = re.compile(
    r"[\[【]\s*(图片|image|文件|file)\s*[:：]\s*(sandbox:[^\]】\s]{1,300})\s*[\]】]",
    re.IGNORECASE,
)
_KIND = {"图片": "image", "image": "image", "文件": "file", "file": "file"}


@dataclass(frozen=True, slots=True)
class OutItem:
    kind: str  # text | sticker | image | file
    value: str
    delay: float = 0.0


# ── 表情包闸门 ─────────────────────────────────────────────────────────────


def _sticker_usage(record: dict[str, Any], today: str) -> tuple[int, float]:
    flags = record.get("flags") or {}
    used = (
        int(flags.get("sticker_used") or 0) if flags.get("sticker_day") == today else 0
    )
    return used, float(flags.get("sticker_last") or 0.0)


def private_sticker_left(
    user_id: int, today: str, *, now_ts: float | None = None
) -> int:
    """这位用户此刻还能收几张表情包（关了/冷却中返回 0）。"""

    settings = load_sticker_settings()
    if not settings.enabled or not settings.private_enabled:
        return 0
    used, last = _sticker_usage(store.load(user_id), today)
    now_ts = time.time() if now_ts is None else now_ts
    if (
        settings.private_cooldown_seconds
        and now_ts - last < settings.private_cooldown_seconds
    ):
        return 0
    return max(min(settings.private_daily_limit - used, settings.max_per_message), 0)


def _note_sticker_sent(user_id: int, today: str, count: int) -> None:
    def change(record: dict[str, Any]) -> None:
        flags = record["flags"]
        if flags.get("sticker_day") != today:
            flags["sticker_day"] = today
            flags["sticker_used"] = 0
        flags["sticker_used"] = int(flags.get("sticker_used") or 0) + count
        flags["sticker_last"] = time.time()

    store.update(user_id, change)


def private_sticker_instruction(user_id: int, today: str, *, allow: bool = True) -> str:
    """本轮表情包菜单与用法；关了、额度用完、冷却中或库里没有通用表情时返回空串。"""

    if not allow or private_sticker_left(user_id, today) <= 0:
        return ""
    stickers = tuple(available_stickers(None))
    if not stickers:
        return ""
    settings = load_sticker_settings()
    menu = menu_entries(
        PRIVATE_STICKER_SCOPE, stickers, size=settings.menu_size, seed=str(user_id)
    )
    tags = [
        (tag, count)
        for tag, count in tag_counts(PRIVATE_STICKER_SCOPE, stickers=stickers)
        if count >= 2
    ][:12]
    return sticker_instruction(
        menu,
        tags,
        frequency=settings.frequency,
        max_per_message=settings.max_per_message,
    )


# ── 规划 ───────────────────────────────────────────────────────────────────


def _clean(text: str) -> str:
    return normalize_qq_text(text).text.strip()


def _tokens(part: str) -> list[tuple[str, str]]:
    """一条里的文字、表情包标记、媒体标记，按出现顺序。代码块原样当文字。"""

    if "```" in part:
        return [("text", part)]
    spans: list[tuple[int, int, str, str]] = []
    for match in _STICKER_RE.finditer(part):
        spans.append((match.start(), match.end(), "sticker", match.group(1).strip()))
    for match in _MEDIA_RE.finditer(part):
        spans.append(
            (match.start(), match.end(), _KIND[match.group(1).lower()], match.group(2))
        )
    spans.sort()
    tokens: list[tuple[str, str]] = []
    cursor = 0
    for start, end, kind, value in spans:
        if start < cursor:
            continue
        if start > cursor:
            tokens.append(("text", part[cursor:start]))
        tokens.append((kind, value))
        cursor = end
    if cursor < len(part):
        tokens.append(("text", part[cursor:]))
    return tokens


def plan_private_reply(text: str) -> tuple[RhythmPlan, list[OutItem]]:
    """模型最终正文 → 要依次发的消息（文字按节奏分条，媒体各自一条）。"""

    markup = parse_rhythm_markup(text)
    plan = plan_text_rhythm(
        markup.text, parts=markup.parts, declared=markup.rhythm, scene="private"
    )
    items: list[OutItem] = []
    for part, delay in zip(plan.parts, plan.delays, strict=False):
        first = True
        for kind, value in _tokens(part):
            if kind == "text":
                cleaned = _clean(value)
                if not cleaned:
                    continue
                value = cleaned
            pause = delay if first else MEDIA_PAUSE_SECONDS
            items.append(OutItem(kind, value, pause if items else 0.0))
            first = False
    if len(items) > MAX_MESSAGES:
        # 太多条：多出来的文字并进最后一条文字，媒体保留前几个。
        head = items[: MAX_MESSAGES - 1]
        tail = [item for item in items[MAX_MESSAGES - 1 :] if item.kind == "text"]
        if tail:
            head.append(
                OutItem("text", "\n".join(item.value for item in tail), tail[0].delay)
            )
        items = head
    return plan, items


def plain_text(text: str) -> str:
    """不发送时（例如历史转述）把标记换成可读占位。"""

    _plan, items = plan_private_reply(text)
    return "\n".join(_placeholder(item) for item in items)


def _placeholder(item: OutItem) -> str:
    if item.kind == "text":
        return item.value
    if item.kind == "sticker":
        return f"[表情包: {item.value}]"
    if item.kind == "image":
        return "[图片]"
    return f"[文件: {item.value.rsplit('/', 1)[-1]}]"


# ── 发送 ───────────────────────────────────────────────────────────────────


@dataclass(slots=True)
class Delivery:
    delivered: list[str]
    stickers: int = 0
    images: int = 0
    files: int = 0
    dropped: int = 0

    @property
    def text(self) -> str:
        return "\n".join(self.delivered)


Sender = Any  # async (Message) -> None


async def _sticker_segment(
    user_id: int, name: str, budget: list[int]
) -> MessageSegment | None:
    if budget[0] <= 0:
        return None
    stickers = available_stickers(None)
    _how, candidates = match_stickers(name, None, stickers=stickers)
    picked = pick_sticker(PRIVATE_STICKER_SCOPE, candidates) if candidates else None
    if picked is None:
        return None
    try:
        data = sticker_path(picked).read_bytes()
    except Exception:  # noqa: BLE001
        return None
    budget[0] -= 1
    image = MessageSegment.image(data)
    image.data["sub_type"] = 1
    image.data["summary"] = f"[{picked.name}]"
    return image


async def deliver(
    user_id: int,
    text: str,
    send: Sender,
    *,
    upload_file: Any = None,
    today: str = "",
    sleep: Any = asyncio.sleep,
) -> Delivery:
    """规划并依次发送；某条发送失败就停（后面的不发），返回已送达的部分。"""

    from .private_computer import read_for_send

    _plan, items = plan_private_reply(text)
    result = Delivery([])
    budget = [private_sticker_left(user_id, today) if today else 0]
    for item in items:
        message: Message | None = None
        record = _placeholder(item)
        note: str | None = None
        if item.kind == "text":
            message = Message(MessageSegment.text(item.value))
        elif item.kind == "sticker":
            segment = await _sticker_segment(user_id, item.value, budget)
            if segment is None:
                result.dropped += 1
                continue
            message = Message(segment)
            record = f"[表情包: {item.value}]"
        elif item.kind == "image":
            try:
                data, kind, _name = await read_for_send(
                    user_id, item.value, max_bytes=MAX_IMAGE_BYTES
                )
                if kind != "image":
                    raise ValueError("不是图片")
                message = Message(MessageSegment.image(data))
            except Exception as exc:  # noqa: BLE001
                note = f"（图片没发出去：{_reason(exc)}）"
        else:
            try:
                data, _kind, name = await read_for_send(
                    user_id, item.value, max_bytes=MAX_FILE_BYTES
                )
                if upload_file is None:
                    raise RuntimeError("当前协议端不支持私聊传文件")
                if item.delay:
                    await sleep(item.delay)
                await upload_file(name, data)
                result.files += 1
                result.delivered.append(f"[文件: {name}]")
                continue
            except Exception as exc:  # noqa: BLE001
                note = f"（文件没发出去：{_reason(exc)}）"
        if note is not None:
            message = Message(MessageSegment.text(note))
            record = note
            result.dropped += 1
        if item.delay:
            await sleep(item.delay)
        try:
            await send(message)
        except Exception:  # noqa: BLE001
            dbg_exc(
                f"私聊用户 {user_id} 第 {len(result.delivered) + 1} 条发送失败，停止后续"
            )
            break
        result.delivered.append(record)
        if item.kind == "sticker":
            result.stickers += 1
        elif item.kind == "image" and note is None:
            result.images += 1
    if result.stickers and today:
        try:
            _note_sticker_sent(user_id, today, result.stickers)
        except Exception:  # noqa: BLE001
            dbg_exc("私聊表情包计数失败(忽略)")
    if result.dropped:
        dbg(f"私聊用户 {user_id} 回复里有 {result.dropped} 个媒体没发出去")
    return result


def _reason(exc: BaseException) -> str:
    text = str(exc).strip() or type(exc).__name__
    return text[:40]


def file_uploader(bot: Any, user_id: int) -> Any:
    """``upload_private_file``（NapCat/LLOneBot 支持），base64 交给协议端。"""

    async def upload(name: str, data: bytes) -> None:
        await bot.call_api(
            "upload_private_file",
            user_id=user_id,
            file="base64://" + base64.b64encode(data).decode("ascii"),
            name=name,
        )

    return upload


__all__ = [
    "Delivery",
    "OutItem",
    "deliver",
    "file_uploader",
    "plain_text",
    "plan_private_reply",
    "private_sticker_instruction",
    "private_sticker_left",
]
