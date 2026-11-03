# ruff: noqa: E501, TID252, TRY003, C901, PLR0911, PLR0912, PLR0913, PLR0915, PLR0917, PLR2004, TRY301, PERF401, TC003
"""群管代办：群主/管理员通过私聊 Agent 或网页让 Bot 代为执行群管理操作。

私聊工具 ``group_manage`` 和 WebUI ``/me/groups/{id}/manage/*`` 共用这里，
保证两条入口的权限、确认、额度与审计完全一致。

权限是**双重实时校验**，每次写操作都重新查一遍，不信任缓存：

1. 发起人此刻是这个群的群主或管理员（OneBot 实时读成员角色，单接口失败走成员列表后备）；
2. Bot 此刻也是这个群的群主或管理员（``probe_group_capabilities(refresh=True)``）。

处置成员时再加三道：目标不是 Bot 自己、不是 Bot 超级管理员（可配置）、发起人有权
处置目标（管理员不能处置群主和其他管理员，群主可以处置管理员）；Bot 自己的身份也要
够（Bot 是管理员时动不了其他管理员）。

每个群一个 JSON（``data/group_manage/<群号>.json``）：本群策略、最近 300 条操作记录、
本功能发出的消息（撤回/设精华/设待办只认这些）。不建表、不需要迁移。

私聊里的写操作先生成“待确认”（``confirm_id``），**必须等他发来新消息之后**才能确认
执行（看 ``chat_state.arrival_count``），模型不能在同一轮自问自答地确认；网页由前端弹窗确认。
"""

from __future__ import annotations

import json
import os
import re
import secrets
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path
from typing import Any, TypeVar

from ..private_chat_bridge import private_agent_config
from .capabilities import (
    _probe_member_role,  # pyright: ignore[reportPrivateUsage]
    actor_can_act_on_target,
    probe_group_capabilities,
    target_can_be_muted,
)
from .context import now_beijing
from .log import dbg, dbg_exc
from .memory_hygiene import looks_like_instruction

T = TypeVar("T")

MANAGER_ROLES = frozenset({"owner", "admin"})
ROLE_LABELS = {"owner": "群主", "admin": "管理员", "member": "成员"}

# 本功能写给群里的文字长度上限
ANNOUNCE_MAX_CHARS = 600
SEND_MAX_CHARS = 500
CARD_MAX_CHARS = 60
TITLE_MAX_BYTES = 18
GROUP_NAME_MAX_CHARS = 60
REASON_MAX_CHARS = 80
AUDIT_LIMIT = 300
SENT_LIMIT = 60
SENT_TTL = timedelta(days=7)
MEMBER_CACHE_TTL = 60.0
MAX_PENDING_PER_USER = 5
BURST_WINDOW = 60.0
BURST_LIMIT = 12


@dataclass(frozen=True, slots=True)
class ActionSpec:
    key: str
    label: str
    # low：可撤回/影响小；mid：影响全群或他人；high：不可撤回（踢人、改群名）
    risk: str
    target: bool = False
    # 只有 Bot 是群主时才能做
    bot_owner: bool = False
    write: bool = True


ACTIONS: dict[str, ActionSpec] = {
    spec.key: spec
    for spec in (
        ActionSpec("members", "查成员", "low", write=False),
        ActionSpec("muted", "查禁言名单", "low", write=False),
        ActionSpec("notices", "查群公告", "low", write=False),
        ActionSpec("audit", "查操作记录", "low", write=False),
        ActionSpec("mute", "禁言", "mid", target=True),
        ActionSpec("unmute", "解除禁言", "low", target=True),
        ActionSpec("whole_mute", "全员禁言开关", "mid"),
        ActionSpec("announce", "发群公告", "mid"),
        ActionSpec("delete_notice", "删群公告", "mid"),
        ActionSpec("send", "以 Bot 身份发群消息", "mid"),
        ActionSpec("todo", "设群待办", "mid"),
        ActionSpec("todo_done", "完成群待办", "low"),
        ActionSpec("todo_cancel", "取消群待办", "low"),
        ActionSpec("essence", "设精华消息", "low"),
        ActionSpec("unessence", "取消精华消息", "low"),
        ActionSpec("recall", "撤回 Bot 代发的消息", "low"),
        ActionSpec("card", "改群名片", "low", target=True),
        ActionSpec("title", "设专属头衔", "mid", target=True, bot_owner=True),
        ActionSpec("kick", "移出群聊", "high", target=True),
        ActionSpec("rename", "改群名", "high"),
    )
}
WRITE_ACTIONS = frozenset(key for key, spec in ACTIONS.items() if spec.write)
READ_ACTIONS = frozenset(key for key, spec in ACTIONS.items() if not spec.write)
# 网页上改本群 Agent 也记进同一份操作记录（不是代办动作，只用于审计显示）
EXTRA_LABELS = {
    "agent_config": "改 Agent 运行配置",
    "agent_persona": "改群人设",
    "manage_policy": "改群管代办策略",
    # 群聊 ↔ 私聊协作：群友私聊让 Bot 在本群转告/提问（群主在操作记录里看得到是谁让发的）
    "bridge_post": "私聊转告到本群",
    "bridge_ask": "私聊托 Bot 在本群提问",
}
# 默认需要确认的操作（策略可改）；撤销类（解禁、取消待办、撤回）默认不用确认。
DEFAULT_CONFIRM = frozenset(
    {
        "mute",
        "whole_mute",
        "announce",
        "delete_notice",
        "send",
        "todo",
        "title",
        "kick",
        "rename",
    }
)
# 默认管理员能用的操作（群主永远能用全部开着的操作）；踢人、改群名默认只给群主。
DEFAULT_ADMIN_ACTIONS = WRITE_ACTIONS - {"kick", "rename"}


