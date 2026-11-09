# ruff: noqa: E501, PLR0913, RUF001, TC002
"""好感玩法命令：查看权益、好感榜、送礼、商店、背包、道具、每日任务、专属称呼、互动动作。

逻辑都在 ``affinity_perks`` / ``affinity_economy``，这里只做事件解析、功能开关与回复。
功能开关跟随“签到”（好感本来就是签到的一部分）：本群或本人的签到被关掉时这些命令也不可用。
每天凌晨 4:10 结算一次长期不来往的好感衰减。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from nonebot import get_driver, logger, on_command
from nonebot.adapters.onebot.v11 import GroupMessageEvent, Message, MessageEvent
from nonebot.matcher import Matcher
from nonebot.params import CommandArg
from nonebot.plugin import PluginMetadata
from nonebot_plugin_orm import async_scoped_session

from . import affinity
from . import affinity_economy as economy
from . import affinity_perks as perks
from . import affinity_store as store
from .affinity_policy import load_policy
from .checkin_service import CHECKIN_TIMEZONE
from .command_catalog import CommandSpec, PluginCommandGroup, register_command_group
from .permission import require_feature

COMMAND_GROUP = register_command_group(
    PluginCommandGroup(
        plugin_id="yawn_core.affinity",
        display_name="好感",
        entrypoint="好感",
        commands=(
            CommandSpec(name="好感", aliases=("我的好感", "好感度"), description="看看和 Bot 的关系、这一级的权益、今天的任务和背包", feature="checkin"),
            CommandSpec(name="好感权益", description="每个等级能解锁什么、额度多给多少", feature="checkin", display_level="advanced"),
            CommandSpec(name="好感榜", description="本群和 Bot 最熟的人", feature="checkin", scope="group"),
            CommandSpec(name="送礼", description="花签到积分送 Bot 礼物涨好感：/送礼 奶茶 2；不写礼物看清单", feature="checkin"),
            CommandSpec(name="好感商店", description="用签到积分兑换道具（补签卡、好感护盾、暴击卡、刷新券）", feature="checkin"),
            CommandSpec(name="兑换", description="/兑换 补签卡 [数量]", feature="checkin", display_level="advanced"),
            CommandSpec(name="背包", description="看看有哪些道具", feature="checkin", display_level="advanced"),
            CommandSpec(name="使用道具", description="/使用道具 补签卡（群里用就续本群的连签）", feature="checkin", display_level="advanced"),
            CommandSpec(name="好感任务", aliases=("每日任务",), description="今天的三个小任务，做完自动领好感", feature="checkin"),
            CommandSpec(name="叫我", description="/叫我 小明：让 Bot 用这个称呼叫你（「熟人」解锁）；/叫我 清除", feature="checkin", display_level="advanced"),
            CommandSpec(name="击掌", aliases=("摸头", "抱抱", "贴贴", "举高高"), description="和 Bot 互动，越熟能做的越多", feature="checkin"),
        ),
    )
)

__plugin_meta__ = PluginMetadata(
    name="好感",
    description="好感等级权益、送礼、商店、每日任务与互动",
    usage="/好感 /好感权益 /好感榜 /送礼 /好感商店 /兑换 /背包 /使用道具 /好感任务 /叫我 /击掌 /摸头 /抱抱 /贴贴 /举高高",
    extra={"command_group": COMMAND_GROUP},
)

_PRIORITY = 5


def _now() -> datetime:
    return datetime.now(CHECKIN_TIMEZONE).replace(tzinfo=None)


def _group_of(event: MessageEvent) -> int | None:
    return int(event.group_id) if isinstance(event, GroupMessageEvent) else None


def _display_name(event: MessageEvent) -> str:
    sender = event.sender
    return str(getattr(sender, "card", "") or getattr(sender, "nickname", "") or "")


async def _reply(matcher: type[Matcher] | Matcher, event: MessageEvent, text: str) -> None:
    if isinstance(event, GroupMessageEvent):
        from nonebot.adapters.onebot.v11 import MessageSegment

        await matcher.finish(MessageSegment.at(event.user_id) + " " + text)
    await matcher.finish(text)


# ── 纯文本拼装（便于测试） ────────────────────────────────────────────


def format_overview(snapshot: affinity.AffinitySnapshot, record: dict[str, Any], wallet: economy.Wallet, now: datetime) -> str:
    pol = load_policy()
    level = snapshot.level
    current = pol.perks(level.name)
    lines = [f"💗 和我的关系：{level.name}（{affinity.level_tone(level.name)}）"]
    if level.next_name:
        lines.append(f"距离「{level.next_name}」还差 {level.to_next(snapshot.points)} 点，今天 +{sum(v for v in snapshot.today.values() if v > 0)}")
    if not pol.enabled:
        lines.append("好感玩法现在没开，等级只影响我说话的语气。")
        return "\n".join(lines)
    public = perks.perks_public(current)
    if public["bonuses"]:
        lines.append("这一级的加成：" + "；".join(f"{b['label']} +{b['value']}{'%' if b['unit'] == '%' else ''}" for b in public["bonuses"][:5]))
    if current.unlocks:
        lines.append("已解锁：" + perks.unlock_summary(current))
    upcoming = perks.next_unlocks(snapshot.points, pol)
    if upcoming and upcoming["unlocks"]:
        lines.append(f"到「{upcoming['level']}」解锁：" + "、".join(u["label"] for u in upcoming["unlocks"]))
    quests = perks.quests_view(record, snapshot.user_id, now, pol)
    if quests["enabled"] and quests["items"]:
        done = sum(1 for q in quests["items"] if q["done"])
        lines.append(f"今日任务 {done}/{len(quests['items'])}：" + "；".join(f"{'✅' if q['done'] else '⬜'}{q['title']}" for q in quests["items"]))
    fav = economy.favorite_gift(now, pol)
    used = economy.gifts_used_today(record, now)
    lines.append(f"签到积分 {wallet.total} · 今天送礼 {used}/{current.gift_daily}" + (f" · 今日心仪 {fav.emoji}{fav.name}" if fav else ""))
    inv = economy.inventory_view(record, pol, today=now.date())
    if inv["items"]:
        lines.append("背包：" + "、".join(f"{i['name']}×{i['count']}" for i in inv["items"]))
    if inv["shield_until"]:
        lines.append(f"🛡️ 好感护盾生效到 {inv['shield_until']}")
    name = perks.nickname_for(record, snapshot.points, pol)
    if name:
        lines.append(f"我会叫你「{name}」")
    return "\n".join(lines)


def format_perk_table() -> str:
    pol = load_policy()
    lines = ["各等级权益（好感点数下限）："]
    for (floor, name), row in zip(affinity.LEVELS, pol.levels, strict=False):
        bits = []
        if row.private_turns_pct:
            bits.append(f"私聊轮数+{row.private_turns_pct}%")
        if row.checkin_points_pct:
            bits.append(f"签到积分+{row.checkin_points_pct}%")
        if row.web_search_bonus:
            bits.append(f"搜索+{row.web_search_bonus}")
        if row.sandbox_bonus:
            bits.append(f"沙箱+{row.sandbox_bonus}")
        bits.append(f"每天送礼{row.gift_daily}次")
        if row.shop_discount_pct:
            bits.append(f"商店打{(100 - row.shop_discount_pct) / 10:g}折")
        unlocked = perks.unlock_summary(row)
        if unlocked:
            bits.append("解锁 " + unlocked)
        lines.append(f"【{name} {floor}+】" + "，".join(bits))
    return "\n".join(lines)


def format_gift_menu(record: dict[str, Any], points: int, now: datetime) -> str:
    pol = load_policy()
    level_perks = perks.perks_for(points, pol)
    fav = economy.favorite_gift(now, pol)
    lines = ["可以送的礼物（价格是签到积分）："]
    for gift in pol.gifts:
        if not gift.enabled:
            continue
        price = economy.price_after_discount(gift.price, level_perks.shop_discount_pct)
        mark = " ⭐今日心仪，效果翻倍" if fav and fav.key == gift.key else ""
        lines.append(f"{gift.emoji}{gift.name}：{price} 积分{mark}")
    lines.append(f"今天还能送 {max(level_perks.gift_daily - economy.gifts_used_today(record, now), 0)} 次；用法：/送礼 奶茶 2")
    return "\n".join(lines)


def format_shop(record: dict[str, Any], points: int) -> str:
    rows = economy.shop_view(points, record)
    if not rows:
        return "好感商店暂时没有上架东西~"
    lines = ["好感商店（价格是签到积分）："]
    for row in rows:
        lock = "" if row["unlocked"] else f"（「{row['min_level']}」解锁）"
        price = f"{row['final_price']}" + (f"（原价 {row['price']}）" if row["final_price"] != row["price"] else "")
        lines.append(f"· {row['name']} {price}{lock} 已有 {row['have']}/{row['max_hold']}：{row['description']}")
    lines.append("用法：/兑换 补签卡 [数量]，/使用道具 补签卡")
    return "\n".join(lines)


def format_quests(view: dict[str, Any]) -> str:
    if not view["enabled"]:
        return "每日任务现在没开~"
    lines = ["今天的任务："]
    for item in view["items"]:
        mark = "✅" if item["done"] else "⬜"
        lines.append(f"{mark} {item['title']}（{item['progress']}/{item['target']}）")
    tail = f"每完成一个好感 +{view['reward_affinity']}"
    if view["all_done_item_name"]:
        tail += f"，全做完再送一张{view['all_done_item_name']}"
    lines.append(tail)
    return "\n".join(lines)


# ── 命令 ────────────────────────────────────────────────────────────

overview_cmd = on_command("好感", aliases={"我的好感", "好感度"}, priority=_PRIORITY, block=True)
perk_cmd = on_command("好感权益", priority=_PRIORITY, block=True)
board_cmd = on_command("好感榜", priority=_PRIORITY, block=True)
gift_cmd = on_command("送礼", priority=_PRIORITY, block=True)
shop_cmd = on_command("好感商店", priority=_PRIORITY, block=True)
buy_cmd = on_command("兑换", priority=_PRIORITY, block=True)
bag_cmd = on_command("背包", priority=_PRIORITY, block=True)
use_cmd = on_command("使用道具", priority=_PRIORITY, block=True)
quest_cmd = on_command("好感任务", aliases={"每日任务"}, priority=_PRIORITY, block=True)
nickname_cmd = on_command("叫我", priority=_PRIORITY, block=True)
interact_cmd = on_command("击掌", aliases={"摸头", "抱抱", "贴贴", "举高高"}, priority=_PRIORITY, block=True)


@overview_cmd.handle()
async def _overview(event: MessageEvent, session: async_scoped_session, _perm: None = require_feature("checkin")) -> None:  # pyright: ignore[reportArgumentType]
    user_id = int(event.user_id)
    now = _now()
    settled = await perks.settle_quests(session, user_id, now=now)
    await session.commit()
    snapshot = await affinity.snapshot(session, user_id, now=now)
    wallet = await economy.wallet(session, user_id)
    text = format_overview(snapshot, store.load(user_id), wallet, now)
    extra = perks.quest_reply(settled)
    await _reply(overview_cmd, event, text + (f"\n{extra}" if extra else ""))


@perk_cmd.handle()
async def _perks(event: MessageEvent, _perm: None = require_feature("checkin")) -> None:  # pyright: ignore[reportArgumentType]
    await _reply(perk_cmd, event, format_perk_table())


@board_cmd.handle()
async def _board(event: GroupMessageEvent, session: async_scoped_session, _perm: None = require_feature("checkin")) -> None:  # pyright: ignore[reportArgumentType]
    from sqlalchemy import select

    from .data_models.bot_user import BotUser
    from .data_models.user_group import UserGroup

    ranking = await affinity.group_ranking(session, int(event.group_id), limit=10)
    if not ranking:
        await board_cmd.finish("本群还没有人和我攒下好感~ 签到、找我聊天都会涨哦")
    ids = [uid for uid, _ in ranking]
    names = {
        int(uid): str(card or nick or uid)
        for uid, card, nick in (
            await session.execute(
                select(UserGroup.user_id, UserGroup.group_nickname, BotUser.nickname)
                .join(BotUser, BotUser.user_id == UserGroup.user_id)
                .where(UserGroup.group_id == int(event.group_id), UserGroup.user_id.in_(ids))
            )
        ).all()
    }
    totals = {
        int(uid): int(points or 0)
        for uid, points in (await session.execute(select(BotUser.user_id, BotUser.affinity).where(BotUser.user_id.in_(ids)))).all()
    }
    medals = ("🥇", "🥈", "🥉")
    lines = ["本群和我最熟的人："]
    for index, (uid, points) in enumerate(ranking):
        mark = medals[index] if index < len(medals) else f"{index + 1}."
        lines.append(f"{mark} {names.get(uid, uid)} · {affinity.level_for(totals.get(uid, 0)).name} · 本群好感 {points}")
    await board_cmd.finish("\n".join(lines))


def _args(arg: Message) -> list[str]:
    return [part for part in arg.extract_plain_text().split() if part]


@gift_cmd.handle()
async def _gift(event: MessageEvent, session: async_scoped_session, arg: Message = CommandArg(), _perm: None = require_feature("checkin")) -> None:  # pyright: ignore[reportArgumentType]
    user_id = int(event.user_id)
    now = _now()
    parts = _args(arg)
    if not parts:
        points = await perks.load_points(session, user_id)
        await _reply(gift_cmd, event, format_gift_menu(store.load(user_id), points, now))
    count = parts[1] if len(parts) > 1 else 1
    group_id = _group_of(event)
    try:
        result = await economy.send_gift(session, user_id, parts[0], count, group_id=group_id, now=now)
        settled = await perks.settle_quests(session, user_id, now=now)
        await session.commit()
    except economy.EconomyError as error:
        await session.rollback()
        await _reply(gift_cmd, event, str(error))
        return
    lines = [result.reaction, f"（花了 {result.cost} 积分" + ("，好感涨了" if result.affinity_gain else "") + f"，今天还能送 {result.left_today} 次）"]
    if result.leveled_up_to:
        lines.append(f"🎉 我们的关系升级为「{result.leveled_up_to}」啦！发 /好感 看看解锁了什么")
    extra = perks.quest_reply(settled)
    if extra:
        lines.append(extra)
    await _reply(gift_cmd, event, "\n".join(lines))


@shop_cmd.handle()
async def _shop(event: MessageEvent, session: async_scoped_session, _perm: None = require_feature("checkin")) -> None:  # pyright: ignore[reportArgumentType]
    user_id = int(event.user_id)
    points = await perks.load_points(session, user_id)
    wallet = await economy.wallet(session, user_id)
    await _reply(shop_cmd, event, format_shop(store.load(user_id), points) + f"\n你的签到积分：{wallet.total}")


@buy_cmd.handle()
async def _buy(event: MessageEvent, session: async_scoped_session, arg: Message = CommandArg(), _perm: None = require_feature("checkin")) -> None:  # pyright: ignore[reportArgumentType]
    parts = _args(arg)
    if not parts:
        await _reply(buy_cmd, event, "要兑换什么？例如 /兑换 补签卡；/好感商店 看清单")
    try:
        result = await economy.buy_item(session, int(event.user_id), parts[0], parts[1] if len(parts) > 1 else 1, now=_now())
        await session.commit()
    except economy.EconomyError as error:
        await session.rollback()
        await _reply(buy_cmd, event, str(error))
        return
    discount = f"（{100 - result['discount_pct']}% 价）" if result["discount_pct"] else ""
    await _reply(buy_cmd, event, f"兑换成功：{result['item']}×{result['count']}，花了 {result['points_spent']} 积分{discount}，现在有 {result['have']} 张")


@bag_cmd.handle()
async def _bag(event: MessageEvent, _perm: None = require_feature("checkin")) -> None:  # pyright: ignore[reportArgumentType]
    now = _now()
    view = economy.inventory_view(store.load(int(event.user_id)), today=now.date())
    if not view["items"] and not view["shield_until"]:
        await _reply(bag_cmd, event, "背包空空的~ 去 /好感商店 看看，升级也会送道具")
    lines = ["你的背包："] + [f"· {i['name']}×{i['count']}：{i['description']}" for i in view["items"]]
    if view["shield_until"]:
        lines.append(f"🛡️ 好感护盾生效到 {view['shield_until']}")
    if view["lucky_today"]:
        lines.append("🎯 今天下一次签到积分翻倍")
    await _reply(bag_cmd, event, "\n".join(lines))


@use_cmd.handle()
async def _use(event: MessageEvent, session: async_scoped_session, arg: Message = CommandArg(), _perm: None = require_feature("checkin")) -> None:  # pyright: ignore[reportArgumentType]
    parts = _args(arg)
    if not parts:
        await _reply(use_cmd, event, "要用什么道具？例如 /使用道具 补签卡；/背包 看看有什么")
    group_id = _group_of(event)
    if len(parts) > 1 and parts[1].isdigit():
        group_id = int(parts[1])
    try:
        result = await economy.use_item(session, int(event.user_id), parts[0], group_id=group_id, now=_now())
        await session.commit()
    except economy.EconomyError as error:
        await session.rollback()
        await _reply(use_cmd, event, str(error))
        return
    await _reply(use_cmd, event, format_use_result(result))


def format_use_result(result: dict[str, Any]) -> str:
    key = result["key"]
    if key == "repair_card":
        return f"补签成功！「{result['group_name']}」补回 {result['days_restored']} 天，连签现在是 {result['streak_days']} 天，今天记得签到哦"
    if key == "shield":
        return f"🛡️ 好感护盾生效到 {result['shield_until']}，这段时间不来找我也不会掉好感"
    if key == "lucky_card":
        return "🎯 暴击卡生效：今天下一次签到积分翻倍！"
    if key == "quest_refresh":
        return "任务换好啦：" + "、".join(result.get("new_quests") or [])
    return f"用掉了一张{result['item']}"


@quest_cmd.handle()
async def _quests(event: MessageEvent, session: async_scoped_session, _perm: None = require_feature("checkin")) -> None:  # pyright: ignore[reportArgumentType]
    user_id = int(event.user_id)
    now = _now()
    settled = await perks.settle_quests(session, user_id, now=now)
    await session.commit()

    def view(record: dict[str, Any]) -> dict[str, Any]:
        return perks.quests_view(record, user_id, now)

    text = format_quests(store.update(user_id, view))
    extra = perks.quest_reply(settled)
    await _reply(quest_cmd, event, text + (f"\n{extra}" if extra else ""))


NICKNAME_MAX = 12


def clean_nickname(raw: str) -> str | None:
    """称呼只留可见字符、最多 12 个字；含指令/敏感内容时返回 None。"""

    raw_text = str(raw or "")
    # CQ 码、@ 提及（含“@全体成员”）一律不收：称呼会被 Bot 原样念出来
    if "[CQ:" in raw_text or "@" in raw_text or "＠" in raw_text or "全体成员" in raw_text:
        return None
    text = "".join(ch for ch in raw_text.strip() if ch.isprintable() and ch not in "\n\r\t[]【】{}<>")
    text = text[:NICKNAME_MAX]
    if not text:
        return None
    try:
        from .yawn_agent.memory_hygiene import looks_like_instruction

        if looks_like_instruction(text):
            return None
    except Exception:  # noqa: BLE001
        pass
    return text


async def set_nickname(session: Any, user_id: int, raw: str) -> tuple[bool, str]:
    """设/清专属称呼；返回 (是否成功, 回复文本)。供命令、Agent 工具和 WebUI 共用。"""

    pol = load_policy()
    points = await perks.load_points(session, user_id)
    if raw.strip() in {"", "清除", "取消", "clear"}:
        store.update(user_id, lambda rec: rec.__setitem__("nickname", ""))
        return True, "好，我不再用专属称呼叫你了"
    if not perks.has_unlock(points, "nickname", pol):
        need = pol.unlock_level("nickname")
        return False, f"等我们熟到「{need}」再定专属称呼吧～" if need else "专属称呼现在没有开放"
    name = clean_nickname(raw)
    if name is None:
        return False, "这个称呼我叫不出口……换一个吧"
    store.update(user_id, lambda rec: rec.__setitem__("nickname", name))
    return True, f"好嘞，以后叫你「{name}」～"


@nickname_cmd.handle()
async def _nickname(event: MessageEvent, session: async_scoped_session, arg: Message = CommandArg(), _perm: None = require_feature("checkin")) -> None:  # pyright: ignore[reportArgumentType]
    raw = arg.extract_plain_text()
    if not raw.strip():
        name = store.load(int(event.user_id)).get("nickname") or ""
        await _reply(nickname_cmd, event, f"现在叫你「{name}」；/叫我 清除 可以取消" if name else "用法：/叫我 小明")
    _ok, text = await set_nickname(session, int(event.user_id), raw)
    await _reply(nickname_cmd, event, text)


async def do_interact(session: Any, user_id: int, word: str, *, group_id: int | None, name: str, now: datetime) -> tuple[bool, str, dict[str, Any]]:
    """一次互动动作：等级不够就婉拒；够了回一句并给一点好感（受日上限）。"""

    unlock = perks.INTERACT_COMMANDS.get(word)
    if unlock is None:
        return False, "我不知道该怎么回应这个～", {}
    pol = load_policy()
    points = await perks.load_points(session, user_id)
    if not pol.enabled:
        return False, "互动现在没开放哦", {}
    if not perks.has_unlock(points, unlock, pol):
        return False, perks.interact_refusal(unlock, points, pol), {}
    record = store.load(user_id)
    call = perks.nickname_for(record, points, pol) or name
    text = perks.interact_reply(unlock, user_id, now, name=call)
    change = await affinity.grant(session, user_id, "interact", perks.INTERACT_AFFINITY, group_id=group_id, note=f"/{word}", now=now)
    day = now.date().isoformat()

    def write(rec: dict[str, Any]) -> None:
        today = rec.get("interact_today") or {}
        if today.get("day") != day:
            today = {"day": day, "count": 0, "by": {}}
        today["count"] = int(today.get("count") or 0) + 1
        today.setdefault("by", {})[unlock] = int(today["by"].get(unlock, 0)) + 1
        rec["interact_today"] = today
        totals = rec.setdefault("interact_totals", {})
        totals[unlock] = int(totals.get(unlock, 0)) + 1

    store.update(user_id, write)
    settled = await perks.settle_quests(session, user_id, now=now)
    extra = perks.quest_reply(settled)
    if change.leveled_up:
        text += f"\n🎉 我们的关系升级为「{change.level_after.name}」啦！"
    if extra:
        text += f"\n{extra}"
    return True, text, {"affinity_up": change.applied > 0, "level": change.level_after.name if change.available else affinity.level_for(points).name}


@interact_cmd.handle()
async def _interact(event: MessageEvent, session: async_scoped_session, _perm: None = require_feature("checkin")) -> None:  # pyright: ignore[reportArgumentType]
    raw = event.get_plaintext().strip().lstrip("/／").split()
    word = raw[0] if raw else "击掌"
    _ok, text, _payload = await do_interact(session, int(event.user_id), word, group_id=_group_of(event), name=_display_name(event), now=_now())
    await session.commit()
    await _reply(interact_cmd, event, text)


# ── 每日衰减 ────────────────────────────────────────────────────────

DECAY_JOB_ID = "yawn_affinity_decay"


async def _decay_job() -> None:
    from nonebot_plugin_orm import get_session

    try:
        async with get_session() as session:
            result = await perks.run_decay(session, now=_now())
            await session.commit()
        if result:
            logger.info(f"好感衰减：{len(result)} 人因长期没有来往掉了好感")
    except Exception:  # noqa: BLE001
        logger.exception("好感衰减结算失败")


@get_driver().on_startup
async def _register_decay_job() -> None:
    try:
        from nonebot_plugin_apscheduler import scheduler
    except Exception:  # noqa: BLE001
        return
    if scheduler.get_job(DECAY_JOB_ID) is None:
        scheduler.add_job(_decay_job, "cron", hour=4, minute=10, id=DECAY_JOB_ID, replace_existing=True)


__all__ = [
    "COMMAND_GROUP",
    "NICKNAME_MAX",
    "clean_nickname",
    "do_interact",
    "format_gift_menu",
    "format_overview",
    "format_perk_table",
    "format_quests",
    "format_shop",
    "format_use_result",
    "set_nickname",
]
