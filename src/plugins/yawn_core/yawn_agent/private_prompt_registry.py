# ruff: noqa: E501, PERF401, PLR0913
"""私聊 Agent 提示词注册表：WebUI「私聊 Agent → 提示词」的只读视图与管理动作。

存储与解析复用 ``prompt_sections``（覆盖模式、修订号乐观锁、历史、草稿），这里只做
私聊专属的东西：

- 分组与 5 条链路（主回合 / 汇报 / 协调 / 后台小助手 / 记忆整理）：每条链路按真实拼装
  顺序列出分段与出现条件，WebUI 据此画“这一轮会发出什么”。
- 目录：每个私聊分段的默认值、全局覆盖、本人覆盖、生效文本与 Token，及有多少用户改过。
- 体检：安全段被删/改、协议段被替换、占位符缺失、覆盖与默认相同、本人覆盖了已不存在的
  分段、生效文本为空或膨胀过多。
- 预览：用这位用户真实的资料与历史，套上未保存的全局/本人草稿和人设草稿，拼出将要发给
  模型的完整请求（不发送），格式与「最近请求」的抓取快照一致。
- 把某位用户的覆盖提升为全局、清空某位用户的全部覆盖。
"""

from __future__ import annotations

from collections.abc import Mapping  # noqa: TC003
from dataclasses import dataclass
from typing import Any

from .context_budget import estimate_tokens
from .prompt_sections import (
    GLOBAL_SCOPE,
    LAYER_LABELS,
    MODE_APPEND,
    MODE_DISABLE,
    MODE_PREPEND,
    MODE_REPLACE,
    SECTIONS,
    Override,
    PromptSection,
    bulk_apply,
    compose,
    get_section,
    is_private_section,
    private_tool_sections,
    resolve_override,
    scope_overrides,
    section_default,
    section_sample,
    store_snapshot,
    use_draft_scopes,
    use_private_user,
    user_scope_key,
    user_scopes,
)

# ── 分组 ────────────────────────────────────────────────────────────────

GROUPS: tuple[tuple[str, str, str], ...] = (
    ("identity", "角色与人设", "开场角色、人设块，以及专属人设时的说明。"),
    ("rules", "规则与安全", "私聊行为规则、安全与保密边界。"),
    ("context", "资料与本轮", "用户资料表头与数据、久别提示、本轮发言方式、发言判断。"),
    ("files", "附件与电脑", "虚拟电脑、能力包、附件说明与文件兜底。"),
    ("team", "后台与团队", "派活、团队留言板、自动转后台、汇报与协调。"),
    ("worker", "后台小助手", "子智能体的角色、规则、任务单、续期与收尾。"),
    ("memory", "记忆整理", "私聊记忆整理模型的 system 提示。"),
    ("tools", "工具描述", "私聊工具与小助手工具的 description。"),
)
GROUP_LABELS = {key: label for key, label, _hint in GROUPS}

_GROUP_OF: dict[str, str] = {
    "private.role": "identity",
    "private.persona": "identity",
    "private.persona_scope": "identity",
    "private.rules": "rules",
    "private.safety": "rules",
    "private.final_round": "rules",
    "private.context_header": "context",
    "private.context": "context",
    "private.time_note": "context",
    "private.turn_style": "context",
    "private.turn_taking": "context",
    "private.resumed_note": "context",
    "private.media": "files",
    "private.toolkits": "files",
    "private.files_hint": "files",
    "private.attachments_unread": "files",
    "private.attachments_missing": "files",
    "private.attachment_excerpt": "files",
    "private.delegation": "team",
    "private.team": "team",
    "private.just_finished": "team",
    "private.handoff_nudge": "team",
    "private.auto_handoff": "team",
    "private.handoff_findings": "team",
    "private.report_turn": "team",
    "private.report_notice": "team",
    "private.coordinate_turn": "team",
    "private.coordinate_notice": "team",
    "private.memory_system": "memory",
}


def section_group(section_id: str) -> str:
    if section_id.startswith("ptool."):
        return "tools"
    if section_id.startswith("private.worker_"):
        return "worker"
    return _GROUP_OF.get(section_id, "context")


