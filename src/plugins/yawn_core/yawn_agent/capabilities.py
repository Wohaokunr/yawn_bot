# ruff: noqa: E501,F401,I001,TID252,PLR0912,PLR0913,PLR0917,C901,TRY003,TRY004,TRY300,TRY301,ASYNC240,UP035,PGH004,ANN001,ANN201,ANN202,ARG001,FBT001,FBT002,COM812,RUF001,RUF100
"""OneBot action 能力和机器人权限探测。"""

from __future__ import annotations

from dataclasses import dataclass
import os
import time
from typing import Any

from ..metrics import record_agent_capability_probe
from .log import dbg, dbg_exc


@dataclass(frozen=True, slots=True)
class BotGroupCapabilities:
    role: str
    can_manage: bool
    actions: frozenset[str]

    def has(self, action: str) -> bool:
        return action in self.actions


# Message/MessageSegment 是 NoneBot 对协议消息的公共抽象。这里不把 segment
# 混进 action 能力：action 仍由 BotGroupCapabilities 管，segment 单独维护兼容矩阵。
COMMON_MESSAGE_SEGMENTS = frozenset(
    {
        "text",
        "reply",
        "at",
        "face",
        "reaction",
        "image",
        "record",
        "video",
        "rps",
        "dice",
        "poke",
    }
)
OPTIONAL_MESSAGE_SEGMENTS = frozenset({"share", "contact", "location", "music"})
FORBIDDEN_MESSAGE_SEGMENTS = frozenset(
    {"xml", "json", "anonymous", "at_all", "raw_cq", "cq"}
)

# 失败时优先怀疑兼容性较差的段；没有精确信息时只记一个候选，避免一次
# 复合消息失败就把 reply/face/image 等所有能力永久误判为不支持。
_SEGMENT_DEGRADE_PRIORITY = (
    "share",
    "contact",
    "location",
    "music",
    "poke",
    "record",
    "video",
    "reaction",
    "reply",
    "face",
    "rps",
    "dice",
    "image",
    "at",
)
_SEGMENT_UNSUPPORTED_TTL = 10 * 60.0
_MAX_SEGMENT_CACHE_ENTRIES = 512
_segment_unsupported_cache: dict[tuple[int, int, str], float] = {}
_segment_failure_status: dict[tuple[int, int, str], dict[str, Any]] = {}


@dataclass(frozen=True, slots=True)
class MessageSegmentCapabilities:
    """当前 bot/group 的消息段能力视图。"""

    allowed: frozenset[str]
    supported: frozenset[str]
    runtime_unsupported: frozenset[str]
    forbidden: frozenset[str] = FORBIDDEN_MESSAGE_SEGMENTS

    @property
    def exposed_types(self) -> frozenset[str]:
        """可以暴露给 LLM 的段类型。"""

        return self.supported - self.runtime_unsupported

    def can_expose(self, segment_type: str) -> bool:
        return segment_type in self.exposed_types

    def is_allowed(self, segment_type: str) -> bool:
        return segment_type in self.allowed and segment_type not in self.forbidden


_CAPABILITY_TTL = 60.0
# 探测失败只是瞬时抖动的概率很高，降级结果只短暂缓存，
# 避免一次 API 失败把管理工具锁死整整一分钟。
_DEGRADED_TTL = 5.0
_MAX_CACHE_ENTRIES = 256
_capability_cache: dict[tuple[int, int], tuple[float, BotGroupCapabilities, float]] = {}
_capability_probe_status: dict[tuple[int, int], dict[str, Any]] = {}


