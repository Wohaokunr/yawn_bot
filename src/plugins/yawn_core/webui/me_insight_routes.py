# ruff: noqa: TID252, E501, C901, FAST002, FBT001, PERF401
"""个人账号「我的空间」2.0：Bot 记得我什么、好感与签到、聊天记录、我在的群、
狼人杀战绩、导出我的数据（前缀 ``/me``）。

全部只读/只写本人：

- 记忆：只列 ``scope=private`` 且 ``subject_user_id`` 是本人的私聊事实，以及各群里关于他
  “能被他自己看到”的画像（``memory_link.user_detail`` 的口径，不带是谁说的）；删除走
  ``private_memory.forget``（管理员手动设的条目受保护），“别再记回来”写 ``memory_link``
  墓碑，和私聊里说“忘掉这件事”一样。
- 聊天记录：只列 ``ChatSession.user_id`` 是本人的会话；删除是软删（``is_deleted``），和私聊
  ``/面板`` 里删对话一致，下一条消息会开新会话。
- 战绩：只统计他本人的真人座位（``is_ai`` 为假），别人的身份只在已结束的对局里出现。
- 导出：只含他自己的数据；不含密码哈希、会话、管理端操作人指纹。
"""

from __future__ import annotations

import json
from collections import Counter
from datetime import datetime
from typing import Any

from fastapi import APIRouter, HTTPException, Query, Response, status
from nonebot_plugin_orm import get_session
from pydantic import BaseModel, ConfigDict, Field
from pydantic.alias_generators import to_camel
from sqlalchemy import func, select

from .. import affinity, webui_accounts
from ..data_models.bot_group import BotGroup
from ..data_models.chat_message import ChatMessage
from ..data_models.chat_session import ChatSession
from ..data_models.user_group import UserGroup
from ..private_chat_bridge import PRIVATE_MEMORY_LABELS
from ..yawn_agent import memory_link, private_memory
from ..yawn_agent import private_store as store
from ..yawn_agent.context import now_beijing
from .config import API_PATH
from .deps import UserReadSession, UserWriteSession, ok
from .hub import hub
from .service import group_feature_rows, iso

router = APIRouter(prefix=f"{API_PATH}/me")

_MESSAGE_PAGE = 60
_SEARCH_LIMIT = 50
_SNIPPET = 80


class _Body(BaseModel):
    model_config = ConfigDict(
        alias_generator=to_camel, populate_by_name=True, extra="forbid"
    )


def _uid(session: Any) -> int:
    return int(session.user_id)


async def _changed(user_id: int) -> None:
    await hub.notify_change("user_private_agent", str(user_id))


# ── Bot 记得我什么 ──────────────────────────────────────────────────────


class FactBody(_Body):
    key: str = Field(min_length=1, max_length=40)
    content: str = Field(min_length=1, max_length=120)


class LinkBody(_Body):
    groups_in_private: bool | None = None
    share_keys: list[str] | None = Field(default=None, max_length=20)
    # 空列表 = 所有能看到的群
    share_groups: list[str] | None = Field(default=None, max_length=30)
    muted_groups: list[str] | None = Field(default=None, max_length=30)


def _link_flags() -> dict[str, Any]:
    return {
        "enabled": memory_link.enabled(),
        "shareEnabled": memory_link.share_enabled(),
        "tombstoneDays": memory_link.tombstone_days(),
    }