# ── 链路 ────────────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class PipelineStep:
    section_id: str
    where: str  # system#1 / system#2 / user / round / tools
    condition: str = ""


@dataclass(frozen=True, slots=True)
class Pipeline:
    key: str
    label: str
    description: str
    trigger: str
    steps: tuple[PipelineStep, ...]
    tools: str = ""  # main / worker / tasks_only / none


_S = PipelineStep
_CORE_STEPS = (
    _S("private.role", "system#1"),
    _S("private.persona", "system#1"),
    _S("private.persona_scope", "system#1", "这位用户有专属人设"),
    _S("private.rules", "system#1"),
    _S("private.turn_taking", "system#1", "发言判断开启"),
    _S("private.media", "system#1", "虚拟电脑可用"),
    _S("private.toolkits", "system#1", "虚拟电脑可用"),
    _S("private.delegation", "system#1", "小助手任务开启"),
    _S("private.team", "system#1", "小助手任务开启"),
    _S("private.just_finished", "system#1", "有刚做完、还没汇报的结果"),
    _S("private.safety", "system#1"),
    _S("private.context_header", "system#2"),
    _S("private.context", "system#2"),
    _S("private.time_note", "system#2", "久别重聊"),
    _S("private.turn_style", "system#2"),
)

PIPELINES: tuple[Pipeline, ...] = (
    Pipeline(
        "turn",
        "主回合",
        "用户发来消息时的一轮：两层 system + 近期历史 + 本条消息，然后进工具循环。",
        "用户私聊发消息",
        (
            *_CORE_STEPS,
            _S("private.files_hint", "system#2", "发了文件且小助手任务开启"),
            _S("private.attachments_unread", "system#2", "附件没进电脑但读出了文字"),
            _S("private.attachments_missing", "system#2", "附件没能放进电脑"),
            _S("private.resumed_note", "round", "【等他说完】计时结束自动接话"),
            _S(
                "private.attachment_excerpt",
                "history",
                "文件没进电脑但读出了文字（写进历史）",
            ),
            _S("private.handoff_nudge", "round", "重活工具次数到阈值（每轮最多一次）"),
            _S("private.auto_handoff", "round", "工具轮次用完且自动转后台成功"),
            _S("private.final_round", "round", "工具轮次用完"),
            _S("private.handoff_findings", "worker", "自动转后台时写进任务单"),
        ),
        tools="main",
    ),
    Pipeline(
        "report",
        "汇报",
        "后台小助手做完时，主智能体把结果告诉用户的一轮。",
        "小助手任务结束",
        (
            *_CORE_STEPS,
            _S("private.report_turn", "round", "插在最后一条之前"),
            _S("private.report_notice", "user"),
        ),
        tools="main",
    ),
    Pipeline(
        "coordinate",
        "协调",
        "小助手在团队留言板上发来需要回复的消息时，主智能体回它的一轮（只给 tasks 工具）。",
        "小助手发来需要回复的消息",
        (
            *(
                step
                for step in _CORE_STEPS
                if step.section_id not in {"private.media", "private.toolkits"}
            ),
            _S("private.coordinate_turn", "round", "插在最后一条之前"),
            _S("private.coordinate_notice", "user"),
        ),
        tools="tasks_only",
    ),
    Pipeline(
        "worker",
        "后台小助手",
        "子智能体只看任务单：自己的 system + 任务单 + 工作台，用 web/computer/toolbox 干活。",
        "派出小助手任务",
        (
            _S("private.worker_role", "system#1"),
            _S("private.worker_rules", "system#1"),
            _S("private.worker_voice", "system#1"),
            _S("private.worker_team", "system#1"),
            _S("private.worker_safety", "system#1"),
            _S("private.worker_brief", "user"),
            _S("private.handoff_findings", "user", "自动转后台的任务"),
            _S("private.worker_inbox", "round", "团队留言板有新消息"),
            _S("private.worker_answer", "round", "收到用户回答"),
            _S("private.worker_workbench", "round", "上下文压缩或续期"),
            _S("private.worker_continue", "round", "轮次用完但还有续期"),
            _S("private.worker_final", "round", "轮次用完"),
        ),
        tools="worker",
    ),
    Pipeline(
        "memory",
        "记忆整理",
        "一段私聊结束后整理成私聊记忆（scope=private），只有一条 system。",
        "私聊回合结束后",
        (_S("private.memory_system", "system#1"),),
        tools="none",
    ),
)
PIPELINE_INDEX = {item.key: item for item in PIPELINES}


