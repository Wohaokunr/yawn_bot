"""Bounded state-space search for deterministic RPG module playtests.

The simulator intentionally depends only on the runtime module schema. It does
not import the online engine, initialize NoneBot, touch ORM state, or call an
LLM. Its local ``random.Random`` instance mirrors the online dice rules while
remaining isolated from the process-global random source.
"""

# Transition logic stays together so this standalone model can be audited
# against the module schema without importing the online engine.
# ruff: noqa: C901,PLR0911,PLR0912,PLR0913,PLR0915,PLR0917,PLR2004,ARG001,E501

from __future__ import annotations

import copy
import math
import random
import re
from collections import deque
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Literal, Optional

import yaml

from tools.rpg_module_editor.schema_loader import (
    SKILLS,
    CheckDifficulty,
    CheckMode,
    ConditionContext,
    ModuleDef,
    evaluate_condition,
)

DEFAULT_MAX_DEPTH = 40
DEFAULT_MAX_STATES = 50_000
DEFAULT_WAIT_MAX = 120

_DEFAULT_TIME_COSTS = {
    "check": 10,
    "move": 10,
    "talk": 10,
    "attack": 5,
}

# Compatibility mirror of engine._GENERIC_ENDINGS. Tests lock id, condition,
# outcome, and order so an engine change cannot silently drift from this tool.
GENERIC_ENDINGS: tuple[tuple[str, str, str], ...] = (
    ("generic_arson_egg", "flag:arson>=4", "neutral"),
    ("generic_fire", "flag:arson>=2", "bad"),
    ("generic_arrest", "flag:murder", "bad"),
    ("generic_subdued", "flag:assault>=3", "bad"),
    ("generic_tpk", "all_players_incapped", "bad"),
)

Reason = Literal[
    "success",
    "invalid_module",
    "unknown_ending",
    "invalid_players",
    "no_path",
    "limit_exceeded",
]


@dataclass(frozen=True)
class SearchConfig:
    """Search controls exposed by the CLI."""

    seed: int
    ending_id: str
    players: Optional[int] = None
    max_depth: int = DEFAULT_MAX_DEPTH
    max_states: int = DEFAULT_MAX_STATES


@dataclass
class Player:
    """Minimal character state required by deterministic rules."""

    seat: int
    name: str
    attributes: dict[str, int]
    skills: dict[str, int]
    hp: int
    san: int
    incapped: bool = False

    def public_dict(self) -> dict[str, Any]:
        return {
            "seat": self.seat,
            "name": self.name,
            "attributes": dict(sorted(self.attributes.items())),
            "skills": dict(sorted(self.skills.items())),
            "hp": self.hp,
            "san": self.san,
            "incapped": self.incapped,
        }


@dataclass
class TraceStep:
    """One stable, serializable transition in a successful trace."""

    index: int
    action: str
    actor: Optional[str]
    target: Optional[str]
    scene_before: str
    scene_after: str
    elapsed_before: int
    elapsed_after: int
    rolls: list[dict[str, Any]] = field(default_factory=list)
    clues_added: list[str] = field(default_factory=list)
    flags_changed: dict[str, int] = field(default_factory=dict)
    detail: str = ""


@dataclass
class SearchResult:
    """Stable result envelope shared by text and JSON output."""

    ok: bool
    reason: Reason
    message: str
    module_id: str = ""
    module_name: str = ""
    seed: int = 0
    target_ending: str = ""
    final_ending: Optional[dict[str, str]] = None
    final_scene: Optional[str] = None
    elapsed_minutes: int = 0
    clues: list[str] = field(default_factory=list)
    flags: dict[str, int] = field(default_factory=dict)
    events: list[str] = field(default_factory=list)
    players: list[dict[str, Any]] = field(default_factory=list)
    final_players: list[dict[str, Any]] = field(default_factory=list)
    steps: list[dict[str, Any]] = field(default_factory=list)
    explored_states: int = 0
    generated_states: int = 0
    max_depth: int = DEFAULT_MAX_DEPTH
    max_states: int = DEFAULT_MAX_STATES

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class _Action:
    kind: str
    actor: int = 0
    target: str = ""
    aux: str = ""
    value: int = 0
    skill: str = ""


@dataclass
class _State:
    scene: str
    clock_start: int
    elapsed: int
    players: list[Player]
    rng: random.Random
    clues: set[str] = field(default_factory=set)
    public_clues: set[str] = field(default_factory=set)
    clue_owners: dict[str, set[int]] = field(default_factory=dict)
    fired_checks: set[str] = field(default_factory=set)
    passed_checks: set[str] = field(default_factory=set)
    flags: dict[str, int] = field(default_factory=dict)
    occurred_events: set[str] = field(default_factory=set)
    dead_monsters: set[str] = field(default_factory=set)
    monster_hp: dict[str, int] = field(default_factory=dict)
    dead_npcs: set[str] = field(default_factory=set)
    npc_hp: dict[str, int] = field(default_factory=dict)
    npc_hostile: set[str] = field(default_factory=set)
    npc_rapport: dict[tuple[str, int], int] = field(default_factory=dict)
    npc_attitude: dict[str, int] = field(default_factory=dict)
    npc_attempts: dict[tuple[str, int, str], int] = field(default_factory=dict)
    npc_rewards: set[tuple[str, int, str, bool]] = field(default_factory=set)
    npc_facts: dict[tuple[str, int], set[str]] = field(default_factory=dict)
    npc_public_facts: dict[str, set[str]] = field(default_factory=dict)
    combat_order: list[int] = field(default_factory=list)
    combat_index: int = 0
    combat_round: int = 0
    move_counts: dict[tuple[str, str], int] = field(default_factory=dict)
    trace: list[TraceStep] = field(default_factory=list)

    def clone(self) -> _State:
        cloned = copy.deepcopy(self)
        cloned.rng = random.Random()
        cloned.rng.setstate(self.rng.getstate())
        return cloned

    def context(self) -> Any:
        return ConditionContext(
            clues=set(self.clues),
            dead_monsters=set(self.dead_monsters),
            current_scene=self.scene,
            all_incapped=all(player.incapped for player in self.players),
            clock_start_minutes=self.clock_start,
            elapsed_minutes=self.elapsed,
            flags=dict(self.flags),
        )