async def _memory_payload(user_id: int) -> dict[str, Any]:
    now = now_beijing()
    async with get_session() as db:
        try:
            detail = await memory_link.user_detail(db, user_id)
            available = True
        except Exception:  # noqa: BLE001 - 记忆表未迁移时只读降级
            detail, available = {}, False
        rows = []
        if available:
            for row in await private_memory.list_facts(db, user_id, now=now):
                rows.append(
                    {
                        "key": str(row.memory_key),
                        "label": PRIVATE_MEMORY_LABELS.get(
                            str(row.memory_key), str(row.memory_key)
                        ),
                        "content": str(row.content or ""),
                        "source": str(row.source_kind or ""),
                        "sourceLabel": "你让我记的"
                        if row.source_kind == "agent"
                        else "聊天里了解到的"
                        if row.source_kind == "auto"
                        else "管理员设置的",
                        "protected": row.source_kind == "manual",
                        "updatedAt": iso(row.updated_at),
                        "expiresAt": iso(row.expires_at),
                    }
                )
    recap = store.load(user_id).get("recap") or {}
    settings = detail.get("settings") or {}
    return {
        "available": available,
        **_link_flags(),
        "labels": dict(PRIVATE_MEMORY_LABELS),
        "listKeys": sorted(private_memory.LIST_KEYS),
        "facts": rows,
        "groupProfile": [
            {
                "key": item["key"],
                "label": PRIVATE_MEMORY_LABELS.get(item["key"], item["key"]),
                "content": item["content"],
                "groups": item["groups"],
                "selfTold": item["self_told"],
                "asOf": item["as_of"],
            }
            for item in detail.get("group_profile") or []
        ],
        "settings": {
            "groupsInPrivate": bool(settings.get("groups_in_private", True)),
            "mutedGroups": list(settings.get("muted_groups") or []),
            "shareKeys": list(settings.get("share_keys") or []),
            "shareGroups": list(settings.get("share_groups") or []),
        },
        "shareableKeys": [
            {"key": key, "label": PRIVATE_MEMORY_LABELS.get(key, key)}
            for key in memory_link.SHAREABLE_KEYS
        ],
        "groups": [
            {
                "groupId": item["group_id"],
                "name": item["name"],
                "agentReadable": item["agent_readable"],
                "inPrivate": item["in_private"],
                "seesShared": item["group_sees_shared"],
            }
            for item in detail.get("groups") or []
        ],
        "tombstones": detail.get("tombstones") or [],
        "ledger": detail.get("ledger") or [],
        "recap": {
            "summary": recap.get("summary") or "",
            "openThreads": list(recap.get("open_threads") or []),
            "updatedAt": recap.get("updated_at"),
        }
        if recap.get("summary") or recap.get("open_threads")
        else None,
    }


@router.get("/memory")
async def my_memory(session: UserReadSession) -> dict[str, Any]:
    return ok(await _memory_payload(_uid(session)))


@router.post("/memory/facts")
async def add_my_fact(body: FactBody, session: UserWriteSession) -> dict[str, Any]:
    """亲口告诉 Bot 一件关于自己的事（等同私聊里说“记住我喜欢……”）。"""

    if body.key not in PRIVATE_MEMORY_LABELS:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "不支持的记忆类别")
    user_id = _uid(session)
    async with get_session() as db:
        try:
            result = await private_memory.remember(
                db, user_id, body.key, body.content, now=now_beijing(), source="agent"
            )
        except ValueError as exc:
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_CONTENT, str(exc)
            ) from exc
        if result.get("kept"):
            raise HTTPException(
                status.HTTP_409_CONFLICT, "这一项是管理员设置的，不能在这里改"
            )
        await db.commit()
    await _changed(user_id)
    return ok(await _memory_payload(user_id))


@router.delete("/memory/facts")
async def forget_my_fact(
    session: UserWriteSession,
    key: str = Query(default="", max_length=40),
    keyword: str = Query(default="", max_length=40),
    block: bool = Query(default=False),
    everywhere: bool = Query(default=False),
) -> dict[str, Any]:
    """忘掉一项（或一项里的某个词）；``block`` 让以后聊天里也别再记回来。"""

    if key and key not in PRIVATE_MEMORY_LABELS:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "不支持的记忆类别")
    if not key and not keyword:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "要说清楚忘掉哪一项")
    user_id = _uid(session)
    now = now_beijing()
    async with get_session() as db:
        result = await private_memory.forget(
            db, user_id, key=key or None, keyword=keyword or None
        )
        await db.commit()
    blocked = None
    if block:
        blocked = memory_link.add_tombstone(
            user_id,
            key=key or None,
            keyword=keyword or None,
            scope="everywhere" if everywhere else "private",
            now=now,
            kind="forget_everywhere" if everywhere else "forget",
        )
    await _changed(user_id)
    return ok(
        {
            **(await _memory_payload(user_id)),
            "result": {**result, "blocked": blocked is not None},
        }
    )