def _tool_where(section: PromptSection) -> str:
    return "worker" if "后台" in section.description else "main"


def pipelines_of(section_id: str) -> list[dict[str, str]]:
    """反查：这个分段出现在哪些链路、什么条件下。"""

    found = []
    for pipe in PIPELINES:
        for step in pipe.steps:
            if step.section_id == section_id:
                found.append(
                    {
                        "pipeline": pipe.key,
                        "label": pipe.label,
                        "where": step.where,
                        "condition": step.condition,
                    }
                )
    if section_id.startswith("ptool."):
        section = get_section(section_id)
        where = _tool_where(section) if section is not None else "main"
        targets = ("worker",) if where == "worker" else ("turn", "report")
        if section_id == "ptool.tasks":
            targets = (*targets, "coordinate")
        for key in targets:
            found.append(
                {
                    "pipeline": key,
                    "label": PIPELINE_INDEX[key].label,
                    "where": "tools",
                    "condition": "本轮装载该工具",
                }
            )
    return found


# ── 目录 ────────────────────────────────────────────────────────────────


def private_sections() -> list[PromptSection]:
    return [
        *(section for section in SECTIONS if is_private_section(section.id)),
        *private_tool_sections(),
    ]


def _payload(override: Override | None) -> dict[str, Any] | None:
    return override.as_dict() if override is not None else None


def _base_text(section: PromptSection) -> tuple[str | None, str]:
    default = section_default(section)
    sample = section_sample(section.id)
    return default, default if default is not None else (sample or {}).get("text") or ""


def override_counts() -> dict[str, int]:
    """每个分段被多少位用户单独改过。"""

    counts: dict[str, int] = {}
    for entries in user_scopes().values():
        for section_id in entries:
            counts[section_id] = counts.get(section_id, 0) + 1
    return counts


def catalog(user_id: int | None = None) -> dict[str, Any]:
    snapshot = store_snapshot()
    global_entries = scope_overrides(None)
    user_entries = scope_overrides(None, user_id=user_id) if user_id else {}
    counts = override_counts()
    rows: list[dict[str, Any]] = []
    for section in private_sections():
        default, base = _base_text(section)
        resolved = (
            resolve_override(section.id, user_id=user_id)
            if user_id
            else _global_only(section.id)
        )
        effective = compose(base, resolved.override)
        sample = section_sample(section.id)
        rows.append(
            {
                "id": section.id,
                "title": section.title,
                "group": section_group(section.id),
                "groupLabel": GROUP_LABELS.get(section_group(section.id), ""),
                "category": section.category,
                "layer": section.layer,
                "layerLabel": LAYER_LABELS.get(section.layer, section.layer),
                "kind": section.kind,
                "description": section.description,
                "when": section.when,
                "safety": section.safety,
                "protocol": section.protocol,
                "editable": section.editable,
                "modes": list(section.modes),
                "defaultText": default,
                "defaultTokens": estimate_tokens(default) if default else 0,
                "sample": sample,
                "globalOverride": _payload(global_entries.get(section.id)),
                "userOverride": _payload(user_entries.get(section.id)),
                "userOverrideCount": counts.get(section.id, 0),
                "effectiveScope": resolved.scope,
                "effectiveMode": resolved.override.mode
                if resolved.override is not None
                else "default",
                "effectiveText": effective if (default is not None or sample) else None,
                "effectiveTokens": estimate_tokens(effective) if effective else 0,
                "pipelines": pipelines_of(section.id),
            }
        )
    by_id = {row["id"]: row for row in rows}
    return {
        "revision": snapshot.get("revision", 0),
        "updatedAt": snapshot.get("updatedAt"),
        "loadError": snapshot.get("loadError"),
        "userId": str(user_id) if user_id else None,
        "groups": [
            {"key": key, "label": label, "hint": hint} for key, label, hint in GROUPS
        ],
        "pipelines": [pipeline_payload(pipe, by_id) for pipe in PIPELINES],
        "sections": rows,
        "issues": health(user_id),
        "summary": {
            "total": len(rows),
            "globalOverrides": sum(
                1 for key in global_entries if is_private_section(key)
            ),
            "userOverrides": len(user_entries),
            "usersWithOverrides": len(user_scopes()),
            "disabled": sum(1 for row in rows if row["effectiveMode"] == MODE_DISABLE),
            "safetyChanged": sum(
                1
                for row in rows
                if row["safety"]
                and row["effectiveMode"] in {MODE_DISABLE, MODE_REPLACE}
            ),
        },
    }


