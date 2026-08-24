# ruff: noqa: I001,TID252,PLW0603
"""跑团模组库 WebUI 只读投影。

模组对象由 RPG 子插件加载并经过 ``ModuleDef`` 校验；本路由只做延迟解析
和管理员只读序列化，不重新读取 YAML，也不向运行中的游戏写入模组状态。
"""

from __future__ import annotations

from collections import Counter
from pathlib import Path
from typing import Any

import yaml
from fastapi import APIRouter, HTTPException, status
from nonebot import logger

from .config import API_PATH
from .deps import ReadSession, ok

router = APIRouter(prefix=API_PATH)

_schema_module: Any = None
_schema_resolved = False


def _rpg_module_schema() -> Any | None:
    """延迟解析 RPG schema；失败不缓存，支持子插件晚加载恢复。"""
    global _schema_module, _schema_resolved
    if not _schema_resolved:
        try:
            from ..yawn_rpg import module_schema as module  # pyright: ignore[reportMissingImports]
        except Exception as exc:  # noqa: BLE001
            logger.debug(f"跑团模组 schema 不可用，模组库降级：{exc}")
            return None
        _schema_module = module
        _schema_resolved = True
    return _schema_module


def _enum_value(value: Any) -> Any:
    return getattr(value, "value", value)


def _module_source(module: Any) -> dict[str, Any] | None:
    """Read the source YAML for optional editor linting without mutating runtime state."""
    schema = _rpg_module_schema()
    schema_file = getattr(schema, "__file__", None)
    if not schema_file:
        return None
    directory = Path(schema_file).parent / "modules"
    for path in sorted(directory.glob("*.yaml")):
        try:
            raw = yaml.safe_load(path.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            continue
        if isinstance(raw, dict) and raw.get("id") == module.id:
            return raw
    return None


def _static_health(module: Any, *, include_issues: bool = False) -> dict[str, Any]:
    """Return read-only schema/lint health; editor tooling remains optional at runtime."""
    base: dict[str, Any] = {
        "status": "healthy",
        "schemaValidated": True,
        "lintAvailable": False,
        "errorCount": 0,
        "warningCount": 0,
        "infoCount": 0,
        "issues": [],
    }
    raw = _module_source(module)
    if raw is None:
        base["status"] = "schema-only"
        return base
    try:
        from tools.rpg_module_editor.lint import run_lint
    except Exception as exc:  # noqa: BLE001
        logger.debug(f"跑团模组编辑器 lint 不可用，WebUI 仅展示 schema 健康状态：{exc}")
        base["status"] = "schema-only"
        return base
    try:
        issues = run_lint(raw)
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"跑团模组 {module.id} 静态检查失败，WebUI 降级：{exc}")
        base["status"] = "schema-only"
        return base
    counts = Counter(issue.severity for issue in issues)
    base.update(
        {
            "lintAvailable": True,
            "errorCount": counts.get("ERROR", 0),
            "warningCount": counts.get("WARNING", 0),
            "infoCount": counts.get("INFO", 0),
        }
    )
    if base["errorCount"]:
        base["status"] = "error"
    elif base["warningCount"]:
        base["status"] = "warning"
    if include_issues:
        base["issues"] = [
            {
                "severity": issue.severity,
                "section": issue.section,
                "path": issue.path_label,
                "message": issue.message,
                "hint": issue.hint,
            }
            for issue in issues
        ]
    return base


def _check(module: Any) -> dict[str, Any]:
    return {
        "id": module.id,
        "skill": module.skill,
        "difficulty": _enum_value(module.difficulty),
        "mode": _enum_value(module.mode),
        "requiredSuccesses": module.required_successes,
        "triggers": list(module.triggers),
        "priority": module.priority,
        "once": module.once,
        "successText": module.success_text,
        "failureText": module.failure_text,
        "clue": module.clue,
        "sanLoss": module.san_loss,
        "damageOnFail": module.damage_on_fail,
        "timeCost": module.time_cost,
    }


def _scene(module: Any) -> dict[str, Any]:
    return {
        "id": module.id,
        "name": module.name,
        "narration": module.narration,
        "idleNarration": module.idle_narration,
        "npcs": list(module.npcs),
        "monsters": list(module.monsters),
        "checks": [_check(check) for check in module.checks],
        "exits": [
            {
                "toScene": exit_.to_scene,
                "condition": exit_.condition,
                "keywords": list(exit_.keywords),
                "auto": exit_.auto,
                "narration": exit_.narration,
                "timeCost": exit_.time_cost,
            }
            for exit_ in module.exits
        ],
    }


def _social_strategy(strategy: Any) -> dict[str, Any]:
    return {
        "skill": strategy.skill,
        "difficulty": _enum_value(strategy.difficulty),
        "name": strategy.name,
        "successRapportDelta": strategy.success_rapport_delta,
        "successAttitudeDelta": strategy.success_attitude_delta,
        "failureRapportDelta": strategy.failure_rapport_delta,
        "failureAttitudeDelta": strategy.failure_attitude_delta,
        "successText": strategy.success_text,
        "failureText": strategy.failure_text,
    }