@router.delete("/memory/all")
async def forget_everything(session: UserWriteSession) -> dict[str, Any]:
    """清空私聊记忆（管理员手动设的除外）和对话回顾；不动群里的记忆。"""

    user_id = _uid(session)
    async with get_session() as db:
        result = await private_memory.forget(db, user_id)
        await db.commit()

    def clear(record: dict[str, Any]) -> None:
        record["recap"] = {}

    store.update(user_id, clear)
    await _changed(user_id)
    return ok({**(await _memory_payload(user_id)), "result": result})


@router.delete("/memory/recap")
async def clear_my_recap(session: UserWriteSession) -> dict[str, Any]:
    user_id = _uid(session)

    def clear(record: dict[str, Any]) -> None:
        record["recap"] = {}

    store.update(user_id, clear)
    await _changed(user_id)
    return ok(await _memory_payload(user_id))


@router.delete("/memory/tombstones/{index}")
async def drop_my_tombstone(index: int, session: UserWriteSession) -> dict[str, Any]:
    """撤销一条“别再记回来”。"""

    user_id = _uid(session)
    if not memory_link.drop_tombstone(user_id, index, now=now_beijing(), by="self"):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "这条记录已经不在了，刷新看看")
    await _changed(user_id)
    return ok(await _memory_payload(user_id))


def _group_ids(raw: list[str] | None, allowed: dict[int, str]) -> list[int]:
    values: list[int] = []
    for item in raw or []:
        if not str(item).isdigit() or int(item) not in allowed:
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_CONTENT, f"你不在群 {item} 里"
            )
        values.append(int(item))
    return list(dict.fromkeys(values))


@router.put("/memory/link")
async def put_my_memory_link(
    body: LinkBody, session: UserWriteSession
) -> dict[str, Any]:
    """群聊 ↔ 私聊记忆怎么流动：私聊里提不提群、哪些群别带进私聊、哪些类别让群里也知道。"""

    if not memory_link.enabled():
        raise HTTPException(
            status.HTTP_409_CONFLICT, "管理员没有开启群聊和私聊记忆打通"
        )
    user_id = _uid(session)
    now = now_beijing()
    current = await _memory_payload(user_id)
    allowed = {
        int(item["groupId"]): str(item["name"] or item["groupId"])
        for item in current["groups"]
    }
    settings = current["settings"]
    if (
        body.groups_in_private is not None
        and body.groups_in_private != settings["groupsInPrivate"]
    ):
        memory_link.set_groups_in_private(user_id, on=body.groups_in_private, now=now)
    if body.muted_groups is not None:
        wanted = set(_group_ids(body.muted_groups, allowed))
        before = {int(gid) for gid in settings["mutedGroups"]}
        for gid in sorted(wanted - before):
            memory_link.set_muted(
                user_id, gid, muted=True, now=now, group_name=allowed.get(gid, "")
            )
        for gid in sorted(before - wanted):
            memory_link.set_muted(
                user_id, gid, muted=False, now=now, group_name=allowed.get(gid, "")
            )
    if body.share_keys is not None:
        if body.share_keys and not memory_link.share_enabled():
            raise HTTPException(
                status.HTTP_409_CONFLICT, "管理员没有开放把私聊记忆共享给群"
            )
        unknown = [
            key for key in body.share_keys if key not in memory_link.SHAREABLE_KEYS
        ]
        if unknown:
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_CONTENT, "有不能共享的类别"
            )
        wanted_keys = list(dict.fromkeys(body.share_keys))
        before_keys = list(settings["shareKeys"])
        add = [key for key in wanted_keys if key not in before_keys]
        drop = [key for key in before_keys if key not in wanted_keys]
        if add:
            memory_link.set_share(user_id, add, on=True, now=now)
        if drop:
            memory_link.set_share(user_id, drop, on=False, now=now)
    if body.share_groups is not None:
        groups = _group_ids(body.share_groups, allowed)
        memory_link.set_share_groups(
            user_id, groups, now=now, group_names=[allowed[gid] for gid in groups]
        )
    await _changed(user_id)
    return ok(await _memory_payload(user_id))


# ── 好感与签到 ──────────────────────────────────────────────────────────