class ManageError(Exception):
    """群管代办失败：带错误码、HTTP 状态和给人/模型看的建议。"""

    def __init__(
        self,
        message: str,
        *,
        code: str = "permission_denied",
        status: int = 403,
        hint: str | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.status = status
        self.hint = hint


# ── 配置 ────────────────────────────────────────────────────────────────


def enabled() -> bool:
    return bool(getattr(private_agent_config, "group_manage_enabled", True))


def webui_enabled() -> bool:
    return enabled() and bool(getattr(private_agent_config, "group_manage_webui", True))


def max_mute_minutes_ceiling() -> int:
    return int(getattr(private_agent_config, "group_manage_max_mute_minutes", 43200))


def daily_limit() -> int:
    return int(getattr(private_agent_config, "group_manage_daily", 60))


def confirm_seconds() -> int:
    return int(getattr(private_agent_config, "group_manage_confirm_seconds", 300))


def protect_superusers() -> bool:
    return bool(getattr(private_agent_config, "group_manage_protect_superusers", True))


def data_dir() -> Path:
    raw = str(getattr(private_agent_config, "group_manage_data_dir", "") or "").strip()
    raw = raw or os.environ.get("GROUP_MANAGE_DATA_DIR", "").strip()
    return Path(raw) if raw else Path("data") / "group_manage"


# ── 每群存储 ────────────────────────────────────────────────────────────

_LOCK = threading.RLock()


def default_policy() -> dict[str, Any]:
    return {
        "enabled": True,
        "private_enabled": True,
        "webui_enabled": True,
        "disabled_actions": [],
        "admin_actions": sorted(DEFAULT_ADMIN_ACTIONS),
        "confirm_actions": sorted(DEFAULT_CONFIRM),
        "max_mute_minutes": 1440,
        "notify_group": False,
        # 群里的人私聊 Bot 说“帮我在群里问一下/转告一句”时，允许 Bot 发到本群（agent_bridge）
        "allow_relay": True,
        "updated_at": None,
        "updated_by": None,
    }


def _empty() -> dict[str, Any]:
    return {"policy": default_policy(), "audit": [], "sent": [], "next_id": 1}


def _path(group_id: int) -> Path:
    return data_dir() / f"{int(group_id)}.json"


def normalize_policy(raw: Any) -> dict[str, Any]:
    policy = default_policy()
    if not isinstance(raw, dict):
        return policy
    for key in ("enabled", "private_enabled", "webui_enabled", "notify_group", "allow_relay"):
        if isinstance(raw.get(key), bool):
            policy[key] = raw[key]
    for key in ("disabled_actions", "admin_actions", "confirm_actions"):
        value = raw.get(key)
        if isinstance(value, list):
            policy[key] = sorted(
                {str(item) for item in value if str(item) in WRITE_ACTIONS}
            )
    try:
        minutes = int(raw.get("max_mute_minutes", policy["max_mute_minutes"]))
    except (TypeError, ValueError):
        minutes = policy["max_mute_minutes"]
    policy["max_mute_minutes"] = max(1, min(minutes, max_mute_minutes_ceiling()))
    for key in ("updated_at", "updated_by"):
        if raw.get(key) is not None:
            policy[key] = str(raw[key])[:40]
    return policy


def load(group_id: int) -> dict[str, Any]:
    path = _path(group_id)
    with _LOCK:
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return _empty()
        except (OSError, ValueError):
            dbg_exc(f"群管代办：读取 {path} 失败，按空记录处理")
            return _empty()
    record = _empty()
    if isinstance(raw, dict):
        record["policy"] = normalize_policy(raw.get("policy"))
        record["audit"] = [
            item for item in raw.get("audit") or [] if isinstance(item, dict)
        ][-AUDIT_LIMIT:]
        record["sent"] = [
            item for item in raw.get("sent") or [] if isinstance(item, dict)
        ][-SENT_LIMIT:]
        try:
            record["next_id"] = max(1, int(raw.get("next_id") or 1))
        except (TypeError, ValueError):
            record["next_id"] = len(record["audit"]) + 1
    return record


def _save(group_id: int, record: dict[str, Any]) -> None:
    path = _path(group_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(record, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(path)


def update(group_id: int, fn: Callable[[dict[str, Any]], T]) -> T:
    with _LOCK:
        record = load(group_id)
        result = fn(record)
        _save(group_id, record)
        return result


def policy_for(group_id: int) -> dict[str, Any]:
    return load(group_id)["policy"]


def set_policy(group_id: int, changes: dict[str, Any], *, actor: str) -> dict[str, Any]:
    def apply(record: dict[str, Any]) -> dict[str, Any]:
        merged = {**record["policy"], **changes}
        merged["updated_at"] = now_beijing().strftime("%Y-%m-%d %H:%M")
        merged["updated_by"] = actor
        record["policy"] = normalize_policy(merged)
        return record["policy"]

    return update(group_id, apply)


# ── 审计 ────────────────────────────────────────────────────────────────


def audit(
    group_id: int,
    *,
    actor_id: int,
    actor_role: str | None,
    source: str,
    action: str,
    result: str,
    target_id: int | None = None,
    target_name: str | None = None,
    detail: str | None = None,
    error: str | None = None,
    reason: str | None = None,
) -> dict[str, Any]:
    """记一条操作（成功、失败、被拒都记）。只存摘要，不存公告全文以外的聊天内容。"""

    def apply(record: dict[str, Any]) -> dict[str, Any]:
        entry = {
            "id": int(record["next_id"]),
            "at": now_beijing().strftime("%Y-%m-%d %H:%M:%S"),
            "actor_id": str(actor_id),
            "actor_role": actor_role,
            "source": source,
            "action": action,
            "label": ACTIONS[action].label
            if action in ACTIONS
            else EXTRA_LABELS.get(action, action),
            "result": result,
        }
        optional = {
            "target_id": str(target_id) if target_id else None,
            "target_name": target_name,
            "detail": (detail or "")[:200] or None,
            "error": (error or "")[:160] or None,
            "reason": (reason or "")[:REASON_MAX_CHARS] or None,
        }
        entry.update({key: value for key, value in optional.items() if value})
        record["next_id"] = entry["id"] + 1
        record["audit"] = [*record["audit"], entry][-AUDIT_LIMIT:]
        return entry

    try:
        return update(group_id, apply)
    except OSError:
        dbg_exc(f"群 {group_id} 群管代办审计写入失败")
        return {}


def audit_rows(
    group_id: int,
    *,
    limit: int = 50,
    actor_id: int | None = None,
    action: str | None = None,
    result: str | None = None,
) -> list[dict[str, Any]]:
    rows = list(reversed(load(group_id)["audit"]))
    if actor_id is not None:
        rows = [row for row in rows if row.get("actor_id") == str(actor_id)]
    if action:
        rows = [row for row in rows if row.get("action") == action]
    if result:
        rows = [row for row in rows if row.get("result") == result]
    return rows[: max(1, min(limit, AUDIT_LIMIT))]


def _remember_sent(
    group_id: int, message_id: str, kind: str, actor_id: int, preview: str
) -> None:
    def apply(record: dict[str, Any]) -> None:
        cutoff = (now_beijing() - SENT_TTL).strftime("%Y-%m-%d %H:%M:%S")
        kept = [item for item in record["sent"] if str(item.get("at") or "") >= cutoff]
        kept.append(
            {
                "message_id": str(message_id),
                "kind": kind,
                "actor_id": str(actor_id),
                "at": now_beijing().strftime("%Y-%m-%d %H:%M:%S"),
                "preview": preview[:40],
            }
        )
        record["sent"] = kept[-SENT_LIMIT:]

    try:
        update(group_id, apply)
    except OSError:
        dbg_exc(f"群 {group_id} 群管代办记录已发消息失败")


def sent_messages(group_id: int) -> list[dict[str, Any]]:
    cutoff = (now_beijing() - SENT_TTL).strftime("%Y-%m-%d %H:%M:%S")
    return [
        item
        for item in reversed(load(group_id)["sent"])
        if str(item.get("at") or "") >= cutoff
    ]


def _require_sent(group_id: int, message_id: str) -> dict[str, Any]:
    for item in sent_messages(group_id):
        if item.get("message_id") == str(message_id):
            return item
    raise ManageError(
        "这条消息不是通过群管代办发出的",
        code="not_found",
        status=404,
        hint="撤回、设精华、设待办只能针对用 send/todo 发出的消息；先用 send 发一条",
    )


# ── 额度 ────────────────────────────────────────────────────────────────

_daily: dict[tuple[int, date], int] = {}
_burst: dict[int, list[float]] = {}


def used_today(user_id: int) -> int:
    return _daily.get((int(user_id), now_beijing().date()), 0)


def _reserve(user_id: int) -> None:
    today = now_beijing().date()
    key = (int(user_id), today)
    limit = daily_limit()
    if limit and _daily.get(key, 0) >= limit:
        raise ManageError(
            f"今天的群管代办次数用完了（每天 {limit} 次）",
            code="quota_exceeded",
            status=429,
            hint="明天再来，或者直接在 QQ 里操作",
        )
    now = time.monotonic()
    recent = [
        stamp for stamp in _burst.get(int(user_id), []) if now - stamp < BURST_WINDOW
    ]
    if len(recent) >= BURST_LIMIT:
        raise ManageError("操作太快了，歇一分钟再来", code="quota_exceeded", status=429)
    recent.append(now)
    _burst[int(user_id)] = recent
    for stale in [item for item in _daily if item[1] != today]:
        _daily.pop(stale, None)
    _daily[key] = _daily.get(key, 0) + 1


def _refund(user_id: int) -> None:
    key = (int(user_id), now_beijing().date())
    if _daily.get(key):
        _daily[key] -= 1


# ── 实时身份校验 ────────────────────────────────────────────────────────


@dataclass(slots=True)
class Access:
    group_id: int
    user_id: int
    user_role: str
    bot_role: str
    policy: dict[str, Any]
    actions: frozenset[str] = frozenset()
    bot: Any = None

    @property
    def is_owner(self) -> bool:
        return self.user_role == "owner"

    def allowed_actions(self) -> list[str]:
        """这个人此刻在这个群能用的写操作（策略 + 他的角色 + Bot 身份 + 协议端支持）。"""

        result = []
        for key in sorted(WRITE_ACTIONS):
            if action_block_reason(self, key) is None:
                result.append(key)
        return result

    def as_dict(self) -> dict[str, Any]:
        return {
            "group_id": str(self.group_id),
            "your_role": self.user_role,
            "bot_role": self.bot_role,
            "allowed_actions": self.allowed_actions(),
        }


def _superusers() -> set[str]:
    try:
        from nonebot import get_driver

        return {str(item) for item in get_driver().config.superusers}
    except Exception:  # noqa: BLE001
        return set()


async def live_role(bot: Any, group_id: int, user_id: int) -> str | None:
    try:
        role, _source = await _probe_member_role(bot, int(group_id), int(user_id))
    except Exception:  # noqa: BLE001
        dbg_exc(f"群 {group_id} 群管代办：读取 {user_id} 的角色失败")
        return None
    return str(role or "member")


async def verify(bot: Any, group_id: int, user_id: int, *, source: str) -> Access:
    """双重实时校验：他此刻是群主/管理员，Bot 此刻也是群主/管理员。"""

    if not enabled():
        raise ManageError("管理员关闭了群管代办", status=403)
    if bot is None:
        raise ManageError(
            "Bot 现在不在线，没法确认身份",
            code="unavailable",
            status=503,
            hint="等 Bot 上线后再试",
        )
    policy = policy_for(group_id)
    if not policy["enabled"]:
        raise ManageError(
            "这个群的群主关掉了群管代办", hint="可以请群主在网页「群管理」里打开"
        )
    if source == "private" and not policy["private_enabled"]:
        raise ManageError(
            "这个群不允许通过私聊代办群管理",
            hint="可以在网页上操作，或者直接在 QQ 里操作",
        )
    if source == "webui" and not policy["webui_enabled"]:
        raise ManageError(
            "这个群不允许通过网页代办群管理", hint="可以私聊 Bot 或者直接在 QQ 里操作"
        )
    role = await live_role(bot, group_id, user_id)
    if role is None:
        raise ManageError(
            "没法向 QQ 确认你在这个群的身份",
            code="unavailable",
            status=503,
            hint="稍后再试",
        )
    if role not in MANAGER_ROLES:
        raise ManageError(
            "你不是这个群的群主或管理员", hint="只有群主和管理员能让 Bot 代办群管理"
        )
    capabilities = await probe_group_capabilities(bot, group_id, refresh=True)
    if capabilities.role not in MANAGER_ROLES:
        raise ManageError(
            "Bot 在这个群不是管理员，做不了管理操作",
            hint="先在 QQ 里把 Bot 设为管理员",
        )
    return Access(
        group_id=int(group_id),
        user_id=int(user_id),
        user_role=role,
        bot_role=capabilities.role,
        policy=policy,
        actions=capabilities.actions,
        bot=bot,
    )


def action_block_reason(access: Access, action: str) -> str | None:
    """这个操作现在为什么不能做；能做返回 None。"""

    spec = ACTIONS.get(action)
    if spec is None:
        return "没有这个操作"
    if not spec.write:
        return None
    policy = access.policy
    if action in policy["disabled_actions"]:
        return f"这个群关掉了「{spec.label}」"
    if not access.is_owner and action not in policy["admin_actions"]:
        return f"这个群只允许群主用「{spec.label}」"
    if spec.bot_owner and access.bot_role != "owner":
        return f"「{spec.label}」需要 Bot 是群主"
    return None


def _check_action(access: Access, action: str) -> None:
    reason = action_block_reason(access, action)
    if reason:
        raise ManageError(reason, hint="可以请群主在网页「群管理 → 策略」里调整")


# ── 成员 ────────────────────────────────────────────────────────────────

_member_cache: dict[int, tuple[float, list[dict[str, Any]]]] = {}


def _member_view(raw: dict[str, Any]) -> dict[str, Any]:
    now = int(time.time())
    shut = 0
    try:
        shut = int(raw.get("shut_up_timestamp") or 0)
    except (TypeError, ValueError):
        shut = 0
    view = {
        "user_id": str(raw.get("user_id") or ""),
        "name": str(raw.get("card") or raw.get("nickname") or raw.get("user_id") or ""),
        "nickname": str(raw.get("nickname") or ""),
        "card": str(raw.get("card") or ""),
        "role": str(raw.get("role") or "member"),
    }
    if raw.get("title"):
        view["title"] = str(raw["title"])
    if shut > now:
        view["muted_until"] = time.strftime("%Y-%m-%d %H:%M", time.localtime(shut))
    return view


async def member_list(
    bot: Any, group_id: int, *, refresh: bool = False
) -> list[dict[str, Any]]:
    cached = _member_cache.get(int(group_id))
    now = time.monotonic()
    if cached and not refresh and now - cached[0] < MEMBER_CACHE_TTL:
        return cached[1]
    raw = await bot.call_api("get_group_member_list", group_id=int(group_id))
    rows = raw if isinstance(raw, list) else (raw or {}).get("members") or []
    members = [_member_view(item) for item in rows if isinstance(item, dict)]
    _member_cache[int(group_id)] = (now, members)
    return members


def search_members(
    members: list[dict[str, Any]], query: str | None, *, limit: int = 20
) -> list[dict[str, Any]]:
    needle = str(query or "").strip().casefold()
    if not needle:
        order = {"owner": 0, "admin": 1}
        rows = sorted(
            members, key=lambda item: (order.get(item["role"], 2), item["name"])
        )
        return rows[:limit]
    if needle.isdigit():
        exact = [item for item in members if item["user_id"] == needle]
        if exact:
            return exact
    exact = [
        item
        for item in members
        if needle in {item["card"].casefold(), item["nickname"].casefold()}
    ]
    if exact:
        return exact[:limit]
    return [
        item
        for item in members
        if needle in item["card"].casefold()
        or needle in item["nickname"].casefold()
        or needle in item["user_id"]
    ][:limit]


async def resolve_member(bot: Any, group_id: int, raw: str | None) -> dict[str, Any]:
    text = str(raw or "").strip().lstrip("@")
    if not text:
        raise ManageError(
            "没说是哪位成员",
            code="invalid_args",
            status=422,
            hint="member 写群名片、昵称或 QQ 号",
        )
    members = await member_list(bot, group_id)
    hits = search_members(members, text, limit=6)
    if not hits:
        members = await member_list(bot, group_id, refresh=True)
        hits = search_members(members, text, limit=6)
    if not hits:
        raise ManageError(
            f"群里没找到「{text}」",
            code="not_found",
            status=404,
            hint="换个名字或直接给 QQ 号",
        )
    if len(hits) > 1:
        names = "、".join(f"{item['name']}({item['user_id']})" for item in hits[:5])
        raise ManageError(
            f"有好几个人匹配「{text}」：{names}",
            code="ambiguous",
            status=409,
            hint="问他具体是哪一位，用 QQ 号指定",
        )
    return hits[0]


async def _check_target(access: Access, member: dict[str, Any], action: str) -> int:
    bot = access.bot
    target_id = int(member["user_id"])
    if target_id == int(getattr(bot, "self_id", 0) or 0):
        raise ManageError("不能对 Bot 自己做这个操作")
    if target_id == access.user_id and action in {"mute", "kick"}:
        raise ManageError("不能对你自己做这个操作", hint="真要这样请直接在 QQ 里操作")
    if (
        protect_superusers()
        and str(target_id) in _superusers()
        and action in {"mute", "kick", "card", "title"}
    ):
        raise ManageError("这位是 Bot 的维护者，不能通过 Bot 处置")
    if action == "card" and target_id == access.user_id:
        return target_id
    if not await target_can_be_muted(bot, access.group_id, target_id, access.bot_role):
        raise ManageError(
            f"Bot 的身份不够处置{ROLE_LABELS.get(member['role'], '这位成员')}",
            hint="群主不能被处置；处置管理员需要 Bot 是群主",
        )
    if not await actor_can_act_on_target(
        bot, access.group_id, access.user_id, target_id
    ):
        raise ManageError(
            "你没有权限处置这位成员",
            hint="管理员不能处置群主和其他管理员；处置管理员需要群主本人",
        )
    return target_id


# ── 待确认（私聊）──────────────────────────────────────────────────────


@dataclass(slots=True)
class Pending:
    id: str
    user_id: int
    group_id: int
    group_name: str
    action: str
    args: dict[str, Any]
    summary: str
    created: float
    arrival: int
    expires: float = field(default=0.0)


_pending: dict[str, Pending] = {}


def _arrivals(user_id: int) -> int:
    try:
        from ..chat_state import arrival_count

        return int(arrival_count(user_id))
    except Exception:  # noqa: BLE001
        return 0


def _gc_pending() -> None:
    now = time.monotonic()
    for key in [key for key, item in _pending.items() if item.expires <= now]:
        _pending.pop(key, None)


def create_pending(
    user_id: int,
    group_id: int,
    group_name: str,
    action: str,
    args: dict[str, Any],
    summary: str,
) -> Pending:
    _gc_pending()
    same = find_pending(user_id, group_id, action, args)
    if same is not None:
        # 同一件事已经在等他确认：沿用原来的 confirm_id 和“问他时”的消息计数。
        # 否则模型下一轮（看不到上一轮的 confirm_id）重新发起时会生成新的待确认，
        # 新的计数把他那句“好”算进去了，永远确认不了——私聊里就会一直“要发吗？”“发”循环。
        return same
    mine = sorted(
        (item for item in _pending.values() if item.user_id == int(user_id)),
        key=lambda item: item.created,
    )
    for stale in mine[: max(0, len(mine) - MAX_PENDING_PER_USER + 1)]:
        _pending.pop(stale.id, None)
    pending_id = "c" + secrets.token_hex(2)
    while pending_id in _pending:
        pending_id = "c" + secrets.token_hex(2)
    now = time.monotonic()
    item = Pending(
        id=pending_id,
        user_id=int(user_id),
        group_id=int(group_id),
        group_name=group_name,
        action=action,
        args=dict(args),
        summary=summary,
        created=now,
        arrival=_arrivals(user_id),
        expires=now + confirm_seconds(),
    )
    _pending[pending_id] = item
    return item


def _same_args(left: dict[str, Any], right: dict[str, Any]) -> bool:
    # reason 只是记录用的，不影响“是不是同一件事”
    def core(raw: dict[str, Any]) -> dict[str, Any]:
        return {key: value for key, value in raw.items() if key != "reason"}

    return core(left) == core(right)


def find_pending(
    user_id: int, group_id: int, action: str, args: dict[str, Any]
) -> Pending | None:
    """他名下内容完全相同、还没过期的待确认操作。"""

    _gc_pending()
    for item in _pending.values():
        if (
            item.user_id == int(user_id)
            and item.group_id == int(group_id)
            and item.action == action
            and _same_args(item.args, args)
        ):
            return item
    return None


def replied_since(item: Pending) -> bool:
    """问过他之后，他有没有发来新消息（有才允许 confirm）。"""

    return _arrivals(item.user_id) > item.arrival


def pending_view(user_id: int) -> list[dict[str, Any]]:
    """给私聊本轮资料用：还在等他确认的群管操作（最多 3 条，最新的在前）。"""

    now = time.monotonic()
    rows = sorted(pending_for(user_id), key=lambda item: item.created, reverse=True)
    return [
        {
            "confirm_id": item.id,
            "summary": item.summary,
            "asked_seconds_ago": int(max(0.0, now - item.created)),
            "expires_in_seconds": int(max(0.0, item.expires - now)),
            "he_replied_since": replied_since(item),
        }
        for item in rows[:3]
    ]


def pending_for(user_id: int) -> list[Pending]:
    _gc_pending()
    return [item for item in _pending.values() if item.user_id == int(user_id)]


def take_pending(user_id: int, pending_id: str) -> Pending:
    _gc_pending()
    item = _pending.get(str(pending_id or "").strip())
    if item is None or item.user_id != int(user_id):
        # 没带 confirm_id（上一轮的工具结果不在历史里）：他名下只有一条在等时就是它。
        # 带了却对不上时不猜，免得把另一件事当成他同意的这件。
        mine = pending_for(user_id) if not str(pending_id or "").strip() else []
        item = mine[0] if len(mine) == 1 else None
    if item is None:
        raise ManageError(
            "没有这个待确认的操作（可能已过期或已处理）",
            code="not_found",
            status=404,
            hint="重新发起一次，等他确认后再用 confirm",
        )
    if _arrivals(user_id) <= item.arrival:
        raise ManageError(
            "还没等到他的确认",
            code="needs_confirm",
            status=409,
            hint="先把操作内容原样问他，等他回复同意后（下一轮）再 confirm；不要替他确认",
        )
    _pending.pop(item.id, None)
    return item


def cancel_pending(user_id: int, pending_id: str | None) -> int:
    _gc_pending()
    if pending_id:
        item = _pending.get(pending_id)
        if item is None or item.user_id != int(user_id):
            return 0
        _pending.pop(pending_id, None)
        return 1
    mine = [key for key, item in _pending.items() if item.user_id == int(user_id)]
    for key in mine:
        _pending.pop(key, None)
    return len(mine)


# ── 文本清理 ────────────────────────────────────────────────────────────

_MEANINGFUL_RE = re.compile(r"[\w一-鿿]")
_CQ_RE = re.compile(r"\[CQ:[^\]]*\]", re.IGNORECASE)


def clean_text(raw: Any, *, limit: int, label: str, multiline: bool = True) -> str:
    text = _CQ_RE.sub("", str(raw or "")).replace("\r\n", "\n").strip()
    if not multiline:
        text = " ".join(text.split())
    if not _MEANINGFUL_RE.search(text):
        raise ManageError(
            f"{label}是空的", code="invalid_args", status=422, hint=f"先和他确认{label}"
        )
    if len(text) > limit:
        raise ManageError(
            f"{label}太长了（最多 {limit} 字）",
            code="invalid_args",
            status=422,
            hint="请他精简一下",
        )
    return text


def _minutes(args: dict[str, Any], access: Access) -> int:
    try:
        minutes = int(args.get("minutes") or 10)
    except (TypeError, ValueError) as exc:
        raise ManageError("禁言时长不对", code="invalid_args", status=422) from exc
    cap = int(access.policy["max_mute_minutes"])
    if minutes < 1:
        raise ManageError(
            "禁言至少 1 分钟", code="invalid_args", status=422, hint="解除禁言用 unmute"
        )
    if minutes > cap:
        raise ManageError(
            f"这个群一次最多禁言 {format_minutes(cap)}",
            code="invalid_args",
            status=422,
            hint="群主可以在网页「群管理 → 策略」里调上限",
        )
    return minutes


def format_minutes(minutes: int) -> str:
    if minutes % 1440 == 0:
        return f"{minutes // 1440} 天"
    if minutes % 60 == 0:
        return f"{minutes // 60} 小时"
    return f"{minutes} 分钟"


def _message_id(args: dict[str, Any]) -> str:
    value = str(args.get("message_id") or "").strip()
    if not value or not value.lstrip("-").isdigit():
        raise ManageError(
            "message_id 不对",
            code="invalid_args",
            status=422,
            hint="用 send/todo 返回的 message_id",
        )
    return value


def _unsupported(exc: BaseException) -> bool:
    text = str(exc).lower()
    info = getattr(exc, "info", None)
    if isinstance(info, dict):
        text += " " + " ".join(str(value).lower() for value in info.values())
    return any(
        marker in text
        for marker in (
            "not found",
            "unknown action",
            "不支持",
            "不存在",
            "404",
            "packet",
        )
    )


# ── 预览（确认用的一句话）──────────────────────────────────────────────


async def describe(
    access: Access, action: str, args: dict[str, Any], group_name: str
) -> tuple[str, dict[str, Any]]:
    """校验参数、确定目标，返回给人看的一句话和规范化后的参数（确认后照此执行）。"""

    _check_action(access, action)
    spec = ACTIONS[action]
    normalized: dict[str, Any] = {}
    reason = str(args.get("reason") or "").strip()[:REASON_MAX_CHARS]
    if reason:
        normalized["reason"] = reason
    where = f"在「{group_name}」"
    if spec.target:
        member = await resolve_member(access.bot, access.group_id, args.get("member"))
        target_id = await _check_target(access, member, action)
        normalized["target_id"] = target_id
        normalized["target_name"] = member["name"]
        who = f"{member['name']}（{target_id}）"
        if action == "mute":
            minutes = _minutes(args, access)
            normalized["minutes"] = minutes
            return f"{where}禁言 {who} {format_minutes(minutes)}", normalized
        if action == "unmute":
            return f"{where}解除 {who} 的禁言", normalized
        if action == "card":
            card = str(args.get("text") or args.get("content") or "").strip()
            card = " ".join(card.split())
            if len(card) > CARD_MAX_CHARS:
                raise ManageError(
                    f"群名片最多 {CARD_MAX_CHARS} 字", code="invalid_args", status=422
                )
            normalized["card"] = card
            return (
                f"{where}把 {who} 的群名片改成「{card}」"
                if card
                else f"{where}清空 {who} 的群名片"
            ), normalized
        if action == "title":
            title = " ".join(str(args.get("text") or args.get("content") or "").split())
            if len(title.encode("utf-8")) > TITLE_MAX_BYTES:
                raise ManageError(
                    "专属头衔最多 18 字节（约 6 个汉字）",
                    code="invalid_args",
                    status=422,
                )
            normalized["title"] = title
            return (
                f"{where}给 {who} 设专属头衔「{title}」"
                if title
                else f"{where}去掉 {who} 的专属头衔"
            ), normalized
        if action == "kick":
            normalized["reject"] = args.get("reject") is True
            extra = "，并拒绝他再申请加群" if normalized["reject"] else ""
            return f"{where}把 {who} 移出群聊{extra}（不可撤回）", normalized
    if action == "whole_mute":
        enable = args.get("enable") is not False
        normalized["enable"] = enable
        return f"{where}{'开启' if enable else '关闭'}全员禁言", normalized
    if action == "announce":
        content = clean_text(
            args.get("content") or args.get("text"),
            limit=ANNOUNCE_MAX_CHARS,
            label="公告内容",
        )
        if looks_like_instruction(content):
            raise ManageError(
                "公告内容像是给 AI 的指令，没有发布", code="invalid_args", status=422
            )
        normalized["content"] = content
        normalized["pinned"] = args.get("pinned") is True
        normalized["confirm_required"] = args.get("confirm_required") is True
        flags = "，置顶" if normalized["pinned"] else ""
        flags += "，需要群员确认" if normalized["confirm_required"] else ""
        preview = content if len(content) <= 80 else content[:80] + "…"
        return f"{where}发群公告{flags}：「{preview}」", normalized
    if action == "delete_notice":
        notice_id = str(args.get("notice_id") or "").strip()
        if not notice_id:
            raise ManageError(
                "没说删哪条公告",
                code="invalid_args",
                status=422,
                hint="先用 notices 查 notice_id",
            )
        notices = await list_notices(access)
        target = next(
            (item for item in notices if str(item.get("notice_id")) == notice_id), None
        )
        if target is None:
            raise ManageError(
                "这个群没有这条公告",
                code="not_found",
                status=404,
                hint="先用 notices 查一下",
            )
        normalized["notice_id"] = notice_id
        preview = str(target.get("content") or "")[:40]
        return f"{where}删除公告「{preview}」", normalized
    if action in {"send", "todo"}:
        if action == "todo" and args.get("message_id"):
            message_id = _message_id(args)
            item = _require_sent(access.group_id, message_id)
            normalized["message_id"] = message_id
            return f"{where}把 Bot 发的「{item.get('preview')}」设为群待办", normalized
        text = clean_text(
            args.get("text") or args.get("content"),
            limit=SEND_MAX_CHARS,
            label="消息内容",
        )
        if looks_like_instruction(text):
            raise ManageError(
                "内容像是给 AI 的指令，没有发送", code="invalid_args", status=422
            )
        normalized["text"] = text
        preview = text if len(text) <= 80 else text[:80] + "…"
        if action == "todo":
            return (
                f"{where}以 Bot 身份发一条消息并设为群待办：「{preview}」",
                normalized,
            )
        return f"{where}以 Bot 身份发消息：「{preview}」", normalized
    if action in {"todo_done", "todo_cancel", "essence", "unessence", "recall"}:
        message_id = _message_id(args)
        item = _require_sent(access.group_id, message_id)
        normalized["message_id"] = message_id
        return f"{where}{spec.label}：「{item.get('preview')}」", normalized
    if action == "rename":
        name = clean_text(
            args.get("text") or args.get("content"),
            limit=GROUP_NAME_MAX_CHARS,
            label="新群名",
            multiline=False,
        )
        normalized["name"] = name
        return f"把「{group_name}」改名为「{name}」", normalized
    raise ManageError("没有这个操作", code="invalid_args", status=422)


# ── 执行 ────────────────────────────────────────────────────────────────


async def list_notices(access: Access) -> list[dict[str, Any]]:
    from .tool_handlers.notice import (
        _fetch_notices,  # pyright: ignore[reportPrivateUsage]
    )

    notices = await _fetch_notices(access.bot, access.group_id)
    return [
        {key: value for key, value in item.items() if key != "_sort_time"}
        for item in notices
    ]


async def list_muted(access: Access) -> list[dict[str, Any]]:
    members = await member_list(access.bot, access.group_id, refresh=True)
    return [item for item in members if item.get("muted_until")]


async def execute(
    access: Access,
    action: str,
    normalized: dict[str, Any],
    *,
    source: str,
    actor_name: str | None = None,
) -> dict[str, Any]:
    """执行一次写操作：再校验一次、扣额度、调 OneBot、记审计。失败抛 ManageError。"""

    _check_action(access, action)
    bot = access.bot
    group_id = access.group_id
    target_id = normalized.get("target_id")
    target_int = int(target_id or 0)
    target_name = normalized.get("target_name")
    if target_id:
        # 确认那一刻对方身份可能变了（比如刚被设为管理员），再查一遍。
        member = {
            "user_id": str(target_id),
            "name": target_name or str(target_id),
            "role": await live_role(bot, group_id, target_int) or "member",
        }
        await _check_target(access, member, action)
    _reserve(access.user_id)
    detail = ""
    result: dict[str, Any] = {"done": True, "action": action}
    try:
        if action == "mute":
            minutes = int(normalized["minutes"])
            await bot.call_api(
                "set_group_ban",
                group_id=group_id,
                user_id=target_int,
                duration=minutes * 60,
            )
            until = now_beijing() + timedelta(minutes=minutes)
            result.update(
                {
                    "member": target_name,
                    "minutes": minutes,
                    "until": until.strftime("%m-%d %H:%M"),
                }
            )
            detail = f"{format_minutes(minutes)}，到 {result['until']}"
        elif action == "unmute":
            await bot.call_api(
                "set_group_ban", group_id=group_id, user_id=target_int, duration=0
            )
            result["member"] = target_name
        elif action == "whole_mute":
            enable = bool(normalized["enable"])
            await bot.call_api("set_group_whole_ban", group_id=group_id, enable=enable)
            result["whole_mute"] = enable
            detail = "开启" if enable else "关闭"
        elif action == "announce":
            name = (
                "_send_group_notice"
                if "_send_group_notice" in access.actions
                or "send_group_notice" not in access.actions
                else "send_group_notice"
            )
            params: dict[str, Any] = {
                "group_id": group_id,
                "content": normalized["content"],
            }
            if normalized.get("pinned"):
                params["pinned"] = 1
            if normalized.get("confirm_required"):
                params["confirm_required"] = 1
            raw = await bot.call_api(name, **params)
            from .tool_handlers.notice import (
                _notice_id_from_result,  # pyright: ignore[reportPrivateUsage]
            )

            notice_id = _notice_id_from_result(raw)
            if notice_id:
                result["notice_id"] = notice_id
            detail = normalized["content"][:120]
        elif action == "delete_notice":
            await bot.call_api(
                "_del_group_notice",
                group_id=group_id,
                notice_id=normalized["notice_id"],
            )
            detail = f"notice_id={normalized['notice_id']}"
        elif action in {"send", "todo"} and normalized.get("text"):
            raw = await bot.call_api(
                "send_group_msg", group_id=group_id, message=normalized["text"]
            )
            message_id = (
                str((raw or {}).get("message_id") or "")
                if isinstance(raw, dict)
                else str(raw or "")
            )
            result["message_id"] = message_id or None
            detail = normalized["text"][:120]
            if message_id:
                _remember_sent(
                    group_id, message_id, action, access.user_id, normalized["text"]
                )
            if action == "todo":
                if not message_id:
                    raise ManageError(
                        "消息发出去了，但没拿到消息编号，设不了待办",
                        code="tool_failed",
                        status=502,
                    )
                await _todo(bot, "set_group_todo", group_id, message_id, sent=True)
                result["todo"] = True
        elif action == "todo":
            await _todo(
                bot, "set_group_todo", group_id, normalized["message_id"], sent=False
            )
            result["todo"] = True
            detail = f"message_id={normalized['message_id']}"
        elif action in {"todo_done", "todo_cancel"}:
            api = (
                "complete_group_todo" if action == "todo_done" else "cancel_group_todo"
            )
            await _todo(bot, api, group_id, normalized["message_id"], sent=False)
            detail = f"message_id={normalized['message_id']}"
        elif action in {"essence", "unessence"}:
            api = "set_essence_msg" if action == "essence" else "delete_essence_msg"
            await bot.call_api(api, message_id=int(normalized["message_id"]))
            detail = f"message_id={normalized['message_id']}"
        elif action == "recall":
            await bot.call_api("delete_msg", message_id=int(normalized["message_id"]))
            detail = f"message_id={normalized['message_id']}"
        elif action == "card":
            await bot.call_api(
                "set_group_card",
                group_id=group_id,
                user_id=target_int,
                card=normalized["card"],
            )
            result.update({"member": target_name, "card": normalized["card"]})
            detail = normalized["card"] or "（清空）"
        elif action == "title":
            await bot.call_api(
                "set_group_special_title",
                group_id=group_id,
                user_id=target_int,
                special_title=normalized["title"],
            )
            result.update({"member": target_name, "title": normalized["title"]})
            detail = normalized["title"] or "（去掉）"
        elif action == "kick":
            await bot.call_api(
                "set_group_kick",
                group_id=group_id,
                user_id=target_int,
                reject_add_request=bool(normalized.get("reject")),
            )
            result["member"] = target_name
            detail = "并拒绝再加群" if normalized.get("reject") else ""
            _member_cache.pop(group_id, None)
        elif action == "rename":
            await bot.call_api(
                "set_group_name", group_id=group_id, group_name=normalized["name"]
            )
            result["group_name"] = normalized["name"]
            detail = normalized["name"]
        else:
            raise ManageError("没有这个操作", code="invalid_args", status=422)
    except ManageError as exc:
        _refund(access.user_id)
        audit(
            group_id,
            actor_id=access.user_id,
            actor_role=access.user_role,
            source=source,
            action=action,
            result="failed",
            target_id=target_id,
            target_name=target_name,
            error=str(exc),
            reason=normalized.get("reason"),
        )
        raise
    except Exception as exc:
        _refund(access.user_id)
        dbg_exc(f"群 {group_id} 群管代办 {action} 失败")
        audit(
            group_id,
            actor_id=access.user_id,
            actor_role=access.user_role,
            source=source,
            action=action,
            result="failed",
            target_id=target_id,
            target_name=target_name,
            error=type(exc).__name__,
            reason=normalized.get("reason"),
        )
        raise ManageError(
            f"QQ 那边没办成：{str(exc)[:80] or type(exc).__name__}",
            code="tool_failed",
            status=502,
            hint="照实告诉他，稍后再试或直接在 QQ 里操作",
        ) from exc
    if action in {"mute", "unmute"}:
        _member_cache.pop(group_id, None)
    entry = audit(
        group_id,
        actor_id=access.user_id,
        actor_role=access.user_role,
        source=source,
        action=action,
        result="success",
        target_id=target_id,
        target_name=target_name,
        detail=detail,
        reason=normalized.get("reason"),
    )
    result["audit_id"] = entry.get("id")
    dbg(f"群 {group_id} 群管代办 {action} 成功（{source}，发起人 {access.user_id}）")
    if access.policy.get("notify_group") and action in {
        "mute",
        "unmute",
        "whole_mute",
        "kick",
        "rename",
        "title",
    }:
        await _notify_group(access, action, target_name, actor_name, detail)
    return result


async def _todo(
    bot: Any, api: str, group_id: int, message_id: str, *, sent: bool
) -> None:
    try:
        await bot.call_api(api, group_id=str(group_id), message_id=str(message_id))
    except Exception as exc:
        if _unsupported(exc):
            raise ManageError(
                ("消息已经发到群里了，但" if sent else "") + "协议端不支持群待办",
                code="unsupported",
                status=501,
                hint="群待办需要较新的 NapCat（set_group_todo）并启用 Packet 后端；可以改成置顶群公告",
            ) from exc
        raise


async def _notify_group(
    access: Access,
    action: str,
    target_name: str | None,
    actor_name: str | None,
    detail: str,
) -> None:
    who = actor_name or ROLE_LABELS.get(access.user_role, "管理员")
    label = ACTIONS[action].label
    text = (
        f"〔群管代办〕{who} 让我{label}"
        + (f"：{target_name}" if target_name else "")
        + (f"（{detail}）" if detail else "")
    )
    try:
        await access.bot.call_api(
            "send_group_msg", group_id=access.group_id, message=text[:120]
        )
    except Exception:  # noqa: BLE001
        dbg_exc(f"群 {access.group_id} 群管代办说明发送失败（忽略）")


def deny(
    group_id: int,
    user_id: int,
    action: str,
    source: str,
    error: str,
    role: str | None = None,
) -> None:
    """被拒的写操作也记一笔（不扣额度），方便群主看到谁试过什么。"""

    if action in WRITE_ACTIONS:
        audit(
            group_id,
            actor_id=user_id,
            actor_role=role,
            source=source,
            action=action,
            result="denied",
            error=error,
        )


# ── 候选群（数据库角色，还没实时确认）─────────────────────────────────

_manager_hint: dict[int, bool] = {}


async def candidate_groups(session: Any, user_id: int) -> list[tuple[int, str, str]]:
    """数据库里他是群主/管理员的群：(群号, 群名, 角色)。只是候选，执行前一律实时确认。"""

    from sqlalchemy import select

    from ..data_models.bot_group import BotGroup
    from ..data_models.user_group import UserGroup

    rows = (
        await session.execute(
            select(UserGroup.group_id, BotGroup.group_name, UserGroup.role)
            .join(BotGroup, BotGroup.group_id == UserGroup.group_id)
            .where(
                UserGroup.user_id == int(user_id),
                UserGroup.role.in_(sorted(MANAGER_ROLES)),
            )
            .order_by(UserGroup.group_id)
        )
    ).all()
    result = [
        (int(gid), str(name or f"群 {gid}"), str(role)) for gid, name, role in rows
    ]
    _manager_hint[int(user_id)] = bool(result)
    return result


def manager_hint(user_id: int) -> bool:
    """上一次查到他是不是某个群的群主/管理员（只决定给不给工具，不决定能不能做）。"""

    return enabled() and _manager_hint.get(int(user_id), False)


async def refresh_manager_hint(session: Any, user_id: int) -> bool:
    if not enabled():
        return False
    try:
        await candidate_groups(session, user_id)
    except Exception:  # noqa: BLE001
        dbg_exc(f"群管代办：查询 {user_id} 的候选群失败")
    return manager_hint(user_id)


def reset_for_tests() -> None:
    _manager_hint.clear()
    _pending.clear()
    _daily.clear()
    _burst.clear()
    _member_cache.clear()


__all__ = [
    "ACTIONS",
    "DEFAULT_ADMIN_ACTIONS",
    "DEFAULT_CONFIRM",
    "READ_ACTIONS",
    "WRITE_ACTIONS",
    "Access",
    "ManageError",
    "Pending",
    "action_block_reason",
    "audit",
    "audit_rows",
    "cancel_pending",
    "candidate_groups",
    "create_pending",
    "default_policy",
    "deny",
    "describe",
    "execute",
    "find_pending",
    "list_muted",
    "list_notices",
    "load",
    "manager_hint",
    "member_list",
    "pending_for",
    "pending_view",
    "policy_for",
    "refresh_manager_hint",
    "replied_since",
    "search_members",
    "sent_messages",
    "set_policy",
    "take_pending",
    "used_today",
    "verify",
]