async def probe_group_capabilities(
    bot: Any, group_id: int, *, refresh: bool = False
) -> BotGroupCapabilities:
    """读取机器人自身群角色；API 不可用时按普通成员降级。"""

    key = (int(getattr(bot, "self_id", 0) or 0), int(group_id))
    cached = _capability_cache.get(key)
    now = time.monotonic()
    if cached is not None and not refresh and now - cached[0] < cached[2]:
        dbg(f"群 {group_id} 能力探测命中缓存: role={cached[1].role!r} key={key}")
        return cached[1]
    dbg(f"群 {group_id} 能力探测缓存未命中,发起 API 探测(refresh={refresh})")
    role = "member"
    degraded = False
    self_id = int(getattr(bot, "self_id", 0) or 0)
    try:
        info = await bot.call_api(
            "get_group_member_info", group_id=group_id, user_id=self_id
        )
        if isinstance(info, dict):
            role = str(info.get("role") or "member")
    except Exception:  # noqa: BLE001
        degraded = True
        error_class = "probe_failed"
        record_agent_capability_probe("degraded")
        dbg_exc(f"群 {group_id} 能力探测失败,按普通成员降级(短 TTL {_DEGRADED_TTL}s)")
    else:
        error_class = None
        record_agent_capability_probe("success")
    actions = {
        "send_group_msg",
        "get_group_info",
        "get_group_member_info",
        "get_group_member_list",
        "get_msg",
        "get_forward_msg",
        "get_group_msg_history",
        "get_group_honor_info",
        "get_essence_msg_list",
        "_get_group_notice",
        "get_group_shut_list",
        "get_group_root_files",
        "get_group_files_by_folder",
        "get_group_file_url",
        "set_msg_emoji_like",
        "send_group_forward_msg",
        # OneBot adapters may allow file delivery for ordinary members; the
        # executor still validates path/domain/size before sending.
        "upload_group_file",
    }
    if role in {"owner", "admin"}:
        # NapCat/go-cqhttp 的群公告接口是扩展 action `_send_group_notice`。
        # `send_group_notice` 仅在适配器显式声明支持时加入，避免在没有
        # supported_actions 元数据时把不存在的别名误判为可用。
        actions.update(
            {
                "set_group_ban",
                "_send_group_notice",
                "set_essence_msg",
                "delete_essence_msg",
                "_del_group_notice",
                "set_group_card",
                "set_group_name",
                "set_group_special_title",
                "create_group_file_folder",
                "set_group_kick",
                "set_group_whole_ban",
                "set_group_admin",
                "delete_group_file",
                "move_group_file",
                "rename_group_file",
                "delete_group_folder",
            }
        )
    declared = getattr(bot, "supported_actions", None)
    if isinstance(declared, (set, frozenset, list, tuple)):
        declared_actions = {str(item) for item in declared}
        actions.intersection_update(declared_actions)
        if role in {"owner", "admin"} and "send_group_notice" in declared_actions:
            actions.add("send_group_notice")
    result = BotGroupCapabilities(role, role in {"owner", "admin"}, frozenset(actions))
    if len(_capability_cache) >= _MAX_CACHE_ENTRIES:
        oldest = min(_capability_cache, key=lambda item: _capability_cache[item][0])
        _capability_cache.pop(oldest, None)
        _capability_probe_status.pop(oldest, None)
    _capability_cache[key] = (
        now,
        result,
        _DEGRADED_TTL if degraded else _CAPABILITY_TTL,
    )
    _capability_probe_status[key] = {
        "degraded": degraded,
        "last_error": error_class,
        "probed_at": time.time(),
        "ttl": _DEGRADED_TTL if degraded else _CAPABILITY_TTL,
    }
    dbg(
        f"群 {group_id} 能力探测完成: role={result.role!r} can_manage={result.can_manage} "
        f"actions={sorted(result.actions)} degraded={degraded}"
    )
    return result


def _optional_segments_enabled() -> frozenset[str]:
    raw = os.environ.get("AGENT_OPTIONAL_MESSAGE_SEGMENTS", "")
    return frozenset(
        item
        for item in (part.strip().lower() for part in raw.split(","))
        if item in OPTIONAL_MESSAGE_SEGMENTS
    )


def default_allowed_segment_types() -> frozenset[str]:
    """本地策略允许的段；不包含后端运行时能力判断。"""

    return COMMON_MESSAGE_SEGMENTS | _optional_segments_enabled()


def _declared_message_segments(bot: Any) -> frozenset[str] | None:
    """读取适配器/测试桩显式声明；没有声明时按 OneBot 常用段乐观开放。"""

    declared = getattr(bot, "supported_message_segments", None)
    if declared is None:
        declared = getattr(bot, "supported_segments", None)
    if not isinstance(declared, (set, frozenset, list, tuple)):
        return None
    return frozenset(str(item).strip().lower() for item in declared if str(item).strip())