@router.get("/affinity")
async def my_affinity(session: UserReadSession) -> dict[str, Any]:
    """好感等级与进度、近 30 天来源、流水、各群签到（和管理台用户抽屉同一口径）。"""

    from .users import _affinity_payload  # pyright: ignore[reportPrivateUsage]

    user_id = _uid(session)
    async with get_session() as db:
        payload = await _affinity_payload(db, user_id)
    payload["tones"] = dict(affinity.LEVEL_TONE)
    # payload["today"] 是 {来源: 今天已得}（和管理台同口径）；页面另要一个合计
    payload["todayTotal"] = sum(payload["today"].values())
    return ok(payload)


# ── 我在的群 ────────────────────────────────────────────────────────────


@router.get("/joined-groups")
async def my_joined_groups(session: UserReadSession) -> dict[str, Any]:
    """我在的所有群（不只是管理的）：我的身份、群里的签到、本群对我开着哪些功能。"""

    from ..checkin_service import checkin_overview
    from ..permission import FEATURE_REGISTRY
    from .service import user_feature_rows

    user_id = _uid(session)
    async with get_session() as db:
        memberships = (
            await db.execute(
                select(UserGroup, BotGroup.group_name, BotGroup.last_active_at)
                .join(BotGroup, BotGroup.group_id == UserGroup.group_id)
                .where(UserGroup.user_id == user_id)
            )
        ).all()
        group_ids = [int(row[0].group_id) for row in memberships]
        counts: dict[int, int] = {}
        if group_ids:
            counts = {
                int(gid): int(count)
                for gid, count in (
                    await db.execute(
                        select(UserGroup.group_id, func.count())
                        .where(UserGroup.group_id.in_(group_ids))
                        .group_by(UserGroup.group_id)
                    )
                ).all()
            }
        checkins = {
            item.group_id: item
            for item in await checkin_overview(db, user_id, active_only=False)
        }
        rows = []
        for membership, name, last_active in memberships:
            gid = int(membership.group_id)
            features = await user_feature_rows(db, user_id, gid)
            off = [row for row in features if not row["effective"]]
            group_rows = await group_feature_rows(db, gid)
            agent_on = next(
                (row["effective"] for row in group_rows if row["key"] == "group_agent"),
                True,
            )
            item = checkins.get(gid)
            rows.append(
                {
                    "groupId": str(gid),
                    "groupName": name,
                    "role": membership.role,
                    "managed": membership.role in {"owner", "admin"},
                    "memberCount": counts.get(gid, 0),
                    "lastActiveAt": iso(last_active),
                    "firstSeenAt": iso(membership.first_seen_at),
                    "agentEnabled": bool(agent_on),
                    "featuresOff": [
                        {
                            "key": row["key"],
                            "label": FEATURE_REGISTRY.get(row["key"], row["key"]),
                        }
                        for row in off
                    ],
                    "checkin": None
                    if item is None
                    else {
                        "checkedToday": item.checked_today,
                        "streakDays": item.streak_days,
                        "streakBroken": item.streak_broken,
                        "totalDays": item.total_days,
                        "points": item.points,
                        "exp": item.exp,
                        "groupAffinity": item.group_affinity,
                        "lastCheckinDate": item.last_checkin_date.isoformat()
                        if item.last_checkin_date
                        else None,
                    },
                }
            )
    rows.sort(key=lambda row: str(row["lastActiveAt"] or ""), reverse=True)
    rows.sort(key=lambda row: not row["managed"])
    return ok(rows)


# ── 聊天记录 ────────────────────────────────────────────────────────────


class ChatTitleBody(_Body):
    title: str = Field(min_length=1, max_length=40)


def _snippet(text: str, needle: str = "") -> str:
    value = " ".join(str(text or "").split())
    if needle:
        at = value.casefold().find(needle.casefold())
        if at > _SNIPPET // 2:
            value = "…" + value[at - _SNIPPET // 3 :]
    return value[:_SNIPPET] + ("…" if len(value) > _SNIPPET else "")


async def _own_chat(db: Any, user_id: int, chat_id: int) -> ChatSession:
    row = await db.get(ChatSession, chat_id)
    if row is None or int(row.user_id) != user_id or row.is_deleted:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "没有这段对话")
    return row


