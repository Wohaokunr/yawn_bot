# ruff: noqa: TID252
"""发言人在签到 / 全局好感系统里的现状，给群 Agent 的发言人层用。

数据由 ``context_repository.load_members`` 随成员查询一起取回（不加往返），这里
只做纯投影：

- ``overall``：全局好感等级 + 一句相处提示（``affinity.level_words``），陌生不写；
- ``checkin_streak``：本群连签天数，只在连签 ≥ ``MIN_STREAK_TO_MENTION`` 且没断时写。

只给词语和天数，不给好感数值。隐私退出的成员由调用方排除。私聊内容不会出现在
这里：好感余额只是一个数，不携带任何私聊原文。
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Any

from sqlalchemy import select

from .. import affinity
from ..affinity import level_for, level_words
from ..data_models.agent_memory import AgentPrivacy
from .log import dbg, dbg_exc
from .schedule_wake import WAKE_TRIGGER_SOURCE

# 连签少于这个天数不值得提，避免每个人都带一条噪音。
MIN_STREAK_TO_MENTION = 3


def standing_view(standing: Any, now: datetime) -> dict[str, Any]:
    """把 ``MemberStanding`` 投影成发言人层字段；没有值得说的返回空 dict。"""

    if standing is None:
        return {}
    view: dict[str, Any] = {}
    points = int(getattr(standing, "affinity", 0) or 0)
    if level_for(points).index > 0:
        view["overall"] = level_words(points)
    streak = int(getattr(standing, "streak_days", 0) or 0)
    last = getattr(standing, "last_checkin_date", None)
    if isinstance(last, datetime):
        last = last.date()
    if (
        streak >= MIN_STREAK_TO_MENTION
        and isinstance(last, date)
        and last >= now.date() - timedelta(days=1)
    ):
        view["checkin_streak"] = f"本群连续签到 {streak} 天"
    return view


def merge_speaker_standing(
    views: list[dict[str, Any]],
    *,
    speaker_id: int | None,
    speaker_name: str | None,
    standing: Any,
    now: datetime,
) -> list[dict[str, Any]]:
    """把发言人的现状并进 bot_bonds：有关系条目就补字段，没有就单独加一条。"""

    if speaker_id is None:
        return views
    extra = standing_view(standing, now)
    if not extra:
        return views
    merged = [dict(item) for item in views]
    for item in merged:
        if item.get("user_id") == speaker_id:
            item.update(extra)
            return merged
    entry: dict[str, Any] = {"user_id": speaker_id}
    if speaker_name:
        entry["name"] = speaker_name
    entry.update(extra)
    return [entry, *merged]


# 一轮群聊互动给的好感；每日上限由 affinity.SOURCE_RULES["group_chat"] 控制。
GROUP_INTERACTION_AFFINITY = 1
# 不是成员在找 Bot 的回合（定时任务唤醒）不给好感。
_NON_MEMBER_TRIGGERS = frozenset({WAKE_TRIGGER_SOURCE})


async def reward_group_interaction(  # noqa: PLR0913
    session: Any,
    *,
    group_id: int,
    bot_id: int | None,
    actor_user_id: int | None,
    trigger_source: str | None,
    now: datetime,
) -> bool:
    """群里成员找 Bot、Bot 也确实回了话：给这位成员一点全局好感。

    只看结构事实（有真实调用者、不是 Bot 自己、不是定时唤醒、成员没有隐私退出），
    不看说了什么。任何失败都吞掉，绝不影响回复后的状态提交。
    """

    actor = int(actor_user_id or 0)
    if actor <= 0 or (bot_id is not None and actor == int(bot_id)):
        return False
    if trigger_source in _NON_MEMBER_TRIGGERS:
        return False
    try:
        opted_out = await session.scalar(
            select(AgentPrivacy.opted_out).where(
                AgentPrivacy.group_id == group_id, AgentPrivacy.user_id == actor
            )
        )
        if opted_out:
            return False
        change = await affinity.grant(
            session,
            actor,
            "group_chat",
            GROUP_INTERACTION_AFFINITY,
            group_id=group_id,
            note="群里和 Bot 聊天",
            now=now,
        )
    except Exception:  # noqa: BLE001
        dbg_exc(f"群 {group_id} 成员 {actor} 群聊好感记录失败(忽略)")
        return False
    if change.applied:
        dbg(f"群 {group_id} 成员 {actor} 群聊互动好感 +{change.applied}")
    return bool(change.applied)


__all__ = [
    "GROUP_INTERACTION_AFFINITY",
    "MIN_STREAK_TO_MENTION",
    "merge_speaker_standing",
    "reward_group_interaction",
    "standing_view",
]
