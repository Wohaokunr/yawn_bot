# ruff: noqa: TID252, E501, C901, PLR0913, FBT003, DTZ007, ARG001
"""群聊记忆 ↔ 私聊记忆打通：同一个“我”，记得的事在两边口径一致。

只管“两边记忆之间怎么流动”，不另存事实本身：

- 事实仍在原处：私聊事实是 ``AgentMemory(scope="private")``（``private_memory``），
  群画像是 ``AgentMemory(group_id=群)``（``memory`` / ``memory_self``）。
- 本模块的数据放在私聊个人数据的 ``memory_link`` 键（``private_store``），无迁移：

  - ``prefs``：他本人定的流动规则——
    ``groups_in_private``（私聊里提不提群里的事）、``muted_groups``（哪些群的事别带进私聊）、
    ``share_keys``（哪些类别的私聊事实允许群里的我也知道）、``share_groups``（只在哪些群；空=所有可读群）；
  - ``tombstones``：他让我忘掉的事（类别 + 关键词 + 范围 private/everywhere），
    在有效期内自动整理不会再记回来（私聊整理、群整理都看）；
  - ``ledger``：跨边流动的记录（开/关共享、忘掉、同步到群），他问“你从哪知道的/群里知道我什么”时能答。

**隐私硬规则**（与 AGENTS.md 一致）：

- 私聊 → 群只有一条路：他明确允许共享的**类别**（``share_keys``），而且只给事实内容本身，
  不带“私聊里说的”这类来源；聊天记录、回顾、待办、个人助理数据永远不进群。
- 群 → 私聊照旧只读他本人活跃、开了 Agent、没隐私退出的群；再叠加他本人的 ``muted_groups``
  和 ``groups_in_private``。
- 忘掉只会让信息变少：私聊里忘掉的事，群里的画像在私聊里也不再提（影子屏蔽），
  选 everywhere 时连群画像一起删，并在整理时挡住重新学回来。
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from .log import dbg, dbg_exc
from .memory_hygiene import (
    contains_pii,
    is_near_duplicate,
    looks_like_instruction,
    scrub_pii,
)
from .memory_self import SELF_FACT_KEYS

KEY = "memory_link"
# 可共享到群里的类别：就是成员自助记忆的类别（本来就过了敏感类别过滤）。
SHAREABLE_KEYS: tuple[str, ...] = SELF_FACT_KEYS
SCOPES = ("private", "everywhere", "group")
MAX_TOMBSTONES = 40
MAX_LEDGER = 60
MAX_MUTED = 30
_KEYWORD_CHARS = 40
_LEDGER_KINDS = frozenset(
    {
        "share",
        "unshare",
        "mute_group",
        "unmute_group",
        "groups_in_private",
        "forget",
        "forget_everywhere",
        "group_forget_everywhere",
        "group_self_fact",
        "tombstone_cleared",
        "admin_reset",
    }
)
_LEDGER_LABELS = {
    "share": "允许群里也知道",
    "unshare": "不再让群里知道",
    "mute_group": "这个群的事别带进私聊",
    "unmute_group": "这个群的事可以带进私聊了",
    "groups_in_private": "私聊里提不提群里的事",
    "forget": "让我忘掉（私聊）",
    "forget_everywhere": "让我忘掉（私聊和各群）",
    "group_forget_everywhere": "在群里让我各处都忘掉",
    "group_self_fact": "群里亲口让我记的，私聊也记着",
    "tombstone_cleared": "重新告诉我，解除了“忘掉”",
    "admin_reset": "管理员重置了记忆打通设置",
}


def _cfg(name: str, default: Any) -> Any:
    try:
        from ..private_chat_bridge import private_agent_config

        return getattr(private_agent_config, name, default)
    except Exception:  # noqa: BLE001
        return default


def enabled() -> bool:
    """总开关（MEMORY_LINK_ENABLED）：关掉后只保留旧行为（不共享、不屏蔽、不写墓碑）。"""

    return bool(_cfg("memory_link_enabled", True))


def share_enabled() -> bool:
    """私聊 → 群共享的总开关（MEMORY_LINK_SHARE）；关掉时他的共享设置保留但暂不生效。"""

    return enabled() and bool(_cfg("memory_link_share", True))


def tombstone_days() -> int:
    return int(_cfg("memory_link_tombstone_days", 180))


def group_self_facts_enabled() -> bool:
    return enabled() and bool(_cfg("memory_link_group_self_facts", True))


def group_fact_limit() -> int:
    return int(_cfg("memory_link_group_facts", 4))


def fmt(moment: datetime) -> str:
    return f"{moment:%Y-%m-%d %H:%M}"


def _parse(raw: Any) -> datetime | None:
    try:
        return datetime.strptime(str(raw), "%Y-%m-%d %H:%M")
    except (TypeError, ValueError):
        return None


# ── 状态 ────────────────────────────────────────────────────


@dataclass(slots=True)
class LinkState:
    """一位用户的记忆打通设置（只读快照）。"""

    groups_in_private: bool = True
    muted_groups: frozenset[int] = frozenset()
    share_keys: frozenset[str] = frozenset()
    share_groups: frozenset[int] = frozenset()
    tombstones: list[dict[str, Any]] = field(default_factory=list)
    ledger: list[dict[str, Any]] = field(default_factory=list)

    def group_visible_in_private(self, group_id: int) -> bool:
        return self.groups_in_private and int(group_id) not in self.muted_groups

    def shares_to(self, group_id: int) -> bool:
        return bool(self.share_keys) and (
            not self.share_groups or int(group_id) in self.share_groups
        )


def _ints(raw: Any, limit: int) -> list[int]:
    values: list[int] = []
    for item in raw if isinstance(raw, list) else []:
        try:
            value = int(item)
        except (TypeError, ValueError):
            continue
        if value > 0 and value not in values:
            values.append(value)
    return values[:limit]


def _section(record: dict[str, Any]) -> dict[str, Any]:
    """取（并就地规整）个人数据里的 ``memory_link`` 段。"""

    data = record.get(KEY)
    if not isinstance(data, dict):
        data = {}
        record[KEY] = data
    prefs = data.get("prefs")
    if not isinstance(prefs, dict):
        prefs = {}
    data["prefs"] = {
        "groups_in_private": prefs.get("groups_in_private", True) is not False,
        "muted_groups": _ints(prefs.get("muted_groups"), MAX_MUTED),
        "share_keys": [
            key
            for key in dict.fromkeys(prefs.get("share_keys") or [])
            if key in SHAREABLE_KEYS
        ],
        "share_groups": _ints(prefs.get("share_groups"), MAX_MUTED),
    }
    for name in ("tombstones", "ledger"):
        items = data.get(name)
        data[name] = (
            [item for item in items if isinstance(item, dict)]
            if isinstance(items, list)
            else []
        )
    return data


def state_from_record(
    record: dict[str, Any], *, now: datetime | None = None
) -> LinkState:
    data = _section(dict(record))
    prefs = data["prefs"]
    tombstones = data["tombstones"]
    if now is not None:
        tombstones = [item for item in tombstones if not _expired(item, now)]
    return LinkState(
        groups_in_private=bool(prefs["groups_in_private"]),
        muted_groups=frozenset(prefs["muted_groups"]),
        share_keys=frozenset(prefs["share_keys"]),
        share_groups=frozenset(prefs["share_groups"]),
        tombstones=[dict(item) for item in tombstones],
        ledger=[dict(item) for item in data["ledger"]],
    )


_CACHE_LOCK = threading.Lock()
# 群聊热路径（每轮都要看发言人的共享设置、整理时看墓碑）按用户缓存；写入时作废。
_cache: dict[int, LinkState] = {}


def load(user_id: int, *, now: datetime | None = None) -> LinkState:
    """读一位用户的设置；失败按默认（不共享、不屏蔽）。"""

    if not enabled():
        return LinkState()
    with _CACHE_LOCK:
        cached = _cache.get(int(user_id))
    if cached is None:
        from . import private_store

        try:
            cached = state_from_record(private_store.load(int(user_id)))
        except Exception:  # noqa: BLE001
            dbg_exc(f"记忆打通设置读取失败: 用户 {user_id}")
            cached = LinkState()
        with _CACHE_LOCK:
            _cache[int(user_id)] = cached
    if now is None:
        return cached
    return LinkState(
        groups_in_private=cached.groups_in_private,
        muted_groups=cached.muted_groups,
        share_keys=cached.share_keys,
        share_groups=cached.share_groups,
        tombstones=[item for item in cached.tombstones if not _expired(item, now)],
        ledger=cached.ledger,
    )


def invalidate(user_id: int | None = None) -> None:
    with _CACHE_LOCK:
        if user_id is None:
            _cache.clear()
        else:
            _cache.pop(int(user_id), None)


def reset() -> None:
    """测试用：清缓存。"""

    invalidate()


def _update(user_id: int, fn: Any) -> Any:
    from . import private_store

    def apply(record: dict[str, Any]) -> Any:
        return fn(_section(record))

    try:
        return private_store.update(int(user_id), apply)
    finally:
        invalidate(user_id)


def _log(data: dict[str, Any], kind: str, now: datetime, **detail: Any) -> None:
    if kind not in _LEDGER_KINDS:
        return
    entry: dict[str, Any] = {"at": fmt(now), "kind": kind}
    for name, value in detail.items():
        if value in (None, "", [], {}):
            continue
        entry[name] = value
    data["ledger"].append(entry)
    del data["ledger"][:-MAX_LEDGER]


# ── 设置：共享、静音、私聊里提不提群 ───────────────────────────


def set_share(
    user_id: int,
    keys: list[str],
    *,
    on: bool,
    now: datetime,
    groups: list[int] | None = None,
    group_names: list[str] | None = None,
) -> dict[str, Any]:
    """开/关某些类别的共享；``groups`` 给了就把共享范围限定到这些群（只在开的时候用）。"""

    wanted = [key for key in dict.fromkeys(keys) if key in SHAREABLE_KEYS]
    if not wanted:
        raise ValueError("没有可以共享的类别")

    def apply(data: dict[str, Any]) -> dict[str, Any]:
        prefs = data["prefs"]
        current = list(prefs["share_keys"])
        if on:
            for key in wanted:
                if key not in current:
                    current.append(key)
            if groups:
                prefs["share_groups"] = _ints(groups, MAX_MUTED)
        else:
            current = [key for key in current if key not in wanted]
            if not current:
                prefs["share_groups"] = []
        prefs["share_keys"] = current
        _log(
            data,
            "share" if on else "unshare",
            now,
            keys=wanted,
            groups=list(group_names or []) if on else None,
        )
        return {
            "share_keys": list(current),
            "share_groups": list(prefs["share_groups"]),
        }

    return _update(user_id, apply)


def set_muted(
    user_id: int, group_id: int, *, muted: bool, now: datetime, group_name: str = ""
) -> list[int]:
    def apply(data: dict[str, Any]) -> list[int]:
        prefs = data["prefs"]
        current = [gid for gid in prefs["muted_groups"] if gid != int(group_id)]
        if muted:
            current.append(int(group_id))
        prefs["muted_groups"] = current[-MAX_MUTED:]
        _log(
            data,
            "mute_group" if muted else "unmute_group",
            now,
            group=group_name or str(group_id),
        )
        return list(prefs["muted_groups"])

    return _update(user_id, apply)


def set_groups_in_private(user_id: int, *, on: bool, now: datetime) -> bool:
    def apply(data: dict[str, Any]) -> bool:
        data["prefs"]["groups_in_private"] = bool(on)
        _log(data, "groups_in_private", now, on=bool(on))
        return bool(on)

    return _update(user_id, apply)


def admin_reset(user_id: int, *, now: datetime, keep_tombstones: bool = True) -> None:
    """WebUI：恢复默认设置；默认保留“忘掉”的记录（那是他本人的隐私要求）。"""

    def apply(data: dict[str, Any]) -> None:
        tombstones = list(data["tombstones"]) if keep_tombstones else []
        data.clear()
        data["prefs"] = {
            "groups_in_private": True,
            "muted_groups": [],
            "share_keys": [],
            "share_groups": [],
        }
        data["tombstones"] = tombstones
        data["ledger"] = []
        _log(data, "admin_reset", now)

    _update(user_id, apply)


def clear_for_purge(user_id: int, *, now: datetime) -> None:
    """清空私聊记忆时：共享设置一起撤掉（共享的事实已不存在），“忘掉”的记录保留。"""

    def apply(data: dict[str, Any]) -> None:
        data["prefs"]["share_keys"] = []
        data["prefs"]["share_groups"] = []
        data["ledger"] = []

    try:
        _update(user_id, apply)
    except Exception:  # noqa: BLE001
        dbg_exc(f"清空记忆打通共享失败: 用户 {user_id}")


# ── 墓碑：忘掉的事不再记回来 ────────────────────────────────


def _expired(item: dict[str, Any], now: datetime) -> bool:
    until = _parse(item.get("until"))
    return until is not None and until < now


def _matches(item: dict[str, Any], key: str, content: str) -> bool:
    item_key = str(item.get("key") or "")
    if item_key and item_key != key:
        return False
    keyword = str(item.get("keyword") or "").strip()
    if not keyword:
        # 只按类别忘掉的：这个类别下的任何内容都算。
        return bool(item_key)
    text = str(content or "")
    if keyword in text:
        return True
    return any(
        is_near_duplicate(keyword, part.strip())
        for part in text.split("、")
        if part.strip()
    )


def add_tombstone(
    user_id: int,
    *,
    key: str | None,
    keyword: str | None,
    scope: str,
    now: datetime,
    kind: str = "forget",
    groups: list[str] | None = None,
    group_id: int | None = None,
) -> dict[str, Any] | None:
    """记一条“他让我忘掉”；同类别同关键词同范围的合并（everywhere 覆盖更小的范围）。

    ``scope="group"`` 要给 ``group_id``：只挡那个群的整理（他只在那个群说了忘掉）。
    """

    if not enabled():
        return None
    key = str(key or "").strip()
    if key and key not in SHAREABLE_KEYS and key != "recurring_topic":
        key = ""
    keyword = str(keyword or "").strip()[:_KEYWORD_CHARS]
    if not key and not keyword:
        return None
    scope = scope if scope in SCOPES else "private"
    if scope == "group" and not group_id:
        scope = "private"
    until = now + timedelta(days=max(tombstone_days(), 1))

    def apply(data: dict[str, Any]) -> dict[str, Any]:
        items: list[dict[str, Any]] = [
            item for item in data["tombstones"] if not _expired(item, now)
        ]
        for item in items:
            same_place = scope == "everywhere" or (
                item.get("scope") == scope
                and int(item.get("group_id") or 0) == int(group_id or 0)
            )
            if (
                str(item.get("key") or "") == key
                and str(item.get("keyword") or "") == keyword
                and same_place
            ):
                if scope == "everywhere":
                    item["scope"] = "everywhere"
                    item.pop("group_id", None)
                item["until"] = fmt(until)
                item["at"] = fmt(now)
                data["tombstones"] = items
                _log(
                    data,
                    kind,
                    now,
                    key=key or None,
                    keyword=keyword or None,
                    groups=groups,
                )
                return dict(item)
        entry: dict[str, Any] = {
            "key": key,
            "keyword": keyword,
            "scope": scope,
            "at": fmt(now),
            "until": fmt(until),
        }
        if scope == "group":
            entry["group_id"] = int(group_id or 0)
        items.append(entry)
        data["tombstones"] = items[-MAX_TOMBSTONES:]
        _log(data, kind, now, key=key or None, keyword=keyword or None, groups=groups)
        return dict(entry)

    return _update(user_id, apply)


def suppressed(
    user_id: int,
    key: str,
    content: str,
    *,
    now: datetime,
    scope: str = "private",
    state: LinkState | None = None,
    group_id: int | None = None,
) -> bool:
    """这条（类别, 内容）是不是他让我忘掉的。

    ``scope="private"``：私聊里看——私聊和 everywhere 的墓碑算（只在某个群忘掉的不算）；
    ``scope="group"``：群 ``group_id`` 整理时看——everywhere 的和这个群自己的才挡
    （只在私聊忘掉的，群里照常）。
    """

    if not enabled() or not user_id:
        return False
    current = state or load(user_id, now=now)
    for item in current.tombstones:
        if _expired(item, now):
            continue
        item_scope = item.get("scope")
        if scope == "group" and not (
            item_scope == "everywhere"
            or (
                item_scope == "group"
                and int(item.get("group_id") or 0) == int(group_id or 0)
            )
        ):
            continue
        if scope == "private" and item_scope == "group":
            continue
        if _matches(item, str(key or ""), str(content or "")):
            return True
    return False


def text_suppressed(
    user_id: int, text: str, *, now: datetime, state: LinkState | None = None
) -> bool:
    """一句话（他在群里说的话、回想到的旧消息）里是不是含着他让我忘掉的关键词。"""

    if not enabled() or not user_id:
        return False
    current = state or load(user_id, now=now)
    value = str(text or "")
    for item in current.tombstones:
        keyword = str(item.get("keyword") or "").strip()
        if item.get("scope") == "group":
            continue
        if keyword and not _expired(item, now) and keyword in value:
            return True
    return False


def clear_tombstones_for(
    user_id: int,
    key: str,
    content: str,
    *,
    now: datetime,
    everywhere: bool,
    group_id: int | None = None,
) -> int:
    """他又亲口让我记住同一件事：对应的墓碑作废。

    私聊里说的（``everywhere=False``、无群号）只解私聊的；在某个群说的解那个群的；
    ``everywhere=True``（他说“群里也这样”或在群里亲口说）连 everywhere 的一起解。
    """

    if not enabled():
        return 0
    current = load(user_id, now=now)
    if not any(_matches(item, key, content) for item in current.tombstones):
        return 0

    def apply(data: dict[str, Any]) -> int:
        kept: list[dict[str, Any]] = []
        removed = 0
        for item in data["tombstones"]:
            item_scope = item.get("scope")
            if item_scope == "group":
                applies = bool(group_id) and int(item.get("group_id") or 0) == int(
                    group_id or 0
                )
            elif item_scope == "everywhere":
                applies = everywhere
            else:
                applies = not group_id or everywhere
            if _matches(item, key, content) and applies:
                removed += 1
                continue
            kept.append(item)
        data["tombstones"] = kept
        if removed:
            _log(data, "tombstone_cleared", now, key=key)
        return removed

    return int(_update(user_id, apply) or 0)


def restore_tombstones(user_id: int, tombstones: list[dict[str, Any]]) -> None:
    """清空个人数据后把“忘掉”的记录放回去（只放墓碑，设置和流动记录不恢复）。"""

    if not tombstones:
        return

    def apply(data: dict[str, Any]) -> None:
        data["tombstones"] = [dict(item) for item in tombstones][-MAX_TOMBSTONES:]

    try:
        _update(user_id, apply)
    except Exception:  # noqa: BLE001
        dbg_exc(f"恢复“忘掉”记录失败: 用户 {user_id}")


def drop_tombstone(user_id: int, index: int, *, now: datetime) -> bool:
    """WebUI：删掉第 index 条墓碑（按当前列表顺序）。"""

    def apply(data: dict[str, Any]) -> bool:
        # 与 tombstone_view 同一口径：先去掉过期的，序号才对得上。
        items = [item for item in data["tombstones"] if not _expired(item, now)]
        data["tombstones"] = items
        if not 0 <= index < len(items):
            return False
        items.pop(index)
        _log(data, "tombstone_cleared", now, by="admin")
        return True

    return bool(_update(user_id, apply))


def log_event(user_id: int, kind: str, *, now: datetime, **detail: Any) -> None:
    """其他模块记一笔跨边流动（失败只记日志）。"""

    if not enabled():
        return
    try:
        _update(user_id, lambda data: _log(data, kind, now, **detail))
    except Exception:  # noqa: BLE001
        dbg_exc(f"记忆打通流水写入失败: 用户 {user_id}")


# ── 私聊 → 群：允许共享的私聊事实 ───────────────────────────


def _clean_shared(text: object, limit: int = 80) -> str | None:
    value = str(text or "").strip()
    if not value or looks_like_instruction(value):
        return None
    if contains_pii(value):
        value = scrub_pii(value)
    return value[:limit] or None


async def shared_facts_for_group(
    session: Any,
    group_id: int,
    user_ids: list[int],
    *,
    now: datetime,
    opted_out: set[int] | frozenset[int] = frozenset(),
) -> dict[int, list[Any]]:
    """群上下文用：这些成员允许本群知道的私聊事实（``AgentMemory`` 行，按成员分组）。

    只给：共享总开关开着、他本人开了这个类别、共享范围包含本群、他是本群活跃成员、
    没在本群隐私退出；内容读时复检（像指令的丢掉、联系方式打码）。
    """

    if not share_enabled() or not user_ids:
        return {}
    candidates: dict[int, LinkState] = {}
    for uid in dict.fromkeys(int(u) for u in user_ids):
        if uid in opted_out or uid <= 0:
            continue
        state = load(uid, now=now)
        if state.shares_to(group_id):
            candidates[uid] = state
    if not candidates:
        return {}
    from sqlalchemy import select

    from ..data_models.agent_memory import AgentMemory
    from ..data_models.user_group import UserGroup
    from ..private_chat_bridge import PRIVATE_MEMORY_SCOPE, PRIVATE_MEMORY_TYPE

    try:
        active = set(
            (
                await session.execute(
                    select(UserGroup.user_id).where(
                        UserGroup.group_id == group_id,
                        UserGroup.user_id.in_(sorted(candidates)),
                        UserGroup.is_active.is_(True),
                    )
                )
            )
            .scalars()
            .all()
        )
        rows = (
            (
                await session.execute(
                    select(AgentMemory)
                    .where(
                        AgentMemory.scope == PRIVATE_MEMORY_SCOPE,
                        AgentMemory.group_id.is_(None),
                        AgentMemory.memory_type == PRIVATE_MEMORY_TYPE,
                        AgentMemory.subject_user_id.in_(sorted(int(u) for u in active)),
                    )
                    .order_by(AgentMemory.subject_user_id, AgentMemory.memory_key)
                )
            )
            .scalars()
            .all()
        )
    except Exception:  # noqa: BLE001 - 表缺失/查询失败时这一段跳过，不影响群聊
        dbg_exc(f"群 {group_id} 读取成员共享的私聊事实失败")
        return {}
    limit = group_fact_limit()
    picked: dict[int, list[Any]] = {}
    for row in rows:
        uid = int(row.subject_user_id or 0)
        state = candidates.get(uid)
        if state is None or str(row.memory_key) not in state.share_keys:
            continue
        if row.expires_at is not None and row.expires_at < now:
            continue
        if _clean_shared(row.content) is None:
            continue
        bucket = picked.setdefault(uid, [])
        if len(bucket) < limit:
            bucket.append(row)
    if picked:
        dbg(
            f"群 {group_id} 成员共享的私聊事实: {sum(len(v) for v in picked.values())} 条"
        )
    return picked


def shared_view(row: Any) -> dict[str, Any] | None:
    """给群工具（get_person_profile）用的共享事实视图；不带来源说明。"""

    content = _clean_shared(row.content)
    if content is None:
        return None
    return {
        "type": "profile",
        "key": str(row.memory_key),
        "content": content,
        "source": "shared_by_member",
        "confidence": round(float(row.confidence or 0.0), 2),
    }


# ── 给工具和 WebUI 看的视图 ───────────────────────────────────


def ledger_view(state: LinkState, *, limit: int = 12) -> list[dict[str, Any]]:
    items = []
    for entry in state.ledger[-limit:]:
        view = {
            "at": entry.get("at"),
            "what": _LEDGER_LABELS.get(str(entry.get("kind")), entry.get("kind")),
        }
        for name in ("key", "keys", "keyword", "group", "groups", "on", "by"):
            if name in entry:
                view[name] = entry[name]
        items.append(view)
    return items


def tombstone_view(state: LinkState) -> list[dict[str, Any]]:
    from ..private_chat_bridge import PRIVATE_MEMORY_LABELS

    return [
        {
            "what": PRIVATE_MEMORY_LABELS.get(str(item.get("key") or ""), "")
            or "任何类别",
            "keyword": item.get("keyword") or "",
            "scope": "私聊和各群"
            if item.get("scope") == "everywhere"
            else ("某个群" if item.get("scope") == "group" else "私聊"),
            "until": item.get("until"),
        }
        for item in state.tombstones
    ]


def settings_view(state: LinkState, groups: list[Any] | None = None) -> dict[str, Any]:
    """他能看懂的设置摘要；``groups`` 是 ``SharedGroup`` 列表，用来把群号换成群名。"""

    from ..private_chat_bridge import PRIVATE_MEMORY_LABELS

    names = {int(g.group_id): g.name for g in groups or []}
    return {
        "groups_in_private": state.groups_in_private,
        "muted_groups": [
            names.get(gid, str(gid)) for gid in sorted(state.muted_groups)
        ],
        "shared_with_groups": [
            PRIVATE_MEMORY_LABELS.get(key, key)
            for key in SHAREABLE_KEYS
            if key in state.share_keys
        ],
        "share_only_in": [
            names.get(gid, str(gid)) for gid in sorted(state.share_groups)
        ],
        "sharing_available": share_enabled(),
    }


def prompt_digest(state: LinkState, groups: list[Any] | None = None) -> dict[str, Any]:
    """放进私聊长期资料（system#2）的一小段：只有和默认不同时才给，键与顺序固定。"""

    from ..private_chat_bridge import PRIVATE_MEMORY_LABELS

    names = {int(g.group_id): g.name for g in groups or []}
    digest: dict[str, Any] = {}
    if not state.groups_in_private:
        digest["no_group_talk"] = True
    muted = sorted(names.get(gid, "") for gid in state.muted_groups if names.get(gid))
    if muted:
        digest["skip_groups"] = muted
    if state.share_keys and share_enabled():
        digest["groups_also_know"] = [
            PRIVATE_MEMORY_LABELS.get(key, key)
            for key in SHAREABLE_KEYS
            if key in state.share_keys
        ]
    return digest


