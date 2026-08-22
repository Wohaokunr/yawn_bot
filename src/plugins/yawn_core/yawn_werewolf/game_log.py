"""WebUI 可视化对局回放用的内存事件日志。

与 ``event_log.py`` 的跨玩法 JSONL 旁路日志不同：该日志面向管理员
面板的实时可视化，允许携带发言正文与 AI 决策上下文（管理员全可见），
因此只保留在进程内存中、按群一份有界环形队列，不落盘、不进 ORM。
引擎与 AI 驱动都只经本模块的函数记录，不写对方的 ``Game`` /
``AIDriver`` 状态（遵守 CLAUDE.md「AI 驱动对 Game 只读」契约）。
单进程一群一局的前提下只有一个事件循环写读，无需加锁。
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone

# 事件类型常量（前端按类型过滤时间线）
TYPE_PHASE = "phase"
TYPE_ANNOUNCE = "announce"
TYPE_DEATH = "death"
TYPE_SPEECH = "speech"
TYPE_VOTE_TALLY = "vote_tally"
TYPE_AI_DECISION = "ai_decision"
TYPE_AI_SPEECH = "ai_speech"
TYPE_SYSTEM = "system"

# 每局事件上限；超出丢弃最旧（长局中最早的公告可让位于新事件）
MAX_EVENTS = 500
# AI 决策上下文快照的截断长度，避免大上下文撑爆内存
MAX_CONTEXT_CHARS = 4000


@dataclass(slots=True)
class GameEvent:
    """一条可视化对局事件。"""

    seq: int
    ts: str
    type: str
    round_no: int | None = None
    phase: str | None = None
    seat: int | None = None
    user_id: int | None = None
    name: str | None = None
    text: str | None = None
    extra: dict = field(default_factory=dict)

    def as_dict(self) -> dict:
        return {
            "seq": self.seq,
            "ts": self.ts,
            "type": self.type,
            "roundNo": self.round_no,
            "phase": self.phase,
            "seat": self.seat,
            "userId": self.user_id,
            "name": self.name,
            "text": self.text,
            "extra": self.extra,
        }


_logs: dict[int, deque[GameEvent]] = {}
_seqs: dict[int, int] = {}


def _now_iso() -> str:
    return (
        datetime.now(timezone.utc)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )


def record(  # noqa: PLR0913
    group_id: int,
    event_type: str,
    *,
    round_no: int | None = None,
    phase: str | None = None,
    seat: int | None = None,
    user_id: int | None = None,
    name: str | None = None,
    text: str | None = None,
    extra: dict | None = None,
) -> GameEvent:
    """追加一条事件；内存操作，绝不抛出影响游戏流程。"""
    seq = _seqs.get(group_id, 0) + 1
    _seqs[group_id] = seq
    event = GameEvent(
        seq=seq,
        ts=_now_iso(),
        type=event_type,
        round_no=round_no,
        phase=str(getattr(phase, "value", phase)) if phase is not None else None,
        seat=seat,
        user_id=user_id,
        name=name,
        text=text,
        extra=extra or {},
    )
    log = _logs.get(group_id)
    if log is None:
        log = deque(maxlen=MAX_EVENTS)
        _logs[group_id] = log
    log.append(event)
    return event


def events(group_id: int, after_seq: int = 0) -> list[GameEvent]:
    """按 seq 升序返回事件；after_seq>0 时只返回更新的事件。"""
    log = _logs.get(group_id)
    if not log:
        return []
    return [event for event in log if event.seq > after_seq]


def clear(group_id: int) -> None:
    """对局清理时释放内存（state.discard_game 调用）。"""
    _logs.pop(group_id, None)
    _seqs.pop(group_id, None)


def reset_for_tests() -> None:
    """清空全部日志；仅供测试隔离使用。"""
    _logs.clear()
    _seqs.clear()