@dataclass(frozen=True)
class _Relevant:
    clues: frozenset[str]
    flags: frozenset[str]
    monsters: frozenset[str]
    facts: frozenset[tuple[str, str]]
    social_nodes: frozenset[tuple[str, str]]
    has_time: bool
    all_players_incapped: bool


def load_module(path: Path | str) -> Any:
    """Load one YAML file through the authoritative runtime schema."""
    raw = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    return ModuleDef.model_validate(raw)


def _roll_attributes(rng: random.Random) -> dict[str, int]:
    attrs: dict[str, int] = {}
    for key in ("str", "con", "dex", "app", "pow", "luck"):
        attrs[key] = sum(rng.randint(1, 6) for _ in range(3)) * 5
    for key in ("siz", "int", "edu"):
        attrs[key] = (sum(rng.randint(1, 6) for _ in range(2)) + 6) * 5
    return attrs


def _skill_values(attributes: dict[str, int]) -> dict[str, int]:
    return {
        skill.key: skill.base if skill.base >= 0 else attributes["dex"] // 2
        for skill in SKILLS
    }


def _build_players(count: int, rng: random.Random) -> list[Player]:
    players: list[Player] = []
    for seat in range(1, count + 1):
        attrs = _roll_attributes(rng)
        players.append(
            Player(
                seat=seat,
                name=f"调查员{seat}",
                attributes=attrs,
                skills=_skill_values(attrs),
                hp=max((attrs["con"] + attrs["siz"]) // 10, 1),
                san=attrs["pow"],
            )
        )
    return players


def _time_cost(module: Any, kind: str) -> int:
    value = module.time.costs.get(kind)
    if value is not None:
        return max(value, 0)
    return _DEFAULT_TIME_COSTS.get(kind, 0)


def _condition_terms(condition: Optional[str]) -> list[tuple[str, str]]:
    terms: list[tuple[str, str]] = []
    if not condition:
        return terms
    for raw in condition.split("&"):
        term = raw.strip()
        kind, sep, value = term.partition(":")
        if sep:
            terms.append((kind, value.partition(">=")[0]))
        elif term:
            terms.append((term, ""))
    return terms


def _has_time_condition(condition: Optional[str]) -> bool:
    return any(kind.startswith("time_") for kind, _value in _condition_terms(condition))


def _relevance(module: Any, ending: Any) -> _Relevant:
    """Compute an internal dependency slice, not a user-facing lint result."""
    clues: set[str] = set()
    flags: set[str] = set()
    monsters: set[str] = set()
    facts: set[tuple[str, str]] = set()
    social_nodes: set[tuple[str, str]] = set()
    has_time = _has_time_condition(ending.condition)
    all_players_incapped = ending.condition == "all_players_incapped"

    conditions = [ending.condition]
    if ending.condition == "all_players_incapped":
        monsters.update(monster.id for monster in module.monsters)
        flags.add("murder")
    conditions.extend(
        exit_.condition for scene in module.scenes for exit_ in scene.exits
    )
    changed = True
    while changed:
        changed = False
        for condition in conditions:
            for kind, value in _condition_terms(condition):
                if kind == "clue" and value not in clues:
                    clues.add(value)
                    changed = True
                elif kind == "clues":
                    before = len(clues)
                    clues.update(part for part in value.split("+") if part)
                    changed |= len(clues) != before
                elif kind == "flag" and value not in flags:
                    flags.add(value)
                    changed = True
                elif kind == "monster_dead" and value not in monsters:
                    monsters.add(value)
                    changed = True
        for npc in module.npcs:
            for node in npc.social_nodes:
                effects = set(node.success_flags) | set(node.failure_flags)
                node_clues = set(node.private_clues) | set(node.public_clues)
                node_facts = {(npc.id, fact) for fact in node.unlock_facts}
                required = {(npc.id, fact) for fact in node.requires_facts}
                useful = bool(
                    effects & flags
                    or node_clues & clues
                    or node_facts & facts
                    or required & facts
                )
                if useful and (npc.id, node.id) not in social_nodes:
                    social_nodes.add((npc.id, node.id))
                    before = len(facts)
                    facts.update(required)
                    changed |= len(facts) != before
        for monster in module.monsters:
            if monster.on_death_clue in clues and monster.id not in monsters:
                monsters.add(monster.id)
                changed = True
    return _Relevant(
        frozenset(clues),
        frozenset(flags),
        frozenset(monsters),
        frozenset(facts),
        frozenset(social_nodes),
        has_time,
        all_players_incapped,
    )


def _state_key(state: _State) -> tuple[Any, ...]:
    players = tuple(
        (p.hp, p.san, p.incapped) for p in state.players
    )
    return (
        state.scene,
        state.elapsed,
        players,
        tuple(sorted(state.clues)),
        tuple(sorted(state.public_clues)),
        tuple(
            sorted((key, tuple(sorted(value))) for key, value in state.clue_owners.items())
        ),
        tuple(sorted(state.fired_checks)),
        tuple(sorted(state.passed_checks)),
        tuple(sorted(state.flags.items())),
        tuple(sorted(state.occurred_events)),
        tuple(sorted(state.dead_monsters)),
        tuple(sorted(state.monster_hp.items())),
        tuple(sorted(state.dead_npcs)),
        tuple(sorted(state.npc_hp.items())),
        tuple(sorted(state.npc_hostile)),
        tuple(sorted(state.npc_rapport.items())),
        tuple(sorted(state.npc_attitude.items())),
        tuple(sorted(state.npc_attempts.items())),
        tuple(sorted(state.npc_rewards)),
        tuple(
            sorted((key, tuple(sorted(value))) for key, value in state.npc_facts.items())
        ),
        tuple(
            sorted(
                (key, tuple(sorted(value)))
                for key, value in state.npc_public_facts.items()
            )
        ),
        tuple(state.combat_order),
        state.combat_index,
        state.combat_round,
        tuple(sorted(state.move_counts.items())),
        state.rng.getstate(),
    )



def _dominance_key(state: _State) -> tuple[Any, ...]:
    """Return the exact story/RNG key without elapsed time."""
    key = _state_key(state)
    return key[:1] + key[2:]


def _ending(module: Any, state: _State) -> Optional[tuple[str, str, str]]:
    ctx = state.context()
    for ending in module.endings:
        if evaluate_condition(ending.condition, ctx):
            return ending.id, ending.display_name, ending.outcome
    if module.generic_endings:
        for ending_id, condition, outcome in GENERIC_ENDINGS:
            if evaluate_condition(condition, ctx):
                return ending_id, ending_id, outcome
    return None


def _update_events(module: Any, state: _State) -> None:
    ctx = state.context()
    state.occurred_events.update(
        event.id
        for event in module.events
        if event.id not in state.occurred_events
        and evaluate_condition(event.condition, ctx)
    )


def _append_step(
    state: _State,
    *,
    action: str,
    actor: Optional[str],
    target: Optional[str],
    before_scene: str,
    before_elapsed: int,
    before_clues: set[str],
    before_flags: dict[str, int],
    rolls: Optional[list[dict[str, Any]]] = None,
    detail: str = "",
) -> None:
    state.trace.append(
        TraceStep(
            index=len(state.trace) + 1,
            action=action,
            actor=actor,
            target=target,
            scene_before=before_scene,
            scene_after=state.scene,
            elapsed_before=before_elapsed,
            elapsed_after=state.elapsed,
            rolls=rolls or [],
            clues_added=sorted(state.clues - before_clues),
            flags_changed={
                key: value
                for key, value in sorted(state.flags.items())
                if before_flags.get(key) != value
            },
            detail=detail,
        )
    )


def _run_auto_exits(module: Any, state: _State) -> bool:
    limit = len(module.scenes) + 1
    for _ in range(limit):
        scene = module.scene(state.scene)
        if scene is None:
            return False
        chosen = next(
            (
                exit_
                for exit_ in scene.exits
                if exit_.auto and evaluate_condition(exit_.condition, state.context())
            ),
            None,
        )
        if chosen is None:
            return True
        before_scene = state.scene
        before_elapsed = state.elapsed
        before_clues = set(state.clues)
        before_flags = dict(state.flags)
        state.scene = chosen.to_scene
        state.combat_order.clear()
        state.combat_index = 0
        state.combat_round = 0
        _append_step(
            state,
            action="auto_move",
            actor=None,
            target=chosen.to_scene,
            before_scene=before_scene,
            before_elapsed=before_elapsed,
            before_clues=before_clues,
            before_flags=before_flags,
            detail="自动出口",
        )
        if _ending(module, state) is not None:
            return True
        _update_events(module, state)
    return False


def _npc_present(module: Any, state: _State, npc_id: str) -> bool:
    if npc_id in state.dead_npcs:
        return False
    presence = module.npc_presence(
        npc_id,
        (state.clock_start + state.elapsed) % 1440,
        state.context(),
    )
    return presence is not None and presence[0] == state.scene


def _check_success(
    state: _State,
    player: Player,
    skill: str,
    difficulty: Any,
) -> tuple[bool, dict[str, Any]]:
    value = max(player.san, 1) if skill == "san" else player.skills.get(skill, 0)
    roll = state.rng.randint(1, 100)
    if roll == 1:
        tier = "critical"
    elif roll >= (100 if value < 50 else 96):
        tier = "fumble"
    elif roll <= value // 5:
        tier = "extreme"
    elif roll <= value // 2:
        tier = "hard"
    elif roll <= value:
        tier = "regular"
    else:
        tier = "failure"
    if tier == "fumble":
        success = False
    elif tier == "critical":
        success = True
    elif difficulty is CheckDifficulty.EXTREME:
        success = tier == "extreme"
    elif difficulty is CheckDifficulty.HARD:
        success = tier in {"extreme", "hard"}
    else:
        success = tier in {"critical", "extreme", "hard", "regular"}
    return success, {
        "kind": "d100",
        "player": player.name,
        "skill": skill,
        "roll": roll,
        "value": value,
        "difficulty": difficulty.value,
        "tier": tier,
        "success": success,
    }


_DICE_RE = re.compile(r"(\d+)d(\d+)([+-]\d+)?")


def _roll_dice(state: _State, expression: str) -> tuple[int, dict[str, Any]]:
    text = expression.strip()
    if text.isdigit():
        total = int(text)
    else:
        match = _DICE_RE.fullmatch(text)
        if match is None:
            raise ValueError(f"非法骰表达式：{expression}")
        count, sides, modifier = match.groups()
        total = sum(state.rng.randint(1, int(sides)) for _ in range(int(count)))
        total += int(modifier or 0)
        total = max(total, 0)
    return total, {"kind": "damage", "expression": expression, "result": total}


def _damage_bonus(player: Player) -> str:
    total = player.attributes["str"] + player.attributes["siz"]
    for threshold, bonus in (
        (64, "-2"),
        (84, "-1"),
        (124, "0"),
        (164, "+1d4"),
        (204, "+1d6"),
    ):
        if total <= threshold:
            return bonus
    return "+2d6"


def _apply_check(module: Any, state: _State, action: _Action) -> list[dict[str, Any]]:
    scene = module.scene(state.scene)
    cp = next(item for item in scene.checks if item.id == action.target)
    actor = state.players[action.actor]
    rolls: list[dict[str, Any]] = []
    if cp.mode is CheckMode.TEAM and cp.skill != "san":
        active = [player for player in state.players if not player.incapped]
        successes = 0
        for player in active:
            success, roll = _check_success(state, player, cp.skill, cp.difficulty)
            rolls.append(roll)
            successes += int(success)
        needed = cp.required_successes or math.ceil(len(active) / 2)
        success = successes >= needed
    else:
        success, roll = _check_success(state, actor, cp.skill, cp.difficulty)
        rolls.append(roll)
    if cp.skill == "san":
        left, _, right = (cp.san_loss or "0/1").partition("/")
        loss, loss_roll = _roll_dice(state, left if success else right)
        loss_roll["kind"] = "san_loss"
        rolls.append(loss_roll)
        actor.san = max(actor.san - loss, 0)
        actor.incapped = actor.san <= 0
    if success and cp.clue:
        state.clues.add(cp.clue)
        state.clue_owners.setdefault(cp.clue, set()).add(actor.seat)
    if not success and cp.damage_on_fail:
        damage, damage_roll = _roll_dice(state, cp.damage_on_fail)
        rolls.append(damage_roll)
        actor.hp = max(actor.hp - damage, 0)
        actor.incapped = actor.hp <= 0
    if success:
        state.passed_checks.add(cp.id)
    if cp.once:
        state.fired_checks.add(cp.id)
    check_cost = cp.time_cost if cp.time_cost is not None else _time_cost(module, "check")
    state.elapsed += max(check_cost, 0)
    return rolls


def _social_delta(strategy: Any, node: Any, field_name: str) -> int:
    value = getattr(strategy, field_name)
    return getattr(node, field_name) if value is None else value


def _apply_social(module: Any, state: _State, action: _Action) -> list[dict[str, Any]]:
    npc = module.npc(action.target)
    node = next(item for item in npc.social_nodes if item.id == action.aux)
    strategy = node.strategy(action.skill)
    if strategy is None:
        return []
    actor = state.players[action.actor]
    key = (npc.id, actor.seat, node.id)
    attempt = state.npc_attempts.get(key, 0) + 1
    state.npc_attempts[key] = attempt
    success, roll = _check_success(state, actor, strategy.skill, strategy.difficulty)
    rapport_field = "success_rapport_delta" if success else "failure_rapport_delta"
    attitude_field = "success_attitude_delta" if success else "failure_attitude_delta"
    rapport_delta = _social_delta(strategy, node, rapport_field)
    attitude_delta = _social_delta(strategy, node, attitude_field)
    if not success:
        rapport_delta -= node.retry_rapport_penalty * (attempt - 1)
        attitude_delta -= node.retry_attitude_penalty * (attempt - 1)
    rapport_key = (npc.id, actor.seat)
    old_rapport = state.npc_rapport.get(rapport_key)
    if old_rapport is None:
        old_rapport = npc.initial_rapport
    old_attitude = state.npc_attitude.get(npc.id)
    if old_attitude is None:
        old_attitude = npc.initial_attitude
    state.npc_rapport[rapport_key] = max(-100, min(100, old_rapport + rapport_delta))
    state.npc_attitude[npc.id] = max(-100, min(100, old_attitude + attitude_delta))
    reward_key = (npc.id, actor.seat, node.id, success)
    if reward_key not in state.npc_rewards:
        state.npc_rewards.add(reward_key)
        if success:
            state.npc_facts.setdefault((npc.id, actor.seat), set()).update(
                node.unlock_facts
            )
            for clue in node.private_clues:
                state.clues.add(clue)
                state.clue_owners.setdefault(clue, set()).add(actor.seat)
            for clue in node.public_clues:
                state.clues.add(clue)
                state.public_clues.add(clue)
            for flag in node.success_flags:
                state.flags[flag] = state.flags.get(flag, 0) + 1
        else:
            for flag in node.failure_flags:
                state.flags[flag] = state.flags.get(flag, 0) + 1
    state.elapsed += _time_cost(module, "talk")
    return [roll]


def _opposed_dodge(
    state: _State,
    attack_roll: dict[str, Any],
    dodge_value: Optional[int],
    label: str,
) -> tuple[bool, Optional[dict[str, Any]]]:
    if not dodge_value:
        return False, None
    dummy = Player(0, label, {}, {"dodge": dodge_value}, 1, 1)
    success, roll = _check_success(
        state, dummy, "dodge", CheckDifficulty.REGULAR
    )
    ranks = {
        "fumble": 0,
        "failure": 1,
        "regular": 2,
        "hard": 3,
        "extreme": 4,
        "critical": 5,
    }
    dodged = success and ranks[roll["tier"]] >= ranks[attack_roll["tier"]]
    return dodged, roll


def _combat_targets(module: Any, state: _State) -> list[tuple[str, str]]:
    scene = module.scene(state.scene)
    targets = [
        ("monster", ident)
        for ident in scene.monsters
        if ident not in state.dead_monsters
    ]
    targets.extend(
        ("npc", npc_id)
        for npc_id in sorted(state.npc_hostile)
        if npc_id not in state.dead_npcs and _npc_present(module, state, npc_id)
    )
    return targets


def _start_combat(state: _State) -> None:
    if state.combat_order:
        return
    active = [index for index, player in enumerate(state.players) if not player.incapped]
    active.sort(key=lambda index: (-state.players[index].attributes["dex"], index))
    state.combat_order = active
    state.combat_index = 0
    state.combat_round = 1


def _advance_combat(module: Any, state: _State) -> None:
    if not _combat_targets(module, state):
        state.combat_order.clear()
        state.combat_index = 0
        state.combat_round = 0
        return
    if not state.combat_order:
        return
    current = state.combat_order[state.combat_index]
    state.combat_order = [
        index for index in state.combat_order if not state.players[index].incapped
    ]
    if not state.combat_order:
        return
    if current in state.combat_order:
        state.combat_index = (state.combat_order.index(current) + 1) % len(
            state.combat_order
        )
    else:
        state.combat_index %= len(state.combat_order)
    if state.combat_index == 0:
        state.combat_round += 1


def _apply_attack(module: Any, state: _State, action: _Action) -> list[dict[str, Any]]:
    target_kind, target_id = action.target.split(":", 1)
    actor = state.players[action.actor]
    started = not state.combat_order
    _start_combat(state)
    if target_kind == "npc" and started:
        state.npc_hostile.add(target_id)
    if state.combat_order[state.combat_index] != action.actor:
        return []
    success, attack_roll = _check_success(
        state, actor, "brawl", CheckDifficulty.REGULAR
    )
    rolls = [attack_roll]
    state.elapsed += _time_cost(module, "attack")
    if target_kind == "monster":
        target = module.monster(target_id)
    else:
        target = module.npc(target_id)
    if target is None:
        return rolls
    dodge = target.dodge
    if target_kind != "monster":
        state.flags["assault"] = state.flags.get("assault", 0) + 1
        rapport_key = (target.id, actor.seat)
        rapport = state.npc_rapport.get(rapport_key)
        if rapport is None:
            rapport = target.initial_rapport
        state.npc_rapport[rapport_key] = max(
            -100,
            rapport - 40,
        )
        attitude = state.npc_attitude.get(target.id)
        if attitude is None:
            attitude = target.initial_attitude
        state.npc_attitude[target.id] = max(
            -100,
            attitude - 30,
        )
    if success:
        dodged, dodge_roll = _opposed_dodge(
            state, attack_roll, dodge, f"{target.name}闪避"
        )
        if dodge_roll:
            rolls.append(dodge_roll)
        if not dodged:
            damage, damage_roll = _roll_dice(state, "1d3")
            rolls.append(damage_roll)
            bonus = _damage_bonus(actor)
            if bonus.startswith("+"):
                added, bonus_roll = _roll_dice(state, bonus[1:])
                rolls.append(bonus_roll)
                damage += added
            elif bonus.startswith("-"):
                damage = max(damage + int(bonus), 0)
            if target_kind == "monster":
                remaining = state.monster_hp.get(target.id)
                if remaining is None:
                    remaining = target.hp
                remaining -= damage
                state.monster_hp[target.id] = remaining
                if remaining <= 0:
                    state.dead_monsters.add(target.id)
                    if target.on_death_clue:
                        state.clues.add(target.on_death_clue)
                        state.public_clues.add(target.on_death_clue)
            else:
                remaining = state.npc_hp.get(target.id)
                if remaining is None:
                    remaining = target.hp
                remaining -= damage
                state.npc_hp[target.id] = remaining
                if remaining <= 0:
                    state.dead_npcs.add(target.id)
                    state.flags[f"npc_dead:{target.id}"] = 1
                    state.flags["murder"] = state.flags.get("murder", 0) + 1
                    if target.on_death_clue:
                        state.clues.add(target.on_death_clue)
                        state.public_clues.add(target.on_death_clue)
    if target_kind == "npc" and target.id not in state.dead_npcs:
        npc_success, npc_roll = _check_success(
            state,
            Player(0, target.name, {}, {"attack": target.attack_skill}, 1, 1),
            "attack",
            CheckDifficulty.REGULAR,
        )
        rolls.append(npc_roll)
        if npc_success:
            dodged, dodge_roll = _opposed_dodge(
                state,
                npc_roll,
                actor.skills.get("dodge"),
                actor.name,
            )
            if dodge_roll:
                rolls.append(dodge_roll)
            if not dodged:
                damage, damage_roll = _roll_dice(state, target.damage)
                rolls.append(damage_roll)
                actor.hp = max(actor.hp - damage, 0)
                actor.incapped = actor.hp <= 0
    _advance_combat(module, state)
    return rolls


def _apply_monster_attack(
    module: Any, state: _State, action: _Action
) -> list[dict[str, Any]]:
    """Resolve the schema's explicit ``monster_attack`` tool offline.

    The online tool does not advance the game clock or player combat index;
    it only performs the monster's opposed attack against the selected active
    investigator.  The BFS exposes it for all-incapacitated target searches,
    where it is a meaningful plot-changing action rather than narration.
    """
    monster_id = action.target.removeprefix("monster:")
    monster = module.monster(monster_id)
    if monster is None or monster_id in state.dead_monsters:
        return []
    if action.actor >= len(state.players) or state.players[action.actor].incapped:
        return []
    victim = state.players[action.actor]
    attacker = Player(
        seat=0,
        name=monster.name,
        attributes={},
        skills={"attack": monster.attack_skill},
        hp=1,
        san=1,
    )
    success, attack_roll = _check_success(
        state,
        attacker,
        "attack",
        CheckDifficulty.REGULAR,
    )
    rolls = [attack_roll]
    if not success:
        return rolls
    dodged, dodge_roll = _opposed_dodge(
        state,
        attack_roll,
        victim.skills.get("dodge"),
        victim.name,
    )
    if dodge_roll:
        rolls.append(dodge_roll)
    if dodged:
        return rolls
    damage, damage_roll = _roll_dice(state, monster.damage)
    rolls.append(damage_roll)
    victim.hp = max(victim.hp - damage, 0)
    victim.incapped = victim.hp <= 0
    return rolls


def _apply_action(module: Any, parent: _State, action: _Action) -> _State:
    state = parent.clone()
    before_scene = state.scene
    before_elapsed = state.elapsed
    before_clues = set(state.clues)
    before_flags = dict(state.flags)
    rolls: list[dict[str, Any]] = []
    detail = ""
    if action.kind == "move":
        state.scene = action.target
        state.elapsed += action.value
        edge = (before_scene, action.target)
        state.move_counts[edge] = state.move_counts.get(edge, 0) + 1
        state.combat_order.clear()
        state.combat_index = 0
        state.combat_round = 0
    elif action.kind == "check":
        rolls = _apply_check(module, state, action)
    elif action.kind == "social":
        rolls = _apply_social(module, state, action)
    elif action.kind == "wait":
        state.elapsed += action.value
    elif action.kind == "attack":
        rolls = _apply_attack(module, state, action)
    elif action.kind == "monster_attack":
        rolls = _apply_monster_attack(module, state, action)
    elif action.kind == "pass":
        _advance_combat(module, state)
    actor_name = state.players[action.actor].name if action.actor < len(state.players) else None
    _append_step(
        state,
        action=action.kind,
        actor=actor_name,
        target=action.target or None,
        before_scene=before_scene,
        before_elapsed=before_elapsed,
        before_clues=before_clues,
        before_flags=before_flags,
        rolls=rolls,
        detail=detail,
    )
    return state


def _last_move_was_reverse(state: _State, target: str) -> bool:
    if not state.trace:
        return False
    last = state.trace[-1]
    return (
        last.action in {"move", "auto_move"}
        and last.scene_before == target
        and last.scene_after == state.scene
    )


def _social_available(module: Any, state: _State, npc: Any, node: Any, actor: int) -> bool:
    player = state.players[actor]
    unlocked = state.npc_facts.get((npc.id, player.seat), set()) | state.npc_public_facts.get(
        npc.id, set()
    )
    if not set(node.requires_facts).issubset(unlocked):
        return False
    rapport = state.npc_rapport.get((npc.id, player.seat), npc.initial_rapport)
    attitude = state.npc_attitude.get(npc.id, npc.initial_attitude)
    attempts = state.npc_attempts.get((npc.id, player.seat, node.id), 0)
    return (
        rapport >= node.min_rapport
        and attitude >= node.min_attitude
        and attempts < node.max_attempts
    )


def _social_is_outcome_invariant(
    npc_id: str, node: Any, relevant: _Relevant
) -> bool:
    """Whether one social representative is enough for this target slice.

    Some modules deliberately write the same plot flag on both success and
    failure.  Enumerating every player/strategy permutation for such a node
    only changes rapport and RNG while never changing target reachability, so
    the first actor and declared strategy remain the deterministic choice.
    """
    success = set(node.success_flags)
    failure = set(node.failure_flags)
    relevant_facts = {
        fact for owner, fact in relevant.facts if owner == npc_id
    }
    relevant_rewards = (
        (set(node.private_clues) | set(node.public_clues)) & set(relevant.clues)
    ) | (set(node.unlock_facts) & relevant_facts)
    return (
        bool(success)
        and success == failure
        and success.issubset(relevant.flags)
        and not relevant_rewards
    )


def _wait_values(module: Any, state: _State, relevant: _Relevant) -> list[int]:
    values = {1, 5, 30, DEFAULT_WAIT_MAX}
    conditions: list[str] = []
    conditions.extend(str(ending.condition) for ending in module.endings)
    conditions.extend(
        str(exit_.condition or "") for scene in module.scenes for exit_ in scene.exits
    )
    for npc in module.npcs:
        if any(npc.id == ident for ident, _node in relevant.social_nodes):
            for entry in npc.schedule:
                conditions.append(entry.condition)
                for raw in (entry.frm, entry.to):
                    hours, minutes = map(int, raw.split(":"))
                    boundary = (hours * 60 + minutes - state.clock_start) % 1440
                    if boundary > state.elapsed:
                        values.add(boundary - state.elapsed)
    for condition in conditions:
        for match in re.finditer(r"time_(?:after|before):([0-2]?\d:[0-5]\d)", condition):
            hours, minutes = map(int, match.group(1).split(":"))
            boundary = (hours * 60 + minutes - state.clock_start) % 1440
            if boundary > state.elapsed:
                values.add(boundary - state.elapsed)
        for match in re.finditer(
            r"time_between:([0-2]?\d:[0-5]\d)-([0-2]?\d:[0-5]\d)",
            condition,
        ):
            for raw in match.groups():
                hours, minutes = map(int, raw.split(":"))
                boundary = (hours * 60 + minutes - state.clock_start) % 1440
                if boundary > state.elapsed:
                    values.add(boundary - state.elapsed)
    return sorted(value for value in values if 1 <= value <= DEFAULT_WAIT_MAX)


def _actions(
    module: Any,
    state: _State,
    relevant: _Relevant,
    *,
    include_rng_noise: bool = False,
) -> list[_Action]:
    if state.combat_order:
        actor = state.combat_order[state.combat_index]
        targets = _combat_targets(module, state)
        actions = (
            [
                _Action("attack", actor, f"{kind}:{target}")
                for kind, target in targets
            ]
            if not relevant.all_players_incapped
            else []
        )
        if relevant.all_players_incapped:
            scene = module.scene(state.scene)
            actions.extend(
                _Action("monster_attack", victim, f"monster:{monster_id}")
                for monster_id in scene.monsters
                if monster_id not in state.dead_monsters
                for victim, player in enumerate(state.players)
                if not player.incapped
            )
        # Passing is retained as a legal fallback when no target can be
        # selected.  With a living target it only adds an RNG-independent
        # permutation and makes a bounded target search explode; the online
        # engine's normal path already offers an attack in that situation.
        if not actions:
            actions.append(_Action("pass", actor))
        return actions

    actions: list[_Action] = []
    scene = module.scene(state.scene)
    if relevant.all_players_incapped:
        monster_actions = [
            _Action("monster_attack", victim, f"monster:{monster_id}")
            for monster_id in scene.monsters
            if monster_id not in state.dead_monsters
            for victim, player in enumerate(state.players)
            if not player.incapped
        ]
        if monster_actions:
            return monster_actions
    for exit_ in scene.exits:
        if exit_.auto or not evaluate_condition(exit_.condition, state.context()):
            continue
        if state.move_counts.get((state.scene, exit_.to_scene), 0) >= 2:
            continue
        if _last_move_was_reverse(state, exit_.to_scene):
            continue
        cost = (
            exit_.time_cost
            if exit_.time_cost is not None
            else _time_cost(module, "move")
        )
        cost = max(cost, 0)
        actions.append(_Action("move", 0, exit_.to_scene, value=cost))
    for cp in scene.checks:
        if cp.once and cp.id in state.fired_checks:
            continue
        irrelevant_roll = (
            cp.skill != "san"
            and not cp.damage_on_fail
            and (cp.clue is None or cp.clue not in relevant.clues)
        )
        if irrelevant_roll and (not include_rng_noise or not cp.once):
            continue
        actors = (
            [0]
            if cp.mode is CheckMode.TEAM or irrelevant_roll
            else range(len(state.players))
        )
        actions.extend(
            _Action("check", actor, cp.id)
            for actor in actors
            if not state.players[actor].incapped
        )
    for npc in module.npcs:
        if not _npc_present(module, state, npc.id):
            continue
        for node in npc.social_nodes:
            if (npc.id, node.id) not in relevant.social_nodes:
                continue
            candidates: list[tuple[int, Any]] = []
            for actor, player in enumerate(state.players):
                if player.incapped or not _social_available(
                    module, state, npc, node, actor
                ):
                    continue
                relevant_done = (
                    set(node.success_flags) | set(node.failure_flags)
                ).issubset(state.flags)
                relevant_facts = {
                    fact for owner, fact in relevant.facts if owner == npc.id
                }
                node_facts = set(node.unlock_facts) & relevant_facts
                facts_done = node_facts.issubset(
                    state.npc_facts.get((npc.id, player.seat), set())
                )
                node_clues = (
                    set(node.private_clues) | set(node.public_clues)
                ) & set(relevant.clues)
                clues_done = node_clues.issubset(state.clues)
                if relevant_done and facts_done and clues_done:
                    continue
                candidates.extend((actor, strategy) for strategy in node.strategies)
            if _social_is_outcome_invariant(npc.id, node, relevant) and candidates:
                candidates = candidates[:1]
            actions.extend(
                _Action(
                    "social",
                    actor,
                    npc.id,
                    node.id,
                    skill=strategy.skill,
                )
                for actor, strategy in candidates
            )
    for monster_id in scene.monsters:
        monster = module.monster(monster_id)
        if monster_id in state.dead_monsters:
            continue
        if (
            not relevant.all_players_incapped
            and (monster_id in relevant.monsters or monster.on_death_clue in relevant.clues)
        ):
            actions.extend(
                _Action("attack", actor, f"monster:{monster_id}")
                for actor, player in enumerate(state.players)
                if not player.incapped
            )
    for npc in module.npcs:
        useful = (
            npc.on_death_clue in relevant.clues
            or "murder" in relevant.flags
            or "assault" in relevant.flags
            or f"npc_dead:{npc.id}" in relevant.flags
        )
        if useful and _npc_present(module, state, npc.id):
            actions.extend(
                _Action("attack", actor, f"npc:{npc.id}")
                for actor, player in enumerate(state.players)
                if not player.incapped
            )
    time_sensitive = relevant.has_time or any(
        any(npc.id == ident for ident, _ in relevant.social_nodes) and npc.schedule
        for npc in module.npcs
    )
    scheduled_target_absent = any(
        npc.schedule
        and any(npc.id == ident for ident, _ in relevant.social_nodes)
        and not _npc_present(module, state, npc.id)
        for npc in module.npcs
    )
    if time_sensitive and (
        relevant.has_time or not actions or scheduled_target_absent
    ):
        actions.extend(
            _Action("wait", 0, value=value)
            for value in _wait_values(module, state, relevant)
        )
    return actions


def _success_result(
    module: Any,
    config: SearchConfig,
    initial_players: list[dict[str, Any]],
    state: _State,
    ending: tuple[str, str, str],
    explored: int,
    generated: int,
) -> SearchResult:
    return SearchResult(
        ok=True,
        reason="success",
        message=f"找到目标结局 {ending[0]}",
        module_id=module.id,
        module_name=module.name,
        seed=config.seed,
        target_ending=config.ending_id,
        final_ending={"id": ending[0], "name": ending[1], "outcome": ending[2]},
        final_scene=state.scene,
        elapsed_minutes=state.elapsed,
        clues=sorted(state.clues),
        flags=dict(sorted(state.flags.items())),
        events=sorted(state.occurred_events),
        players=initial_players,
        final_players=[player.public_dict() for player in state.players],
        steps=[asdict(step) for step in state.trace],
        explored_states=explored,
        generated_states=generated,
        max_depth=config.max_depth,
        max_states=config.max_states,
    )


def _failure_result(
    module: Any,
    config: SearchConfig,
    reason: Reason,
    message: str,
    *,
    players: Optional[list[dict[str, Any]]] = None,
    explored: int = 0,
    generated: int = 0,
) -> SearchResult:
    return SearchResult(
        ok=False,
        reason=reason,
        message=message,
        module_id=getattr(module, "id", ""),
        module_name=getattr(module, "name", ""),
        seed=config.seed,
        target_ending=config.ending_id,
        players=players or [],
        explored_states=explored,
        generated_states=generated,
        max_depth=config.max_depth,
        max_states=config.max_states,
    )


def search_module_data(
    data: Any,
    config: SearchConfig,
) -> SearchResult:
    """Validate an in-memory YAML mapping and search it without disk I/O."""
    try:
        module = ModuleDef.model_validate(data)
    except Exception as error:  # noqa: BLE001
        return SearchResult(
            ok=False,
            reason="invalid_module",
            message=f"模组读取或校验失败：{error}",
            seed=config.seed,
            target_ending=config.ending_id,
            max_depth=config.max_depth,
            max_states=config.max_states,
        )
    return search_module(module, config)


def search_module(
    module: Any,
    config: SearchConfig,
    *,
    _include_rng_noise: bool = False,
) -> SearchResult:
    """Find a shortest fixed-seed trace to one declared or generic ending."""
    endings = {ending.id: ending for ending in module.endings}
    generic = {item[0]: item for item in GENERIC_ENDINGS}
    if config.ending_id not in endings and not (
        module.generic_endings and config.ending_id in generic
    ):
        return _failure_result(
            module,
            config,
            "unknown_ending",
            f"模组不存在结局 {config.ending_id!r}",
        )
    count = config.players if config.players is not None else module.min_players
    if not module.min_players <= count <= module.max_players:
        return _failure_result(
            module,
            config,
            "invalid_players",
            f"玩家数必须在 {module.min_players}-{module.max_players} 之间",
        )
    if config.max_depth < 0 or config.max_states < 1:
        return _failure_result(
            module,
            config,
            "limit_exceeded",
            "搜索上限必须满足 max_depth >= 0 且 max_states >= 1",
        )

    rng = random.Random(config.seed)
    players = _build_players(count, rng)
    initial_players = [player.public_dict() for player in players]
    state = _State(
        scene=module.start_scene,
        clock_start=module.time.start_minutes,
        elapsed=0,
        players=players,
        rng=rng,
    )
    # Online play scans declared endings before events and automatic exits.
    # Keep that ordering at the root as well; an opening scene can itself be
    # the target (or an already-terminal non-target branch).
    initial_ending = _ending(module, state)
    if initial_ending is not None:
        if initial_ending[0] == config.ending_id:
            return _success_result(
                module,
                config,
                initial_players,
                state,
                initial_ending,
                explored=1,
                generated=1,
            )
        return _failure_result(
            module,
            config,
            "no_path",
            f"开局已命中非目标结局 {initial_ending[0]!r}",
            players=initial_players,
            explored=1,
            generated=1,
        )
    if not _run_auto_exits(module, state):
        return _failure_result(
            module,
            config,
            "no_path",
            "自动出口形成循环或指向非法场景",
            players=initial_players,
        )
    target: Any = endings.get(config.ending_id)
    if target is None:
        condition = generic[config.ending_id][1]
        target = type("GenericEnding", (), {"condition": condition})()
    relevant = _relevance(module, target)
    preserve_clock = _has_time_condition(target.condition)
    target_scenes = {
        value for kind, value in _condition_terms(target.condition) if kind == "scene"
    }
    if not preserve_clock and target_scenes:
        for scene in module.scenes:
            for exit_ in scene.exits:
                if exit_.to_scene not in target_scenes or not _has_time_condition(
                    exit_.condition
                ):
                    continue
                has_alternative = any(
                    other.to_scene == exit_.to_scene
                    and not _has_time_condition(other.condition)
                    for other in scene.exits
                )
                if not has_alternative:
                    preserve_clock = True
                    break
            if preserve_clock:
                break
    if not preserve_clock:
        for npc_id, _node_id in relevant.social_nodes:
            npc = module.npc(npc_id)
            if npc is not None and npc.schedule and not _npc_present(module, state, npc_id):
                preserve_clock = True
                break
    queue: deque[_State] = deque([state])
    visited = {_state_key(state)}
    frontier: dict[tuple[Any, ...], list[tuple[int, int]]] = {
        _dominance_key(state): [(state.elapsed, len(state.trace))]
    }
    explored = 0
    generated = 1
    hit_depth_limit = False

    while queue:
        current = queue.popleft()
        explored += 1
        ending = _ending(module, current)
        if ending is not None:
            if ending[0] == config.ending_id:
                return _success_result(
                    module,
                    config,
                    initial_players,
                    current,
                    ending,
                    explored,
                    generated,
                )
            continue
        _update_events(module, current)
        if len(current.trace) >= config.max_depth:
            hit_depth_limit = True
            continue
        for action in _actions(
            module,
            current,
            relevant,
            include_rng_noise=_include_rng_noise,
        ):
            child = _apply_action(module, current, action)
            ending = _ending(module, child)
            if ending is None:
                _update_events(module, child)
                if not _run_auto_exits(module, child):
                    continue
            key = _state_key(child)
            if key in visited:
                continue
            if not preserve_clock:
                dominance_key = _dominance_key(child)
                depth = len(child.trace)
                frontier_points = frontier.setdefault(dominance_key, [])
                if any(
                    old_elapsed <= child.elapsed and old_depth <= depth
                    for old_elapsed, old_depth in frontier_points
                ):
                    continue
                frontier[dominance_key] = [
                    (old_elapsed, old_depth)
                    for old_elapsed, old_depth in frontier_points
                    if not (child.elapsed <= old_elapsed and depth <= old_depth)
                ] + [(child.elapsed, depth)]
            if generated >= config.max_states:
                return _failure_result(
                    module,
                    config,
                    "limit_exceeded",
                    f"搜索达到状态上限 {config.max_states}",
                    players=initial_players,
                    explored=explored,
                    generated=generated,
                )
            visited.add(key)
            generated += 1
            queue.append(child)

    reason: Reason = "limit_exceeded" if hit_depth_limit else "no_path"
    message = (
        f"搜索达到动作深度上限 {config.max_depth}"
        if hit_depth_limit
        else "穷尽状态后仍未找到目标结局"
    )
    if reason == "no_path" and not _include_rng_noise:
        return search_module(module, config, _include_rng_noise=True)
    return _failure_result(
        module,
        config,
        reason,
        message,
        players=initial_players,
        explored=explored,
        generated=generated,
    )