# ── WebUI ─────────────────────────────────────────────────────


def overview(limit: int = 50) -> dict[str, Any]:
    """全站概况：多少人开了共享/静音了群/让我忘掉过，最近的流动记录（只有类别和群名）。"""

    from . import private_store
    from .context import now_beijing

    now = now_beijing()
    users: list[dict[str, Any]] = []
    events: list[dict[str, Any]] = []
    for user_id in private_store.known_user_ids():
        state = load(user_id, now=now)
        changed = (
            bool(state.share_keys)
            or bool(state.muted_groups)
            or not state.groups_in_private
            or bool(state.tombstones)
        )
        if changed:
            users.append(
                {
                    "user_id": str(user_id),
                    "shares": [key for key in SHAREABLE_KEYS if key in state.share_keys],
                    "share_groups": len(state.share_groups),
                    "muted_groups": len(state.muted_groups),
                    "groups_in_private": state.groups_in_private,
                    "tombstones": len(state.tombstones),
                }
            )
        events.extend({**item, "user_id": str(user_id)} for item in ledger_view(state, limit=MAX_LEDGER))
    events.sort(key=lambda item: str(item.get("at")), reverse=True)
    users.sort(key=lambda item: int(item["user_id"]))
    return {
        "enabled": enabled(),
        "share_enabled": share_enabled(),
        "group_self_facts": group_self_facts_enabled(),
        "tombstone_days": tombstone_days(),
        "group_fact_limit": group_fact_limit(),
        "totals": {
            "sharing": sum(1 for item in users if item["shares"]),
            "muting": sum(1 for item in users if item["muted_groups"] or not item["groups_in_private"]),
            "forgetting": sum(1 for item in users if item["tombstones"]),
        },
        "users": users[:limit],
        "events": events[:limit],
    }


