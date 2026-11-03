# ruff: noqa: TID252, TC003, E501, C901, PLR0912, PLR0913, PLR0915, PLW2901
"""私聊 Agent 的上下文：Bot 对这位用户的了解，从签到、好感、群里的关系和记忆汇总。

边界（与 AGENTS.md「私聊 Agent」一节一致）：

- 只读用户**本人是活跃成员**的群；群里 Agent 总开关关着、或用户在该群隐私退出的，
  这个群的关系和画像一律不读（群名仍可列出，签到也照常）。
- 只给词语和天数，不给好感/熟悉度数值。
- 群里的记忆是不可信数据，再过一遍 ``memory_hygiene``，超长截断。
- 整个上下文有字符预算，超出时按优先级丢弃。
"""

from __future__ import annotations

import contextlib
import json
import re
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

# 每一层上下文的字符上限（JSON）：长期资料一份、本轮资料一份。
CONTEXT_CHAR_BUDGET = 2400
# 长期资料（几乎不变，放在历史前面，前缀缓存能命中）里的键；其余都算本轮资料。
STABLE_KEYS = (
    "user",
    "relationship",
    "told_you_in_private",
    "group_profile",
    "bonds_in_groups",
    "people_you_both_know",
    "group_happenings",
    "groups",
    "names_in_groups",
    "memory_conflicts",
    "memory_rules",
)
MAX_PEOPLE = 6
MAX_HAPPENINGS = 3
HAPPENING_DAYS = 3
_HAPPENING_CHARS = 120
_LINE_CHARS = 60
# 私聊里不主动提的关系类型（尴尬或容易冒犯）。
_QUIET_RELATIONS = frozenset({"对立"})
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

    def stable(self) -> dict[str, Any]:
        """长期资料：只有记忆整理、关系变化时才变。键顺序固定，渲染结果逐字稳定。"""

        return {key: self.payload[key] for key in STABLE_KEYS if key in self.payload}

    def volatile(self) -> dict[str, Any]:
        """本轮资料：时间、签到、待办、附件、刚做完的事……每轮都可能变。"""

        return {
            key: value for key, value in self.payload.items() if key not in STABLE_KEYS
        }

    def render_stable(self) -> str:
        stable = _fit_layer(self.stable(), _STABLE_TRIM)
        return json.dumps(stable, ensure_ascii=False, separators=(",", ":")) if stable else ""

    def render_volatile(self) -> str:
        volatile = _fit_layer(self.volatile(), _VOLATILE_TRIM)
        return (
            json.dumps(volatile, ensure_ascii=False, separators=(",", ":"))
            if volatile
            else ""
        )


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


async def superuser_tool_groups(
    session: Any, user_id: int, known: list[SharedGroup]
) -> list[SharedGroup]:
    """超级管理员的私聊工具范围：他所在的群 + Bot 认识的其他群（只给工具，不进提示词）。

    普通用户原样返回 ``known``。补进来的群按 Agent 是否开着标 ``agent_readable``，
    他本人在那些群没有隐私退出记录（不在群里），群名后不带他的群名片。
    """

    from ..superuser import superuser_full_access

    if session is None or not superuser_full_access(user_id):
        return list(known)
    seen = {group.group_id for group in known}
    try:
        rows = (
            await session.execute(
                select(BotGroup.group_id, BotGroup.group_name, GroupAgentConfig.enabled)
                .outerjoin(
                    GroupAgentConfig, GroupAgentConfig.group_id == BotGroup.group_id
                )
                .order_by(BotGroup.last_active_at.desc().nullslast(), BotGroup.group_id)
            )
        ).all()
    except Exception:  # noqa: BLE001
        return list(known)
    extra = [
        SharedGroup(
            group_id=int(group_id),
            name=str(group_name or f"群 {group_id}"),
            agent_readable=bool(enabled),
        )
        for group_id, group_name, enabled in rows
        if int(group_id) not in seen
    ]
    return [*known, *extra]


def _safe_text(text: object, limit: int = _FACT_CHARS) -> str | None:
    value = str(text or "").strip()
    if not value or looks_like_instruction(value):
        return None
    if contains_pii(value):
        value = scrub_pii(value)
    return value[:limit] or None


# 能跨群合并的列表类画像（爱好可能分散在几个群里）。
_PROFILE_LIST_KEYS = frozenset({"hobby", "preference", "dislike", "skill", "recurring_topic"})
_PROFILE_LIST_MAX = 6


