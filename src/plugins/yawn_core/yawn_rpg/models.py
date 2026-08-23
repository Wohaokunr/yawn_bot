"""跑团对局记录数据模型。

表归属子插件 yawn_rpg（bind_key=yawn_rpg，表名自动前缀
yawn_rpg_）。对 yawn_core_botgroup / yawn_core_botuser 仅作
逻辑引用，不建跨 bind 外键——用户与群的存在性由父插件
presence 模块保证。
"""

from datetime import datetime, timedelta, timezone
from typing import Optional

from nonebot_plugin_orm import Model
from sqlalchemy import BigInteger, Boolean, ForeignKey, String
from sqlalchemy.orm import Mapped, mapped_column

_BJ_TZ = timezone(timedelta(hours=8))


def _now_bj() -> datetime:
    """返回当前北京时间（naive），与项目时间约定一致。"""
    return datetime.now(_BJ_TZ).replace(tzinfo=None)


class RPGGame(Model):
    """一局跑团的对局记录。"""

    id: Mapped[int] = mapped_column(
        primary_key=True,
        autoincrement=True,
    )

    # 对局所在群（逻辑引用 yawn_core_botgroup.group_id）
    group_id: Mapped[int] = mapped_column(BigInteger, index=True)

    # 房主 QQ 号（逻辑引用 yawn_core_botuser.user_id）
    host_user_id: Mapped[int] = mapped_column(BigInteger)

    # 模组标识与显示名
    module_id: Mapped[str] = mapped_column(String(64))
    module_name: Mapped[str] = mapped_column(String(64))

    player_count: Mapped[int]

    # 事件日志稳定 id（state.Game.event_log_id）；赛后据此定位 JSONL 回放。
    # 旧版本对局与写库失败的开局为 None，回放端点据此优雅降级。
    event_log_id: Mapped[Optional[str]] = mapped_column(
        String(64), nullable=True, index=True
    )

    started_at: Mapped[datetime] = mapped_column(default=_now_bj)

    # 对局未正常结束时为 None（如流局、强制解散不写终局字段）
    ended_at: Mapped[Optional[datetime]] = mapped_column(
        nullable=True,
    )

    # 达成的结局 id（Ending.id）
    ending_id: Mapped[Optional[str]] = mapped_column(
        String(64),
        nullable=True,
    )

    # 结局倾向："good" | "bad" | "neutral"
    outcome: Mapped[Optional[str]] = mapped_column(
        String(8),
        nullable=True,
    )

    termination_reason: Mapped[Optional[str]] = mapped_column(
        String(32), nullable=True
    )


class RPGPlayer(Model):
    """单个玩家在一局中的记录。"""

    id: Mapped[int] = mapped_column(
        primary_key=True,
        autoincrement=True,
    )

    # 同 bind 内外键，迁移安全。类型须与 RPGGame.id（自增 Integer
    # 主键）一致：BigInteger 引用 Integer 会被 PostgreSQL / MySQL
    # 以"外键列类型不兼容"拒绝建表
    game_id: Mapped[int] = mapped_column(
        ForeignKey(
            "yawn_rpg_rpggame.id",
            ondelete="CASCADE",
        ),
        index=True,
    )

    # 玩家 QQ 号（逻辑引用 yawn_core_botuser.user_id）
    user_id: Mapped[int] = mapped_column(BigInteger, index=True)

    # 角色名（系统生成）
    char_name: Mapped[str] = mapped_column(String(32))

    # 开局时的 HP / SAN 上限
    start_hp: Mapped[int]
    start_san: Mapped[int]

    # 终局时的 HP / SAN（对局未结束为 None）
    final_hp: Mapped[Optional[int]] = mapped_column(nullable=True)
    final_san: Mapped[Optional[int]] = mapped_column(nullable=True)

    # 是否失去行动能力（倒地 / 永久疯狂）
    is_incapped: Mapped[bool] = mapped_column(
        Boolean,
        default=False,
    )

    # 是否存活到结局；对局未结束为 None
    survived: Mapped[Optional[bool]] = mapped_column(nullable=True)


class RPGPlayerGuide(Model):
    """RPG 新手引导完成状态；不保存任何玩法私密内容。"""

    user_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    tutorial_version: Mapped[int] = mapped_column(default=1)
    completed_at: Mapped[Optional[datetime]] = mapped_column(nullable=True)
    skipped_at: Mapped[Optional[datetime]] = mapped_column(nullable=True)
