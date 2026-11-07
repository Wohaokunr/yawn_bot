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

**打断**：传入 ``private_rhythm.InterruptWatch`` 时，逐条发送（包括条与条之间的停顿）
期间他又发来新消息，剩下的文字和表情包就不发了，也不出现在返回的 ``delivered`` 里，
所以不会进历史；虚拟电脑里的图片/文件是他要的东西，默认照发。条数上限用私聊自己的
``PRIVATE_AGENT_REPLY_MAX_PARTS``（见 ``private_rhythm``）。
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
from .private_rhythm import (
    InterruptWatch,
    keep_media_on_interrupt,
    note_cut,
    note_delivery,
    private_rhythm_settings,
)
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
    return max(min(_daily_limit(user_id, settings.private_daily_limit) - used, settings.max_per_message), 0)


def _daily_limit(user_id: int, limit: int) -> int:
    """他私聊人设的表现力档位收窄每日额度（低档更少），不会超过后台设的上限。"""

    if limit <= 0:
        return limit
    try:
        from .private_persona import sticker_scale

        scale = sticker_scale(user_id)
    except Exception:  # noqa: BLE001
        return limit
    return min(limit, max(1, round(limit * scale)))


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
        markup.text,
        parts=markup.parts,
        declared=markup.rhythm,
        scene="private",
        settings=private_rhythm_settings(),
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
        # 先收拢说明文字，为产物保留发送位置；不能把文件静默截掉。
        texts = [item.value for item in items if item.kind == "text"]
        media = [item for item in items if item.kind != "text"]
        if len(media) + bool(texts) > MAX_MESSAGES:
            kept = media[: MAX_MESSAGES - 1]
            omitted = media[MAX_MESSAGES - 1 :]
            texts.append(
                f"（本次消息条数已到上限，还有 {len(omitted)} 个附件或表情未发送；"
                "请让我分批发送剩余附件。）"
            )
            media = kept
        items = ([OutItem("text", "\n".join(texts))] if texts else []) + media
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
    # 分条发到一半他插话：后面没发的条数（文字/表情包）。没发的内容不在 delivered 里。
    interrupted: bool = False
    skipped: int = 0
    skipped_stickers: int = 0
    # 实际送达的文字条数与本次节奏（统计与 Trace 用）
    text_parts: int = 0
    rhythm: str = ""
    planned: int = 0

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
    interrupt: InterruptWatch | None = None,
    pipeline: str = "turn",
) -> Delivery:
    """规划并依次发送；某条发送失败就停（后面的不发），返回已送达的部分。

    ``interrupt`` 触发后（他发了新消息）剩下的文字/表情包不再发送，图片/文件按
    ``PRIVATE_AGENT_INTERRUPT_KEEP_MEDIA`` 决定是否照发；第一条永远会发。
    """

    from .private_computer import read_for_send

    plan, items = plan_private_reply(text)
    result = Delivery([], rhythm=plan.rhythm, planned=len(items))
    budget = [private_sticker_left(user_id, today) if today else 0]
    keep_media = keep_media_on_interrupt()
    for index, item in enumerate(items):
        if index and interrupt is not None and not result.interrupted:
            if await interrupt.pause(item.delay, sleep):
                result.interrupted = True
                dbg(
                    f"私聊用户 {user_id} 分条发到第 {len(result.delivered)} 条时发来新消息，"
                    "剩下的不发了"
                )
            delay = 0.0
        else:
            delay = item.delay
        if result.interrupted and not (keep_media and item.kind in {"image", "file"}):
            result.skipped += 1
            if item.kind == "sticker":
                result.skipped_stickers += 1
            continue
        if result.interrupted:
            delay = min(delay, MEDIA_PAUSE_SECONDS)
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
                if delay:
                    await sleep(delay)
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
        if delay:
            await sleep(delay)
        try:
            await send(message)
        except Exception:  # noqa: BLE001
            dbg_exc(
                f"私聊用户 {user_id} 第 {len(result.delivered) + 1} 条发送失败，停止后续"
            )
            break
        result.delivered.append(record)
        if item.kind == "text":
            result.text_parts += 1
        elif item.kind == "sticker":
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
    _after_delivery(user_id, result, pipeline)
    return result


def _after_delivery(user_id: int, result: Delivery, pipeline: str) -> None:
    """记条数分布；被打断时记下“说到一半”（只记条数）并写 Trace。都不影响发送结果。"""

    try:
        note_delivery(
            user_id,
            parts=result.text_parts,
            rhythm=result.rhythm,
            media=result.stickers + result.images + result.files,
            interrupted=result.interrupted,
            skipped=result.skipped,
        )
        if result.interrupted and result.skipped:
            note_cut(
                user_id,
                sent_parts=len(result.delivered),
                unsent_parts=result.skipped - result.skipped_stickers,
                unsent_media=result.skipped_stickers,
                pipeline=pipeline,
            )
    except Exception:  # noqa: BLE001
        dbg_exc("私聊分条统计失败(忽略)")
    try:
        from .execution_trace import trace_event

        trace_event(
            "decision",
            "私聊分条发送",
            status="success",
            output={
                "user_id": user_id,
                "pipeline": pipeline,
                "rhythm": result.rhythm,
                "planned": result.planned,
                "sent": len(result.delivered),
                "text_parts": result.text_parts,
                "interrupted": result.interrupted,
                "skipped": result.skipped,
            },
        )
    except Exception:  # noqa: BLE001
        dbg_exc("私聊分条 Trace 记录失败(忽略)")


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
