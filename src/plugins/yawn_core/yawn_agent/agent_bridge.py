# ruff: noqa: E501, TID252, TRY003, PLR0911, PLR0913, PLR2004, DTZ007, PERF401, FBT003
"""群聊 Agent ↔ 私聊 Agent 协作：群里和私聊里是同一个“我”，事情可以在两边接力。

三条线：

1. **群里转私聊**（handoff）：群聊工具 ``move_to_private``。群里答应“私聊你”的事，
   这里记一笔，等群里那句发出去后起一轮私聊开场回合（``private_bridge_turn``），
   私聊 Agent 带着全部私聊能力（后台任务、虚拟电脑、个人助理）接着办。
2. **私聊转群里**（relay）：私聊工具 ``group_bridge`` 的 post/ask。他让我在群里转告
   一句话或问大家一个问题；确认后以“〔某某 让我转告〕”发到群里（落库成 Bot 发言，
   群里的我也看得到）。ask 会收集**引用了那条提问**的回复，攒一会儿合成一条私聊转给他。
3. **群里给私聊小助手记事**（assist）：群聊工具 ``add_to_my_assistant``。群里说
   “提醒我明天交作业”时直接记进他的私聊提醒/待办/笔记，到点私聊提醒。

隐私边界（硬规则）：

- 私聊内容不进群聊提示词。群里只看得到他本人明确让转告的话（本来就要发到群里）和
  从这个群转出去的事的话题（本来就来自这个群）。
- 群→私聊只带他本人在群里的原话和群里的我写的话题，不带别人的消息。
- 回复只收引用了那条提问的；已退出 Agent 记忆的成员在群消息入口就被跳过了。
- 每次写到群里都要他确认（他发来下一条消息之后才能确认，和群管代办一致）；
  群主可以在群管理策略里关掉本群的私聊转告（``allow_relay``）。
- 只对好友私聊（临时会话发不出去，也不该从群里主动找陌生人）。

前缀缓存：私聊侧的状态只进本轮资料（system#3）；群侧只是一个可发现的工具族，
静态前缀不变。

存储：每人私聊数据 JSON 的 ``bridge`` 键（handoffs / relays），回复索引
``data/agent_bridge/index.json``（群号:消息号 → 用户、提问编号）。无迁移。
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import secrets
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from ..private_chat_bridge import private_agent_config
from ..superuser import bypasses_group_policy, skips_daily_limits
from .context import now_beijing
from .log import dbg, dbg_exc
from .memory_hygiene import looks_like_instruction

TIME_FORMAT = "%Y-%m-%d %H:%M:%S"
MAX_HANDOFFS = 20
MAX_RELAYS = 20
TOPIC_MAX_CHARS = 300
ASKED_MAX_CHARS = 300
RELAY_MAX_CHARS = 300
REPLY_MAX_CHARS = 300
HANDOFF_DEDUPE_MINUTES = 10
ACTIVE_HANDOFF_HOURS = 24
FRIEND_CACHE_SECONDS = 300.0
MAX_PENDING_PER_USER = 3
HANDOFF_REASONS = ("asked", "long_task", "personal", "files", "followup", "busy_group")
REASON_LABELS = {
    "asked": "他说私聊",
    "long_task": "费时间的活",
    "personal": "私人的事",
    "files": "要发文件/长内容",
    "followup": "后续跟进",
    "busy_group": "群里太吵",
}
RELAY_PREFIX = {"post": "〔{name} 让我转告〕", "ask": "〔{name} 想问问大家〕"}
ASK_SUFFIX = "（引用回复这条，我会转给 ta）"


class BridgeError(Exception):
    """协作失败：带错误码和给模型看的建议。"""

    def __init__(self, message: str, *, code: str = "invalid_args", hint: str | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.hint = hint


def enabled() -> bool:
    return bool(getattr(private_agent_config, "agent_bridge_enabled", True)) and bool(
        getattr(private_agent_config, "private_agent_enabled", True)
    )


def _cfg(name: str, default: Any) -> Any:
    return getattr(private_agent_config, name, default)


def fmt(moment: datetime) -> str:
    return moment.strftime(TIME_FORMAT)


def parse(raw: Any) -> datetime | None:
    try:
        return datetime.strptime(str(raw), TIME_FORMAT)
    except (TypeError, ValueError):
        return None


def _new_id(prefix: str, existing: set[str]) -> str:
    while True:
        value = prefix + secrets.token_hex(2)
        if value not in existing:
            return value


_CQ_RE = re.compile(r"\[CQ:[^\]]*\]", re.IGNORECASE)
_MEANINGFUL_RE = re.compile(r"[\w一-鿿]")
_AT_ALL_RE = re.compile(r"@全体成员|@all\b", re.IGNORECASE)


def clean(raw: Any, *, limit: int) -> str:
    text = _CQ_RE.sub("", str(raw or "")).replace("\r\n", "\n").strip()
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text[:limit].strip()


# ── 存储（私聊个人数据的 bridge 键）──────────────────────────────────────


def _bridge(record: dict[str, Any]) -> dict[str, Any]:
    data = record.setdefault("bridge", {})
    if not isinstance(data, dict):
        data = {}
        record["bridge"] = data
    for key in ("handoffs", "relays"):
        if not isinstance(data.get(key), list):
            data[key] = []
    return data


def _load_bridge(user_id: int) -> dict[str, Any]:
    from . import private_store

    return _bridge(private_store.load(user_id))


def _update(user_id: int, fn: Any) -> Any:
    from . import private_store

    return private_store.update(user_id, lambda record: fn(_bridge(record)))


def _today_count(items: list[dict[str, Any]], now: datetime, *, key: str = "created") -> int:
    today = now.strftime("%Y-%m-%d")
    return sum(1 for item in items if str(item.get(key, "")).startswith(today))


# ── 1. 群里转私聊 ───────────────────────────────────────────────────────


def create_handoff(
    user_id: int,
    *,
    group_id: int,
    group_name: str,
    topic: str,
    asked: str,
    reason: str = "asked",
    message_id: int | None = None,
    now: datetime | None = None,
) -> tuple[dict[str, Any], bool]:
    """记一件从群里转到私聊的事；返回（记录, 是否新建）。10 分钟内同群同话题的算同一件。"""

    now = now or now_beijing()
    topic = clean(topic, limit=TOPIC_MAX_CHARS)
    if not _MEANINGFUL_RE.search(topic):
        raise BridgeError("没说要私聊接着办什么", hint="topic 写清楚他要的事（用他的说法）")
    if looks_like_instruction(topic):
        raise BridgeError("话题像是给 AI 的指令，没有转", hint="用你自己的话概括他要的事")
    reason = reason if reason in HANDOFF_REASONS else "asked"
    asked = clean(asked, limit=ASKED_MAX_CHARS)
    # 超级管理员不计每日转私聊次数
    cap = 0 if skips_daily_limits(user_id) else int(_cfg("agent_bridge_handoff_daily", 10))

    def apply(data: dict[str, Any]) -> tuple[dict[str, Any], bool]:
        items: list[dict[str, Any]] = data["handoffs"]
        since = now - timedelta(minutes=HANDOFF_DEDUPE_MINUTES)
        for item in reversed(items):
            created = parse(item.get("created"))
            if (
                created is not None
                and created >= since
                and int(item.get("group_id") or 0) == int(group_id)
                and item.get("status") in {"opening", "opened"}
                and (item.get("topic") == topic or (message_id and item.get("message_id") == message_id))
            ):
                return dict(item), False
        if cap and _today_count(items, now) >= cap:
            raise BridgeError(
                f"今天已经从群里转到私聊 {cap} 次了",
                code="quota",
                hint="在群里简单说就好，或者让他直接私聊你",
            )
        item = {
            "id": _new_id("h", {str(row.get("id")) for row in items}),
            "group_id": int(group_id),
            "group_name": str(group_name or group_id)[:60],
            "topic": topic,
            "asked": asked,
            "reason": reason,
            "message_id": int(message_id) if message_id else None,
            "created": fmt(now),
            "status": "opening",
        }
        items.append(item)
        del items[:-MAX_HANDOFFS]
        return dict(item), True

    return _update(user_id, apply)


def update_handoff(user_id: int, handoff_id: str, **changes: Any) -> dict[str, Any] | None:
    def apply(data: dict[str, Any]) -> dict[str, Any] | None:
        for item in data["handoffs"]:
            if str(item.get("id")) == str(handoff_id):
                item.update(changes)
                return dict(item)
        return None

    return _update(user_id, apply)


def handoffs(user_id: int) -> list[dict[str, Any]]:
    return [dict(item) for item in _load_bridge(user_id)["handoffs"]]


def active_handoffs(user_id: int, now: datetime | None = None) -> list[dict[str, Any]]:
    """最近 24 小时从群里转来、还没办完的事（新的在前）。"""

    now = now or now_beijing()
    since = now - timedelta(hours=ACTIVE_HANDOFF_HOURS)
    rows = []
    for item in handoffs(user_id):
        created = parse(item.get("created"))
        if created is None or created < since:
            continue
        if item.get("status") in {"opening", "opened"}:
            rows.append(item)
    return list(reversed(rows))


# ── 2. 私聊转群里 ───────────────────────────────────────────────────────

_group_sent: dict[int, list[float]] = {}
_group_lock = threading.Lock()


def _group_recent(group_id: int) -> int:
    cutoff = time.monotonic() - 3600
    with _group_lock:
        stamps = [stamp for stamp in _group_sent.get(int(group_id), []) if stamp > cutoff]
        _group_sent[int(group_id)] = stamps
        return len(stamps)


def _note_group_sent(group_id: int) -> None:
    with _group_lock:
        _group_sent.setdefault(int(group_id), []).append(time.monotonic())


def check_relay_quota(user_id: int, group_id: int, now: datetime | None = None) -> None:
    now = now or now_beijing()
    if skips_daily_limits(user_id):
        # 超级管理员不计每日/每群每小时转告额度（仍要他确认每一句）
        return
    daily = int(_cfg("agent_bridge_relay_daily", 10))
    if daily and _today_count(_load_bridge(user_id)["relays"], now) >= daily:
        raise BridgeError(
            f"今天已经替他在群里说了 {daily} 次",
            code="quota",
            hint="照实告诉他今天的次数用完了，可以自己去群里说",
        )
    hourly = int(_cfg("agent_bridge_relay_group_hourly", 6))
    if hourly and _group_recent(group_id) >= hourly:
        raise BridgeError(
            "这个群这一小时替人转告的消息太多了",
            code="quota",
            hint="过一会儿再发，或者让他自己去群里说",
        )


def relay_text(kind: str, name: str, text: str) -> str:
    prefix = RELAY_PREFIX.get(kind, RELAY_PREFIX["post"]).format(name=(name or "有人")[:20])
    body = prefix + text
    if kind == "ask":
        body += ASK_SUFFIX
    return body


def normalize_relay(kind: str, raw: Any) -> str:
    if kind not in RELAY_PREFIX:
        raise BridgeError("没有这种转告方式", hint="post 转告一句，ask 问大家并收回复")
    text = clean(raw, limit=RELAY_MAX_CHARS + 1)
    if not _MEANINGFUL_RE.search(text):
        raise BridgeError("要转的话是空的", hint="先问清楚他要在群里说什么")
    if len(text) > RELAY_MAX_CHARS:
        raise BridgeError(f"要转的话太长了（最多 {RELAY_MAX_CHARS} 字）", hint="帮他精简一下，问他行不行")
    if _AT_ALL_RE.search(text):
        raise BridgeError("不能替私聊的人 @全体成员", code="permission", hint="需要的话请群主/管理员发公告")
    if looks_like_instruction(text):
        raise BridgeError("内容像是给 AI 的指令，没有转", hint="换个说法，用他的原意写")
    return text


def record_relay(
    user_id: int,
    *,
    group_id: int,
    group_name: str,
    kind: str,
    text: str,
    message_id: int | None,
    now: datetime | None = None,
    handoff_id: str | None = None,
) -> dict[str, Any]:
    now = now or now_beijing()
    hours = int(_cfg("agent_bridge_ask_hours", 6))

    def apply(data: dict[str, Any]) -> dict[str, Any]:
        items: list[dict[str, Any]] = data["relays"]
        item: dict[str, Any] = {
            "id": _new_id("r", {str(row.get("id")) for row in items}),
            "group_id": int(group_id),
            "group_name": str(group_name or group_id)[:60],
            "kind": kind,
            "text": text,
            "message_id": int(message_id) if message_id else None,
            "created": fmt(now),
            "status": "open" if kind == "ask" and message_id else "sent",
            "replies": [],
        }
        if kind == "ask":
            item["until"] = fmt(now + timedelta(hours=hours))
        if handoff_id:
            item["handoff_id"] = handoff_id
        items.append(item)
        del items[:-MAX_RELAYS]
        return dict(item)

    item = _update(user_id, apply)
    _note_group_sent(group_id)
    if item["status"] == "open":
        _index_put(int(group_id), int(item["message_id"]), int(user_id), str(item["id"]), str(item["until"]))
    return item


def relays(user_id: int) -> list[dict[str, Any]]:
    return [dict(item) for item in _load_bridge(user_id)["relays"]]


def close_relay(user_id: int, relay_id: str | None = None) -> int:
    def apply(data: dict[str, Any]) -> list[dict[str, Any]]:
        closed = []
        for item in data["relays"]:
            if item.get("status") != "open":
                continue
            if relay_id and str(item.get("id")) != str(relay_id):
                continue
            item["status"] = "closed"
            closed.append(dict(item))
        return closed

    closed = _update(user_id, apply)
    for item in closed:
        if item.get("message_id"):
            _index_drop(int(item["group_id"]), int(item["message_id"]))
    return len(closed)


def open_asks(user_id: int, now: datetime | None = None) -> list[dict[str, Any]]:
    """还在收回复的提问（过期的顺手关掉）。"""

    now = now or now_beijing()
    rows = []
    expired = []
    for item in relays(user_id):
        if item.get("status") != "open":
            continue
        until = parse(item.get("until"))
        if until is not None and until <= now:
            expired.append(str(item["id"]))
            continue
        rows.append(item)
    for relay_id in expired:
        close_relay(user_id, relay_id)
    return list(reversed(rows))


# ── 回复索引（群号:消息号 → 谁的哪个提问）──────────────────────────────

_index_lock = threading.RLock()
_index_cache: dict[str, Any] | None = None


def data_dir() -> Path:
    raw = os.environ.get("AGENT_BRIDGE_DATA_DIR", "").strip()
    return Path(raw) if raw else Path("data") / "agent_bridge"


def _index_path() -> Path:
    return data_dir() / "index.json"


def _index_load() -> dict[str, Any]:
    global _index_cache  # noqa: PLW0603
    with _index_lock:
        if _index_cache is not None:
            return _index_cache
        try:
            raw = json.loads(_index_path().read_text(encoding="utf-8"))
        except (OSError, ValueError):
            raw = {}
        _index_cache = raw if isinstance(raw, dict) else {}
        return _index_cache


def _index_save(index: dict[str, Any]) -> None:
    path = _index_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(index, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
        tmp.replace(path)
    except OSError:
        dbg_exc("协作回复索引写入失败(忽略，重启前仍在内存里)")


def _index_key(group_id: int, message_id: int) -> str:
    return f"{int(group_id)}:{int(message_id)}"


def _index_put(group_id: int, message_id: int, user_id: int, relay_id: str, until: str) -> None:
    with _index_lock:
        index = _index_load()
        now = fmt(now_beijing())
        for key in [key for key, row in index.items() if str(row.get("until", "")) <= now]:
            index.pop(key, None)
        index[_index_key(group_id, message_id)] = {"user_id": int(user_id), "relay_id": relay_id, "until": until}
        _index_save(index)


def _index_drop(group_id: int, message_id: int) -> None:
    with _index_lock:
        index = _index_load()
        if index.pop(_index_key(group_id, message_id), None) is not None:
            _index_save(index)


def reply_target(group_id: int, message_id: int, now: datetime | None = None) -> tuple[int, str] | None:
    """这条群消息引用的是不是某个私聊提问；是就返回（提问人, 提问编号）。"""

    now = now or now_beijing()
    with _index_lock:
        row = _index_load().get(_index_key(group_id, message_id))
    if not isinstance(row, dict):
        return None
    if str(row.get("until", "")) <= fmt(now):
        _index_drop(group_id, message_id)
        return None
    return int(row["user_id"]), str(row["relay_id"])


def add_reply(
    user_id: int,
    relay_id: str,
    *,
    from_id: int,
    from_name: str,
    text: str,
    message_id: int | None,
    now: datetime | None = None,
) -> dict[str, Any] | None:
    """记一条群友回复；同一条消息只记一次，超过上限后不再收（并关掉提问）。"""

    now = now or now_beijing()
    text = clean(text, limit=REPLY_MAX_CHARS)
    if not _MEANINGFUL_RE.search(text):
        return None
    limit = int(_cfg("agent_bridge_reply_limit", 20))

    def apply(data: dict[str, Any]) -> tuple[dict[str, Any] | None, bool]:
        for item in data["relays"]:
            if str(item.get("id")) != str(relay_id) or item.get("status") != "open":
                continue
            replies = item.setdefault("replies", [])
            if message_id and any(row.get("message_id") == message_id for row in replies):
                return None, False
            reply = {
                "from_id": int(from_id),
                "from_name": str(from_name or from_id)[:30],
                "text": text,
                "message_id": message_id,
                "at": fmt(now),
                "delivered": False,
            }
            replies.append(reply)
            full = len(replies) >= limit
            if full:
                item["status"] = "closed"
            return dict(reply), full
        return None, False

    reply, full = _update(user_id, apply)
    if full:
        for item in relays(user_id):
            if str(item.get("id")) == str(relay_id) and item.get("message_id"):
                _index_drop(int(item["group_id"]), int(item["message_id"]))
    return reply


def take_undelivered(user_id: int) -> list[dict[str, Any]]:
    """取走还没转给他的回复（按提问分组），并标记已转。"""

    def apply(data: dict[str, Any]) -> list[dict[str, Any]]:
        out = []
        for item in data["relays"]:
            fresh = [row for row in item.get("replies") or [] if not row.get("delivered")]
            if not fresh:
                continue
            for row in fresh:
                row["delivered"] = True
            out.append(
                {
                    "id": item.get("id"),
                    "group_name": item.get("group_name"),
                    "text": item.get("text"),
                    "replies": [dict(row) for row in fresh],
                }
            )
        return out

    return _update(user_id, apply)


def format_replies(batches: list[dict[str, Any]]) -> str:
    """转给他的那条私聊（模板，不过模型：群友原话不能被改写）。"""

    lines: list[str] = []
    for batch in batches:
        question = str(batch.get("text") or "")
        question = question if len(question) <= 24 else question[:24] + "…"
        lines.append(f"「{batch.get('group_name')}」里有人回你问的「{question}」：")
        for row in batch.get("replies") or []:
            lines.append(f"{row.get('from_name')}：{row.get('text')}")
    return "\n".join(lines)


_delivery_tasks: dict[int, asyncio.Task[None]] = {}


def schedule_reply_delivery(bot: Any, user_id: int) -> None:
    """攒一会儿再转：几个人前后脚回复时合成一条私聊。"""

    existing = _delivery_tasks.get(int(user_id))
    if existing is not None and not existing.done():
        return
    delay = float(_cfg("agent_bridge_reply_batch_seconds", 20.0))

    async def run() -> None:
        try:
            if delay > 0:
                await asyncio.sleep(delay)
            await deliver_replies(bot, user_id)
        except Exception:  # noqa: BLE001
            dbg_exc(f"私聊用户 {user_id} 转群回复失败(忽略)")
        finally:
            _delivery_tasks.pop(int(user_id), None)

    _delivery_tasks[int(user_id)] = asyncio.create_task(run())


async def deliver_replies(bot: Any, user_id: int) -> str:
    batches = take_undelivered(user_id)
    if not batches:
        return ""
    text = format_replies(batches)
    await bot.send_private_msg(user_id=int(user_id), message=text)
    try:
        from .private_scheduler import record_outgoing

        await record_outgoing(int(user_id), text, now_beijing())
    except Exception:  # noqa: BLE001
        dbg_exc(f"私聊用户 {user_id} 群回复入库失败(忽略)")
    dbg(f"私聊用户 {user_id} 转去 {sum(len(b['replies']) for b in batches)} 条群回复")
    return text


async def capture_group_reply(bot: Any, event: Any, normalized: Any) -> bool:
    """群消息入口：引用了某个私聊提问的回复，记下来转给提问人；返回 True 表示这条不再交给群聊回合。"""

    if not enabled():
        return False
    reply = getattr(event, "reply", None)
    if reply is None:
        return False
    try:
        replied_id = int(getattr(reply, "message_id", 0) or 0)
        group_id = int(event.group_id)
        author = int(event.get_user_id())
    except (TypeError, ValueError):
        return False
    if not replied_id:
        return False
    target = reply_target(group_id, replied_id)
    if target is None:
        return False
    user_id, relay_id = target
    if author == user_id:
        # 提问人自己在群里补充：交给群聊照常处理。
        return False
    sender = getattr(event, "sender", None)
    name = str(getattr(sender, "card", "") or getattr(sender, "nickname", "") or author)
    text = str(getattr(normalized, "plain_text", "") or "")
    stored = add_reply(
        user_id,
        relay_id,
        from_id=author,
        from_name=name,
        text=text,
        message_id=int(getattr(event, "message_id", 0) or 0) or None,
    )
    if stored is None:
        return False
    dbg(f"群 {group_id} 成员 {author} 回复了私聊用户 {user_id} 的提问 {relay_id}")
    try:
        # 贴个表情让回复的人知道收到了（协议端不支持就算了）。
        await bot.call_api("set_msg_emoji_like", message_id=int(event.message_id), emoji_id="124")
    except Exception:  # noqa: BLE001
        dbg("贴表情回执失败(忽略)")
    schedule_reply_delivery(bot, user_id)
    return True


# ── 待确认（私聊里发到群里之前）────────────────────────────────────────


@dataclass(slots=True)
class PendingRelay:
    id: str
    user_id: int
    group_id: int
    group_name: str
    kind: str
    text: str
    summary: str
    arrival: int
    created: float
    expires: float
    handoff_id: str | None = None
    extra: dict[str, Any] = field(default_factory=dict)


_pending: dict[str, PendingRelay] = {}


def _arrivals(user_id: int) -> int:
    try:
        from ..chat_state import arrival_count

        return int(arrival_count(user_id))
    except Exception:  # noqa: BLE001
        return 0


def _gc() -> None:
    now = time.monotonic()
    for key in [key for key, item in _pending.items() if item.expires <= now]:
        _pending.pop(key, None)


def confirm_seconds() -> int:
    return int(_cfg("group_manage_confirm_seconds", 300))


def create_pending(
    user_id: int,
    *,
    group_id: int,
    group_name: str,
    kind: str,
    text: str,
    summary: str,
    handoff_id: str | None = None,
) -> tuple[PendingRelay, bool]:
    """返回（待确认, 是否沿用了已有的同一件事）。同一件事沿用原来的计数，避免确认循环。"""

    _gc()
    for item in _pending.values():
        if item.user_id == int(user_id) and item.group_id == int(group_id) and item.kind == kind and item.text == text:
            return item, True
    mine = sorted((item for item in _pending.values() if item.user_id == int(user_id)), key=lambda item: item.created)
    for stale in mine[: max(0, len(mine) - MAX_PENDING_PER_USER + 1)]:
        _pending.pop(stale.id, None)
    now = time.monotonic()
    item = PendingRelay(
        id=_new_id("b", set(_pending)),
        user_id=int(user_id),
        group_id=int(group_id),
        group_name=group_name,
        kind=kind,
        text=text,
        summary=summary,
        arrival=_arrivals(user_id),
        created=now,
        expires=now + confirm_seconds(),
        handoff_id=handoff_id,
    )
    _pending[item.id] = item
    return item, False


def replied_since(item: PendingRelay) -> bool:
    return _arrivals(item.user_id) > item.arrival


def pending_for(user_id: int) -> list[PendingRelay]:
    _gc()
    return sorted((item for item in _pending.values() if item.user_id == int(user_id)), key=lambda item: item.created, reverse=True)


def take_pending(user_id: int, pending_id: str | None) -> PendingRelay:
    """取出要执行的待确认；没等到他回话时抛 ``needs_reply``（不弹出）。"""

    mine = pending_for(user_id)
    raw = str(pending_id or "").strip()
    item = next((row for row in mine if row.id == raw), None)
    if item is None and not raw and len(mine) == 1:
        item = mine[0]
    if item is None:
        if mine:
            listing = "；".join(f"{row.id} {row.summary}" for row in mine)
            raise BridgeError("没有这个待确认的转告", code="not_found", hint=f"还在等他确认的是：{listing}")
        raise BridgeError("没有待确认的转告（可能已过期或已发出）", code="not_found", hint="需要的话重新发起")
    if not replied_since(item):
        raise BridgeError("还没等到他的确认", code="needs_reply", hint="把要发的话问他，等他回复同意后再 confirm")
    _pending.pop(item.id, None)
    return item


def cancel_pending(user_id: int, pending_id: str | None = None) -> int:
    mine = pending_for(user_id)
    gone = [row for row in mine if not pending_id or row.id == pending_id]
    for row in gone:
        _pending.pop(row.id, None)
    return len(gone)


def pending_view(user_id: int) -> list[dict[str, Any]]:
    now = time.monotonic()
    return [
        {
            "confirm_id": item.id,
            "summary": item.summary,
            "asked_seconds_ago": int(max(0.0, now - item.created)),
            "he_replied_since": replied_since(item),
        }
        for item in pending_for(user_id)[:3]
    ]


# ── 好友（只对好友私聊）─────────────────────────────────────────────────

_friends: dict[str, tuple[float, frozenset[int]]] = {}


async def is_friend(bot: Any, user_id: int) -> bool:
    key = str(getattr(bot, "self_id", ""))
    cached = _friends.get(key)
    if cached is None or time.monotonic() - cached[0] > FRIEND_CACHE_SECONDS:
        try:
            rows = await bot.call_api("get_friend_list")
        except Exception:  # noqa: BLE001
            dbg_exc("读好友列表失败，按不是好友处理")
            return False
        ids = frozenset(int(row.get("user_id") or 0) for row in rows or [] if isinstance(row, dict))
        cached = (time.monotonic(), ids)
        _friends[key] = cached
    return int(user_id) in cached[1]


def can_message_privately(user_id: int) -> str | None:
    """私聊这边能不能主动找他；能就返回 None，不能返回原因。"""

    if not enabled():
        return "私聊协作没开"
    if not _cfg("private_agent_auto_reply", True):
        return "私聊 Agent 没有自动接管"
    try:
        from ..private_chat_bridge import auto_paused

        if auto_paused(user_id):
            return "他在私聊里用 /退出 暂停了 Agent"
    except Exception:  # noqa: BLE001
        dbg_exc("读私聊暂停状态失败(忽略)")
    return None


async def opted_out(session: Any, group_id: int, user_id: int) -> bool:
    if session is None:
        return False
    try:
        from ..data_models.agent_memory import AgentPrivacy

        row = await session.get(AgentPrivacy, (int(group_id), int(user_id)))
    except Exception:  # noqa: BLE001
        return False
    return bool(row is not None and row.opted_out)


def relay_allowed_in_group(group_id: int, user_id: int | None = None) -> bool:
    """群主可以在群管理策略里关掉本群的私聊转告；超级管理员不受这条策略限制。"""

    if user_id is not None and bypasses_group_policy(user_id):
        return True
    try:
        from .group_manage import policy_for

        return bool(policy_for(group_id).get("allow_relay", True))
    except Exception:  # noqa: BLE001
        return True


# ── 给两边提示词/工具用的视图 ──────────────────────────────────────────


def private_view(user_id: int, now: datetime | None = None) -> dict[str, Any]:
    """私聊本轮资料（system#3）：从群里转来的事、还在收回复的提问、等他确认的转告。"""

    if not enabled():
        return {}
    now = now or now_beijing()
    view: dict[str, Any] = {}
    rows = active_handoffs(user_id, now)[:3]
    if rows:
        view["from_groups"] = [
            {
                "id": row["id"],
                "group": row.get("group_name"),
                "topic": row.get("topic"),
                "his_words_in_group": row.get("asked") or None,
                "when": row.get("created"),
                "status": row.get("status"),
            }
            for row in rows
        ]
    asks = open_asks(user_id, now)[:3]
    if asks:
        view["asks_in_groups"] = [
            {
                "id": row["id"],
                "group": row.get("group_name"),
                "question": row.get("text"),
                "until": row.get("until"),
                "replies": [
                    {"who": reply.get("from_name"), "said": reply.get("text")}
                    for reply in (row.get("replies") or [])[-5:]
                ],
                "reply_count": len(row.get("replies") or []),
            }
            for row in asks
        ]
    waiting = pending_view(user_id)
    if waiting:
        view["group_relay_waiting"] = waiting
    return view