@router.get("/chats")
async def my_chats(session: UserReadSession) -> dict[str, Any]:
    """我和 Bot 的对话（私聊和群里的 /对话），最近的在前。"""

    user_id = _uid(session)
    async with get_session() as db:
        sessions = (
            await db.execute(
                select(ChatSession, BotGroup.group_name)
                .outerjoin(BotGroup, BotGroup.group_id == ChatSession.group_id)
                .where(
                    ChatSession.user_id == user_id, ChatSession.is_deleted.is_(False)
                )
                .order_by(
                    ChatSession.updated_at.desc().nullslast(), ChatSession.id.desc()
                )
            )
        ).all()
        ids = [int(row[0].id) for row in sessions]
        stats: dict[int, tuple[int, Any, Any]] = {}
        if ids:
            for sid, total, first, last in (
                await db.execute(
                    select(
                        ChatMessage.session_id,
                        func.count(),
                        func.min(ChatMessage.created_at),
                        func.max(ChatMessage.created_at),
                    )
                    .where(
                        ChatMessage.session_id.in_(ids),
                        ChatMessage.is_deleted.is_(False),
                    )
                    .group_by(ChatMessage.session_id)
                )
            ).all():
                stats[int(sid)] = (int(total), first, last)
            mine_counts = {
                int(sid): int(count)
                for sid, count in (
                    await db.execute(
                        select(ChatMessage.session_id, func.count())
                        .where(
                            ChatMessage.session_id.in_(ids),
                            ChatMessage.is_deleted.is_(False),
                            ChatMessage.role == "user",
                        )
                        .group_by(ChatMessage.session_id)
                    )
                ).all()
            }
            last_lines: dict[int, str] = {}
            for sid in ids[:50]:
                text = await db.scalar(
                    select(ChatMessage.content)
                    .where(
                        ChatMessage.session_id == sid, ChatMessage.is_deleted.is_(False)
                    )
                    .order_by(ChatMessage.created_at.desc(), ChatMessage.id.desc())
                    .limit(1)
                )
                last_lines[sid] = _snippet(text or "")
        else:
            mine_counts, last_lines = {}, {}
    rows = []
    for chat, group_name in sessions:
        sid = int(chat.id)
        total, first, last = stats.get(sid, (0, None, None))
        rows.append(
            {
                "id": str(sid),
                "title": chat.title or None,
                "where": "private" if chat.group_id is None else "group",
                "groupId": str(chat.group_id) if chat.group_id is not None else None,
                "groupName": group_name,
                "messages": total,
                "mine": mine_counts.get(sid, 0),
                "createdAt": iso(chat.created_at),
                "updatedAt": iso(chat.updated_at),
                "firstAt": iso(first),
                "lastAt": iso(last),
                "preview": last_lines.get(sid, ""),
            }
        )
    return ok(rows)


@router.get("/chats/search")
async def search_my_chats(
    session: UserReadSession, q: str = Query(min_length=1, max_length=40)
) -> dict[str, Any]:
    user_id = _uid(session)
    needle = q.strip()
    if not needle:
        return ok([])
    async with get_session() as db:
        rows = (
            await db.execute(
                select(ChatMessage, ChatSession.title, ChatSession.group_id)
                .join(ChatSession, ChatSession.id == ChatMessage.session_id)
                .where(
                    ChatSession.user_id == user_id,
                    ChatSession.is_deleted.is_(False),
                    ChatMessage.is_deleted.is_(False),
                    ChatMessage.content.contains(needle),
                )
                .order_by(ChatMessage.created_at.desc(), ChatMessage.id.desc())
                .limit(_SEARCH_LIMIT)
            )
        ).all()
    return ok(
        [
            {
                "chatId": str(message.session_id),
                "messageId": str(message.id),
                "role": message.role,
                "at": iso(message.created_at),
                "snippet": _snippet(message.content, needle),
                "chatTitle": title,
                "where": "private" if group_id is None else "group",
            }
            for message, title, group_id in rows
        ],
        {"limit": _SEARCH_LIMIT},
    )