def get_segment_capabilities(bot: Any, group_id: int) -> MessageSegmentCapabilities:
    """构造 segment 能力矩阵，不发网络请求。"""

    allowed = default_allowed_segment_types()
    declared = _declared_message_segments(bot)
    supported = allowed if declared is None else allowed & declared
    now = time.monotonic()
    bot_id = int(getattr(bot, "self_id", 0) or 0)
    runtime_unsupported: set[str] = set()
    expired: list[tuple[int, int, str]] = []
    for key, expires_at in _segment_unsupported_cache.items():
        if expires_at <= now:
            expired.append(key)
            continue
        key_bot_id, key_group_id, segment_type = key
        if key_bot_id == bot_id and key_group_id == int(group_id):
            runtime_unsupported.add(segment_type)
    for key in expired:
        _segment_unsupported_cache.pop(key, None)
        _segment_failure_status.pop(key, None)
    return MessageSegmentCapabilities(
        allowed=frozenset(allowed),
        supported=frozenset(supported),
        runtime_unsupported=frozenset(runtime_unsupported),
    )


def mark_segment_unsupported(
    bot: Any,
    group_id: int,
    segment_type: str,
    *,
    reason: str = "runtime_unsupported",
) -> None:
    """把一次确定/推断的协议不兼容短期记住，后续直接走降级。"""

    normalized = str(segment_type).strip().lower()
    if normalized not in COMMON_MESSAGE_SEGMENTS | OPTIONAL_MESSAGE_SEGMENTS:
        return
    if len(_segment_unsupported_cache) >= _MAX_SEGMENT_CACHE_ENTRIES:
        oldest = min(_segment_unsupported_cache, key=_segment_unsupported_cache.__getitem__)
        _segment_unsupported_cache.pop(oldest, None)
        _segment_failure_status.pop(oldest, None)
    key = (
        int(getattr(bot, "self_id", 0) or 0),
        int(group_id),
        normalized,
    )
    expires_at = time.monotonic() + _SEGMENT_UNSUPPORTED_TTL
    _segment_unsupported_cache[key] = expires_at
    _segment_failure_status[key] = {
        "reason": str(reason or "runtime_unsupported")[:160],
        "marked_at": time.time(),
        "expires_at_monotonic": expires_at,
    }
    dbg(f"群 {group_id} segment 能力降级缓存: {normalized}")


def clear_group_capability_cache(
    bot: Any,
    group_id: int,
    *,
    segment_type: str | None = None,
) -> None:
    """只清当前 bot/group 的能力缓存，供运维重探测而不影响其他群。"""

    bot_id = int(getattr(bot, "self_id", 0) or 0)
    group = int(group_id)
    if segment_type is None:
        key = (bot_id, group)
        _capability_cache.pop(key, None)
        _capability_probe_status.pop(key, None)
    normalized = str(segment_type or "").strip().lower()
    for key in list(_segment_unsupported_cache):
        key_bot_id, key_group_id, key_segment = key
        if key_bot_id != bot_id or key_group_id != group:
            continue
        if normalized and key_segment != normalized:
            continue
        _segment_unsupported_cache.pop(key, None)
        _segment_failure_status.pop(key, None)


def capability_runtime_snapshot(bot: Any, group_id: int) -> dict[str, Any]:
    """返回可直接给 WebUI 的 action/segment 运行时能力快照。"""

    bot_id = int(getattr(bot, "self_id", 0) or 0)
    group = int(group_id)
    key = (bot_id, group)
    now_mono = time.monotonic()
    cached = _capability_cache.get(key)
    probe = dict(_capability_probe_status.get(key) or {})
    action_caps = cached[1] if cached is not None else None
    if cached is not None:
        probe["cache_remaining_seconds"] = max(
            0.0, cached[2] - (now_mono - cached[0])
        )
    segments = get_segment_capabilities(bot, group)
    segment_rows: list[dict[str, Any]] = []
    for segment_type in sorted(
        COMMON_MESSAGE_SEGMENTS | OPTIONAL_MESSAGE_SEGMENTS | FORBIDDEN_MESSAGE_SEGMENTS
    ):
        failure_key = (bot_id, group, segment_type)
        failure = _segment_failure_status.get(failure_key)
        retry_after = None
        if failure is not None:
            retry_after = max(
                0.0,
                float(failure.get("expires_at_monotonic") or 0.0) - now_mono,
            )
        segment_rows.append(
            {
                "type": segment_type,
                "allowed": segment_type in segments.allowed,
                "supported": segment_type in segments.supported,
                "exposed": segment_type in segments.exposed_types,
                "forbidden": segment_type in segments.forbidden,
                "runtimeUnsupported": segment_type in segments.runtime_unsupported,
                "lastFailureReason": (
                    str(failure.get("reason")) if failure is not None else None
                ),
                "retryAfterSeconds": (
                    round(retry_after, 1) if retry_after is not None else None
                ),
            }
        )
    return {
        "botId": str(bot_id),
        "groupId": str(group),
        "action": {
            "cached": action_caps is not None,
            "role": action_caps.role if action_caps is not None else None,
            "canManage": action_caps.can_manage if action_caps is not None else False,
            "actions": sorted(action_caps.actions) if action_caps is not None else [],
            "degraded": bool(probe.get("degraded")),
            "lastError": probe.get("last_error"),
            "probedAt": probe.get("probed_at"),
            "cacheRemainingSeconds": round(
                float(probe.get("cache_remaining_seconds") or 0.0), 1
            ),
        },
        "segments": segment_rows,
    }