def _global_only(section_id: str) -> Any:
    # 不带用户：只看全局层（不受当前 contextvar 影响）。
    with use_private_user(None):
        return resolve_override(section_id)


def pipeline_payload(
    pipe: Pipeline, by_id: Mapping[str, Mapping[str, Any]]
) -> dict[str, Any]:
    steps = []
    always = 0
    conditional = 0
    for step in pipe.steps:
        row = by_id.get(step.section_id) or {}
        tokens = int(row.get("effectiveTokens") or 0)
        if step.condition:
            conditional += tokens
        else:
            always += tokens
        steps.append(
            {
                "sectionId": step.section_id,
                "title": row.get("title") or step.section_id,
                "where": step.where,
                "condition": step.condition,
                "mode": row.get("effectiveMode") or "default",
                "scope": row.get("effectiveScope"),
                "safety": bool(row.get("safety")),
                "tokens": tokens,
            }
        )
    tool_rows = [
        row
        for row in by_id.values()
        if str(row.get("id", "")).startswith("ptool.")
        and any(item["pipeline"] == pipe.key for item in row.get("pipelines") or [])
    ]
    return {
        "key": pipe.key,
        "label": pipe.label,
        "description": pipe.description,
        "trigger": pipe.trigger,
        "tools": pipe.tools,
        "steps": steps,
        "toolSections": [row["id"] for row in tool_rows],
        "tokens": {
            "always": always,
            "conditional": conditional,
            "tools": sum(int(row.get("effectiveTokens") or 0) for row in tool_rows),
        },
    }


# ── 体检 ────────────────────────────────────────────────────────────────

_PLACEHOLDERS = {"private.attachment_excerpt": ("{title}", "{text}")}
_BLOAT_FACTOR = 3
_BLOAT_MIN_CHARS = 400


def _issue(
    level: str, code: str, section_id: str, message: str, scope: str
) -> dict[str, str]:
    return {
        "level": level,
        "code": code,
        "sectionId": section_id,
        "message": message,
        "scope": scope,
    }


