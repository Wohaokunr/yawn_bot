# ruff: noqa: TID252,TRY003,TRY004,TRY301,ASYNC240,C901,PLR0912,PLR0913,PLR0915,PLR2004
"""OneBot v11 群消息编排、校验与发送。

模型只描述受限的结构化消息段；本模块负责把它们验证为 NoneBot Message，
并统一执行发送。禁止模型直接构造 CQ 码或任意 OneBot payload。
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import mimetypes
import os
import tempfile
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import httpx
from nonebot import logger
from nonebot.adapters.onebot.v11 import Message, MessageSegment
from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError

from ..data_models.agent_audit import AgentAudit
from ..data_models.group_agent_message import GroupAgentMessage
from ..data_models.user_group import UserGroup
from ..metrics import record_agent_outbound
from .capabilities import (
    COMMON_MESSAGE_SEGMENTS,
    OPTIONAL_MESSAGE_SEGMENTS,
    default_allowed_segment_types,
    get_segment_capabilities,
    infer_unsupported_segment,
    mark_segment_unsupported,
)
from .context import now_beijing
from .execution_trace import trace_event
from .media import validate_outbound_image_path
from .reactions import resolve_reaction
from .speech import (
    SpeechPlan,
    SpeechStyle,
    speech_plan_from_segments,
    speech_plan_from_text,
)
from .speech_quality import finalize_speech_plan

MAX_OUTBOUND_SEGMENTS = 12
MAX_OUTBOUND_TEXT_CHARS = 4_000
MAX_OUTBOUND_MEDIA_SEGMENTS = 3
MAX_OUTBOUND_FILE_BYTES = 32 * 1024 * 1024
MAX_FORWARD_NODES = 20
MAX_FORWARD_BYTES = 128 * 1024
SEND_TIMEOUT_SECONDS = 15.0

DELIVERY_CONFIRMED_SUCCESS = "confirmed_success"
DELIVERY_CONFIRMED_FAILURE = "confirmed_failure"
DELIVERY_UNKNOWN = "unknown"
DELIVERY_DEGRADED_SUCCESS = "degraded_success"

_FILE_ROOT = Path(os.environ.get("AGENT_FILE_ROOT", "data/agent_files")).resolve()
_SUPPORTED_TYPES = COMMON_MESSAGE_SEGMENTS | OPTIONAL_MESSAGE_SEGMENTS
_MEDIA_TYPES = frozenset({"image", "record", "video"})
_UNSUPPORTED_ERROR_HINTS = (
    "unsupported",
    "not support",
    "not supported",
    "message type",
    "message segment",
    "segment type",
    "不支持",
    "未支持",
)
_DEGRADED_NOTICE = "当前 QQ 后端暂不支持这类复合消息。"


@dataclass(frozen=True, slots=True)
class PreparedOutboundMessage:
    """已经过业务校验、可直接交给 OneBot 的消息。"""

    message: Message
    normalized_text: str
    segment_records: tuple[dict[str, Any], ...]
    reply_chain: tuple[dict[str, Any], ...] = ()
    media_refs: tuple[dict[str, Any], ...] = ()
    temporary_files: tuple[Path, ...] = ()
    speech_scene: str = "conversation"
    quality_issues: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class PreparedForwardMessage:
    nodes: tuple[MessageSegment, ...]
    normalized_text: str
    forward_tree: tuple[dict[str, Any], ...]


@dataclass(frozen=True, slots=True)
class SendResult:
    sent: bool
    message_id: int | None
    normalized_text: str
    segment_types: tuple[str, ...]
    message_type: str = "text"
    outcome: str = "success"
    delivery_state: str = DELIVERY_CONFIRMED_SUCCESS
    degraded_from: str | None = None
    segments: tuple[dict[str, Any], ...] = ()
    reply_chain: tuple[dict[str, Any], ...] = ()
    forward_tree: tuple[dict[str, Any], ...] = ()
    media_refs: tuple[dict[str, Any], ...] = ()

    @property
    def ends_turn(self) -> bool:
        """成功或投递未知都必须终止本轮，避免回执丢失后重复发送。"""

        return self.delivery_state in {
            DELIVERY_CONFIRMED_SUCCESS,
            DELIVERY_DEGRADED_SUCCESS,
            DELIVERY_UNKNOWN,
        }

    @property
    def may_have_delivered(self) -> bool:
        """unknown 表示消息可能已经被 QQ 接收。"""

        return self.delivery_state != DELIVERY_CONFIRMED_FAILURE

    def storage_payload(self) -> dict[str, Any]:
        return {
            "message_id": self.message_id,
            "text": self.normalized_text,
            "message_type": self.message_type,
            "outcome": self.outcome,
            "delivery_state": self.delivery_state,
            "degraded_from": self.degraded_from,
            "segments": list(self.segments),
            "reply_chain": list(self.reply_chain),
            "forward_tree": list(self.forward_tree),
            "media_refs": list(self.media_refs),
        }


def classify_message_type(segment_types: tuple[str, ...] | list[str]) -> str:
    """生成低基数、可用于审计/指标的消息类型标签。"""

    ordered = tuple(dict.fromkeys(str(item).strip().lower() for item in segment_types))
    if ordered == ("text",):
        return "text"
    if ordered == ("forward",):
        return "forward"
    if ordered == ("image",):
        return "image"
    return "+".join(ordered)[:64] or "empty"


def _safe_audit_arguments(
    message_type: str,
    segment_types: tuple[str, ...] | list[str],
    *,
    source: str,
    degraded_from: str | None = None,
    delivery_state: str | None = None,
    onebot_action: str | None = None,
    error_class: str | None = None,
    actual_segment_types: tuple[str, ...] | list[str] | None = None,
) -> dict[str, object]:
    payload: dict[str, object] = {
        "message_type": message_type,
        "segment_types": list(segment_types),
        "source": source[:32],
    }
    if degraded_from:
        payload["degraded_from"] = degraded_from[:64]
    if delivery_state:
        payload["delivery_state"] = delivery_state[:32]
    if onebot_action:
        payload["onebot_action"] = onebot_action[:64]
    if error_class:
        payload["error_class"] = error_class[:96]
    if actual_segment_types is not None:
        payload["actual_segment_types"] = list(actual_segment_types)
    return payload


async def _audit_outbound(
    session: Any,
    group_id: int,
    actor_user_id: int | None,
    *,
    message_type: str,
    segment_types: tuple[str, ...] | list[str],
    source: str,
    outcome: str,
    detail: str = "",
    degraded_from: str | None = None,
    delivery_state: str | None = None,
    onebot_action: str | None = None,
    error_class: str | None = None,
    actual_segment_types: tuple[str, ...] | list[str] | None = None,
) -> None:
    """输出审计只记结构摘要；不写媒体字节、URL 或卡片 payload。"""

    record_agent_outbound(message_type, outcome)
    if session is None or not hasattr(session, "add") or not hasattr(session, "flush"):
        return
    try:
        session.add(
            AgentAudit(
                group_id=group_id,
                actor_user_id=actor_user_id,
                tool_name="outbound_message",
                arguments=_safe_audit_arguments(
                    message_type,
                    segment_types,
                    source=source,
                    degraded_from=degraded_from,
                    delivery_state=delivery_state,
                    onebot_action=onebot_action,
                    error_class=error_class,
                    actual_segment_types=actual_segment_types,
                ),
                result=outcome[:24],
                detail=detail[:500] or None,
            )
        )
        await session.flush()
    except SQLAlchemyError:
        logger.debug("群 %s Agent 输出审计写入失败，已忽略", group_id, exc_info=True)
        with contextlib.suppress(SQLAlchemyError):
            await session.rollback()


def extract_message_id(result: Any) -> int | None:
    """兼容不同 OneBot 实现的 send_group_msg 返回结构。"""

    raw: Any
    if isinstance(result, dict):
        raw = result.get("message_id")
    else:
        raw = getattr(result, "message_id", result)
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return None
    return value or None


def _require_exact_keys(
    raw: dict[str, Any], *, required: set[str], allowed: set[str]
) -> None:
    missing = required - raw.keys()
    if missing:
        raise ValueError(f"消息段缺少字段: {', '.join(sorted(missing))}")
    extra = raw.keys() - allowed
    if extra:
        raise ValueError(f"消息段包含不支持字段: {', '.join(sorted(extra))}")


def _bounded_int(value: Any, *, field: str, minimum: int, maximum: int) -> int:
    if isinstance(value, bool):
        raise ValueError(f"{field} 必须是整数")
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field} 必须是整数") from exc
    if parsed < minimum or parsed > maximum:
        raise ValueError(f"{field} 超出允许范围")
    return parsed


def _bounded_float(
    value: Any, *, field: str, minimum: float, maximum: float
) -> float:
    if isinstance(value, bool):
        raise ValueError(f"{field} 必须是数字")
    try:
        parsed = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field} 必须是数字") from exc
    if parsed < minimum or parsed > maximum:
        raise ValueError(f"{field} 超出允许范围")
    return parsed


def _allowed_file_hosts() -> frozenset[str]:
    return frozenset(
        host.strip().lower()
        for host in os.environ.get("AGENT_FILE_ALLOWED_HOSTS", "").split(",")
        if host.strip()
    )


def _validate_share_url(value: Any) -> str:
    url = str(value or "").strip()
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    allowed_hosts = frozenset(
        item.strip().lower()
        for item in os.environ.get("AGENT_SHARE_ALLOWED_HOSTS", "").split(",")
        if item.strip()
    )
    if parsed.scheme not in {"http", "https"} or not host:
        raise ValueError("share.url 必须是 http/https URL")
    if not allowed_hosts or host not in allowed_hosts:
        raise PermissionError("share URL 域名不在白名单")
    return url


def _check_local_path(path: Path) -> Path:
    resolved = path.expanduser().resolve()
    if _FILE_ROOT not in resolved.parents and resolved != _FILE_ROOT:
        raise PermissionError("本地媒体必须位于 Agent 文件目录")
    if not resolved.is_file():
        raise ValueError("媒体文件不存在")
    if resolved.stat().st_size > MAX_OUTBOUND_FILE_BYTES:
        raise ValueError("媒体文件超过大小限制")
    return resolved


def _check_media_mime(path: Path, media_type: str, content_type: str | None) -> None:
    mime = (content_type or mimetypes.guess_type(path.name)[0] or "").lower()
    expected = {
        "image": "image/",
        "record": "audio/",
        "video": "video/",
    }[media_type]
    if not mime.startswith(expected):
        raise ValueError(f"文件不是受支持的{media_type}类型")


async def _download_allowed_media(file_ref: str, media_type: str) -> Path:
    parsed = urlparse(file_ref)
    host = (parsed.hostname or "").lower()
    if not host or host not in _allowed_file_hosts():
        raise PermissionError("远程媒体域名不在白名单")

    suffix = Path(parsed.path).suffix or ".download"
    async with (
        httpx.AsyncClient(timeout=15, follow_redirects=True) as client,
        client.stream("GET", file_ref) as response,
    ):
        response.raise_for_status()
        content_type = response.headers.get("content-type", "").split(";", 1)[0]
        with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as handle:
            temporary = Path(handle.name)
            total = 0
            try:
                async for chunk in response.aiter_bytes(64 * 1024):
                    total += len(chunk)
                    if total > MAX_OUTBOUND_FILE_BYTES:
                        raise ValueError("媒体文件超过大小限制")
                    handle.write(chunk)
            except BaseException:
                handle.close()
                temporary.unlink(missing_ok=True)
                raise
    try:
        _check_media_mime(temporary, media_type, content_type)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise
    return temporary


async def _resolve_media(
    file_ref: str,
    media_type: str,
    *,
    session: Any,
    group_id: int,
) -> tuple[Path, bool]:
    ref = file_ref.strip()
    if not ref:
        raise ValueError("媒体 file 不能为空")
    if ref.startswith(("http://", "https://")):
        return await _download_allowed_media(ref, media_type), True
    if media_type == "image":
        path = await validate_outbound_image_path(
            Path(ref), group_id=group_id, session=session
        )
    else:
        path = _check_local_path(Path(ref))
        _check_media_mime(path, media_type, None)
    return path, False


async def _get_known_message(
    session: Any, group_id: int, message_id: int
) -> GroupAgentMessage:
    if session is None:
        raise PermissionError("引用消息需要数据库会话")
    row = await session.scalar(
        select(GroupAgentMessage).where(
            GroupAgentMessage.group_id == group_id,
            GroupAgentMessage.message_id == message_id,
            (
                GroupAgentMessage.expires_at.is_(None)
                | (GroupAgentMessage.expires_at >= now_beijing())
            ),
        )
    )
    if row is None:
        raise ValueError("只能引用当前群已知的近期消息")
    return row


async def _ensure_group_member(
    session: Any,
    group_id: int,
    user_id: int,
    *,
    actor_user_id: int | None,
) -> None:
    if actor_user_id is not None and user_id == actor_user_id:
        return
    if session is None:
        raise PermissionError("@成员需要数据库会话")
    row = await session.get(UserGroup, (group_id, user_id))
    if row is None:
        raise ValueError("只能 @ 当前群已知成员")


async def _known_member_name(session: Any, group_id: int, user_id: int) -> str:
    if session is None:
        raise PermissionError("自定义转发节点需要数据库会话")
    row = await session.get(UserGroup, (group_id, user_id))
    if row is None:
        raise ValueError("自定义转发节点只能使用当前群已知成员身份")
    nickname = str(getattr(row, "group_nickname", "") or "").strip()
    return nickname[:64] or str(user_id)


def _segment_record(
    segment_type: str, data: dict[str, Any], text: str = ""
) -> dict[str, Any]:
    return {
        "type": segment_type,
        "data": data,
        "text": text,
        "children": [],
        "depth": 0,
    }


def _quality_codes(plan: SpeechPlan) -> tuple[str, ...]:
    return tuple(dict.fromkeys(item.code for item in plan.issues))



async def prepare_speech_plan(
    plan: SpeechPlan,
    *,
    session: Any,
    group_id: int,
    actor_user_id: int | None = None,
    allowed_segment_types: frozenset[str] | None = None,
    speech_user_text: str = "",
    recent_speech: tuple[str, ...] | list[str] = (),
    speech_autofix: bool = True,
    trace_context: dict[str, Any] | None = None,
) -> PreparedOutboundMessage:
    """Finalize one SpeechPlan, trace the decision, then enter OneBot validation."""

    resolved = finalize_speech_plan(
        plan,
        user_text=speech_user_text,
        recent_texts=recent_speech,
        autofix=speech_autofix,
    )
    trace_payload = resolved.trace_payload()
    if trace_context:
        trace_payload.update(trace_context)
    trace_event(
        "speech",
        "发言决策",
        status="planned" if resolved.should_speak else "skipped",
        output=trace_payload,
        detail=(
            "SpeechPlan 已通过表达质量层，准备进入 OneBot 发送校验。"
            if resolved.should_speak
            else "SpeechPlan 决定不产生用户可见消息。"
        ),
    )
    if not resolved.should_speak:
        raise ValueError("SpeechPlan 当前动作不应发送消息")
    if resolved.segments:
        prepared = await prepare_outbound_message(
            list(resolved.segments),
            session=session,
            group_id=group_id,
            actor_user_id=actor_user_id,
            allowed_segment_types=allowed_segment_types,
            speech_scene=resolved.scene,
            speech_style=resolved.style,
            speech_user_text=speech_user_text,
            recent_speech=recent_speech,
            speech_autofix=False,
            trace_speech=False,
        )
    else:
        prepared = prepare_text_message(
            resolved.text,
            speech_scene=resolved.scene,
            speech_style=resolved.style,
            speech_user_text=speech_user_text,
            recent_speech=recent_speech,
            speech_autofix=False,
            trace_speech=False,
        )
    return replace(
        prepared,
        speech_scene=resolved.scene,
        quality_issues=_quality_codes(resolved),
    )

def prepare_text_message(
    text: str,
    *,
    speech_scene: str = "conversation",
    speech_style: SpeechStyle | None = None,
    speech_user_text: str = "",
    recent_speech: tuple[str, ...] | list[str] = (),
    speech_autofix: bool = True,
    trace_speech: bool = True,
) -> PreparedOutboundMessage:
    """纯文本也先形成 SpeechPlan，再走统一 outbound 表示。"""

    plan = finalize_speech_plan(
        speech_plan_from_text(text, scene=speech_scene, style=speech_style),
        user_text=speech_user_text,
        recent_texts=recent_speech,
        autofix=speech_autofix,
    )
    if trace_speech:
        trace_event(
            "speech",
            "发言决策",
            status="planned" if plan.should_speak else "skipped",
            output=plan.trace_payload(),
        )
    bounded = str(plan.text)
    if not bounded:
        raise ValueError("消息文本不能为空")
    if len(bounded) > MAX_OUTBOUND_TEXT_CHARS:
        bounded = bounded[:MAX_OUTBOUND_TEXT_CHARS]
    return PreparedOutboundMessage(
        message=Message(MessageSegment.text(bounded)),
        normalized_text=bounded,
        segment_records=(_segment_record("text", {"text": bounded}, bounded),),
        speech_scene=plan.scene,
        quality_issues=_quality_codes(plan),
    )


async def prepare_outbound_message(
    raw_segments: Any,
    *,
    session: Any,
    group_id: int,
    actor_user_id: int | None = None,
    allowed_segment_types: frozenset[str] | None = None,
    speech_scene: str = "conversation",
    speech_style: SpeechStyle | None = None,
    speech_user_text: str = "",
    recent_speech: tuple[str, ...] | list[str] = (),
    speech_autofix: bool = False,
    trace_speech: bool = True,
) -> PreparedOutboundMessage:
    """先经 SpeechPlan 质量层，再验证受限消息段并转换为 NoneBot Message。"""

    if not isinstance(raw_segments, list) or not raw_segments:
        raise ValueError("segments 必须是非空数组")
    if len(raw_segments) > MAX_OUTBOUND_SEGMENTS:
        raise ValueError(f"单条消息最多 {MAX_OUTBOUND_SEGMENTS} 个消息段")
    if any(not isinstance(item, dict) for item in raw_segments):
        raise ValueError("segments 中每一项都必须是对象")

    plan = finalize_speech_plan(
        speech_plan_from_segments(
            raw_segments,
            scene=speech_scene,
            style=speech_style,
        ),
        user_text=speech_user_text,
        recent_texts=recent_speech,
        autofix=speech_autofix,
    )
    if trace_speech:
        trace_event(
            "speech",
            "发言决策",
            status="planned" if plan.should_speak else "skipped",
            output=plan.trace_payload(),
        )
    raw_segments = list(plan.segments)

    reply_count = sum(item.get("type") == "reply" for item in raw_segments)
    media_count = sum(
        item.get("type") in _MEDIA_TYPES or item.get("type") == "reaction"
        for item in raw_segments
    )
    if reply_count > 1:
        raise ValueError("单条消息最多只能引用一条消息")
    if media_count > MAX_OUTBOUND_MEDIA_SEGMENTS:
        raise ValueError(f"单条消息最多 {MAX_OUTBOUND_MEDIA_SEGMENTS} 个媒体段")

    # OneBot/QQ 对 reply 段位置最稳定的形式是放在消息首部。
    ordered = sorted(
        raw_segments,
        key=lambda item: 0 if item.get("type") == "reply" else 1,
    )
    message = Message()
    records: list[dict[str, Any]] = []
    temporary_files: list[Path] = []
    text_parts: list[str] = []
    reply_chain: list[dict[str, Any]] = []
    media_refs: list[dict[str, Any]] = []
    policy_allowed = allowed_segment_types or default_allowed_segment_types()

    try:
        for raw in ordered:
            segment_type = str(raw.get("type") or "").strip().lower()
            if segment_type not in _SUPPORTED_TYPES:
                raise ValueError(f"不支持的消息段类型: {segment_type or '<empty>'}")
            if segment_type not in policy_allowed:
                raise PermissionError(f"消息段 {segment_type} 未在当前协议策略中开放")

            if segment_type == "text":
                _require_exact_keys(
                    raw,
                    required={"type", "text"},
                    allowed={"type", "text"},
                )
                text = str(raw["text"])
                if not text:
                    raise ValueError("text 消息段不能为空")
                if len(text) > MAX_OUTBOUND_TEXT_CHARS:
                    raise ValueError("text 消息段超过长度限制")
                message += MessageSegment.text(text)
                records.append(_segment_record("text", {"text": text}, text))
                text_parts.append(text)
                continue

            if segment_type == "reply":
                _require_exact_keys(
                    raw,
                    required={"type", "message_id"},
                    allowed={"type", "message_id"},
                )
                message_id = _bounded_int(
                    raw["message_id"],
                    field="message_id",
                    minimum=-(2**63) + 1,
                    maximum=2**63 - 1,
                )
                if message_id == 0:
                    raise ValueError("message_id 不能为 0")
                target = await _get_known_message(session, group_id, message_id)
                message += MessageSegment.reply(message_id)
                records.append(_segment_record("reply", {"id": str(message_id)}))
                reply_chain.append(
                    {
                        "message_id": message_id,
                        "user_id": int(target.user_id),
                        "nickname": str(target.sender_name or target.user_id)[:64],
                        "text": str(target.normalized_text or "")[:720],
                    }
                )
                continue

            if segment_type == "at":
                _require_exact_keys(
                    raw,
                    required={"type", "user_id"},
                    allowed={"type", "user_id"},
                )
                user_id = _bounded_int(
                    raw["user_id"],
                    field="user_id",
                    minimum=1,
                    maximum=2**63 - 1,
                )
                await _ensure_group_member(
                    session,
                    group_id,
                    user_id,
                    actor_user_id=actor_user_id,
                )
                message += MessageSegment.at(user_id)
                records.append(_segment_record("at", {"qq": str(user_id)}))
                continue

            if segment_type == "face":
                _require_exact_keys(
                    raw,
                    required={"type", "id"},
                    allowed={"type", "id"},
                )
                face_id = _bounded_int(
                    raw["id"], field="id", minimum=0, maximum=65_535
                )
                message += MessageSegment.face(face_id)
                records.append(_segment_record("face", {"id": str(face_id)}))
                continue

            if segment_type == "reaction":
                _require_exact_keys(
                    raw,
                    required={"type", "reaction_id"},
                    allowed={"type", "reaction_id"},
                )
                reaction_id = str(raw["reaction_id"]).strip()
                reaction_path = resolve_reaction(reaction_id)
                path = await validate_outbound_image_path(
                    reaction_path, group_id=group_id, session=session
                )
                message += MessageSegment.image(str(path))
                records.append(
                    _segment_record("image", {"file": f"reaction:{reaction_id}"})
                )
                media_refs.append(
                    {"type": "image", "reaction_id": reaction_id, "source": "reaction"}
                )
                continue

            if segment_type in _MEDIA_TYPES:
                _require_exact_keys(
                    raw,
                    required={"type", "file"},
                    allowed={"type", "file"},
                )
                path, temporary = await _resolve_media(
                    str(raw["file"]),
                    segment_type,
                    session=session,
                    group_id=group_id,
                )
                if temporary:
                    temporary_files.append(path)
                if segment_type == "image":
                    message += MessageSegment.image(str(path))
                elif segment_type == "record":
                    message += MessageSegment.record(str(path))
                else:
                    message += MessageSegment.video(str(path))
                records.append(
                    _segment_record(segment_type, {"file": "[redacted]"})
                )
                media_refs.append({"type": segment_type, "file": "[redacted]"})
                continue

            if segment_type == "rps":
                _require_exact_keys(raw, required={"type"}, allowed={"type"})
                message += MessageSegment.rps()
                records.append(_segment_record("rps", {}))
                continue

            if segment_type == "dice":
                _require_exact_keys(raw, required={"type"}, allowed={"type"})
                message += MessageSegment.dice()
                records.append(_segment_record("dice", {}))
                continue

            if segment_type == "share":
                _require_exact_keys(
                    raw,
                    required={"type", "url", "title"},
                    allowed={"type", "url", "title", "content"},
                )
                url = _validate_share_url(raw["url"])
                title = str(raw["title"]).strip()[:120]
                content = str(raw.get("content") or "").strip()[:300] or None
                if not title:
                    raise ValueError("share.title 不能为空")
                message += MessageSegment.share(url=url, title=title, content=content)
                records.append(
                    _segment_record(
                        "share",
                        {"url": "[redacted]", "title": title, "content": content or ""},
                    )
                )
                text_parts.append(title)
                continue

            if segment_type == "contact":
                _require_exact_keys(
                    raw,
                    required={"type", "contact_type", "id"},
                    allowed={"type", "contact_type", "id"},
                )
                contact_type = str(raw["contact_type"]).strip().lower()
                contact_id = _bounded_int(
                    raw["id"], field="id", minimum=1, maximum=2**63 - 1
                )
                if contact_type == "qq":
                    await _ensure_group_member(
                        session,
                        group_id,
                        contact_id,
                        actor_user_id=actor_user_id,
                    )
                elif contact_type == "group":
                    if contact_id != int(group_id):
                        raise ValueError("群名片只能指向当前群")
                else:
                    raise ValueError("contact_type 只能是 qq 或 group")
                message += MessageSegment.contact(contact_type, contact_id)
                records.append(
                    _segment_record(
                        "contact", {"type": contact_type, "id": str(contact_id)}
                    )
                )
                continue

            if segment_type == "location":
                _require_exact_keys(
                    raw,
                    required={"type", "latitude", "longitude"},
                    allowed={"type", "latitude", "longitude", "title", "content"},
                )
                latitude = _bounded_float(
                    raw["latitude"], field="latitude", minimum=-90.0, maximum=90.0
                )
                longitude = _bounded_float(
                    raw["longitude"], field="longitude", minimum=-180.0, maximum=180.0
                )
                title = str(raw.get("title") or "").strip()[:120] or None
                content = str(raw.get("content") or "").strip()[:300] or None
                message += MessageSegment.location(
                    latitude, longitude, title=title, content=content
                )
                records.append(
                    _segment_record(
                        "location",
                        {
                            "latitude": "[redacted]",
                            "longitude": "[redacted]",
                            "title": title or "",
                        },
                    )
                )
                if title:
                    text_parts.append(title)
                continue

            if segment_type == "music":
                _require_exact_keys(
                    raw,
                    required={"type", "provider", "id"},
                    allowed={"type", "provider", "id"},
                )
                provider = str(raw["provider"]).strip().lower()
                if provider not in {"qq", "163", "xm"}:
                    raise ValueError("music.provider 只能是 qq/163/xm")
                music_id = _bounded_int(
                    raw["id"], field="id", minimum=1, maximum=2**63 - 1
                )
                message += MessageSegment.music(provider, music_id)
                records.append(
                    _segment_record("music", {"type": provider, "id": str(music_id)})
                )
                continue

            _require_exact_keys(
                raw,
                required={"type", "poke_type", "poke_id"},
                allowed={"type", "poke_type", "poke_id"},
            )
            poke_type = str(raw["poke_type"]).strip()
            poke_id = str(raw["poke_id"]).strip()
            if (
                not poke_type
                or not poke_id
                or len(poke_type) > 32
                or len(poke_id) > 32
            ):
                raise ValueError("poke_type/poke_id 格式无效")
            message += MessageSegment.poke(poke_type, poke_id)
            records.append(
                _segment_record(
                    "poke", {"type": poke_type, "id": poke_id}
                )
            )
    except BaseException:
        for path in temporary_files:
            path.unlink(missing_ok=True)
        raise

    if not message:
        raise ValueError("消息不能为空")
    return PreparedOutboundMessage(
        message=message,
        normalized_text="".join(text_parts),
        segment_records=tuple(records),
        reply_chain=tuple(reply_chain),
        media_refs=tuple(media_refs),
        temporary_files=tuple(temporary_files),
        speech_scene=plan.scene,
        quality_issues=_quality_codes(plan),
    )


async def prepare_speech_plan(
    plan: SpeechPlan,
    *,
    session: Any,
    group_id: int,
    actor_user_id: int | None = None,
    allowed_segment_types: frozenset[str] | None = None,
    speech_user_text: str = "",
    recent_speech: tuple[str, ...] | list[str] = (),
    speech_autofix: bool = True,
) -> PreparedOutboundMessage:
    """Canonical SpeechPlan -> validated OneBot outbound adapter."""

    if plan.segments:
        return await prepare_outbound_message(
            list(plan.segments),
            session=session,
            group_id=group_id,
            actor_user_id=actor_user_id,
            allowed_segment_types=allowed_segment_types,
            speech_scene=plan.scene,
            speech_style=plan.style,
            speech_user_text=speech_user_text,
            recent_speech=recent_speech,
            speech_autofix=speech_autofix,
        )
    if plan.text:
        return prepare_text_message(
            plan.text,
            speech_scene=plan.scene,
            speech_style=plan.style,
            speech_user_text=speech_user_text,
            recent_speech=recent_speech,
            speech_autofix=speech_autofix,
        )
    raise ValueError("SpeechPlan 没有可发送内容")


def _is_unsupported_error(error: BaseException) -> bool:
    payload = str(error).lower()
    info = getattr(error, "info", None)
    if isinstance(info, dict):
        payload += " " + " ".join(str(value).lower() for value in info.values())
    return any(hint in payload for hint in _UNSUPPORTED_ERROR_HINTS)


def _is_ambiguous_delivery_error(error: BaseException) -> bool:
    """传输层错误无法证明服务端未处理请求，按投递未知处理。

    OneBot 业务错误通常会带明确 retcode/信息，可安全视为失败；超时、连接
    中断等则可能发生在服务端已经执行 send_group_msg 之后。
    """

    return isinstance(
        error,
        (
            asyncio.TimeoutError,
            TimeoutError,
            httpx.TimeoutException,
            httpx.NetworkError,
        ),
    )


def _build_degraded_message(
    prepared: PreparedOutboundMessage,
    *,
    allow_at: bool,
) -> PreparedOutboundMessage:
    """把复合消息收敛成 @ + text；必要时再收敛成纯文本。"""

    at_ids: list[int] = []
    text_mentions: list[str] = []
    for record in prepared.segment_records:
        if record.get("type") != "at":
            continue
        data = record.get("data")
        if not isinstance(data, dict):
            continue
        raw_id = data.get("qq")
        if isinstance(raw_id, bool) or not isinstance(raw_id, (int, str)):
            continue
        try:
            user_id = int(raw_id)
        except (TypeError, ValueError):
            continue
        if user_id > 0 and user_id not in at_ids:
            at_ids.append(user_id)
            text_mentions.append(f"@{user_id}")
    for reply in prepared.reply_chain:
        try:
            user_id = int(reply.get("user_id") or 0)
        except (TypeError, ValueError):
            continue
        nickname = str(reply.get("nickname") or user_id).strip()[:64]
        if user_id > 0 and user_id not in at_ids:
            at_ids.append(user_id)
            text_mentions.append(f"@{nickname}")

    body = prepared.normalized_text.strip() or _DEGRADED_NOTICE
    message = Message()
    records: list[dict[str, Any]] = []
    if allow_at:
        for user_id in at_ids[:3]:
            message += MessageSegment.at(user_id)
            records.append(_segment_record("at", {"qq": str(user_id)}))
        message += MessageSegment.text(body)
        records.append(_segment_record("text", {"text": body}, body))
        normalized = body
    else:
        prefix = " ".join(text_mentions[:3])
        normalized = f"{prefix} {body}".strip()
        message += MessageSegment.text(normalized)
        records.append(_segment_record("text", {"text": normalized}, normalized))
    return PreparedOutboundMessage(
        message=message,
        normalized_text=normalized,
        segment_records=tuple(records),
        speech_scene=prepared.speech_scene,
        quality_issues=prepared.quality_issues,
    )


async def _call_group_send(bot: Any, group_id: int, message: Message) -> Any:
    return await asyncio.wait_for(
        bot.call_api("send_group_msg", group_id=group_id, message=message),
        timeout=SEND_TIMEOUT_SECONDS,
    )


async def send_prepared_outbound(
    bot: Any,
    group_id: int,
    prepared: PreparedOutboundMessage,
    *,
    session: Any = None,
    actor_user_id: int | None = None,
    source: str = "agent",
) -> SendResult:
    """统一发送状态机：发送、兼容性失败缓存、结构降级、审计。"""

    original_types = tuple(str(item["type"]) for item in prepared.segment_records)
    original_type = classify_message_type(original_types)
    caps = get_segment_capabilities(bot, group_id)
    known_unsupported = tuple(
        item
        for item in original_types
        if item not in caps.supported or item in caps.runtime_unsupported
    )
    trace_event(
        "outbound",
        "构建发送计划",
        status="planned",
        input={
            "message_type": original_type,
            "segment_types": original_types,
            "source": source,
            "speech_scene": prepared.speech_scene,
            "quality_issues": prepared.quality_issues,
            "text_chars": len(prepared.normalized_text),
            "text_preview": prepared.normalized_text[:320],
        },
        output={"known_unsupported": known_unsupported},
    )
    try:
        if not known_unsupported:
            try:
                raw_result = await _call_group_send(bot, group_id, prepared.message)
            except Exception as exc:
                if _is_ambiguous_delivery_error(exc):
                    trace_event(
                        "outbound",
                        "OneBot 发送",
                        status="unknown",
                        output={
                            "delivery_state": DELIVERY_UNKNOWN,
                            "segment_types": original_types,
                        },
                        detail=(
                            f"{type(exc).__name__}: 回执不确定，"
                            "为避免重复发送不自动重试"
                        ),
                    )
                    await _audit_outbound(
                        session,
                        group_id,
                        actor_user_id,
                        message_type=original_type,
                        segment_types=original_types,
                        source=source,
                        outcome="delivery_unknown",
                        detail=type(exc).__name__,
                    )
                    return SendResult(
                        sent=False,
                        message_id=None,
                        normalized_text=prepared.normalized_text,
                        segment_types=original_types,
                        message_type=original_type,
                        outcome="delivery_unknown",
                        delivery_state=DELIVERY_UNKNOWN,
                        segments=prepared.segment_records,
                        reply_chain=prepared.reply_chain,
                        media_refs=prepared.media_refs,
                    )
                if not _is_unsupported_error(exc):
                    trace_event(
                        "outbound",
                        "OneBot 发送",
                        status="failed",
                        output={"delivery_state": DELIVERY_CONFIRMED_FAILURE},
                        detail=type(exc).__name__,
                    )
                    await _audit_outbound(
                        session,
                        group_id,
                        actor_user_id,
                        message_type=original_type,
                        segment_types=original_types,
                        source=source,
                        outcome="send_failed",
                        detail=type(exc).__name__,
                    )
                    raise
                candidate = infer_unsupported_segment(exc, original_types)
                trace_event(
                    "outbound",
                    "协议段不兼容",
                    status="degraded",
                    output={
                        "unsupported_segment": candidate or "unknown",
                        "original_types": original_types,
                    },
                    detail="进入受控消息降级链",
                )
                if candidate:
                    mark_segment_unsupported(
                        bot, group_id, candidate, reason=type(exc).__name__
                    )
                await _audit_outbound(
                    session,
                    group_id,
                    actor_user_id,
                    message_type=original_type,
                    segment_types=original_types,
                    source=source,
                    outcome="unsupported_segment",
                    detail=f"segment={candidate or 'unknown'}",
                )
            else:
                trace_event(
                    "outbound",
                    "OneBot 发送",
                    output={
                        "delivery_state": DELIVERY_CONFIRMED_SUCCESS,
                        "segment_types": original_types,
                        "message_id": extract_message_id(raw_result),
                        "text_preview": prepared.normalized_text[:320],
                    },
                )
                await _audit_outbound(
                    session,
                    group_id,
                    actor_user_id,
                    message_type=original_type,
                    segment_types=original_types,
                    source=source,
                    outcome="success",
                )
                return SendResult(
                    sent=True,
                    message_id=extract_message_id(raw_result),
                    normalized_text=prepared.normalized_text,
                    segment_types=original_types,
                    message_type=original_type,
                    outcome="success",
                    segments=prepared.segment_records,
                    reply_chain=prepared.reply_chain,
                    media_refs=prepared.media_refs,
                )
        # 协议不兼容才进入自动降级。reply 会转成引用目标 @，face/media 等剥离。
        caps = get_segment_capabilities(bot, group_id)
        allow_at = "at" in caps.exposed_types
        fallback = _build_degraded_message(prepared, allow_at=allow_at)
        fallback_types = tuple(str(item["type"]) for item in fallback.segment_records)
        fallback_type = classify_message_type(fallback_types)
        trace_event(
            "outbound",
            "生成降级发送计划",
            status="degraded",
            input={"original_types": original_types},
            output={"fallback_types": fallback_types, "allow_at": allow_at},
        )
        try:
            raw_result = await _call_group_send(bot, group_id, fallback.message)
        except Exception as exc:
            if _is_ambiguous_delivery_error(exc):
                trace_event(
                    "outbound",
                    "降级消息发送",
                    status="unknown",
                    output={
                        "delivery_state": DELIVERY_UNKNOWN,
                        "segment_types": fallback_types,
                    },
                    detail=f"{type(exc).__name__}: 回执不确定，不继续重试",
                )
                await _audit_outbound(
                    session,
                    group_id,
                    actor_user_id,
                    message_type=fallback_type,
                    segment_types=fallback_types,
                    source=source,
                    outcome="delivery_unknown",
                    detail=type(exc).__name__,
                    degraded_from=original_type,
                )
                return SendResult(
                    sent=False,
                    message_id=None,
                    normalized_text=fallback.normalized_text,
                    segment_types=fallback_types,
                    message_type=fallback_type,
                    outcome="delivery_unknown",
                    delivery_state=DELIVERY_UNKNOWN,
                    degraded_from=original_type,
                    segments=fallback.segment_records,
                )
            if not allow_at or not _is_unsupported_error(exc):
                trace_event(
                    "outbound",
                    "降级消息发送",
                    status="failed",
                    output={"delivery_state": DELIVERY_CONFIRMED_FAILURE},
                    detail=type(exc).__name__,
                )
                await _audit_outbound(
                    session,
                    group_id,
                    actor_user_id,
                    message_type=fallback_type,
                    segment_types=fallback_types,
                    source=source,
                    outcome="send_failed",
                    detail=type(exc).__name__,
                    degraded_from=original_type,
                )
                raise
            mark_segment_unsupported(
                bot, group_id, "at", reason=type(exc).__name__
            )
            text_fallback = _build_degraded_message(prepared, allow_at=False)
            text_types = ("text",)
            trace_event(
                "outbound",
                "再次降级为纯文本",
                status="degraded",
                output={"fallback_types": text_types},
                detail="@ 段也不兼容，移除可选段后只保留文本",
            )
            try:
                raw_result = await _call_group_send(
                    bot, group_id, text_fallback.message
                )
            except Exception as final_exc:
                if _is_ambiguous_delivery_error(final_exc):
                    trace_event(
                        "outbound",
                        "纯文本降级发送",
                        status="unknown",
                        output={"delivery_state": DELIVERY_UNKNOWN},
                        detail=f"{type(final_exc).__name__}: 回执不确定，不继续重试",
                    )
                    await _audit_outbound(
                        session,
                        group_id,
                        actor_user_id,
                        message_type="text",
                        segment_types=text_types,
                        source=source,
                        outcome="delivery_unknown",
                        detail=type(final_exc).__name__,
                        degraded_from=original_type,
                    )
                    return SendResult(
                        sent=False,
                        message_id=None,
                        normalized_text=text_fallback.normalized_text,
                        segment_types=text_types,
                        message_type="text",
                        outcome="delivery_unknown",
                        delivery_state=DELIVERY_UNKNOWN,
                        degraded_from=original_type,
                        segments=text_fallback.segment_records,
                    )
                trace_event(
                    "outbound",
                    "纯文本降级发送",
                    status="failed",
                    output={"delivery_state": DELIVERY_CONFIRMED_FAILURE},
                    detail=type(final_exc).__name__,
                )
                await _audit_outbound(
                    session,
                    group_id,
                    actor_user_id,
                    message_type="text",
                    segment_types=text_types,
                    source=source,
                    outcome="send_failed",
                    detail=type(final_exc).__name__,
                    degraded_from=original_type,
                )
                raise
            fallback = text_fallback
            fallback_types = text_types
            fallback_type = "text"

        await _audit_outbound(
            session,
            group_id,
            actor_user_id,
            message_type=fallback_type,
            segment_types=fallback_types,
            source=source,
            outcome="degraded_to_text",
            degraded_from=original_type,
        )
        trace_event(
            "outbound",
            "降级发送完成",
            status="degraded",
            output={
                "delivery_state": DELIVERY_DEGRADED_SUCCESS,
                "segment_types": fallback_types,
                "message_id": extract_message_id(raw_result),
                "degraded_from": original_type,
            },
        )
        return SendResult(
            sent=True,
            message_id=extract_message_id(raw_result),
            normalized_text=fallback.normalized_text,
            segment_types=fallback_types,
            message_type=fallback_type,
            outcome="degraded_to_text",
            delivery_state=DELIVERY_DEGRADED_SUCCESS,
            degraded_from=original_type,
            segments=fallback.segment_records,
        )
    finally:
        for path in prepared.temporary_files:
            path.unlink(missing_ok=True)


async def prepare_forward_message(
    raw_nodes: Any,
    *,
    session: Any,
    group_id: int,
) -> PreparedForwardMessage:
    """把受限 forward spec 转成 OneBot node/node_custom。

    模型不能提交原始 OneBot 节点，也不能自填 nickname。引用节点只能指向当前群
    近期已知消息；自定义节点的 nickname 由当前群成员记录解析。
    """

    if not isinstance(raw_nodes, list) or not raw_nodes:
        raise ValueError("nodes 必须是非空数组")
    if len(raw_nodes) > MAX_FORWARD_NODES:
        raise ValueError(f"合并转发最多 {MAX_FORWARD_NODES} 个节点")
    if any(not isinstance(item, dict) for item in raw_nodes):
        raise ValueError("nodes 中每一项都必须是对象")

    nodes: list[MessageSegment] = []
    tree: list[dict[str, Any]] = []
    text_parts: list[str] = []
    for raw in raw_nodes:
        node_type = str(raw.get("type") or "").strip().lower()
        if node_type in {"message", "node", "reference"}:
            _require_exact_keys(
                raw,
                required={"type", "message_id"},
                allowed={"type", "message_id"},
            )
            message_id = _bounded_int(
                raw["message_id"],
                field="message_id",
                minimum=-(2**63) + 1,
                maximum=2**63 - 1,
            )
            if message_id == 0:
                raise ValueError("message_id 不能为 0")
            target = await _get_known_message(session, group_id, message_id)
            nodes.append(MessageSegment.node(message_id))
            content = str(target.normalized_text or "")[:4000]
            tree.append(
                {
                    "message_id": message_id,
                    "user_id": int(target.user_id),
                    "nickname": str(target.sender_name or target.user_id)[:64],
                    "content": content,
                    "segments": [],
                    "children": [],
                    "depth": 0,
                    "source": "reference",
                }
            )
            if content:
                text_parts.append(content)
            continue

        if node_type in {"custom", "node_custom"}:
            _require_exact_keys(
                raw,
                required={"type", "user_id", "content"},
                allowed={"type", "user_id", "content"},
            )
            user_id = _bounded_int(
                raw["user_id"], field="user_id", minimum=1, maximum=2**63 - 1
            )
            nickname = await _known_member_name(session, group_id, user_id)
            content = str(raw["content"]).strip()
            if not content:
                raise ValueError("自定义转发节点 content 不能为空")
            if len(content) > MAX_OUTBOUND_TEXT_CHARS:
                raise ValueError("自定义转发节点 content 超过长度限制")
            nodes.append(
                MessageSegment.node_custom(user_id, nickname, Message(content))
            )
            tree.append(
                {
                    "user_id": user_id,
                    "nickname": nickname,
                    "content": content,
                    "segments": [
                        _segment_record("text", {"text": content}, content)
                    ],
                    "children": [],
                    "depth": 0,
                    "source": "custom",
                }
            )
            text_parts.append(content)
            continue

        raise ValueError("转发节点 type 只能是 message 或 custom")

    if len(json.dumps(tree, ensure_ascii=False).encode("utf-8")) > MAX_FORWARD_BYTES:
        raise ValueError("转发内容超过 128 KiB 限制")
    return PreparedForwardMessage(
        nodes=tuple(nodes),
        normalized_text="\n".join(text_parts)[:MAX_OUTBOUND_TEXT_CHARS],
        forward_tree=tuple(tree),
    )


async def send_prepared_forward(
    bot: Any,
    group_id: int,
    prepared: PreparedForwardMessage,
    *,
    session: Any = None,
    actor_user_id: int | None = None,
    source: str = "agent",
) -> SendResult:
    try:
        result = await asyncio.wait_for(
            bot.call_api(
                "send_group_forward_msg",
                group_id=group_id,
                messages=list(prepared.nodes),
            ),
            timeout=SEND_TIMEOUT_SECONDS,
        )
    except Exception as exc:
        if _is_ambiguous_delivery_error(exc):
            await _audit_outbound(
                session,
                group_id,
                actor_user_id,
                message_type="forward",
                segment_types=("forward",),
                source=source,
                outcome="delivery_unknown",
                detail=type(exc).__name__,
            )
            return SendResult(
                sent=False,
                message_id=None,
                normalized_text=prepared.normalized_text,
                segment_types=("forward",),
                message_type="forward",
                outcome="delivery_unknown",
                delivery_state=DELIVERY_UNKNOWN,
                forward_tree=prepared.forward_tree,
            )
        await _audit_outbound(
            session,
            group_id,
            actor_user_id,
            message_type="forward",
            segment_types=("forward",),
            source=source,
            outcome="send_failed",
            detail=type(exc).__name__,
        )
        raise
    await _audit_outbound(
        session,
        group_id,
        actor_user_id,
        message_type="forward",
        segment_types=("forward",),
        source=source,
        outcome="success",
    )
    return SendResult(
        sent=True,
        message_id=extract_message_id(result),
        normalized_text=prepared.normalized_text,
        segment_types=("forward",),
        message_type="forward",
        outcome="success",
        forward_tree=prepared.forward_tree,
    )


async def send_forward_message(
    bot: Any,
    group_id: int,
    raw_nodes: Any,
    *,
    session: Any,
    actor_user_id: int | None = None,
    source: str = "agent",
) -> SendResult:
    try:
        prepared = await prepare_forward_message(
            raw_nodes, session=session, group_id=group_id
        )
    except Exception as exc:
        await _audit_outbound(
            session,
            group_id,
            actor_user_id,
            message_type="forward",
            segment_types=("forward",),
            source=source,
            outcome="validation_failed",
            detail=type(exc).__name__,
        )
        raise
    return await send_prepared_forward(
        bot,
        group_id,
        prepared,
        session=session,
        actor_user_id=actor_user_id,
        source=source,
    )


async def send_outbound_message(
    bot: Any,
    group_id: int,
    raw_segments: Any,
    *,
    session: Any,
    actor_user_id: int | None = None,
    source: str = "agent",
) -> SendResult:
    caps = get_segment_capabilities(bot, group_id)
    try:
        prepared = await prepare_outbound_message(
            raw_segments,
            session=session,
            group_id=group_id,
            actor_user_id=actor_user_id,
            allowed_segment_types=caps.allowed,
        )
    except Exception as exc:
        raw_types = tuple(
            str(item.get("type") or "").strip().lower()
            for item in raw_segments
            if isinstance(item, dict)
        ) if isinstance(raw_segments, list) else ()
        await _audit_outbound(
            session,
            group_id,
            actor_user_id,
            message_type=classify_message_type(list(raw_types)),
            segment_types=raw_types,
            source=source,
            outcome="validation_failed",
            detail=type(exc).__name__,
        )
        raise
    return await send_prepared_outbound(
        bot,
        group_id,
        prepared,
        session=session,
        actor_user_id=actor_user_id,
        source=source,
    )


__all__ = [
    "MAX_FORWARD_BYTES",
    "MAX_FORWARD_NODES",
    "MAX_OUTBOUND_MEDIA_SEGMENTS",
    "MAX_OUTBOUND_SEGMENTS",
    "MAX_OUTBOUND_TEXT_CHARS",
    "PreparedForwardMessage",
    "PreparedOutboundMessage",
    "SendResult",
    "extract_message_id",
    "prepare_forward_message",
    "prepare_outbound_message",
    "prepare_speech_plan",
    "prepare_text_message",
    "send_forward_message",
    "send_outbound_message",
    "send_prepared_forward",
    "send_prepared_outbound",
]