def infer_unsupported_segment(
    error: BaseException,
    segment_types: tuple[str, ...] | list[str],
) -> str | None:
    """从后端错误里尽量定位不支持的 segment；信息不足时只猜一个。"""

    types = tuple(dict.fromkeys(str(item).strip().lower() for item in segment_types))
    payload = str(error).lower()
    info = getattr(error, "info", None)
    if isinstance(info, dict):
        payload += " " + " ".join(str(value).lower() for value in info.values())
    for segment_type in types:
        if segment_type != "text" and segment_type in payload:
            return segment_type
    for segment_type in _SEGMENT_DEGRADE_PRIORITY:
        if segment_type in types:
            return segment_type
    return None


def reset_capability_cache() -> None:
    _capability_cache.clear()
    _segment_unsupported_cache.clear()
    _capability_probe_status.clear()
    _segment_failure_status.clear()


async def user_can_manage_group(bot: Any, group_id: int, user_id: int) -> bool:
    """实时确认调用者仍是群主或管理员。"""

    try:
        info = await bot.call_api(
            "get_group_member_info", group_id=group_id, user_id=user_id
        )
    except Exception:  # noqa: BLE001
        dbg_exc(f"群 {group_id} 查询用户 {user_id} 管理权限失败,视为无权限")
        return False
    role = str(info.get("role") or "member") if isinstance(info, dict) else "member"
    allowed = role in {"owner", "admin"}
    dbg(f"群 {group_id} 用户 {user_id} 管理权限判定: role={role!r} → {allowed}")
    return allowed


async def target_can_be_muted(
    bot: Any, group_id: int, user_id: int, bot_role: str
) -> bool:
    try:
        info = await bot.call_api(
            "get_group_member_info", group_id=group_id, user_id=user_id
        )
    except Exception:  # noqa: BLE001
        dbg_exc(f"群 {group_id} 查询成员 {user_id} 禁言可行性失败,视为不可禁言")
        return False
    target_role = (
        str(info.get("role") or "member") if isinstance(info, dict) else "member"
    )
    if target_role == "owner":
        dbg(f"群 {group_id} 成员 {user_id} 是群主,不可禁言")
        return False
    if target_role == "admin" and bot_role != "owner":
        dbg(f"群 {group_id} 成员 {user_id} 是管理员且机器人非群主,不可禁言")
        return False
    allowed = bot_role in {"owner", "admin"}
    dbg(
        f"群 {group_id} 禁言判定: target_role={target_role!r} bot_role={bot_role!r} → {allowed}"
    )
    return allowed


__all__ = [
    "COMMON_MESSAGE_SEGMENTS",
    "FORBIDDEN_MESSAGE_SEGMENTS",
    "OPTIONAL_MESSAGE_SEGMENTS",
    "BotGroupCapabilities",
    "MessageSegmentCapabilities",
    "capability_runtime_snapshot",
    "clear_group_capability_cache",
    "default_allowed_segment_types",
    "get_segment_capabilities",
    "infer_unsupported_segment",
    "mark_segment_unsupported",
    "probe_group_capabilities",
    "reset_capability_cache",
    "target_can_be_muted",
    "user_can_manage_group",
]
