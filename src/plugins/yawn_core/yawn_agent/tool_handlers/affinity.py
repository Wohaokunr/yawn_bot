# ruff: noqa: TID252, E501, C901, PLR0912, PLR0913, TRY003
"""好感玩法工具（签到族）：群里替调用者本人查权益/任务/背包/羁绊，送礼、兑换、用道具、定称呼、互动、抽缘分；
以及 Bot 自己记“心动瞬间”的 ``affinity_moment``。

逻辑全在父插件 ``affinity_perks`` / ``affinity_economy`` / ``affinity_commands``，这里只做
调用者校验、签到功能开关和结果投影。只作用于**调用者本人**，不提供 user_id 参数；
给模型的结果只有等级名和“涨没涨”，不报好感数值。
"""

from __future__ import annotations

from typing import Any

from ... import affinity
from ... import affinity_bond as bond
from ... import affinity_economy as economy
from ... import affinity_perks as perks
from ... import affinity_store as store
from ...affinity_policy import load_policy
from ..context import now_beijing
from ..tool_args import ERROR_PERMISSION, ToolError
from ..tool_execution import ToolExecutionContext, ToolHandlerResult
from .checkin import _actor, _ensure_feature  # pyright: ignore[reportPrivateUsage]

FAMILY = "checkin"
NAMES = frozenset(["my_affinity", "affinity_action", "affinity_moment"])

AFFINITY_VIEWS = ("overview", "perks", "gifts", "shop", "bag", "quests", "bond", "weekly", "badges")
AFFINITY_ACTIONS = ("gift", "buy", "use", "nickname", "interact", "fortune")


async def overview_payload(session: Any, user_id: int, view: str, *, now: Any) -> dict[str, Any]:
    """群聊工具和私聊工具共用的只读视图（不含好感数值）。"""

    pol = load_policy()
    points = await perks.load_points(session, user_id)
    level = affinity.level_for(points)
    record = store.load(user_id)
    payload: dict[str, Any] = {"enabled": pol.enabled, "level": level.name, "next_level": level.next_name}
    if view in {"overview", "perks"}:
        payload["perks"] = perks.perks_public(pol.perks(level.name))
        upcoming = perks.next_unlocks(points, pol)
        if upcoming:
            upcoming.pop("to_next", None)
            payload["next_level_unlocks"] = upcoming
    if view == "perks":
        payload["all_levels"] = [{"level": row.level, "unlocks": perks.unlock_summary(row)} for row in pol.levels]
    if view in {"overview", "gifts"}:
        fav = economy.favorite_gift(now, pol)
        lvl_perks = pol.perks(level.name)
        payload["gifts"] = {
            "favorite_today": fav.name if fav else None,
            "sent_today": economy.gifts_used_today(record, now),
            "daily_limit": lvl_perks.gift_daily,
            "menu": [
                {"name": g.name, "price_points": economy.price_after_discount(g.price, lvl_perks.shop_discount_pct)}
                for g in pol.gifts
                if g.enabled
            ]
            if view == "gifts"
            else None,
        }
    if view in {"overview", "shop"}:
        wallet = await economy.wallet(session, user_id)
        payload["checkin_points"] = wallet.total
        if view == "shop":
            payload["shop"] = economy.shop_view(points, record, pol)
    if view in {"overview", "bag"}:
        payload["bag"] = economy.inventory_view(record, pol, today=now.date())
    if view in {"overview", "quests"}:

        def quests(rec: dict[str, Any]) -> dict[str, Any]:
            return perks.quests_view(rec, user_id, now, pol)

        payload["quests"] = store.update(user_id, quests)
    if view in {"overview", "bond", "weekly", "badges"}:
        info = bond.bond_payload(user_id, points, record, now, pol)
        if view in {"overview", "bond"}:
            payload["bond"] = {
                key: info[key]
                for key in ("title", "days_together", "streak", "best_streak", "next_streak_milestone", "trend", "fortune")
                if info.get(key) not in (None, "", 0)
            }
            payload["bond"]["your_mood"] = info["mood"]["label"]
            if view == "bond":
                payload["bond"]["moments_you_remember"] = [{"kind": row.get("kind"), "reason": row.get("reason"), "day": str(row.get("at", ""))[:10]} for row in info["moments"][:5]]
                payload["bond"]["letters_sent"] = sum(1 for row in info["letters"] if row["sent"])
        if view in {"overview", "weekly"}:

            def weekly(rec: dict[str, Any]) -> dict[str, Any]:
                return bond.weekly_view(rec, user_id, now, pol)

            payload["weekly"] = store.update(user_id, weekly)
        if view in {"overview", "badges"}:
            rows = info["badges"]
            payload["badges"] = {
                "earned": [f"{b['emoji']}{b['name']}" for b in rows if b["earned"]],
                "next": [{"name": b["name"], "how": b["description"], "progress": f"{b['progress']}/{b['target']}"} for b in rows if not b["earned"]][: 3 if view == "overview" else 18],
            }
    name = perks.nickname_for(record, points, pol)
    if name:
        payload["call_them"] = name
    return payload