async def group_profile_facts(
    session: Any,
    user_id: int,
    group_ids: list[int],
    *,
    now: datetime,
    link: Any = None,
) -> list[dict[str, Any]]:
    """各群画像里关于本人的事实。

    单值类别（称呼、近期状态……）按显著性取一条；列表类（爱好、喜好……）跨群合并去重，
    不再“哪个群先到就只给哪个群的”。他让我忘掉的（``memory_link`` 墓碑）在私聊里不再提。
    """

    from .memory_hygiene import is_near_duplicate
    from .memory_link import suppressed

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
    by_key: dict[str, dict[str, Any]] = {}
    order: list[str] = []
    for row in rows:
        if row.expires_at is not None and row.expires_at < now:
            continue
        key = str(row.memory_key)
        content = _safe_text(row.content)
        if content is None:
            continue
        if link is not None and suppressed(user_id, key, content, now=now, state=link):
            continue
        updated = row.updated_at or row.created_at
        existing = by_key.get(key)
        if existing is None:
            if len(order) >= MAX_PROFILE_FACTS:
                continue
            by_key[key] = {
                "key": key,
                "content": content,
                "group_id": int(row.group_id or 0),
                "id": int(row.id or 0),
                "ids": [int(row.id or 0)],
                "group_ids": [int(row.group_id or 0)],
                "self_told": row.source_kind == "agent",
                "as_of": f"{updated:%Y-%m-%d}" if updated else "",
            }
            order.append(key)
            continue
        if key not in _PROFILE_LIST_KEYS:
            continue
        parts = [part for part in existing["content"].split("、") if part.strip()]
        added = False
        for part in content.split("、"):
            part = part.strip()
            if part and not any(is_near_duplicate(part, item) for item in parts):
                parts.append(part)
                added = True
        if added:
            existing["content"] = "、".join(parts[:_PROFILE_LIST_MAX])[:_FACT_CHARS]
            existing["ids"].append(int(row.id or 0))
            if int(row.group_id or 0) not in existing["group_ids"]:
                existing["group_ids"].append(int(row.group_id or 0))
            existing["self_told"] = existing["self_told"] or row.source_kind == "agent"
    return [by_key[key] for key in order]


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
    """去掉空列表；真正的裁剪按层做（``render_stable`` / ``render_volatile``）。"""

    for key in [key for key, value in payload.items() if value in ([], {}, None)]:
        payload.pop(key, None)
    return payload


# 超预算时先缩谁（越靠前越先缩）。
_STABLE_TRIM = (
    "names_in_groups",
    "memory_conflicts",
    "group_happenings",
    "people_you_both_know",
    "group_profile",
    "bonds_in_groups",
    "groups",
    "told_you_in_private",
)
_VOLATILE_TRIM = (
    "recent_in_groups",
    "helper_messages",
    "assistant",
    "tasks",
    "just_finished",
)


def _fit_layer(layer: dict[str, Any], order: tuple[str, ...]) -> dict[str, Any]:
    """按优先级把一层裁到字符预算（从列表尾部一条条去掉）；不改原 payload。"""

    layer = {
        key: (list(value) if isinstance(value, list) else value)
        for key, value in layer.items()
    }

    def size() -> int:
        return len(json.dumps(layer, ensure_ascii=False, separators=(",", ":")))

    for key in order:
        while size() > CONTEXT_CHAR_BUDGET and isinstance(layer.get(key), list) and layer[key]:
            layer[key] = layer[key][:-1]
        if key in layer and not layer[key]:
            layer.pop(key, None)
    return layer


async def _names_in_group(
    session: Any, group_id: int, user_ids: set[int]
) -> dict[int, str]:
    """群名片优先，没有就用 QQ 昵称；都没有的不给（不报 QQ 号）。"""

    if not user_ids:
        return {}
    rows = (
        await session.execute(
            select(UserGroup.user_id, UserGroup.group_nickname, BotUser.nickname)
            .outerjoin(BotUser, BotUser.user_id == UserGroup.user_id)
            .where(UserGroup.group_id == group_id, UserGroup.user_id.in_(sorted(user_ids)))
        )
    ).all()
    names: dict[int, str] = {}
    for uid, card, nickname in rows:
        name = _safe_text(card or nickname, 20)
        if name:
            names[int(uid)] = name
    return names