def group_view(user_id: int, group_id: int) -> dict[str, Any]:
    """群里查他从本群起的私聊事项：只给源自本群的话题和状态，不给私聊内容。"""

    items = [row for row in handoffs(user_id) if int(row.get("group_id") or 0) == int(group_id)]
    asks = [row for row in relays(user_id) if int(row.get("group_id") or 0) == int(group_id) and row.get("kind") == "ask"]
    return {
        "moved_to_private": [
            {"topic": row.get("topic"), "when": row.get("created"), "status": STATUS_LABELS.get(str(row.get("status")), row.get("status"))}
            for row in items[-5:][::-1]
        ],
        "asked_here_for_him": [
            {"question": row.get("text"), "when": row.get("created"), "replies": len(row.get("replies") or []), "open": row.get("status") == "open"}
            for row in asks[-5:][::-1]
        ],
    }


STATUS_LABELS = {
    "opening": "正在私聊他",
    "opened": "已经私聊他了",
    "done": "办完了",
    "failed": "没能私聊上",
    "dropped": "不用办了",
}


def overview(limit: int = 50) -> dict[str, Any]:
    """WebUI：最近的协作记录（全站，按时间倒序）。私聊内容不在这里，只有话题/转告原文。"""

    from . import private_store

    rows_h: list[dict[str, Any]] = []
    rows_r: list[dict[str, Any]] = []
    for user_id in private_store.known_user_ids():
        data = _load_bridge(user_id)
        rows_h.extend({**item, "user_id": str(user_id)} for item in data["handoffs"])
        for item in data["relays"]:
            row = {key: value for key, value in item.items() if key != "replies"}
            row["user_id"] = str(user_id)
            row["reply_count"] = len(item.get("replies") or [])
            row["group_id"] = str(item.get("group_id"))
            rows_r.append(row)
    for row in rows_h:
        row["group_id"] = str(row.get("group_id"))
    rows_h.sort(key=lambda row: str(row.get("created")), reverse=True)
    rows_r.sort(key=lambda row: str(row.get("created")), reverse=True)
    now = now_beijing()
    return {
        "enabled": enabled(),
        "today": {
            "handoffs": sum(1 for row in rows_h if str(row.get("created", "")).startswith(now.strftime("%Y-%m-%d"))),
            "relays": sum(1 for row in rows_r if str(row.get("created", "")).startswith(now.strftime("%Y-%m-%d"))),
            "open_asks": sum(1 for row in rows_r if row.get("status") == "open"),
        },
        "handoffs": rows_h[:limit],
        "relays": rows_r[:limit],
        "limits": {
            "handoff_daily": int(_cfg("agent_bridge_handoff_daily", 10)),
            "relay_daily": int(_cfg("agent_bridge_relay_daily", 10)),
            "relay_group_hourly": int(_cfg("agent_bridge_relay_group_hourly", 6)),
            "ask_hours": int(_cfg("agent_bridge_ask_hours", 6)),
            "reply_limit": int(_cfg("agent_bridge_reply_limit", 20)),
            "confirm": bool(_cfg("agent_bridge_confirm", True)),
        },
    }