async def user_detail(session: Any, user_id: int) -> dict[str, Any]:
    """一位用户：设置、忘掉的记录、流动记录、私聊事实、各群画像，以及每个群里的我能看到哪些共享事实。"""

    from .context import now_beijing
    from .private_context import group_profile_facts, shared_groups, visible_groups
    from .private_memory import fact_view, list_facts

    now = now_beijing()
    state = load(user_id, now=now)
    groups = await shared_groups(session, user_id)
    seen = visible_groups(groups, state)
    private = [fact_view(row) for row in await list_facts(session, user_id, now=now)]
    names = {g.group_id: g.name for g in groups}
    profile = await group_profile_facts(
        session, user_id, [g.group_id for g in seen if g.agent_readable], now=now, link=state
    )
    per_group = []
    for group in groups:
        shared = (
            [item for item in private if item["key"] in state.share_keys]
            if share_enabled() and group.agent_readable and state.shares_to(group.group_id)
            else []
        )
        per_group.append(
            {
                "group_id": str(group.group_id),
                "name": group.name,
                "agent_readable": group.agent_readable,
                "in_private": group.agent_readable and state.group_visible_in_private(group.group_id),
                "group_sees_shared": [{"label": item["label"], "content": item["content"]} for item in shared],
            }
        )
    return {
        "user_id": str(user_id),
        "settings": {
            "groups_in_private": state.groups_in_private,
            "muted_groups": [str(gid) for gid in sorted(state.muted_groups)],
            "share_keys": [key for key in SHAREABLE_KEYS if key in state.share_keys],
            "share_groups": [str(gid) for gid in sorted(state.share_groups)],
        },
        "tombstones": [
            {**view, "index": index}
            for index, view in enumerate(tombstone_view(state))
        ],
        "ledger": list(reversed(ledger_view(state, limit=MAX_LEDGER))),
        "private_facts": private,
        "group_profile": [
            {
                "key": item["key"],
                "content": item["content"],
                "groups": [names.get(gid, str(gid)) for gid in item.get("group_ids") or [item["group_id"]]],
                "self_told": bool(item.get("self_told")),
                "as_of": item.get("as_of") or "",
            }
            for item in profile
        ],
        "groups": per_group,
    }


__all__ = [
    "KEY",
    "SHAREABLE_KEYS",
    "LinkState",
    "add_tombstone",
    "admin_reset",
    "clear_for_purge",
    "clear_tombstones_for",
    "drop_tombstone",
    "enabled",
    "group_self_facts_enabled",
    "invalidate",
    "ledger_view",
    "load",
    "log_event",
    "overview",
    "prompt_digest",
    "reset",
    "restore_tombstones",
    "set_groups_in_private",
    "set_muted",
    "set_share",
    "settings_view",
    "share_enabled",
    "shared_facts_for_group",
    "shared_view",
    "state_from_record",
    "suppressed",
    "text_suppressed",
    "tombstone_view",
    "user_detail",
]