async def _opted_out(session: Any, group_id: int) -> set[int]:
    rows = (
        await session.execute(
            select(AgentPrivacy.user_id).where(
                AgentPrivacy.group_id == group_id, AgentPrivacy.opted_out.is_(True)
            )
        )
    ).scalars()
    return {int(uid) for uid in rows}


async def people_you_both_know(
    session: Any, user_id: int, groups: list[SharedGroup], *, now: datetime
) -> list[dict[str, Any]]:
    """群里记下的、他和别人的关系（好友/搭子/同学……），只给名字和关系词。

    只看可读的群；对方在那个群隐私退出的跳过；@提及这类噪声边和“对立”不给；
    半年没再见到的旧关系不给。按置信度、证据数排序后再按群名、名字固定顺序。
    """

    from datetime import timedelta

    from ..data_models.agent_memory import AgentRelation
    from .memory import RELATION_TTL_DAYS, RELATION_TYPE_CHOICES

    readable = {item.group_id: item for item in groups if item.agent_readable}
    if not readable:
        return []
    try:
        rows = (
            (
                await session.execute(
                    select(AgentRelation)
                    .where(
                        AgentRelation.group_id.in_(sorted(readable)),
                        (AgentRelation.subject_user_id == user_id)
                        | (AgentRelation.object_user_id == user_id),
                        AgentRelation.relation_type.in_(RELATION_TYPE_CHOICES),
                        AgentRelation.last_seen_at
                        >= now - timedelta(days=RELATION_TTL_DAYS),
                    )
                    .order_by(
                        AgentRelation.confidence.desc(),
                        AgentRelation.evidence_count.desc(),
                        AgentRelation.id.asc(),
                    )
                    .limit(40)
                )
            )
            .scalars()
            .all()
        )
    except Exception:  # noqa: BLE001 - 关系表缺失时整段跳过
        return []
    picked: list[tuple[int, int, str, str]] = []
    seen: set[tuple[int, str]] = set()
    for row in rows:
        if str(row.relation_type) in _QUIET_RELATIONS:
            continue
        other = (
            int(row.object_user_id)
            if int(row.subject_user_id) == user_id
            else int(row.subject_user_id)
        )
        if other == user_id or (other, str(row.relation_type)) in seen:
            continue
        seen.add((other, str(row.relation_type)))
        picked.append((int(row.group_id), other, str(row.relation_type), str(row.note or "")))
        if len(picked) >= MAX_PEOPLE * 2:
            break
    people: list[dict[str, Any]] = []
    by_group: dict[int, list[tuple[int, int, str, str]]] = {}
    for item in picked:
        by_group.setdefault(item[0], []).append(item)
    for group_id, items in by_group.items():
        blocked = await _opted_out(session, group_id)
        names = await _names_in_group(
            session, group_id, {other for _g, other, _t, _n in items} - blocked
        )
        for _g, other, relation, note in items:
            name = names.get(other)
            if not name or other in blocked:
                continue
            view: dict[str, Any] = {
                "name": name,
                "relation": relation,
                "group": readable[group_id].name,
            }
            clean_note = _safe_text(note, 40)
            if clean_note:
                view["note"] = clean_note
            people.append(view)
    people = people[:MAX_PEOPLE]
    people.sort(key=lambda item: (item["group"], item["name"], item["relation"]))
    return people


_HAPPENING_DIGITS = re.compile(r"\d{5,}")


async def group_happenings(
    session: Any,
    groups: list[SharedGroup],
    *,
    now: datetime,
    user_id: int | None = None,
    link: Any = None,
) -> list[dict[str, Any]]:
    """可读的群最近几天发生了什么：每群取最新一条“可跨群分享”的公开摘要。

    用 ``public_daily``（整理时就按跨群分享的标准写、过了卫生检查），不用群内部的
    ``daily``；读时再复检一遍注入/联系方式，长串数字打码。按群名固定顺序。
    """

    from datetime import timedelta

    readable = {item.group_id: item for item in groups if item.agent_readable}
    if not readable:
        return []
    since = (now - timedelta(days=HAPPENING_DAYS)).date()
    rows = (
        (
            await session.execute(
                select(AgentMemory)
                .where(
                    AgentMemory.group_id.in_(sorted(readable)),
                    AgentMemory.subject_user_id == 0,
                    AgentMemory.memory_type == "summary",
                    AgentMemory.memory_key.like("public_daily:%"),
                    AgentMemory.memory_key >= f"public_daily:{since:%Y-%m-%d}",
                )
                .order_by(AgentMemory.memory_key.desc(), AgentMemory.id.desc())
                .limit(len(readable) * 3)
            )
        )
        .scalars()
        .all()
    )
    picked: dict[int, dict[str, Any]] = {}
    for row in rows:
        group_id = int(row.group_id or 0)
        if group_id in picked or (row.expires_at is not None and row.expires_at < now):
            continue
        text = _safe_text(row.content, _HAPPENING_CHARS)
        if not text:
            continue
        if link is not None and user_id and _text_forgotten(user_id, text, now, link):
            continue
        item: dict[str, Any] = {
            "group": readable[group_id].name,
            "day": str(row.memory_key).split(":", 1)[-1],
            "what": _HAPPENING_DIGITS.sub("[号码]", text),
        }
        # 那天他也在场（摘要的相关成员里有他）：私聊里可以说“你们那天……”，而不是当新闻讲。
        if user_id and int(user_id) in {int(uid) for uid in row.related_user_ids or []}:
            item["you_were_there"] = True
        picked[group_id] = item
    items = sorted(picked.values(), key=lambda item: (item["group"], item["day"]))
    return items[:MAX_HAPPENINGS]