@router.get("/chats/{chat_id}/messages")
async def my_chat_messages(  # noqa: PLR0913, PLR0917
    chat_id: int,
    session: UserReadSession,
    before: int | None = Query(default=None, ge=1),
    after: int | None = Query(default=None, ge=1),
    around: int | None = Query(default=None, ge=1),
    limit: int = Query(default=_MESSAGE_PAGE, ge=1, le=200),
) -> dict[str, Any]:
    """一段对话的消息，按时间正序；``before`` 往前翻页、``after`` 往后翻页（消息编号），
    ``around`` 取某条消息前后各半页（从搜索结果跳过来用）。"""

    user_id = _uid(session)
    async with get_session() as db:
        await _own_chat(db, user_id, chat_id)
        base = select(ChatMessage).where(
            ChatMessage.session_id == chat_id, ChatMessage.is_deleted.is_(False)
        )

        async def older(than: int | None, count: int) -> tuple[list[Any], bool]:
            query = base if than is None else base.where(ChatMessage.id < than)
            found = list(
                (await db.execute(query.order_by(ChatMessage.id.desc()).limit(count + 1)))
                .scalars()
                .all()
            )
            return list(reversed(found[:count])), len(found) > count

        async def newer(than: int, count: int, *, inclusive: bool = False) -> tuple[list[Any], bool]:
            edge = ChatMessage.id >= than if inclusive else ChatMessage.id > than
            found = list(
                (await db.execute(base.where(edge).order_by(ChatMessage.id).limit(count + 1)))
                .scalars()
                .all()
            )
            return found[:count], len(found) > count

        has_newer = False
        if around is not None:
            half = max(limit // 2, 1)
            head, more = await older(around, half)
            tail, has_newer = await newer(around, half, inclusive=True)
            rows = head + tail
        elif after is not None:
            rows, has_newer = await newer(after, limit)
            more = True
        else:
            rows, more = await older(before, limit)
    return ok(
        [
            {
                "id": str(row.id),
                "role": row.role,
                "content": str(row.content or ""),
                "at": iso(row.created_at),
            }
            for row in rows
            if row.role in {"user", "assistant"}
        ],
        {
            "hasMore": more,
            "before": str(rows[0].id) if rows else None,
            "hasNewer": has_newer,
            "after": str(rows[-1].id) if rows else None,
        },
    )


@router.patch("/chats/{chat_id}")
async def rename_my_chat(
    chat_id: int, body: ChatTitleBody, session: UserWriteSession
) -> dict[str, Any]:
    user_id = _uid(session)
    async with get_session() as db:
        row = await _own_chat(db, user_id, chat_id)
        row.title = " ".join(body.title.split())[:40]
        title = row.title
        await db.commit()
    return ok({"id": str(chat_id), "title": title})


@router.delete("/chats/{chat_id}")
async def delete_my_chat(chat_id: int, session: UserWriteSession) -> dict[str, Any]:
    """删除一段对话（软删，和私聊 /面板 里删对话一样）；下一条消息会开新对话。"""

    user_id = _uid(session)
    async with get_session() as db:
        row = await _own_chat(db, user_id, chat_id)
        row.is_deleted = True
        await db.commit()
    await _changed(user_id)
    return ok({"deleted": str(chat_id)})


@router.get("/chats/{chat_id}/export")
async def export_my_chat(
    chat_id: int,
    session: UserReadSession,
    fmt: str = Query(default="txt", alias="format", pattern="^(txt|md|json)$"),
) -> Response:
    """把一段对话导出成纯文本、Markdown 或 JSON。"""

    user_id = _uid(session)
    async with get_session() as db:
        chat = await _own_chat(db, user_id, chat_id)
        title = chat.title or f"对话 {chat_id}"
        rows = (
            (
                await db.execute(
                    select(ChatMessage)
                    .where(
                        ChatMessage.session_id == chat_id,
                        ChatMessage.is_deleted.is_(False),
                    )
                    .order_by(ChatMessage.id)
                )
            )
            .scalars()
            .all()
        )
    messages = [
        {
            "who": "我" if row.role == "user" else "Bot",
            "role": row.role,
            "at": row.created_at.strftime("%Y-%m-%d %H:%M") if row.created_at else "",
            "content": str(row.content or ""),
        }
        for row in rows
        if row.role in {"user", "assistant"}
    ]
    if fmt == "json":
        body = json.dumps(
            {"id": str(chat_id), "title": title, "messages": messages},
            ensure_ascii=False,
            indent=2,
        )
        media = "application/json; charset=utf-8"
    elif fmt == "md":
        parts = [f"# {title}", ""]
        day = ""
        for message in messages:
            if message["at"][:10] != day:
                day = message["at"][:10]
                parts += [f"## {day or '未知日期'}", ""]
            parts += [f"**{message['who']}** `{message['at'][11:]}`", "", message["content"], ""]
        body = "\n".join(parts)
        media = "text/markdown; charset=utf-8"
    else:
        lines = [f"# {title}", ""]
        lines += [f"[{m['at']}] {m['who']}：{m['content']}" for m in messages]
        body = "\n".join(lines) + "\n"
        media = "text/plain; charset=utf-8"
    return Response(
        body,
        media_type=media,
        headers={
            "Content-Disposition": f'attachment; filename="yawnbot-chat-{chat_id}.{fmt}"',
            "X-Content-Type-Options": "nosniff",
            "Cache-Control": "no-store",
        },
    )


# ── 狼人杀战绩 ──────────────────────────────────────────────────────────


def personal_werewolf_stats(
    rows: list[tuple[Any, Any]], group_names: dict[int, str | None]
) -> dict[str, Any]:
    """纯函数：把 (玩家行, 对局行) 聚合成本人战绩。未结束的对局只计入总数和“中断”。"""

    finished = [(p, g) for p, g in rows if g.ended_at is not None and g.winner_faction]
    wins = sum(1 for p, _g in finished if p.is_winner)
    by_role: dict[str, dict[str, Any]] = {}
    by_faction = {"wolf": {"games": 0, "wins": 0}, "good": {"games": 0, "wins": 0}}
    for player, _game in finished:
        role = by_role.setdefault(
            player.role, {"role": player.role, "games": 0, "wins": 0}
        )
        role["games"] += 1
        role["wins"] += 1 if player.is_winner else 0
        faction = by_faction.setdefault(player.faction, {"games": 0, "wins": 0})
        faction["games"] += 1
        faction["wins"] += 1 if player.is_winner else 0
    survived = sum(1 for p, _g in finished if p.death_round is None)
    causes = Counter(str(p.death_cause) for p, _g in finished if p.death_cause)
    # 连胜：按结束时间从新到旧数
    streak = 0
    for player, _game in sorted(
        finished, key=lambda item: item[1].ended_at, reverse=True
    ):
        if not player.is_winner:
            break
        streak += 1
    best = run = 0
    for player, _game in sorted(finished, key=lambda item: item[1].ended_at):
        run = run + 1 if player.is_winner else 0
        best = max(best, run)
    recent = sorted(rows, key=lambda item: item[1].started_at, reverse=True)[:20]
    return {
        "games": len(rows),
        "finished": len(finished),
        "aborted": len(rows) - len(finished),
        "wins": wins,
        "winRate": round(wins / len(finished), 3) if finished else None,
        "survived": survived,
        "sheriff": sum(1 for p, _g in rows if p.is_sheriff),
        "currentStreak": streak,
        "bestStreak": best,
        "byFaction": by_faction,
        "byRole": sorted(
            by_role.values(), key=lambda row: (-row["games"], row["role"])
        ),
        "deathCauses": dict(causes),
        "recent": [
            {
                "gameId": str(game.id),
                "groupId": str(game.group_id),
                "groupName": group_names.get(int(game.group_id)),
                "board": game.board,
                "players": int(game.player_count or 0),
                "startedAt": iso(game.started_at),
                "endedAt": iso(game.ended_at),
                "role": player.role,
                "faction": player.faction,
                "seat": player.seat,
                "sheriff": bool(player.is_sheriff),
                "won": player.is_winner,
                "deathRound": player.death_round,
                "deathCause": player.death_cause,
                "rounds": game.end_round,
                "winner": game.winner_faction,
            }
            for player, game in recent
        ],
    }


@router.get("/games")
async def my_games(session: UserReadSession) -> dict[str, Any]:
    user_id = _uid(session)
    try:
        from ..yawn_werewolf.models import WerewolfGame, WerewolfPlayer
    except Exception:  # noqa: BLE001 - 狼人杀子插件没加载
        return ok({"available": False})
    async with get_session() as db:
        try:
            rows = (
                await db.execute(
                    select(WerewolfPlayer, WerewolfGame)
                    .join(WerewolfGame, WerewolfGame.id == WerewolfPlayer.game_id)
                    .where(
                        WerewolfPlayer.user_id == user_id,
                        WerewolfPlayer.is_ai.is_(False),
                    )
                )
            ).all()
        except Exception:  # noqa: BLE001 - 表未迁移
            return ok({"available": False})
        group_ids = sorted({int(game.group_id) for _player, game in rows})
        names: dict[int, str | None] = {}
        if group_ids:
            names = {
                int(gid): name
                for gid, name in (
                    await db.execute(
                        select(BotGroup.group_id, BotGroup.group_name).where(
                            BotGroup.group_id.in_(group_ids)
                        )
                    )
                ).all()
            }
    return ok(
        {"available": True, **personal_werewolf_stats([(p, g) for p, g in rows], names)}
    )


# ── 导出我的数据 ────────────────────────────────────────────────────────


_EXPORT_DROP = {"flags", "mail", "persona"}


@router.get("/export")
async def export_my_data(session: UserReadSession) -> Response:
    """下载一份 JSON：个人数据、私聊记忆、好感流水、各群签到、对话列表、账号信息（不含密码）。"""

    from ..checkin_service import checkin_overview

    user_id = _uid(session)
    now = now_beijing()
    record = store.load(user_id)
    personal = {key: value for key, value in record.items() if key not in _EXPORT_DROP}
    persona = record.get("persona") or {}
    personal["persona"] = {
        key: value
        for key, value in persona.items()
        if key in {"mode", "card", "template_id"}
    }
    async with get_session() as db:
        try:
            facts = [
                {
                    "key": str(row.memory_key),
                    "content": str(row.content or ""),
                    "source": row.source_kind,
                    "updatedAt": iso(row.updated_at),
                }
                for row in await private_memory.list_facts(db, user_id, now=now)
            ]
        except Exception:  # noqa: BLE001
            facts = []
        events = await affinity.recent_events(db, user_id, limit=500) or []
        snap = await affinity.snapshot(db, user_id)
        checkins = await checkin_overview(db, user_id, active_only=False)
        chats = (
            await db.execute(
                select(
                    ChatSession.id,
                    ChatSession.title,
                    ChatSession.group_id,
                    ChatSession.created_at,
                ).where(
                    ChatSession.user_id == user_id, ChatSession.is_deleted.is_(False)
                )
            )
        ).all()
    account = webui_accounts.get_account(user_id)
    payload = {
        "exportedAt": now.strftime("%Y-%m-%d %H:%M:%S"),
        "userId": str(user_id),
        "account": account.as_dict() if account is not None else None,
        "personal": personal,
        "privateMemory": facts,
        "affinity": {"points": snap.points, "level": snap.level.name, "events": events},
        "checkin": [
            {
                "groupId": str(item.group_id),
                "groupName": item.group_name,
                "streakDays": item.streak_days,
                "totalDays": item.total_days,
                "points": item.points,
                "lastCheckinDate": item.last_checkin_date.isoformat()
                if item.last_checkin_date
                else None,
            }
            for item in checkins
        ],
        "chats": [
            {
                "id": str(cid),
                "title": title,
                "groupId": str(gid) if gid else None,
                "createdAt": iso(created),
            }
            for cid, title, gid, created in chats
        ],
        "note": "聊天内容请在「聊天记录」里逐段导出；这里只列对话目录。",
    }
    stamp = datetime.now().strftime("%Y%m%d")  # noqa: DTZ005 - 仅用于文件名
    return Response(
        json.dumps(payload, ensure_ascii=False, indent=2, default=str),
        media_type="application/json; charset=utf-8",
        headers={
            "Content-Disposition": f'attachment; filename="yawnbot-me-{user_id}-{stamp}.json"',
            "X-Content-Type-Options": "nosniff",
            "Cache-Control": "no-store",
        },
    )


__all__ = ["personal_werewolf_stats", "router"]
