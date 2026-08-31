from datetime import datetime
from typing import Optional

from nonebot_plugin_orm import Model
from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column


class GroupAgentConfig(Model):
    """群聊 Agent 配置；一群一份。"""

    group_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("yawn_core_botgroup.group_id", ondelete="CASCADE"),
        primary_key=True,
    )
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    # Persona v2 only. ``persona_enabled`` means this group owns a custom profile;
    # False means follow the global natural Persona while keeping no legacy fallback.
    persona_enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    persona_version: Mapped[int] = mapped_column(Integer, default=1)
    persona_profile: Mapped[dict[str, object]] = mapped_column(JSON, default=dict)
    # Legacy compatibility only. Runtime behavior is controlled by the orthogonal
    # capability flags below; keep this field for one compatibility window.
    trigger_mode: Mapped[str] = mapped_column(
        String(24), default="mention_or_proactive"
    )
    reply_trigger_enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    explicit_wakeup_enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    proactive_enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    proactive_probability: Mapped[float] = mapped_column(Float, default=0.35)
    idle_threshold_minutes: Mapped[int] = mapped_column(Integer, default=15)
    cooldown_minutes: Mapped[int] = mapped_column(Integer, default=8)
    # 热闹插话：话题间隙内像真人群友一样插嘴，与冷场暖场分开配置。
    proactive_active_enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    # 短会话续聊独立于主动参与能力；保留现有群的历史配置。
    short_conversation_enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    proactive_active_probability: Mapped[float] = mapped_column(Float, default=0.25)
    proactive_active_window_minutes: Mapped[int] = mapped_column(Integer, default=12)
    daily_limit: Mapped[int] = mapped_column(Integer, default=30)
    raw_retention_days: Mapped[int] = mapped_column(Integer, default=7)
    cross_group_visibility: Mapped[str] = mapped_column(
        String(24), default="public_summary"
    )
    media_cache_enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    updated_by: Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True)
    last_agent_at: Mapped[Optional[datetime]] = mapped_column(nullable=True)
    # 主动发言专用冷却基准：与被动回复写入的 last_agent_at 解耦，
    # 被@答话不再封锁主动发言一个完整冷却期。
    last_proactive_at: Mapped[Optional[datetime]] = mapped_column(nullable=True)
    proactive_day: Mapped[Optional[str]] = mapped_column(String(16), nullable=True)
    proactive_count: Mapped[int] = mapped_column(Integer, default=0)
    active_topic: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    emotion_state: Mapped[dict[str, object]] = mapped_column(JSON, default=dict)
    last_response_fingerprint: Mapped[Optional[str]] = mapped_column(
        String(64), nullable=True
    )
    last_response_input_fingerprint: Mapped[Optional[str]] = mapped_column(
        String(64), nullable=True
    )
    last_response_at: Mapped[Optional[datetime]] = mapped_column(nullable=True)
    recent_response_fingerprints: Mapped[list[dict[str, object]]] = mapped_column(
        JSON, default=list
    )
    context_epoch: Mapped[int] = mapped_column(Integer, default=0)
    last_compacted_message_id: Mapped[Optional[int]] = mapped_column(
        BigInteger, nullable=True
    )
    memory_rebuild_required: Mapped[bool] = mapped_column(Boolean, default=False)
    memory_last_attempt_at: Mapped[Optional[datetime]] = mapped_column(nullable=True)
    memory_last_success_at: Mapped[Optional[datetime]] = mapped_column(nullable=True)
    memory_last_error: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    memory_consecutive_failures: Mapped[int] = mapped_column(Integer, default=0)
    tool_day: Mapped[Optional[str]] = mapped_column(String(16), nullable=True)
    admin_tool_count: Mapped[int] = mapped_column(Integer, default=0)
    admin_tool_daily_limit: Mapped[int] = mapped_column(Integer, default=30)
    # 默认值为全量管理工具；空列表语义为全部禁用。
    tool_allowlist: Mapped[list[str]] = mapped_column(
        JSON, default=lambda: ["mute_member", "create_group_announcement"]
    )
    updated_at: Mapped[datetime] = mapped_column(
        server_default=func.current_timestamp(), onupdate=func.current_timestamp()
    )