async def recent_group_lines(
    session: Any,
    user_id: int,
    groups: list[SharedGroup],
    *,
    now: datetime,
    limit: int,
    hours: int,
    link: Any = None,
) -> list[dict[str, Any]]:
    """他最近在群里说过的几句话（只是他自己的话），私聊里能自然接上“你刚在群里说……”。"""

    from datetime import timedelta

    from ..data_models.group_agent_message import GroupAgentMessage

    readable = {item.group_id: item for item in groups if item.agent_readable}
    if not readable or limit <= 0:
        return []
    try:
        rows = (
            await session.execute(
                select(
                    GroupAgentMessage.group_id,
                    GroupAgentMessage.normalized_text,
                    GroupAgentMessage.received_at,
                )
                .where(
                    GroupAgentMessage.user_id == user_id,
                    GroupAgentMessage.group_id.in_(sorted(readable)),
                    GroupAgentMessage.role != "bot",
                    GroupAgentMessage.received_at >= now - timedelta(hours=hours),
                )
                .order_by(GroupAgentMessage.received_at.desc(), GroupAgentMessage.id.desc())
                .limit(limit * 4)
            )
        ).all()
    except Exception:  # noqa: BLE001 - 消息表缺失时整段跳过
        return []
    lines: list[dict[str, Any]] = []
    seen: set[str] = set()
    for group_id, text, received_at in rows:
        raw = str(text or "").strip()
        # 只有占位符（[图片]）或很短的“哈哈”不值得带。
        if len(raw.replace("[图片]", "").replace("[表情]", "").strip()) < 4:  # noqa: PLR2004
            continue
        clean = _safe_text(raw, _LINE_CHARS)
        if not clean or clean in seen:
            continue
        if link is not None and _text_forgotten(user_id, clean, now, link):
            continue
        seen.add(clean)
        minutes = (now - received_at).total_seconds() / 60 if received_at else 0
        lines.append(
            {
                "group": readable[int(group_id)].name,
                "said": _HAPPENING_DIGITS.sub("[号码]", clean),
                "ago": _ago(max(minutes, 1)),
            }
        )
        if len(lines) >= limit:
            break
    return lines


def _text_forgotten(user_id: int, text: str, now: datetime, link: Any) -> bool:
    from .memory_link import text_suppressed

    return text_suppressed(user_id, text, now=now, state=link)


def visible_groups(groups: list[SharedGroup], link: Any) -> list[SharedGroup]:
    """私聊里能提的群：可读，且他本人没说“别在私聊提（这个）群的事”。"""

    if link is None:
        return list(groups)
    return [
        SharedGroup(
            group_id=item.group_id,
            name=item.name,
            agent_readable=item.agent_readable and link.group_visible_in_private(item.group_id),
            nickname=item.nickname,
        )
        for item in groups
    ]


_SINGLE_VALUE_KEYS = ("display_name", "preferred_address", "current_status")


def resolve_call_them(
    private_facts: list[dict[str, Any]],
    assistant_style: dict[str, Any],
    profile: list[dict[str, Any]],
) -> str | None:
    """他希望我怎么叫他：私聊里亲口说的 > 个人助理里设的 > 群里亲口说的称呼。"""

    for item in private_facts:
        if item.get("key") == "preferred_address" and item.get("content"):
            return str(item["content"])[:20]
    if assistant_style.get("call_me"):
        return str(assistant_style["call_me"])[:20]
    for item in profile:
        if item.get("key") == "preferred_address" and item.get("self_told"):
            return str(item["content"])[:20]
    return None


