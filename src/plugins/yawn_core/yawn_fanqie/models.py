"""番茄小说任务与章节元数据模型。

正文不进入 ORM：每章正文写入插件数据目录的临时文件，任务完成后合并为
短期保留的 TXT。用户和群只作逻辑引用，避免跨 bind 外键。
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Optional

from nonebot_plugin_orm import Model
from sqlalchemy import BigInteger, Boolean, ForeignKey, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

_BJ_TZ = timezone(timedelta(hours=8))


def _now_bj() -> datetime:
    """返回项目约定的北京时间 naive datetime。"""

    return datetime.now(_BJ_TZ).replace(tzinfo=None)


class FanqieBook(Model):
    """书籍公开元数据快照。"""

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    book_id: Mapped[str] = mapped_column(String(32), unique=True, index=True)
    title: Mapped[str] = mapped_column(String(256))
    author: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    description: Mapped[Optional[str]] = mapped_column(String(1000), nullable=True)
    url: Mapped[str] = mapped_column(String(512))
    created_at: Mapped[datetime] = mapped_column(default=_now_bj)
    updated_at: Mapped[datetime] = mapped_column(default=_now_bj, onupdate=_now_bj)


class FanqieJob(Model):
    """一次 TXT 生成任务。"""

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    book_record_id: Mapped[int] = mapped_column(
        ForeignKey("yawn_fanqie_fanqiebook.id", ondelete="CASCADE"),
        index=True,
    )
    requester_user_id: Mapped[int] = mapped_column(BigInteger, index=True)
    group_id: Mapped[Optional[int]] = mapped_column(
        BigInteger, index=True, nullable=True
    )
    start_chapter: Mapped[int]
    end_chapter: Mapped[int]
    total_chapters: Mapped[int]
    completed_chapters: Mapped[int] = mapped_column(default=0)
    status: Mapped[str] = mapped_column(String(16), default="queued", index=True)
    cancel_requested: Mapped[bool] = mapped_column(Boolean, default=False)
    output_path: Mapped[Optional[str]] = mapped_column(String(1024), nullable=True)
    output_name: Mapped[Optional[str]] = mapped_column(String(256), nullable=True)
    last_error: Mapped[Optional[str]] = mapped_column(String(1000), nullable=True)
    send_status: Mapped[str] = mapped_column(String(16), default="pending")
    send_error: Mapped[Optional[str]] = mapped_column(String(1000), nullable=True)
    created_at: Mapped[datetime] = mapped_column(default=_now_bj, index=True)
    updated_at: Mapped[datetime] = mapped_column(default=_now_bj, onupdate=_now_bj)
    started_at: Mapped[Optional[datetime]] = mapped_column(nullable=True)
    completed_at: Mapped[Optional[datetime]] = mapped_column(nullable=True)


class FanqieJobChapter(Model):
    """任务中的单章状态和临时文件索引。"""

    __table_args__ = (
        UniqueConstraint("job_id", "chapter_index", name="uq_fanqie_job_chapter_index"),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    job_id: Mapped[int] = mapped_column(
        ForeignKey("yawn_fanqie_fanqiejob.id", ondelete="CASCADE"),
        index=True,
    )
    chapter_index: Mapped[int]
    item_id: Mapped[str] = mapped_column(String(32))
    title: Mapped[str] = mapped_column(String(256))
    is_locked: Mapped[bool] = mapped_column(Boolean, default=False)
    status: Mapped[str] = mapped_column(String(16), default="pending")
    temp_path: Mapped[Optional[str]] = mapped_column(String(1024), nullable=True)
    last_error: Mapped[Optional[str]] = mapped_column(String(1000), nullable=True)
    completed_at: Mapped[Optional[datetime]] = mapped_column(nullable=True)


__all__ = ["FanqieBook", "FanqieJob", "FanqieJobChapter"]