def user_detail(user_id: int) -> dict[str, Any]:
    data = _load_bridge(user_id)
    return {
        "handoffs": list(reversed(data["handoffs"])),
        "relays": list(reversed(data["relays"])),
        "waiting_confirm": pending_view(user_id),
    }


def reset_for_tests() -> None:
    global _index_cache  # noqa: PLW0603
    _pending.clear()
    _friends.clear()
    _group_sent.clear()
    _delivery_tasks.clear()
    with _index_lock:
        _index_cache = None


__all__ = [
    "HANDOFF_REASONS",
    "REASON_LABELS",
    "STATUS_LABELS",
    "BridgeError",
    "PendingRelay",
    "active_handoffs",
    "add_reply",
    "can_message_privately",
    "cancel_pending",
    "capture_group_reply",
    "check_relay_quota",
    "close_relay",
    "create_handoff",
    "create_pending",
    "deliver_replies",
    "enabled",
    "format_replies",
    "group_view",
    "handoffs",
    "is_friend",
    "normalize_relay",
    "open_asks",
    "opted_out",
    "overview",
    "pending_for",
    "pending_view",
    "private_view",
    "record_relay",
    "relay_allowed_in_group",
    "relay_text",
    "relays",
    "replied_since",
    "reply_target",
    "reset_for_tests",
    "schedule_reply_delivery",
    "take_pending",
    "take_undelivered",
    "update_handoff",
    "user_detail",
]