def merge_with_private(
    profile: list[dict[str, Any]], private_facts: list[dict[str, Any]]
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """群画像与私聊事实合并：私聊里亲口说的优先。

    - 内容和私聊事实近似重复的群画像不再重复给；
    - 单值类别（称呼、近期状态）私聊里有就不再给群里的版本，不同时记一条 conflict，
      让模型知道“群里还记着旧说法”，必要时问一句；
    - 列表类去掉已在私聊事实里的那几项。
    """

    from .memory_hygiene import is_near_duplicate

    by_key = {str(item.get("key")): str(item.get("content") or "") for item in private_facts}
    kept: list[dict[str, Any]] = []
    conflicts: list[dict[str, Any]] = []
    for item in profile:
        key = str(item["key"])
        content = str(item["content"])
        private = by_key.get(key)
        if any(is_near_duplicate(content, other) or content == other for other in by_key.values()):
            continue
        if private is not None and key in _SINGLE_VALUE_KEYS:
            conflicts.append({"key": key, "private": private[:40], "group": content[:40], "group_id": item["group_id"]})
            continue
        if private is not None and key in _PROFILE_LIST_KEYS:
            private_parts = [p for p in private.split("、") if p.strip()]
            parts = [
                part
                for part in content.split("、")
                if part.strip() and not any(is_near_duplicate(part, p) for p in private_parts)
            ]
            if not parts:
                continue
            item = {**item, "content": "、".join(parts)}
        kept.append(item)
    return kept, conflicts


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


async def build_private_context(
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
    link = _link_state(user_id, now)
    # 群里来的东西只读“私聊里能提”的群；groups 本身（群名、签到、工具选群）照旧用全部。
    seen_groups = visible_groups(groups, link)
    snap = await affinity.snapshot(session, user_id, now=now)
    statuses = await checkin_overview(session, user_id, now=now)
    readable_ids = [item.group_id for item in seen_groups if item.agent_readable]
    profile = await group_profile_facts(session, user_id, readable_ids, now=now, link=link)
    bonds = await group_bonds(session, user_id, bot_id, seen_groups, now=now)
    private_facts = [
        fact_view(row) for row in await list_facts(session, user_id, now=now)
    ]
    group_memory = _group_memory_enabled()
    people = (
        await people_you_both_know(session, user_id, seen_groups, now=now)
        if group_memory
        else []
    )
    happenings = (
        await group_happenings(session, seen_groups, now=now, user_id=user_id, link=link)
        if group_memory
        else []
    )
    lines_limit, lines_hours = _group_lines_config()
    recent_lines = (
        await recent_group_lines(
            session,
            user_id,
            seen_groups,
            now=now,
            limit=lines_limit,
            hours=lines_hours,
            link=link,
        )
        if group_memory
        else []
    )
    # 私聊里亲口说过的优先：同样内容的群画像不再重复，单值类别冲突时记下来。
    profile_all = list(profile)
    profile, conflicts = merge_with_private(profile, private_facts)
    _record_injected(profile, now)

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
    name_by_id = {item.group_id: item.name for item in groups}
    if profile:
        payload["group_profile"] = [_profile_view(item, name_by_id) for item in profile]
    if conflicts:
        payload["memory_conflicts"] = [
            {
                "what": _label(item["key"]),
                "private_says": item["private"],
                "group_remembers": item["group"],
                "group": name_by_id.get(item["group_id"], ""),
            }
            for item in conflicts
        ]
    if people:
        payload["people_you_both_know"] = people
    if happenings:
        payload["group_happenings"] = happenings
    if groups:
        # 取最近活跃的几个群，但按群名排序：活跃顺序每说一句话都可能变，会击穿前缀缓存。
        payload["groups"] = sorted(item.name for item in groups[:MAX_GROUPS])
        names_there = _names_in_groups_view(payload["user"]["name"], groups[:MAX_GROUPS])
        if names_there:
            payload["names_in_groups"] = names_there
    rules = _link_digest(link, groups)
    if rules:
        payload["memory_rules"] = rules
    payload["time_of_day"] = time_of_day(now)
    if recent_lines:
        payload["recent_in_groups"] = recent_lines
    recap = _recap(user_id)
    if recap:
        payload["earlier_chat"] = recap
    previous = await last_chat(session, user_id, now)
    if previous:
        payload["last_chat"] = previous
    tasks = _tasks_digest(user_id, now)
    if tasks:
        payload["tasks"] = tasks
    group_soon = await _group_tasks_soon(session, user_id, now)
    if group_soon:
        # 他在各群设的、24 小时内要跑的群定时任务（本轮层；私聊提醒在 assistant 摘要里）。
        payload["group_tasks_next_24h"] = group_soon
    assistant = _assistant_digest(user_id, now)
    style = (assistant.get("chat_style") or {}) if assistant else {}
    call_them = resolve_call_them(private_facts, style, profile_all)
    if call_them:
        payload["user"]["call_them"] = call_them
    if assistant:
        payload["assistant"] = [assistant]
    return PrivateContext(payload=_fit(payload), groups=groups, queries=8)


def _label(key: str) -> str:
    from ..private_chat_bridge import PRIVATE_MEMORY_LABELS

    return PRIVATE_MEMORY_LABELS.get(key, key)


def _profile_view(item: dict[str, Any], name_by_id: dict[int, str]) -> dict[str, Any]:
    """群画像条目：哪个群（合并的给几个群名）、他亲口说的还是我看出来的、记到哪天为止。"""

    group_names = [name_by_id.get(gid, "") for gid in item.get("group_ids") or [item["group_id"]]]
    view: dict[str, Any] = {
        "key": item["key"],
        "content": item["content"],
        "group": "、".join(sorted(name for name in group_names if name)),
    }
    if item.get("self_told"):
        view["he_told_you"] = True
    if item.get("as_of"):
        view["as_of"] = item["as_of"]
    return view


def _names_in_groups_view(name: str, groups: list[SharedGroup]) -> list[dict[str, str]]:
    """他在各群的群名片（和私聊里的名字不同时才给），私聊里提群里的事时对得上人。"""

    views = []
    for item in sorted(groups, key=lambda g: g.name):
        card = _safe_text(item.nickname, 20)
        if card and card != name:
            views.append({"group": item.name, "card": card})
    return views[:MAX_GROUPS]


def _link_state(user_id: int, now: datetime) -> Any:
    from . import memory_link

    try:
        return memory_link.load(user_id, now=now) if memory_link.enabled() else None
    except Exception:  # noqa: BLE001
        return None


def _link_digest(link: Any, groups: list[SharedGroup]) -> dict[str, Any]:
    if link is None:
        return {}
    from .memory_link import prompt_digest

    try:
        return prompt_digest(link, groups)
    except Exception:  # noqa: BLE001
        return {}


def _group_memory_enabled() -> bool:
    try:
        from ..private_chat_bridge import private_agent_config

        return bool(private_agent_config.private_agent_group_memory)
    except Exception:  # noqa: BLE001
        return True


def _group_lines_config() -> tuple[int, int]:
    try:
        from ..private_chat_bridge import private_agent_config

        return (
            int(private_agent_config.private_agent_group_lines),
            int(private_agent_config.private_agent_group_lines_hours),
        )
    except Exception:  # noqa: BLE001
        return 4, 24


def _record_injected(profile: list[dict[str, Any]], now: datetime) -> None:
    """注入私聊的群画像也算一次“用到”：常被想起的记忆衰减更慢（与群聊同一口径）。"""

    from .memory_access import record_access

    with contextlib.suppress(Exception):
        record_access((int(item.get("id") or 0) for item in profile), now=now)


def _recap(user_id: int) -> dict[str, Any]:
    """记忆整理时写下的“之前聊到哪”（历史窗口外的旧话题也接得上）。"""

    from . import private_store

    try:
        recap = private_store.load(user_id).get("recap") or {}
    except Exception:  # noqa: BLE001
        return {}
    view: dict[str, Any] = {}
    if recap.get("summary"):
        view["summary"] = str(recap["summary"])
    if recap.get("open_threads"):
        view["follow_up"] = list(recap["open_threads"])[:3]
    if view and recap.get("updated_at"):
        view["as_of"] = str(recap["updated_at"])
    return view


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


async def _group_tasks_soon(session: Any, user_id: int, now: datetime) -> list[str]:
    from .schedule_hub import soon_digest

    try:
        return await soon_digest(session, user_id, now)
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
    "group_happenings",
    "group_profile_facts",
    "merge_with_private",
    "people_you_both_know",
    "recent_group_lines",
    "resolve_call_them",
    "shared_groups",
    "visible_groups",
]