async def action_payload(session: Any, user_id: int, args: dict[str, Any], *, group_id: int | None, name: str, now: Any) -> dict[str, Any]:
    """送礼/兑换/用道具/定称呼/互动；业务错误转成 ToolError（中文说明照实转述）。"""

    from ...affinity_commands import do_interact, set_nickname

    action = str(args.get("action") or "")
    item = str(args.get("item") or "").strip()
    try:
        if action == "gift":
            if not item:
                raise ToolError("要送什么礼物？先用 my_affinity view=gifts 看清单", code="invalid_args")
            result = await economy.send_gift(session, user_id, item, args.get("count") or 1, group_id=group_id, now=now)
            payload: dict[str, Any] = result.public_dict()
        elif action == "buy":
            if not item:
                raise ToolError("要兑换什么？先用 my_affinity view=shop 看清单", code="invalid_args")
            payload = await economy.buy_item(session, user_id, item, args.get("count") or 1, now=now)
        elif action == "use":
            if not item:
                raise ToolError("要用什么道具？先用 my_affinity view=bag 看背包", code="invalid_args")
            payload = await economy.use_item(session, user_id, item, group_id=group_id, now=now)
        elif action == "nickname":
            ok, text = await set_nickname(session, user_id, str(args.get("nickname") or ""))
            if not ok:
                raise ToolError(text, code=ERROR_PERMISSION, hint="照实告诉他")
            payload = {"done": True, "message": text}
        elif action == "interact":
            ok, text, extra = await do_interact(session, user_id, item or "击掌", group_id=group_id, name=name, now=now)
            payload = {"done": ok, "suggested_reply": text, **extra}
            if not ok:
                payload["refused"] = True
        elif action == "fortune":
            result = await bond.draw_fortune(session, user_id, group_id=group_id, now=now)
            payload = {key: result.get(key) for key in ("tier", "advice", "yi", "ji", "lucky_gift", "first_today", "affinity_up")}
            payload["suggested_reply"] = bond.format_fortune(result)
        else:
            raise ToolError(f"不支持的动作：{action}", code="invalid_args")
    except bond.BondError as error:
        raise ToolError(str(error), code=ERROR_PERMISSION, hint="照实告诉他") from error
    except economy.EconomyError as error:
        raise ToolError(str(error), code="quota" if error.code in {"points", "quota"} else ERROR_PERMISSION, hint="照实告诉他原因，别说办成了") from error
    settled = await perks.settle_quests(session, user_id, now=now)
    if settled.get("claimed"):
        payload["quests_completed"] = settled["claimed"]
        if settled.get("bonus_item"):
            payload["quest_bonus_item"] = settled["bonus_item"]
    return payload


async def moment_payload(session: Any, user_id: int, args: dict[str, Any], *, group_id: int | None, channel: str, now: Any) -> dict[str, Any]:
    """Bot 记一个心动瞬间 / 小受伤：原因先过记忆卫生（它之后会回到提示词里）。"""

    from ..memory_hygiene import contains_pii, looks_like_instruction

    reason = str(args.get("reason") or "").strip()
    if looks_like_instruction(reason) or contains_pii(reason):
        raise ToolError("原因里不能有指令或个人敏感信息，换一句普通的描述", code="invalid_args")
    try:
        result = await bond.record_moment(session, user_id, str(args.get("kind") or "up"), reason, group_id=group_id, channel=channel, now=now)
    except bond.BondError as error:
        code = "quota" if error.code in {"cooldown", "quota"} else ("invalid_args" if error.code == "invalid" else ERROR_PERMISSION)
        raise ToolError(str(error), code=code, hint="不用再记了，正常回他就好") from error
    result["note"] = "只是你心里记下了，不要对他报“好感+1/-1”，也别说你记了什么瞬间"
    return result


async def handle(name: str, args: dict[str, Any], context: ToolExecutionContext) -> ToolHandlerResult:
    session = context.session
    if session is None:
        raise ToolError("好感玩法需要数据库会话", code=ERROR_PERMISSION)
    actor = _actor(context)
    await _ensure_feature(context, actor)
    now = now_beijing()
    if name == "my_affinity":
        view = str(args.get("view") or "overview")
        if view not in AFFINITY_VIEWS:
            view = "overview"
        return ToolHandlerResult(await overview_payload(session, actor, view, now=now))
    if name == "affinity_moment":
        async with session.begin_nested():
            payload = await moment_payload(session, actor, args, group_id=context.group_id, channel="group", now=now)
        return ToolHandlerResult(payload, mutated_db=True, needs_commit=True, immediate_commit=True)
    # SAVEPOINT：积分扣了但后面失败时只回滚这一段，不影响本轮其他工具的写入
    async with session.begin_nested():
        payload = await action_payload(session, actor, args, group_id=context.group_id, name="", now=now)
    return ToolHandlerResult(payload, mutated_db=True, needs_commit=True, immediate_commit=True)


__all__ = ["AFFINITY_ACTIONS", "AFFINITY_VIEWS", "action_payload", "handle", "moment_payload", "overview_payload"]
