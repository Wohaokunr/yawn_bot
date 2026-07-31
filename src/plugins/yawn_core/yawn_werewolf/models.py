"""狼人杀对局记录数据模型。

表归属子插件 yawn_werewolf（bind_key=yawn_werewolf，
表名自动前缀 yawn_werewolf_）。对 yawn_core_botgroup /
yawn_core_botuser 仅作逻辑引用，不建跨 bind 外键——
用户与群的存在性由父插件 presence 模块保证。
"""

from datetime import datetime, timedelta, timezone
from typing import Optional

from nonebot_plugin_orm import Model
from sqlalchemy import BigInteger, ForeignKey, String
from sqlalchemy.orm import Mapped, mapped_column

_BJ_TZ = timezone(timedelta(hours=8))


def _now_bj() -> datetime:
    """返回当前北京时间（naive），与项目时间约定一致。"""
    return datetime.now(_BJ_TZ).replace(tzinfo=None)


class WerewolfGame(Model):
    """一局狼人杀的对局记录。"""

    id: Mapped[int] = mapped_column(
        primary_key=True,
        autoincrement=True,
    )

    # 对局所在群（逻辑引用 yawn_core_botgroup.group_id）
    group_id: Mapped[int] = mapped_column(BigInteger, index=True)

    # 房主 QQ 号（逻辑引用 yawn_core_botuser.user_id）
    host_user_id: Mapped[int] = mapped_column(BigInteger)

    # 板子键名（roles.BOARDS 的键，如 "预女猎白混"）；旧记录为 None
    board: Mapped[Optional[str]] = mapped_column(String(16), nullable=True)

    player_count: Mapped[int]

    started_at: Mapped[datetime] = mapped_column(default=_now_bj)

    # 对局未正常结束时为 None（如流局、强制解散不写终局字段）
    ended_at: Mapped[Optional[datetime]] = mapped_column(
        nullable=True,
    )

    # 获胜阵营："wolf" | "good"
    winner_faction: Mapped[Optional[str]] = mapped_column(
        String(8),
        nullable=True,
    )

    # 结束时的回合数
    end_round: Mapped[Optional[int]] = mapped_column(nullable=True)


class WerewolfPlayer(Model):
    """单个玩家在一局中的记录。"""

    id: Mapped[int] = mapped_column(
        primary_key=True,
        autoincrement=True,
    )

    # 同 bind 内外键，迁移安全
    game_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey(
            "yawn_werewolf_werewolfgame.id",
            ondelete="CASCADE",
        ),
        index=True,
    )

    # 玩家 QQ 号（逻辑引用 yawn_core_botuser.user_id；AI 玩家为负数合成 ID）
    user_id: Mapped[int] = mapped_column(BigInteger, index=True)

    # 是否为 AI 玩家（战绩统计据此过滤）
    is_ai: Mapped[bool] = mapped_column(default=False)

    seat: Mapped[int]

    # 角色标识（Role.value，如 "预言家"）
    role: Mapped[str] = mapped_column(String(16))

    # 阵营："wolf" | "good"
    faction: Mapped[str] = mapped_column(String(8))

    # 是否属于获胜阵营；对局未结束为 None
    is_winner: Mapped[Optional[bool]] = mapped_column(nullable=True)

    # 是否曾担任警长
    is_sheriff: Mapped[bool] = mapped_column(default=False)

    death_round: Mapped[Optional[int]] = mapped_column(nullable=True)

    # 死因（DeathCause.value）：WOLF_KILL / WITCH_POISON / VOTED /
    # HUNTER_SHOT / SELF_DETONATION / KNIGHT_KILL / KNIGHT_DEATH；存活为 None
    death_cause: Mapped[Optional[str]] = mapped_column(
        String(16),
        nullable=True,
    )
