from datetime import datetime
from typing import Optional

from nonebot_plugin_orm import Model
from sqlalchemy import BigInteger, String, func
from sqlalchemy.orm import Mapped, mapped_column


class FriendRequest(Model):
    """好友申请审批记录，每个用户只保留一条。"""

    # 申请人 QQ 号，主键，同一用户只有一行
    user_id: Mapped[int] = mapped_column(
        BigInteger, primary_key=True
    )

    # OneBot 协议用于审批的 flag（每次申请覆盖）
    flag: Mapped[str] = mapped_column(String(255))

    # 申请验证信息
    comment: Mapped[Optional[str]] = mapped_column(
        String(512), nullable=True
    )

    # 状态: pending / approved / rejected
    status: Mapped[str] = mapped_column(
        String(16), default="pending"
    )

    created_at: Mapped[datetime] = mapped_column(
        server_default=func.current_timestamp(),
    )
    processed_at: Mapped[Optional[datetime]] = (
        mapped_column(nullable=True)
    )