def _social_node(node: Any) -> dict[str, Any]:
    return {
        "id": node.id,
        "name": node.name,
        "goal": node.goal,
        "strategies": [_social_strategy(item) for item in node.strategies],
        "requiresFacts": list(node.requires_facts),
        "minRapport": node.min_rapport,
        "minAttitude": node.min_attitude,
        "maxAttempts": node.max_attempts,
        "retryRapportPenalty": node.retry_rapport_penalty,
        "retryAttitudePenalty": node.retry_attitude_penalty,
        "successRapportDelta": node.success_rapport_delta,
        "successAttitudeDelta": node.success_attitude_delta,
        "failureRapportDelta": node.failure_rapport_delta,
        "failureAttitudeDelta": node.failure_attitude_delta,
        "successText": node.success_text,
        "failureText": node.failure_text,
        "unlockFacts": list(node.unlock_facts),
        "privateClues": list(node.private_clues),
        "publicClues": list(node.public_clues),
        "successFlags": list(node.success_flags),
        "failureFlags": list(node.failure_flags),
    }


def _npc(npc: Any) -> dict[str, Any]:
    return {
        "id": npc.id,
        "name": npc.name,
        "publicDesc": npc.public_desc,
        "persona": npc.persona,
        "knows": list(npc.knows),
        "secrets": list(npc.secrets),
        "fallbackLine": npc.fallback_line,
        "initialRapport": npc.initial_rapport,
        "initialAttitude": npc.initial_attitude,
        "facts": [
            {"id": fact.id, "name": fact.name, "text": fact.text}
            for fact in npc.facts
        ],
        "socialNodes": [_social_node(node) for node in npc.social_nodes],
        "schedule": [
            {
                "from": entry.frm,
                "to": entry.to,
                "scene": entry.scene,
                "activity": entry.activity,
                "condition": entry.condition,
                "away": entry.away,
            }
            for entry in npc.schedule
        ],
        "hp": npc.hp,
        "attackSkill": npc.attack_skill,
        "attackName": npc.attack_name,
        "damage": npc.damage,
        "dodge": npc.dodge,
        "onDeathClue": npc.on_death_clue,
        "onDeathText": npc.on_death_text,
    }


def _monster(monster: Any) -> dict[str, Any]:
    return {
        "id": monster.id,
        "name": monster.name,
        "hp": monster.hp,
        "attackSkill": monster.attack_skill,
        "attackName": monster.attack_name,
        "damage": monster.damage,
        "dodge": monster.dodge,
        "onDeathClue": monster.on_death_clue,
        "onDeathText": monster.on_death_text,
    }


def _clue(clue: Any) -> dict[str, Any]:
    return {
        "id": clue.id,
        "name": clue.name,
        "text": clue.text,
        "category": clue.category,
        "sourceHint": clue.source_hint,
    }


def _deduction(deduction: Any) -> dict[str, Any]:
    return {
        "id": deduction.id,
        "name": deduction.name,
        "requiredClues": list(deduction.required_clues),
        "conclusionKeywords": [list(group) for group in deduction.conclusion_keywords],
        "successText": deduction.success_text,
        "failureHint": deduction.failure_hint,
        "unlockFlags": list(deduction.unlock_flags),
        "grantClues": list(deduction.grant_clues),
        "once": deduction.once,
        "failureTimeCost": deduction.failure_time_cost,
    }


def _ending(ending: Any) -> dict[str, Any]:
    return {
        "id": ending.id,
        "name": ending.name,
        "displayName": ending.display_name,
        "condition": ending.condition,
        "text": ending.text,
        "outcome": ending.outcome,
        "summary": ending.summary,
    }


def _event(event: Any) -> dict[str, Any]:
    return {
        "id": event.id,
        "name": event.name,
        "summary": event.summary,
        "condition": event.condition,
    }


def _summary(module: Any) -> dict[str, Any]:
    return {
        "id": module.id,
        "name": module.name,
        "description": module.description,
        "difficulty": module.difficulty,
        "minPlayers": module.min_players,
        "maxPlayers": module.max_players,
        "startScene": module.start_scene,
        "sceneCount": len(module.scenes),
        "npcCount": len(module.npcs),
        "monsterCount": len(module.monsters),
        "clueCount": len(module.clues),
        "deductionCount": len(module.deductions),
        "endingCount": len(module.endings),
        "eventCount": len(module.events),
        "health": _static_health(module),
    }


def _detail(module: Any) -> dict[str, Any]:
    return {
        **_summary(module),
        "health": _static_health(module, include_issues=True),
        "opening": module.opening,
        "genericEndings": module.generic_endings,
        "time": {"start": module.time.start, "costs": dict(module.time.costs)},
        "scenes": [_scene(scene) for scene in module.scenes],
        "npcs": [_npc(npc) for npc in module.npcs],
        "monsters": [_monster(monster) for monster in module.monsters],
        "clues": [_clue(clue) for clue in module.clues],
        "deductions": [_deduction(deduction) for deduction in module.deductions],
        "endings": [_ending(ending) for ending in module.endings],
        "events": [_event(event) for event in module.events],
    }


@router.get("/rpg/modules")
async def list_rpg_modules(_session: ReadSession) -> dict[str, Any]:
    schema = _rpg_module_schema()
    if schema is None:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "跑团子插件未加载")
    return ok([_summary(module) for module in schema.list_modules()])


@router.get("/rpg/modules/{module_id}")
async def get_rpg_module(module_id: str, _session: ReadSession) -> dict[str, Any]:
    schema = _rpg_module_schema()
    if schema is None:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "跑团子插件未加载")
    module = schema.get_module(module_id)
    if module is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "没有找到这个跑团模组")
    return ok(_detail(module))


__all__ = ["_rpg_module_schema", "router"]