def _check_override(
    section: PromptSection, override: Override, default: str | None, scope: str
) -> list[dict[str, str]]:
    issues: list[dict[str, str]] = []
    sid = section.id
    if section.safety and override.mode in {MODE_DISABLE, MODE_REPLACE}:
        issues.append(
            _issue(
                "danger",
                "safety_changed",
                sid,
                "安全段被删除或替换，模型不再看到这些约束（代码侧校验仍生效）",
                scope,
            )
        )
    if section.protocol and override.mode in {MODE_DISABLE, MODE_REPLACE}:
        issues.append(
            _issue(
                "warning",
                "protocol_changed",
                sid,
                "协议段被改，模型可能不再按约定格式输出",
                scope,
            )
        )
    if (
        override.mode in {MODE_REPLACE, MODE_APPEND, MODE_PREPEND}
        and not override.text.strip()
    ):
        issues.append(_issue("warning", "empty_text", sid, "覆盖文字为空", scope))
    if (
        override.mode == MODE_REPLACE
        and default is not None
        and override.text.strip() == default.strip()
    ):
        issues.append(
            _issue(
                "info",
                "same_as_default",
                sid,
                "替换文字和默认值完全相同，可以删掉这条覆盖",
                scope,
            )
        )
    if override.mode == MODE_REPLACE:
        for token in _PLACEHOLDERS.get(sid, ()):
            if token not in override.text:
                issues.append(
                    _issue(
                        "danger",
                        "missing_placeholder",
                        sid,
                        f"替换文字缺少占位符 {token}",
                        scope,
                    )
                )
    if (
        default
        and override.mode in {MODE_REPLACE, MODE_APPEND, MODE_PREPEND}
        and len(compose(default, override))
        > max(_BLOAT_MIN_CHARS, len(default) * _BLOAT_FACTOR)
    ):
        issues.append(
            _issue(
                "info",
                "bloated",
                sid,
                f"生效文本超过默认值的 {_BLOAT_FACTOR} 倍，会多占 Token",
                scope,
            )
        )
    return issues


def health(user_id: int | None = None) -> list[dict[str, str]]:
    """全局层与（给了用户时）本人层的问题清单；级别 danger / warning / info。"""

    issues: list[dict[str, str]] = []
    layers: list[tuple[str, dict[str, Override]]] = [
        (GLOBAL_SCOPE, scope_overrides(None))
    ]
    if user_id:
        layers.append((user_scope_key(user_id), scope_overrides(None, user_id=user_id)))
    for scope, entries in layers:
        for section_id, override in entries.items():
            if not is_private_section(section_id):
                continue
            section = get_section(section_id)
            if section is None:
                issues.append(
                    _issue(
                        "warning",
                        "unknown_section",
                        section_id,
                        "这个分段已不存在（工具被删或改名），覆盖不再生效",
                        scope,
                    )
                )
                continue
            issues.extend(
                _check_override(section, override, section_default(section), scope)
            )
    order = {"danger": 0, "warning": 1, "info": 2}
    issues.sort(key=lambda item: (order.get(item["level"], 3), item["sectionId"]))
    return issues


# ── 用户层管理 ──────────────────────────────────────────────────────────


def users_with_overrides() -> list[dict[str, Any]]:
    rows = []
    for uid, entries in user_scopes().items():
        latest = max((item.updated_at for item in entries.values()), default="")
        rows.append(
            {
                "userId": str(uid),
                "count": len(entries),
                "sections": sorted(entries),
                "safety": sum(
                    1
                    for sid, item in entries.items()
                    if (sec := get_section(sid)) is not None
                    and sec.safety
                    and item.mode in {MODE_DISABLE, MODE_REPLACE}
                ),
                "updatedAt": latest or None,
            }
        )
    rows.sort(key=lambda row: row["updatedAt"] or "", reverse=True)
    return rows


def promote_user(
    user_id: int,
    section_ids: list[str] | None,
    *,
    actor: str = "",
    expected_revision: int | None = None,
    confirm_safety: bool = False,
    clear_user: bool = True,
) -> dict[str, Any]:
    """把这位用户的覆盖提升为全局（可选只提升部分分段），默认同时从本人层删掉。"""

    entries = scope_overrides(None, user_id=user_id)
    chosen = [sid for sid in (section_ids or list(entries)) if sid in entries]
    if not chosen:
        return {"changed": 0, "promoted": []}
    result = bulk_apply(
        [
            {
                "sectionId": sid,
                "mode": entries[sid].mode,
                "text": entries[sid].text,
                "note": entries[sid].note,
            }
            for sid in chosen
        ],
        group_id=None,
        actor=actor,
        expected_revision=expected_revision,
        confirm_safety=confirm_safety,
    )
    if clear_user:
        result = bulk_apply(
            [{"sectionId": sid, "mode": "inherit"} for sid in chosen],
            group_id=None,
            user_id=user_id,
            actor=actor,
            expected_revision=result["revision"],
        )
    return {**result, "promoted": chosen}


