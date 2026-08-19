"""只读的 P1-7 对局回放投影。

回放只读取 P1-5 的结构化 JSONL 事件，不读取 ORM 中的角色卡、聊天正文或
个人线索。公开视角只显示当时已经可以公开的事件；个人视角在此基础上增加
指定座位自己的结构化行动，因此即使日志文件被复制给投影层，也不会把夜间
行动或事件内部标识直接渲染到群聊。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

from .event_log import GameKind, export_events

if TYPE_CHECKING:
    from pathlib import Path

ReplayView = Literal["public", "personal"]

_KNOWN_EVENT_TYPES = frozenset(
    {
        "game_created",
        "game_started",
        "phase_changed",
        "scene_entered",
        "plot_event_triggered",
        "action_received",
        "game_ended",
        "game_interrupted",
        "deduction_proposed",
        "deduction_confirmed",
        "deduction_succeeded",
        "deduction_failed",
        "deduction_withdrawn",
        "tutorial_started",
        "tutorial_step_shown",
        "tutorial_completed",
        "tutorial_skipped",
    }
)
_RPG_ACTION_LABELS = {
    "say": "发言",
    "check": "检定",
    "talk_npc": "与 NPC 互动",
    "share_fact": "分享 NPC 情报",
    "share_clue": "分享线索",
    "attack": "攻击",
    "move": "移动",
    "wait": "等待",
    "assist": "协助",
    "pass_turn": "结束行动",
    "start_game": "请求开局",
    "module_select": "选择模组",
    "join_game": "报名",
    "leave_game": "退报名",
    "transfer_host": "移交房主",
    "reroll": "重掷角色卡",
    "add_skill": "调整技能",
    "sub_skill": "调整技能",
    "reset_skills": "重置技能",
    "show_card": "查看角色卡",
    "confirm_card": "确认角色卡",
    "propose_deduction": "发起推理",
    "confirm_deduction": "确认推理",
    "withdraw_deduction": "撤回推理",
}
_RPG_PRIVATE_ACTIONS = frozenset(
    {
        "reroll",
        "add_skill",
        "sub_skill",
        "reset_skills",
        "show_card",
        "confirm_card",
    }
)
_WEREWOLF_ACTION_LABELS = {
    "run": "上警",
    "withdraw": "退水",
    "order": "安排发言序",
    "pass_badge": "移交警徽",
    "tear_badge": "撕警徽",
    "self_detonate": "自爆",
    "duel": "决斗",
    "vote": "投票",
    "abstain": "弃票",
    "skip": "跳过",
    "say": "狼队发言",
    "kill": "夜间行动",
    "save": "夜间行动",
    "poison": "夜间行动",
    "check": "夜间行动",
    "shoot": "夜间行动",
    "no_shoot": "夜间行动",
    "choose_owner": "夜间行动",
    "silence": "夜间行动",
}
_WEREWOLF_NIGHT_PHASES = frozenset(
    {
        "NIGHT_HALFBLOOD",
        "NIGHT_WOLVES",
        "NIGHT_WITCH",
        "NIGHT_SEER",
        "NIGHT_ELDER",
    }
)
_WEREWOLF_PUBLIC_ACTIONS = frozenset(
    {
        "run",
        "withdraw",
        "order",
        "pass_badge",
        "tear_badge",
        "self_detonate",
        "duel",
        "vote",
        "abstain",
    }
)
_PHASE_LABELS = {
    "SIGNUP": "报名",
    "CHAR_CREATE": "建卡",
    "PLAY": "进行中",
    "DEALING": "发牌",
    "DAY_ANNOUNCE": "天亮结算",
    "LAST_WORDS": "遗言",
    "HUNTER_SHOT": "猎人开枪",
    "BADGE_TRANSFER": "警徽移交",
    "SHERIFF_REGISTER": "警长竞选报名",
    "SHERIFF_SPEECH": "竞选发言",
    "SHERIFF_VOTE": "警长投票",
    "SHERIFF_FINAL_SPEECH": "警长终辩",
    "SHERIFF_REVOTE": "警长重投",
    "DAY_SPEECH": "白天发言",
    "DAY_VOTE": "放逐投票",
    "PK_SPEECH": "PK 发言",
    "PK_VOTE": "PK 投票",
    "ENDED": "已结束",
}
_MAX_REPLAY_ACCESS_RECORDS = 256
_REPLAY_ACCESS: dict[tuple[GameKind, str], dict[int, int]] = {}


@dataclass(frozen=True, slots=True)
class ReplayEvent:
    """已经按视角过滤并适合渲染的一条回放事件。"""

    sequence: int
    occurred_at: str
    event_type: str
    phase: str | None
    round_no: int | None
    actor_seat: int | None
    detail: str
    audience: Literal["public", "personal"]

    def as_dict(self) -> dict[str, object]:
        """返回稳定的只读 API 结构。"""

        return {
            "sequence": self.sequence,
            "occurred_at": self.occurred_at,
            "event_type": self.event_type,
            "phase": self.phase,
            "round": self.round_no,
            "actor_seat": self.actor_seat,
            "detail": self.detail,
            "audience": self.audience,
        }


@dataclass(frozen=True, slots=True)
class ReplayProjection:
    """一局回放的投影结果；不可用时保留明确原因。"""

    game_id: str
    game_kind: GameKind | None
    view: ReplayView
    viewer_seat: int | None
    available: bool
    reason: str | None
    title: str
    started_at: str | None
    ended_at: str | None
    summary: dict[str, object]
    events: tuple[ReplayEvent, ...]
    warnings: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, object]:
        """返回给 Web/API 适配器使用的 JSON 兼容字典。"""

        return {
            "game_id": self.game_id,
            "game_kind": self.game_kind,
            "view": self.view,
            "viewer_seat": self.viewer_seat,
            "available": self.available,
            "reason": self.reason,
            "title": self.title,
            "started_at": self.started_at,
            "ended_at": self.ended_at,
            "summary": dict(self.summary),
            "events": [event.as_dict() for event in self.events],
            "warnings": list(self.warnings),
        }


def register_replay_participants(
    game_id: str,
    game_kind: GameKind,
    participants: Mapping[int, int],
) -> None:
    """在进程内登记座位访问映射；不写入事件日志。"""

    if not game_id or game_kind not in {"rpg", "werewolf"}:
        return
    access = {
        user_id: seat
        for user_id, seat in participants.items()
        if isinstance(user_id, int)
        and not isinstance(user_id, bool)
        and user_id > 0
        and isinstance(seat, int)
        and not isinstance(seat, bool)
        and seat > 0
    }
    key = (game_kind, game_id)
    if key not in _REPLAY_ACCESS and len(_REPLAY_ACCESS) >= _MAX_REPLAY_ACCESS_RECORDS:
        _REPLAY_ACCESS.pop(next(iter(_REPLAY_ACCESS)))
    _REPLAY_ACCESS[key] = access


def replay_viewer_seat(
    game_id: str,
    user_id: int,
    *,
    game_kind: GameKind | None = None,
) -> int | None:
    """按当前进程登记的参与者映射解析个人回放座位。"""

    if not isinstance(user_id, int) or isinstance(user_id, bool) or user_id <= 0:
        return None
    kinds = (game_kind,) if game_kind is not None else ("rpg", "werewolf")
    matches = [
        access[user_id]
        for kind in kinds
        if (access := _REPLAY_ACCESS.get((kind, game_id))) is not None
        and user_id in access
    ]
    return matches[0] if len(matches) == 1 else None


def reset_replay_access_for_tests() -> None:
    """清理进程内访问映射；仅供测试隔离使用。"""

    _REPLAY_ACCESS.clear()


def _unavailable(  # noqa: PLR0913
    game_id: str,
    *,
    game_kind: GameKind | None,
    view: ReplayView,
    viewer_seat: int | None,
    reason: str,
    warnings: Sequence[str] = (),
) -> ReplayProjection:
    label = (
        "RPG"
        if game_kind == "rpg"
        else "狼人杀"
        if game_kind == "werewolf"
        else "对局"
    )
    return ReplayProjection(
        game_id=game_id,
        game_kind=game_kind,
        view=view,
        viewer_seat=viewer_seat,
        available=False,
        reason=reason,
        title=f"{label}·局后回放",
        started_at=None,
        ended_at=None,
        summary={},
        events=(),
        warnings=tuple(warnings),
    )


def _string(value: object, *, max_length: int = 128) -> str | None:
    if not isinstance(value, str) or not value or len(value) > max_length:
        return None
    return value


def _positive_int(value: object) -> int | None:
    if isinstance(value, int) and not isinstance(value, bool) and value > 0:
        return value
    return None


def _nonnegative_int(value: object) -> int | None:
    if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
        return value
    return None


def _payload(event: Mapping[str, object]) -> Mapping[str, object]:
    value = event.get("payload")
    return value if isinstance(value, Mapping) else {}


def _raw_phase(event: Mapping[str, object]) -> str | None:
    return _string(event.get("phase"), max_length=64)


def _public_phase(game_kind: GameKind, phase: str | None) -> str | None:
    if phase is None:
        return None
    if game_kind == "werewolf" and phase in _WEREWOLF_NIGHT_PHASES:
        return "NIGHT"
    return phase


def _phase_detail(game_kind: GameKind, phase: str | None) -> str:
    if phase is None:
        return "阶段变化"
    if game_kind == "werewolf" and phase == "NIGHT":
        return "阶段：夜间"
    return f"阶段：{_PHASE_LABELS.get(phase, phase)}"


def _action_label(game_kind: GameKind, action_kind: str | None) -> str:
    if action_kind is None:
        return "结构化行动"
    labels = _RPG_ACTION_LABELS if game_kind == "rpg" else _WEREWOLF_ACTION_LABELS
    return labels.get(action_kind, "结构化行动")


def _action_audience(  # noqa: PLR0913, PLR0917
    game_kind: GameKind,
    phase: str | None,
    action_kind: str | None,
    actor_seat: int | None,
    view: ReplayView,
    viewer_seat: int | None,
) -> Literal["public", "personal"] | None:
    if game_kind == "rpg":
        if action_kind in _RPG_PRIVATE_ACTIONS:
            if view == "personal" and actor_seat == viewer_seat:
                return "personal"
            return None
        return "public"
    is_private = (
        phase in _WEREWOLF_NIGHT_PHASES
        or action_kind not in _WEREWOLF_PUBLIC_ACTIONS
    )
    if not is_private:
        return "public"
    if view == "personal" and actor_seat == viewer_seat:
        return "personal"
    return None


def _action_detail(
    game_kind: GameKind,
    action_kind: str | None,
    actor_seat: int | None,
    audience: Literal["public", "personal"],
    viewer_seat: int | None,
) -> str:
    label = _action_label(game_kind, action_kind)
    if audience == "personal" and actor_seat == viewer_seat:
        return f"你的行动：{label}"
    if actor_seat is None:
        return f"结构化行动：{label}"
    return f"{actor_seat}号行动：{label}"


def _event_detail(  # noqa: C901, PLR0911, PLR0912, PLR0913, PLR0917
    game_kind: GameKind,
    event_type: str,
    event: Mapping[str, object],
    phase: str | None,
    audience: Literal["public", "personal"],
    viewer_seat: int | None,
) -> str | None:
    payload = _payload(event)
    actor_seat = _positive_int(event.get("actor_seat"))
    if event_type == "game_created":
        count = _positive_int(payload.get("player_count"))
        return f"对局创建（报名人数：{count}）" if count is not None else "对局创建"
    if event_type == "game_started":
        if game_kind == "rpg":
            module_id = _string(payload.get("module_id"))
            return f"开始：模组 {module_id}" if module_id else "对局开始"
        board = _string(payload.get("board"))
        return f"开始：板子 {board}" if board else "对局开始"
    if event_type == "phase_changed":
        return _phase_detail(game_kind, phase)
    if event_type == "scene_entered" and game_kind == "rpg":
        scene_id = _string(payload.get("scene_id"))
        return f"进入场景：{scene_id}" if scene_id else "进入场景"
    if event_type == "plot_event_triggered" and game_kind == "rpg":
        return "触发具名事件"
    if event_type == "action_received":
        action_kind = _string(payload.get("action_kind"), max_length=64)
        return _action_detail(
            game_kind,
            action_kind,
            actor_seat,
            audience,
            viewer_seat,
        )
    if event_type == "game_ended":
        if game_kind == "rpg":
            ending_id = _string(payload.get("ending_id"))
            outcome = _string(payload.get("outcome"), max_length=32)
            if ending_id and outcome:
                return f"结局：{ending_id}（{outcome}）"
            return f"结局：{ending_id}" if ending_id else "对局结束"
        winner = _string(payload.get("winner"), max_length=32)
        return f"结局：{winner}阵营获胜" if winner else "对局结束"
    if event_type == "game_interrupted":
        reason = _string(payload.get("termination_reason"), max_length=32)
        return f"对局中断：{reason}" if reason else "对局中断"
    if event_type.startswith("deduction_") and game_kind == "rpg":
        labels = {
            "deduction_proposed": "发起联合推理",
            "deduction_confirmed": "确认联合推理",
            "deduction_succeeded": "联合推理成立",
            "deduction_failed": "联合推理未成立",
            "deduction_withdrawn": "撤回联合推理",
        }
        deduction_id = _string(payload.get("deduction_id"))
        detail = labels[event_type]
        return f"{detail}：{deduction_id}" if deduction_id else detail
    if event_type.startswith("tutorial_") and game_kind == "rpg":
        # 引导事件仅供运营统计；回放不展示个人引导进度。
        return None
    return None


def project_events(  # noqa: C901, PLR0911, PLR0912, PLR0915
    game_id: str,
    events: Sequence[Mapping[str, object]],
    *,
    game_kind: GameKind | None = None,
    view: ReplayView = "public",
    viewer_seat: int | None = None,
) -> ReplayProjection:
    """从事件序列重建公开或个人视角的回放。"""

    if view not in {"public", "personal"}:
        return _unavailable(
            game_id,
            game_kind=game_kind,
            view="public",
            viewer_seat=None,
            reason="回放视角无效，本局不可回放",
        )
    if view == "personal" and _positive_int(viewer_seat) is None:
        return _unavailable(
            game_id,
            game_kind=game_kind,
            view=view,
            viewer_seat=viewer_seat,
            reason="个人回放需要有效座位号，本局不可回放",
        )

    normalized: list[Mapping[str, object]] = []
    warnings: list[str] = []
    sequences: list[int] = []
    kinds: set[str] = set()
    unknown_types: set[str] = set()
    for event in events:
        if not isinstance(event, Mapping):
            continue
        sequence = _positive_int(event.get("sequence"))
        event_type = _string(event.get("event_type"), max_length=64)
        kind = _string(event.get("game_kind"), max_length=16)
        if sequence is None or event_type is None or kind not in {"rpg", "werewolf"}:
            continue
        if event.get("game_id") != game_id:
            continue
        normalized.append(event)
        sequences.append(sequence)
        kinds.add(kind)
        if event_type not in _KNOWN_EVENT_TYPES:
            unknown_types.add(event_type)

    if not normalized:
        return _unavailable(
            game_id,
            game_kind=game_kind,
            view=view,
            viewer_seat=viewer_seat,
            reason="未找到事件日志，本局不可回放",
        )
    if game_kind is not None and kinds != {game_kind}:
        return _unavailable(
            game_id,
            game_kind=game_kind,
            view=view,
            viewer_seat=viewer_seat,
            reason="事件日志玩法类型不一致，本局不可回放",
        )
    if len(kinds) != 1:
        return _unavailable(
            game_id,
            game_kind=None,
            view=view,
            viewer_seat=viewer_seat,
            reason="事件日志包含多个玩法类型，本局不可回放",
        )
    kind_value = next(iter(kinds))
    actual_kind: GameKind = "rpg" if kind_value == "rpg" else "werewolf"
    normalized.sort(key=lambda item: (_positive_int(item.get("sequence")) or 0))
    if sequences:
        expected = list(range(min(sequences), max(sequences) + 1))
        if sorted(set(sequences)) != expected:
            warnings.append("事件序列不连续，回放可能不完整")
    if unknown_types:
        warnings.append("日志包含未识别事件，已从回放中隐藏")

    created = False
    ended = False
    started_at: str | None = None
    ended_at: str | None = None
    summary: dict[str, object] = {}
    replay_events: list[ReplayEvent] = []
    last_public_phase: str | None = None

    for event in normalized:
        event_type = _string(event.get("event_type"), max_length=64)
        if event_type is None:
            continue
        phase = _public_phase(actual_kind, _raw_phase(event))
        sequence = _positive_int(event.get("sequence"))
        if sequence is None:
            continue
        if event_type == "game_created":
            created = True
        elif event_type in {"game_ended", "game_interrupted"}:
            ended = True
            ended_at = _string(event.get("occurred_at"), max_length=64)
        elif event_type == "game_started" and started_at is None:
            started_at = _string(event.get("occurred_at"), max_length=64)

        payload = _payload(event)
        if event_type == "game_started":
            if actual_kind == "rpg" and _string(payload.get("module_id")):
                summary["module_id"] = _string(payload.get("module_id"))
            if actual_kind == "werewolf" and _string(payload.get("board")):
                summary["board"] = _string(payload.get("board"))
        elif event_type == "scene_entered" and actual_kind == "rpg":
            scene_id = _string(payload.get("scene_id"))
            if scene_id:
                summary["last_scene"] = scene_id
        elif event_type == "game_ended":
            for key in ("ending_id", "outcome", "winner"):
                value = _string(payload.get(key), max_length=64)
                if value:
                    summary[key] = value
        elif event_type == "game_interrupted":
            reason = _string(payload.get("termination_reason"), max_length=32)
            if reason:
                summary["termination_reason"] = reason

        if event_type not in _KNOWN_EVENT_TYPES:
            continue
        actor_seat = _positive_int(event.get("actor_seat"))
        audience: Literal["public", "personal"] | None = "public"
        if event_type == "action_received":
            action_kind = _string(_payload(event).get("action_kind"), max_length=64)
            audience = _action_audience(
                actual_kind,
                _raw_phase(event),
                action_kind,
                actor_seat,
                view,
                viewer_seat,
            )
        if audience is None:
            continue
        if event_type == "phase_changed":
            if phase == last_public_phase:
                continue
            last_public_phase = phase
        detail = _event_detail(
            actual_kind,
            event_type,
            event,
            phase,
            audience,
            viewer_seat,
        )
        if detail is None:
            continue
        replay_events.append(
            ReplayEvent(
                sequence=sequence,
                occurred_at=_string(event.get("occurred_at"), max_length=64) or "",
                event_type=event_type,
                phase=phase,
                round_no=_nonnegative_int(event.get("round")),
                actor_seat=actor_seat,
                detail=detail,
                audience=audience,
            )
        )

    if not created:
        return _unavailable(
            game_id,
            game_kind=actual_kind,
            view=view,
            viewer_seat=viewer_seat,
            reason="事件日志缺少创建事件，本局不可回放",
            warnings=warnings,
        )
    if not ended:
        return _unavailable(
            game_id,
            game_kind=actual_kind,
            view=view,
            viewer_seat=viewer_seat,
            reason="事件日志缺少终局事件，本局不可回放",
            warnings=warnings,
        )
    label = "RPG" if actual_kind == "rpg" else "狼人杀"
    return ReplayProjection(
        game_id=game_id,
        game_kind=actual_kind,
        view=view,
        viewer_seat=viewer_seat,
        available=True,
        reason=None,
        title=f"{label}·局后回放",
        started_at=started_at,
        ended_at=ended_at,
        summary=summary,
        events=tuple(replay_events),
        warnings=tuple(warnings),
    )


def load_replay(
    game_id: str,
    *,
    game_kind: GameKind | None = None,
    view: ReplayView = "public",
    viewer_seat: int | None = None,
    root: Path | None = None,
) -> ReplayProjection:
    """从 JSONL 事件日志加载并投影一局回放。"""

    events = export_events(game_id, game_kind=game_kind, root=root)
    return project_events(
        game_id,
        events,
        game_kind=game_kind,
        view=view,
        viewer_seat=viewer_seat,
    )


def render_replay(projection: ReplayProjection, *, max_events: int = 120) -> str:
    """渲染给 OneBot 群/私聊的短文本；API 应优先使用 ``as_dict``。"""

    if not projection.available:
        return f"回放不可用：{projection.reason or '事件日志不完整'}"
    view_label = (
        "公开视角"
        if projection.view == "public"
        else f"个人视角（{projection.viewer_seat}号）"
    )
    lines = [
        f"═══ {projection.title} · {view_label} ═══",
        f"编号：{projection.game_id}",
    ]
    for key, label in (
        ("module_id", "模组"),
        ("board", "板子"),
        ("outcome", "结果"),
        ("winner", "获胜阵营"),
        ("ending_id", "结局"),
    ):
        value = projection.summary.get(key)
        if isinstance(value, str):
            lines.append(f"{label}：{value}")
    lines.append("─── 时间线 ───")
    visible_events = projection.events[: max(max_events, 1)]
    for event in visible_events:
        round_text = f" · 第{event.round_no}回合" if event.round_no is not None else ""
        lines.append(f"#{event.sequence}{round_text} {event.detail}")
    if len(projection.events) > len(visible_events):
        lines.append(
            f"（其余 {len(projection.events) - len(visible_events)} 条事件"
            "请通过 API 读取）"
        )
    lines.extend(f"提示：{warning}" for warning in projection.warnings)
    return "\n".join(lines)


__all__ = [
    "ReplayEvent",
    "ReplayProjection",
    "ReplayView",
    "load_replay",
    "project_events",
    "register_replay_participants",
    "render_replay",
    "replay_viewer_seat",
    "reset_replay_access_for_tests",
]
