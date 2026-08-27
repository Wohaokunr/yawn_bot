"""版本化、渐进式 RPG 新手引导。"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from nonebot import logger
from nonebot_plugin_orm import get_session

from ..event_log import record_game_event  # noqa: TID252
from ..metrics import record_rpg_tutorial  # noqa: TID252
from .models import RPGPlayerGuide

TUTORIAL_VERSION = 1

HELP_TEXT = {
    "报名": (
        "报名阶段：/报名 加入，/查看报名 看名单，/模组列表 看剧本；"
        "房主负责选模组和开局。请先加机器人好友，以接收角色卡和私人线索。"
    ),
    "建卡": (
        "建卡在私聊完成。可发送“确认”、查看角色卡，或按提示有限调整技能；"
        "内容不会转发到群内。"
    ),
    "行动": (
        "大部分探索和 NPC 对话直接说话即可，不需要背指令；系统负责掷骰。"
        "每个探索轮每人一次主要行动，不确定时发送 /局面。"
    ),
    "线索": "个人线索只对你可见；用 /分享线索 名称 公开后，才会进入团队 /线索板。",
    "推理": (
        "用 /线索板 查看公开证据；/推理 线索A + 线索B：结论 发起，"
        "多人局由另一位玩家 /赞成推理。"
    ),
}
STEP_IDS = {
    "报名": "signup",
    "建卡": "character_creation",
    "行动": "play_action",
    "线索": "private_clue",
    "推理": "deduction",
}

_BJ_TZ = timezone(timedelta(hours=8))


def _now_bj() -> datetime:
    return datetime.now(_BJ_TZ).replace(tzinfo=None)


def help_text(topic: str = "") -> str:
    topic = topic.strip()
    if topic in HELP_TEXT:
        return f"═══ 跑团帮助 · {topic} ═══\n{HELP_TEXT[topic]}"
    return "\n".join(
        [
            "═══ 跑团帮助 ═══",
            "/跑团 — 创建房间",
            "/模组列表 — 查看可选模组",
            "五步入门：报名 → 建卡 → 行动 → 线索 → 推理。",
            "创建房间后，帮助会按报名、建卡和游玩阶段自动更新。",
            "可发送：/跑团帮助 报名|建卡|行动|线索|推理",
        ]
    )


async def guide_enabled(user_id: int) -> bool:
    try:
        async with get_session() as session:
            row = await session.get(RPGPlayerGuide, user_id)
            return row is None or (
                row.tutorial_version < TUTORIAL_VERSION
                or (row.completed_at is None and row.skipped_at is None)
            )
    except Exception:  # noqa: BLE001
        logger.warning("读取 RPG 新手引导档案失败，本次跳过自动引导", exc_info=True)
        return False


async def set_guide_state(user_id: int, state: str) -> None:
    try:
        async with get_session() as session:
            row = await session.get(RPGPlayerGuide, user_id)
            if row is None:
                row = RPGPlayerGuide(user_id=user_id, tutorial_version=TUTORIAL_VERSION)
                session.add(row)
            row.tutorial_version = TUTORIAL_VERSION
            if state == "completed":
                row.completed_at = _now_bj()
                row.skipped_at = None
            elif state == "skipped":
                row.skipped_at = _now_bj()
                row.completed_at = None
            elif state == "reset":
                row.completed_at = None
                row.skipped_at = None
            await session.commit()
    except Exception:  # noqa: BLE001
        logger.warning("写入 RPG 新手引导档案失败", exc_info=True)
    record_rpg_tutorial("profile", state)


async def record_step(game: object, user_id: int, step: str) -> None:
    player = getattr(game, "player_by_user", lambda _uid: None)(user_id)
    seat = getattr(player, "seat", None)
    record_game_event(
        game,
        "rpg",
        "tutorial_step_shown",
        phase=getattr(game, "phase", None),
        actor_seat=seat,
        payload={"step": STEP_IDS.get(step, "unknown")},
    )
    record_rpg_tutorial(step, "shown")