def reset_user(
    user_id: int, *, actor: str = "", expected_revision: int | None = None
) -> dict[str, Any]:
    return bulk_apply(
        [],
        group_id=None,
        user_id=user_id,
        actor=actor,
        expected_revision=expected_revision,
        replace_scope=True,
    )


# ── 预览 ────────────────────────────────────────────────────────────────


async def preview(
    session: Any,
    user_id: int,
    *,
    message: str = "",
    global_drafts: Mapping[str, Mapping[str, Any]] | None = None,
    user_drafts: Mapping[str, Mapping[str, Any]] | None = None,
    persona_card: Any = None,
    computer: bool = False,
    tasks: bool | None = None,
    allow_silence: bool | None = None,
    include_history: bool = True,
) -> dict[str, Any]:
    """拼出这位用户主回合将要发给模型的完整请求（不发送、不写库、不记样例）。"""

    from ..private_chat_bridge import PrivateTurnRequest  # noqa: TID252
    from . import private_turn_taking
    from .context import now_beijing
    from .private_agent import (
        _is_superuser,
        _recent_history,
        build_messages,
        turn_style,
    )
    from .private_context import build_private_context
    from .private_persona import resolve_private_persona, resolve_user_persona
    from .private_store import load as load_record
    from .private_tasks import tasks_enabled
    from .private_tools import tool_schemas
    from .prompt_capture import build_snapshot

    now = now_beijing()
    use_tasks = tasks_enabled() if tasks is None else bool(tasks)
    silence = (
        private_turn_taking.enabled() if allow_silence is None else bool(allow_silence)
    )
    text = str(message or "").strip()[:2000] or "（预览：这里是用户发来的消息）"
    layers: dict[str, Mapping[str, Mapping[str, Any]]] = {}
    if global_drafts:
        layers[GLOBAL_SCOPE] = global_drafts
    if user_drafts:
        layers[user_scope_key(user_id)] = user_drafts
    with use_private_user(user_id, pipeline="turn"), use_draft_scopes(layers):
        context = await build_private_context(
            session, user_id, bot_id=None, nickname=None, now=now
        )
        history = await _recent_history(session, user_id) if include_history else []
        request = PrivateTurnRequest(
            bot=None,
            event=None,
            user_id=user_id,
            user_input=text,
            raw_text=text,
            history=[*history, {"role": "user", "content": text}],
            session=session,
        )
        persona = resolve_private_persona(user_id, draft=persona_card)
        emoji_pref = (load_record(user_id).get("prefs") or {}).get("emoji")
        messages = build_messages(
            persona,
            context.render(),
            request,
            computer=computer,
            tasks=use_tasks,
            turn_style=turn_style(user_id, now, emoji_pref=emoji_pref, persona=persona),
            allow_silence=silence,
        )
        tools = tool_schemas(
            is_superuser=_is_superuser(user_id), computer=computer, tasks=use_tasks
        )
        snapshot = build_snapshot(
            {
                "kind": "preview",
                "task": "agent_dialogue",
                "messages": messages,
                "tools": tools,
                "params": {},
                "outcome": "not_sent",
                "duration_ms": 0.0,
            },
            round_index=1,
        )
    resolved = resolve_user_persona(user_id)
    snapshot["userId"] = str(user_id)
    snapshot["pipeline"] = "turn"
    snapshot["persona"] = {
        "source": "draft" if persona_card is not None else resolved.source,
        "label": persona.get("source_label") or resolved.label,
        "name": persona.get("name"),
        "templateName": resolved.template_name if persona_card is None else None,
    }
    snapshot["flags"] = {
        "computer": computer,
        "tasks": use_tasks,
        "allowSilence": silence,
        "history": len(history),
    }
    return snapshot


__all__ = [
    "GROUPS",
    "PIPELINES",
    "Pipeline",
    "PipelineStep",
    "catalog",
    "health",
    "override_counts",
    "pipelines_of",
    "preview",
    "private_sections",
    "promote_user",
    "reset_user",
    "section_group",
    "users_with_overrides",
]
