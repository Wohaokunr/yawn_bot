"""轻量的跨玩法对局事件日志。

事件日志故意与 ORM 对局表分离：对局引擎只把小型、已筛选的 envelope
放入进程内有界队列，由单独 writer 顺序追加到 localstore 的 JSONL 文件。
因此磁盘故障、目录不可写或队列满不会阻塞或改变引擎裁决。

调用方只能写结构化枚举和标识符，不能把提示词、密钥、消息正文或玩家
user_id 放入 envelope。局后回放所需的细节由后续 P1-7 按公开/个人视角
从这些事件和既有对局数据投影，而不是把原始聊天复制进日志。
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import TYPE_CHECKING, Literal

if TYPE_CHECKING:
    from asyncio import AbstractEventLoop
    from collections.abc import Mapping

logger = logging.getLogger(__name__)

GameKind = Literal["rpg", "werewolf"]
EVENT_SCHEMA_VERSION = 1
_EVENT_QUEUE_MAX = 2048
_GAME_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")
_EVENT_TYPE_RE = re.compile(r"^[a-z][a-z0-9_.-]{1,63}$")
_SAFE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:/+-]{0,127}$")

# 事件 payload 只允许这些小型结构化字段。尤其不允许 text/message/prompt/
# secret/key/raw 等字段，避免以后新增调用点时把私密正文顺手写入日志。
_SAFE_STRING_KEYS = frozenset(
    {
        "action_kind",
        "action_result",
        "board",
        "ending_id",
        "deduction_id",
        "event_id",
        "from_scene",
        "module_id",
        "outcome",
        "persistence",
        "result",
        "scene_id",
        "to_scene",
        "termination_reason",
        "step",
        "winner",
    }
)
_SAFE_STRING_LIST_KEYS = frozenset({"clue_ids"})
_SAFE_INT_KEYS = frozenset(
    {
        "count",
        "duration_minutes",
        "phase_token",
        "player_count",
        "round",
    }
)
_SAFE_BOOL_KEYS = frozenset({"opening"})


def _call_metric(name: str, *args: object, **kwargs: object) -> None:
    """可选调用 P1-6 指标；指标故障不能影响事件旁路。"""

    try:
        from . import metrics

        getattr(metrics, name)(*args, **kwargs)
    except (ImportError, AttributeError):
        # tests may load this module directly without its package context.
        return
    except Exception:
        logger.debug("game metric update failed", exc_info=True)


def _enum_value(value: object) -> object:
    """把 Phase/Faction 等枚举转换成稳定的字符串值。"""

    return value.value if isinstance(value, Enum) else value


def _safe_identifier(value: object) -> str | None:
    value = _enum_value(value)
    if not isinstance(value, str) or not _SAFE_ID_RE.fullmatch(value):
        return None
    return value


def _safe_payload(payload: Mapping[str, object] | None) -> dict[str, object]:
    """只保留白名单字段及其可预测的标量值。"""

    if payload is None:
        return {}
    output: dict[str, object] = {}
    for key, raw_value in payload.items():
        if key in _SAFE_STRING_KEYS:
            value = _safe_identifier(raw_value)
            if value is not None:
                output[key] = value
        elif key in _SAFE_INT_KEYS:
            if isinstance(raw_value, int) and not isinstance(raw_value, bool):
                output[key] = raw_value
        elif key in _SAFE_BOOL_KEYS and isinstance(raw_value, bool):
            output[key] = raw_value
        elif key in _SAFE_STRING_LIST_KEYS and isinstance(raw_value, (list, tuple)):
            values = [value for item in raw_value if (value := _safe_identifier(item))]
            output[key] = values[:16]
    return output


def _now_utc() -> str:
    return (
        datetime.now(timezone.utc)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )


@dataclass(frozen=True, slots=True)
class EventEnvelope:
    """跨 RPG/狼人杀共用的事件 envelope。"""

    schema_version: int
    event_id: str
    game_kind: GameKind
    game_id: str
    sequence: int
    occurred_at: str
    event_type: str
    phase: str | None
    round_no: int | None
    actor_seat: int | None
    payload: dict[str, object]

    def as_dict(self) -> dict[str, object]:
        """返回可直接 JSON 编码的稳定字段。"""

        return {
            "schema_version": self.schema_version,
            "event_id": self.event_id,
            "game_kind": self.game_kind,
            "game_id": self.game_id,
            "sequence": self.sequence,
            "occurred_at": self.occurred_at,
            "event_type": self.event_type,
            "phase": self.phase,
            "round": self.round_no,
            "actor_seat": self.actor_seat,
            "payload": dict(self.payload),
        }


@dataclass(frozen=True, slots=True)
class _PendingWrite:
    path: Path
    line: str
    game_kind: GameKind


def get_event_log_dir() -> Path:
    """返回运行时事件目录；默认落在插件专用 localstore 中。"""

    try:
        from nonebot_plugin_localstore import get_plugin_data_dir

        return Path(get_plugin_data_dir()) / "game-events"
    except Exception:  # noqa: BLE001
        # 单元测试或极早期启动阶段可能尚未注册 localstore；仍让事件
        # 记录路径可预测，并把失败交给 writer 的非阻塞错误处理。
        return Path("data") / "yawn_core" / "game-events"


def event_log_path(
    game_kind: GameKind,
    game_id: str,
    *,
    root: Path | None = None,
) -> Path:
    """返回某局 JSONL 路径；game_id 经过校验后才参与拼接。"""

    if game_kind not in {"rpg", "werewolf"}:
        raise ValueError
    if _GAME_ID_RE.fullmatch(game_id) is None:
        raise ValueError
    directory = Path(root) if root is not None else get_event_log_dir()
    return directory / f"{game_kind}-{game_id}.jsonl"


def _append_line(path: Path, line: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8", newline="\n") as stream:
        stream.write(line)


class _EventWriter:
    """每个事件循环一个顺序 writer。"""

    def __init__(self) -> None:
        self.queue: asyncio.Queue[_PendingWrite] = asyncio.Queue(
            maxsize=_EVENT_QUEUE_MAX
        )
        self.task = asyncio.create_task(self._run())

    def submit(self, item: _PendingWrite) -> bool:
        try:
            self.queue.put_nowait(item)
        except asyncio.QueueFull:
            logger.warning("game event log queue is full; event was dropped")
            return False
        return True

    async def _run(self) -> None:
        while True:
            item = await self.queue.get()
            try:
                await asyncio.to_thread(_append_line, item.path, item.line)
            except Exception:
                # 事件日志是旁路观测，不得把 I/O 异常传播回游戏引擎。
                _call_metric("record_event_log_write_failure", item.game_kind)
                logger.warning("game event log write failed", exc_info=True)
            finally:
                self.queue.task_done()

    async def drain(self) -> None:
        await self.queue.join()


_WRITERS: dict[AbstractEventLoop, _EventWriter] = {}
_SEQUENCES: dict[tuple[GameKind, str], int] = {}
# 只用于把 phase_changed 事件转换成阶段耗时；不导出，也不作为标签。
_PHASES: dict[tuple[GameKind, str], str] = {}


def _writer_for_current_loop() -> _EventWriter | None:
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return None
    writer = _WRITERS.get(loop)
    if writer is None or writer.task.done():
        writer = _EventWriter()
        _WRITERS[loop] = writer
    return writer


def record_event(  # noqa: C901, PLR0912, PLR0913
    game_kind: GameKind,
    game_id: str,
    event_type: str,
    *,
    phase: object = None,
    round_no: int | None = None,
    actor_seat: int | None = None,
    payload: Mapping[str, object] | None = None,
    root: Path | None = None,
) -> EventEnvelope | None:
    """记录一个结构化事件，并立即返回，不等待磁盘 I/O。"""

    if game_kind not in {"rpg", "werewolf"}:
        logger.warning("game event has unknown kind; event was dropped")
        return None
    if _GAME_ID_RE.fullmatch(game_id) is None:
        logger.warning("game event has invalid game id; event was dropped")
        return None
    if _EVENT_TYPE_RE.fullmatch(event_type) is None:
        logger.warning("game event has invalid event type; event was dropped")
        return None
    phase_value = _safe_identifier(phase) if phase is not None else None
    if phase is not None and phase_value is None:
        phase_value = None
    if round_no is not None and (
        not isinstance(round_no, int) or isinstance(round_no, bool)
    ):
        round_no = None
    if actor_seat is not None and (
        not isinstance(actor_seat, int)
        or isinstance(actor_seat, bool)
        or actor_seat <= 0
    ):
        actor_seat = None

    sequence_key = (game_kind, game_id)
    sequence = _SEQUENCES.get(sequence_key, 0) + 1
    _SEQUENCES[sequence_key] = sequence
    envelope = EventEnvelope(
        schema_version=EVENT_SCHEMA_VERSION,
        event_id=uuid.uuid4().hex,
        game_kind=game_kind,
        game_id=game_id,
        sequence=sequence,
        occurred_at=_now_utc(),
        event_type=event_type,
        phase=phase_value,
        round_no=round_no,
        actor_seat=actor_seat,
        payload=_safe_payload(payload),
    )
    line = (
        json.dumps(
            envelope.as_dict(),
            ensure_ascii=False,
            separators=(",", ":"),
        )
        + "\n"
    )
    try:
        path = event_log_path(game_kind, game_id, root=root)
    except ValueError:
        return None

    if event_type == "game_created":
        initial_phase = phase_value or "UNKNOWN"
        _PHASES[sequence_key] = initial_phase
        _call_metric(
            "start_game_phase",
            game_kind,
            game_id,
            initial_phase,
        )
    elif event_type == "phase_changed" and phase_value is not None:
        previous_phase = _PHASES.get(sequence_key)
        if previous_phase is not None:
            _call_metric(
                "record_phase_change",
                game_kind,
                game_id,
                previous_phase,
                phase_value,
            )
        else:
            _call_metric(
                "start_game_phase",
                game_kind,
                game_id,
                phase_value,
            )
        if phase_value == "ENDED":
            _PHASES.pop(sequence_key, None)
        else:
            _PHASES[sequence_key] = phase_value
    elif event_type == "game_ended":
        _call_metric(
            "record_game_ending",
            game_kind,
            outcome=envelope.payload.get("outcome"),
            ending=envelope.payload.get("ending_id"),
            winner=envelope.payload.get("winner"),
        )

    writer = _writer_for_current_loop()
    if writer is not None:
        accepted = writer.submit(_PendingWrite(path, line, game_kind))
        if not accepted:
            _call_metric(
                "record_queue_rejection",
                "event_log_writer",
                game_kind,
                "queue_full",
            )
        return envelope
    try:
        _append_line(path, line)
    except Exception:
        _call_metric("record_event_log_write_failure", game_kind)
        logger.warning("game event log write failed", exc_info=True)
    return envelope


def record_game_event(  # noqa: PLR0913
    game: object,
    game_kind: GameKind,
    event_type: str,
    *,
    phase: object = None,
    round_no: int | None = None,
    actor_seat: int | None = None,
    payload: Mapping[str, object] | None = None,
    root: Path | None = None,
) -> EventEnvelope | None:
    """按内存 Game 的稳定事件 id 记录事件，不读取玩家私密字段。"""

    game_id = getattr(game, "event_log_id", None)
    if not isinstance(game_id, str):
        logger.warning("game event has no stable game id; event was dropped")
        return None
    return record_event(
        game_kind,
        game_id,
        event_type,
        phase=phase,
        round_no=round_no,
        actor_seat=actor_seat,
        payload=payload,
        root=root,
    )


async def flush_events() -> None:
    """等待当前事件循环已经提交的事件写入完成；供测试和优雅停机使用。"""

    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return
    writer = _WRITERS.get(loop)
    if writer is not None:
        await writer.drain()


def export_events(  # noqa: C901, PLR0912
    game_id: str,
    *,
    game_kind: GameKind | None = None,
    root: Path | None = None,
) -> list[dict[str, object]]:
    """按 game id 导出有序事件；损坏行被跳过并记录诊断。"""

    if _GAME_ID_RE.fullmatch(game_id) is None:
        return []
    directory = Path(root) if root is not None else get_event_log_dir()
    if game_kind is None:
        paths = sorted(directory.glob(f"*-{game_id}.jsonl"))
    else:
        try:
            paths = [event_log_path(game_kind, game_id, root=directory)]
        except ValueError:
            return []
    events: list[dict[str, object]] = []
    for path in paths:
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except FileNotFoundError:
            continue
        except OSError:
            logger.warning("game event log export failed", exc_info=True)
            continue
        for line in lines:
            try:
                value = json.loads(line)
            except (TypeError, ValueError):
                logger.warning("invalid game event log line was skipped")
                continue
            if not isinstance(value, dict):
                continue
            if value.get("game_id") != game_id:
                continue
            if game_kind is not None and value.get("game_kind") != game_kind:
                continue
            if not isinstance(value.get("sequence"), int):
                continue
            events.append(value)
    events.sort(
        key=lambda item: (
            item["sequence"] if isinstance(item.get("sequence"), int) else 0,
            str(item.get("event_id")),
        )
    )
    return events


def export_events_jsonl(
    game_id: str,
    *,
    game_kind: GameKind | None = None,
    root: Path | None = None,
) -> str:
    """以稳定 JSONL 文本导出一局事件，便于命令/API 层继续投影。"""

    return "".join(
        json.dumps(event, ensure_ascii=False, separators=(",", ":")) + "\n"
        for event in export_events(game_id, game_kind=game_kind, root=root)
    )


def reset_event_log_state_for_tests() -> None:
    """清理序号账本；仅供测试隔离使用。"""

    _SEQUENCES.clear()
    _PHASES.clear()


__all__ = [
    "EVENT_SCHEMA_VERSION",
    "EventEnvelope",
    "event_log_path",
    "export_events",
    "export_events_jsonl",
    "flush_events",
    "get_event_log_dir",
    "record_event",
    "record_game_event",
    "reset_event_log_state_for_tests",
]
