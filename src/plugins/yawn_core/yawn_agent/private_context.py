# ruff: noqa: TID252, TC003
"""私聊 Agent 的上下文：Bot 对这位用户的了解，从签到、好感、群里的关系和记忆汇总。

边界（与 AGENTS.md「私聊 Agent」一节一致）：

- 只读用户**本人是活跃成员**的群；群里 Agent 总开关关着、或用户在该群隐私退出的，
  这个群的关系和画像一律不读（群名仍可列出，签到也照常）。
- 只给词语和天数，不给好感/熟悉度数值。
- 群里的记忆是不可信数据，再过一遍 ``memory_hygiene``，超长截断。
- 整个上下文有字符预算，超出时按优先级丢弃。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from sqlalchemy import select

from .. import affinity
from ..checkin_service import checkin_overview
from ..data_models.agent_bot_bond import AgentBotBond
from ..data_models.agent_memory import AgentMemory, AgentPrivacy
from ..data_models.bot_group import BotGroup
from ..data_models.bot_user import BotUser
from ..data_models.chat_message import ChatMessage
from ..data_models.chat_session import ChatSession
from ..data_models.group_agent_config import GroupAgentConfig
from ..data_models.user_group import UserGroup
from .bot_bond import bond_summary
from .memory_hygiene import contains_pii, looks_like_instruction, scrub_pii
from .private_memory import fact_view, list_facts

# 整块上下文的字符上限（JSON）。
CONTEXT_CHAR_BUDGET = 2400
MAX_GROUPS = 8
MAX_PROFILE_FACTS = 8
MAX_JOKES = 3
_PROFILE_TYPES = ("core", "profile")
_FACT_CHARS = 80


@dataclass(slots=True)
class SharedGroup:
    """用户与 Bot 共同所在的群，以及该群里能不能读 Agent 数据。"""

    group_id: int
    name: str
    agent_readable: bool
    nickname: str | None = None


@dataclass(slots=True)
class PrivateContext:
    payload: dict[str, Any]
    groups: list[SharedGroup] = field(default_factory=list)
    queries: int = 0

    def readable_group_ids(self) -> list[int]:
        return [item.group_id for item in self.groups if item.agent_readable]

    def render(self) -> str:
        return json.dumps(self.payload, ensure_ascii=False, separators=(",", ":"))


async def shared_groups(session: Any, user_id: int) -> list[SharedGroup]:
    """用户活跃所在的群（最近活跃在前），带“能否读 Agent 数据”的标记。"""

    rows = (
        await session.execute(
            select(
                UserGroup.group_id,
                UserGroup.group_nickname,
                BotGroup.group_name,
                GroupAgentConfig.enabled,
                AgentPrivacy.opted_out,
            )
            .join(BotGroup, BotGroup.group_id == UserGroup.group_id)
            .outerjoin(
                GroupAgentConfig, GroupAgentConfig.group_id == UserGroup.group_id
            )
            .outerjoin(
                AgentPrivacy,
                (AgentPrivacy.group_id == UserGroup.group_id)
                & (AgentPrivacy.user_id == UserGroup.user_id),
            )
            .where(UserGroup.user_id == user_id, UserGroup.is_active.is_(True))
            .order_by(UserGroup.last_seen_at.desc().nullslast())
        )
    ).all()
    return [
        SharedGroup(
            group_id=int(group_id),
            name=str(group_name or f"群 {group_id}"),
            agent_readable=bool(enabled) and not bool(opted_out),
            nickname=str(nickname) if nickname else None,
        )
        for group_id, nickname, group_name, enabled, opted_out in rows
    ]


def _safe_text(text: object, limit: int = _FACT_CHARS) -> str | None:
    value = str(text or "").strip()
    if not value or looks_like_instruction(value):
        return None
    if contains_pii(value):
        value = scrub_pii(value)
    return value[:limit] or None


async def group_profile_facts(
    session: Any, user_id: int, group_ids: list[int], *, now: datetime
) -> list[dict[str, Any]]:
    """各群画像里关于本人的事实，按类别去重（显著性高者优先）。"""

    if not group_ids:
        return []
    rows = (
        (
            await session.execute(
                select(AgentMemory)
                .where(
                    AgentMemory.group_id.in_(group_ids),
                    AgentMemory.subject_user_id == user_id,
                    AgentMemory.memory_type.in_(_PROFILE_TYPES),
                )
                .order_by(AgentMemory.salience.desc(), AgentMemory.updated_at.desc())
                .limit(60)
            )
        )
        .scalars()
        .all()
    )
    seen: set[str] = set()
    facts: list[dict[str, Any]] = []
    for row in rows:
        if row.expires_at is not None and row.expires_at < now:
            continue
        key = str(row.memory_key)
        if key in seen:
            continue
        content = _safe_text(row.content)
        if content is None:
            continue
        seen.add(key)
        facts.append(
            {"key": key, "content": content, "group_id": int(row.group_id or 0)}
        )
        if len(facts) >= MAX_PROFILE_FACTS:
            break
    return facts


async def group_bonds(
    session: Any,
    user_id: int,
    bot_id: int | None,
    groups: list[SharedGroup],
    *,
    now: datetime,
) -> list[dict[str, Any]]:
    """各群里 Bot 与本人的关系概况（词语），按熟悉度降序。"""

    readable = {item.group_id: item for item in groups if item.agent_readable}
    if not readable or bot_id is None:
        return []
    try:
        rows = (
            (
                await session.execute(
                    select(AgentBotBond).where(
                        AgentBotBond.user_id == user_id,
                        AgentBotBond.bot_id == bot_id,
                        AgentBotBond.group_id.in_(sorted(readable)),
                    )
                )
            )
            .scalars()
            .all()
        )
    except Exception:  # noqa: BLE001 - 关系表未迁移时整段跳过
        return []
    views: list[tuple[float, dict[str, Any]]] = []
    for bond in rows:
        summary = bond_summary(bond, now)
        view: dict[str, Any] = {
            "group": readable[int(bond.group_id)].name,
            "stage": summary["stage_label"],
        }
        for source, target in (
            ("label", "label"),
            ("member_calls_bot", "calls_you"),
            ("bot_calls_member", "you_call"),
            ("tone", "tone"),
        ):
            text = _safe_text(summary.get(source), 40)
            if text:
                view[target] = text
        jokes = [
            j
            for j in (_safe_text(x, 30) for x in summary["inside_jokes"][:MAX_JOKES])
            if j
        ]
        if jokes:
            view["inside_jokes"] = jokes
        boundaries = [
            b for b in (_safe_text(x, 40) for x in summary["boundaries"]) if b
        ]
        if boundaries:
            view["boundaries"] = boundaries
        views.append((float(summary["familiarity"]), view))
    views.sort(key=lambda item: -item[0])
    return [view for _score, view in views]


def _fit(payload: dict[str, Any]) -> dict[str, Any]:
    """按优先级裁剪到字符预算：先缩画像，再缩关系，最后缩群列表。"""

    def size() -> int:
        return len(json.dumps(payload, ensure_ascii=False, separators=(",", ":")))

    for key in ("group_profile", "bonds_in_groups", "groups", "assistant"):
        while size() > CONTEXT_CHAR_BUDGET and payload.get(key):
            payload[key] = payload[key][:-1]
        if not payload.get(key):
            payload.pop(key, None)
    return payload


LAST_CHAT_GAP_MINUTES = 30


def _ago(minutes: float) -> str:
    if minutes < 60:  # noqa: PLR2004
        return f"{int(minutes)} 分钟前"
    hours = minutes / 60
    if hours < 48:  # noqa: PLR2004
        return f"{int(hours)} 小时前"
    return f"{int(hours // 24)} 天前"


async def last_chat(session: Any, user_id: int, now: datetime) -> dict[str, Any] | None:
    """上次私聊里 Bot 说的话和距今多久；隔得不久（同一段对话里）就不给。"""

    row = (
        await session.execute(
            select(ChatMessage.content, ChatMessage.created_at)
            .join(ChatSession, ChatSession.id == ChatMessage.session_id)
            .where(
                ChatSession.user_id == user_id,
                ChatSession.group_id.is_(None),
                ChatMessage.role == "assistant",
                ChatMessage.is_deleted.is_(False),
            )
            .order_by(ChatMessage.created_at.desc())
            .limit(1)
        )
    ).first()
    if row is None or row.created_at is None:
        return None
    minutes = (now - row.created_at).total_seconds() / 60
    if minutes < LAST_CHAT_GAP_MINUTES:
        return None
    said = _safe_text(row.content, 60)
    payload: dict[str, Any] = {"ago": _ago(minutes)}
    if said:
        payload["you_said"] = said
    return payload


async def build_private_context(  # noqa: C901
    session: Any,
    user_id: int,
    *,
    bot_id: int | None,
    nickname: str | None,
    now: datetime,
) -> PrivateContext:
    """组装私聊上下文。只读，不写库。"""

    user = await session.get(BotUser, user_id)
    groups = await shared_groups(session, user_id)
    snap = await affinity.snapshot(session, user_id, now=now)
    statuses = await checkin_overview(session, user_id, now=now)
    readable_ids = [item.group_id for item in groups if item.agent_readable]
    profile = await group_profile_facts(session, user_id, readable_ids, now=now)
    bonds = await group_bonds(session, user_id, bot_id, groups, now=now)
    private_facts = [
        fact_view(row) for row in await list_facts(session, user_id, now=now)
    ]

    payload: dict[str, Any] = {
        "now": f"{now:%Y-%m-%d %H:%M}",
        "user": {
            "name": _safe_text(nickname or (user.nickname if user else None), 30)
            or "这位用户",
        },
        "relationship": {
            "level": snap.level.name,
            "how_to_treat": affinity.LEVEL_TONE.get(snap.level.name, ""),
        },
    }
    if user is not None and user.first_interaction_at:
        payload["user"]["known_since"] = f"{user.first_interaction_at:%Y-%m-%d}"
    if statuses:
        best = max(statuses, key=lambda item: item.streak_days)
        payload["checkin"] = {
            "groups": len(statuses),
            "checked_today": sum(1 for item in statuses if item.checked_today),
            "best_streak_days": best.streak_days,
        }
    if private_facts:
        payload["told_you_in_private"] = [
            {"what": item["label"], "content": item["content"]}
            for item in private_facts
        ]
    if bonds:
        payload["bonds_in_groups"] = bonds
    if profile:
        name_by_id = {item.group_id: item.name for item in groups}
        payload["group_profile"] = [
            {
                "key": item["key"],
                "content": item["content"],
                "group": name_by_id.get(item["group_id"], ""),
            }
            for item in profile
        ]
    if groups:
        payload["groups"] = [item.name for item in groups[:MAX_GROUPS]]
    payload["time_of_day"] = time_of_day(now)
    previous = await last_chat(session, user_id, now)
    if previous:
        payload["last_chat"] = previous
    tasks = _tasks_digest(user_id, now)
    if tasks:
        payload["tasks"] = tasks
    assistant = _assistant_digest(user_id, now)
    if assistant:
        style = assistant.get("chat_style") or {}
        if style.get("call_me"):
            payload["user"]["call_them"] = style["call_me"]
        payload["assistant"] = [assistant]
    return PrivateContext(payload=_fit(payload), groups=groups, queries=8)


def time_of_day(now: datetime) -> str:  # noqa: PLR0911
    hour = now.hour
    if hour < 5:  # noqa: PLR2004
        return "深夜"
    if hour < 9:  # noqa: PLR2004
        return "早上"
    if hour < 12:  # noqa: PLR2004
        return "上午"
    if hour < 14:  # noqa: PLR2004
        return "中午"
    if hour < 18:  # noqa: PLR2004
        return "下午"
    if hour < 23:  # noqa: PLR2004
        return "晚上"
    return "深夜"


def _tasks_digest(user_id: int, now: datetime) -> list[dict[str, Any]]:
    """后台小助手任务：没结束的 + 最近结束的；读失败不影响本轮。"""

    from .private_tasks import context_digest

    try:
        return context_digest(user_id, now)
    except Exception:  # noqa: BLE001
        return []


def _assistant_digest(user_id: int, now: datetime) -> dict[str, Any]:
    """个人助理摘要（偏好、待办、提醒、倒数日）；读失败不影响本轮。"""

    from . import private_store
    from .private_organizer import prompt_digest

    try:
        return prompt_digest(private_store.load(user_id), now)
    except Exception:  # noqa: BLE001
        return {}


__all__ = [
    "CONTEXT_CHAR_BUDGET",
    "PrivateContext",
    "SharedGroup",
    "build_private_context",
    "group_bonds",
    "group_profile_facts",
    "shared_groups",
]
