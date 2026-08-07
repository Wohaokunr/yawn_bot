"""写作规范 linter：把 modules/README.md 的约定变成可见诊断。

结构错误（悬空引用、类型不对）由 validate.py 借引擎 schema 拦截；
本层专管「合法但违背约定」的写法，每条标注 README 出处。
"""

from __future__ import annotations

import re
from typing import Any, Optional

from .schema_loader import (
    NPC,
    CheckPoint,
    Clue,
    Ending,
    Exit,
    ModuleDef,
    Monster,
    PlotEvent,
    Scene,
    ScheduleEntry,
    TimeConfig,
)
from .state import condition_fields, get_list
from .validate import SEVERITY_ERROR, SEVERITY_INFO, SEVERITY_WARNING, Issue

_ASCII_SNAKE_RE = re.compile(r"^[a-z][a-z0-9_]*$")
_DIGIT_RE = re.compile(r"[0-9０-９]")

# 各层实体的合法键（YAML 侧键名；ScheduleEntry 兼容 from / frm）
_KNOWN_KEYS: dict[type, set[str]] = {
    ModuleDef: set(ModuleDef.model_fields),
    Scene: set(Scene.model_fields),
    CheckPoint: set(CheckPoint.model_fields),
    Exit: set(Exit.model_fields),
    NPC: set(NPC.model_fields),
    Monster: set(Monster.model_fields),
    Clue: set(Clue.model_fields),
    Ending: set(Ending.model_fields),
    PlotEvent: set(PlotEvent.model_fields),
    TimeConfig: set(TimeConfig.model_fields),
    ScheduleEntry: {"from", "frm", "to", "scene", "activity", "condition", "away"},
}

# 引擎发言关键词扫描写入的 flag（作者只能消费，不能配置）
_ENGINE_FLAGS = ("arson", "threat", "destroy", "assault", "murder", "npc_dead")


def _tag(item: Any, fallback: str = "?") -> str:
    if isinstance(item, dict):
        ident = item.get("id", "")
        name = item.get("name", "")
        if ident and name:
            return f"〈{ident} {name}〉"
        return f"〈{ident or name or fallback}〉"
    return f"〈{fallback}〉"


def _issue(
    severity: str, section: str, path_label: str, message: str, hint: str = ""
) -> Issue:
    return Issue(
        severity=severity,
        section=section,
        path_label=path_label,
        message=message,
        hint=hint,
    )


def _digits_issue(
    section: str, path_label: str, text: str, field_name: str
) -> Optional[Issue]:
    if _DIGIT_RE.search(text):
        return _issue(
            SEVERITY_WARNING,
            section,
            f"{path_label} › {field_name}",
            "播报文案含阿拉伯数字：一切数值应由系统播报，KP 硬规则禁止数字",
            hint="README 规则 8",
        )
    return None


def _check_id_lint(data: dict[str, Any], issues: list[Issue]) -> None:
    """规则 1：所有 id 一律 ASCII snake_case。"""
    targets: list[tuple[str, str, str, Any]] = [
        ("模组", "顶层", "id", data.get("id", "")),
    ]
    for section, key in (
        ("场景", "scenes"),
        ("NPC", "npcs"),
        ("怪物", "monsters"),
        ("线索", "clues"),
        ("结局", "endings"),
        ("事件", "events"),
    ):
        targets.extend(
            (section, f"{section}{_tag(item)}", "id", item.get("id", ""))
            for item in get_list(data, key)
            if isinstance(item, dict)
        )
    for scene in get_list(data, "scenes"):
        if not isinstance(scene, dict):
            continue
        targets.extend(
            (
                "场景",
                f"场景{_tag(scene)} › 检定点{_tag(check)}",
                "id",
                check.get("id", ""),
            )
            for check in get_list(scene, "checks")
            if isinstance(check, dict)
        )
    for section, label, _field, value in targets:
        if not isinstance(value, str) or not _ASCII_SNAKE_RE.match(value):
            issues.append(
                _issue(
                    SEVERITY_ERROR,
                    section,
                    f"{label} › id",
                    f"id 应为 ASCII snake_case（小写字母/数字/下划线），当前 {value!r}",
                    hint="README 规则 1",
                )
            )


def _unknown_keys_lint(  # noqa: C901,PLR0912
    data: dict[str, Any], issues: list[Issue]
) -> None:
    """未知键会被 pydantic 静默忽略——报 ERROR 把拼写错误钉出来。"""

    def check(
        entity: dict[str, Any], model_cls: type, label: str, section: str
    ) -> None:
        known = _KNOWN_KEYS[model_cls]
        issues.extend(
            _issue(
                SEVERITY_ERROR,
                section,
                f"{label} › {key}",
                f"未知键 {key!r}：加载时会被静默忽略，请检查拼写",
            )
            for key in entity
            if key not in known
        )

    check(data, ModuleDef, "顶层", "模组")
    time_block = data.get("time")
    if isinstance(time_block, dict):
        check(time_block, TimeConfig, "time", "模组")
    for scene in get_list(data, "scenes"):
        if not isinstance(scene, dict):
            continue
        label = f"场景{_tag(scene)}"
        check(scene, Scene, label, "场景")
        for i, chk in enumerate(get_list(scene, "checks")):
            if isinstance(chk, dict):
                check(chk, CheckPoint, f"{label} › 检定点 #{i + 1}", "场景")
        for i, exit_ in enumerate(get_list(scene, "exits")):
            if isinstance(exit_, dict):
                check(exit_, Exit, f"{label} › 出口 #{i + 1}", "场景")
    for npc in get_list(data, "npcs"):
        if not isinstance(npc, dict):
            continue
        label = f"NPC{_tag(npc)}"
        check(npc, NPC, label, "NPC")
        for i, entry in enumerate(get_list(npc, "schedule")):
            if isinstance(entry, dict):
                check(entry, ScheduleEntry, f"{label} › 行程 #{i + 1}", "NPC")
    for i, monster in enumerate(get_list(data, "monsters")):
        if isinstance(monster, dict):
            check(monster, Monster, f"怪物{_tag(monster, f'#{i + 1}')}", "怪物")
    for i, clue in enumerate(get_list(data, "clues")):
        if isinstance(clue, dict):
            check(clue, Clue, f"线索{_tag(clue, f'#{i + 1}')}", "线索")
    for i, ending in enumerate(get_list(data, "endings")):
        if isinstance(ending, dict):
            check(ending, Ending, f"结局{_tag(ending, f'#{i + 1}')}", "结局")
    for i, event in enumerate(get_list(data, "events")):
        if isinstance(event, dict):
            check(event, PlotEvent, f"事件{_tag(event, f'#{i + 1}')}", "事件")


def _narration_digits_lint(  # noqa: C901,PLR0912
    data: dict[str, Any], issues: list[Issue]
) -> None:
    """规则 8：播报类文案不出现数字。"""
    opening = data.get("opening")
    if isinstance(opening, str):
        issue = _digits_issue("模组", "顶层", opening, "opening")
        if issue:
            issues.append(issue)
    for clue in get_list(data, "clues"):
        if isinstance(clue, dict):
            text = clue.get("text")
            if isinstance(text, str):
                issue = _digits_issue("线索", f"线索{_tag(clue)}", text, "text")
                if issue:
                    issues.append(issue)
    for ending in get_list(data, "endings"):
        if isinstance(ending, dict):
            text = ending.get("text")
            if isinstance(text, str):
                issue = _digits_issue("结局", f"结局{_tag(ending)}", text, "text")
                if issue:
                    issues.append(issue)
    for scene in get_list(data, "scenes"):
        if not isinstance(scene, dict):
            continue
        label = f"场景{_tag(scene)}"
        for field_name in ("narration", "idle_narration"):
            text = scene.get(field_name)
            if isinstance(text, str):
                issue = _digits_issue("场景", label, text, field_name)
                if issue:
                    issues.append(issue)
        for i, chk in enumerate(get_list(scene, "checks")):
            if not isinstance(chk, dict):
                continue
            for field_name in ("success_text", "failure_text"):
                text = chk.get(field_name)
                if isinstance(text, str):
                    issue = _digits_issue(
                        "场景", f"{label} › 检定点 #{i + 1}", text, field_name
                    )
                    if issue:
                        issues.append(issue)
        for i, exit_ in enumerate(get_list(scene, "exits")):
            if isinstance(exit_, dict):
                text = exit_.get("narration")
                if isinstance(text, str):
                    issue = _digits_issue(
                        "场景", f"{label} › 出口 #{i + 1}", text, "narration"
                    )
                    if issue:
                        issues.append(issue)


def _check_rules_lint(data: dict[str, Any], issues: list[Issue]) -> None:  # noqa: C901
    """规则 3/4：SAN 检点优先级与 once；线索/伤害检点 once；双文案齐全。"""
    for scene in get_list(data, "scenes"):
        if not isinstance(scene, dict):
            continue
        label = f"场景{_tag(scene)}"
        for i, chk in enumerate(get_list(scene, "checks")):
            if not isinstance(chk, dict):
                continue
            where = f"{label} › 检定点 #{i + 1}{_tag(chk)}"
            skill = chk.get("skill")
            once = chk.get("once", False)
            if skill == "san":
                if chk.get("priority", 0) < 1:
                    issues.append(
                        _issue(
                            SEVERITY_WARNING,
                            "场景",
                            where,
                            "SAN 检点建议 priority: 1（与搜索类检点同时命中时优先）",
                            hint="README 规则 3",
                        )
                    )
                if not once:
                    issues.append(
                        _issue(
                            SEVERITY_WARNING,
                            "场景",
                            where,
                            "SAN 检点务必 once: true（同一刺激不应反复扣 SAN）",
                            hint="README 规则 4",
                        )
                    )
                if not chk.get("triggers"):
                    issues.append(
                        _issue(
                            SEVERITY_WARNING,
                            "场景",
                            where,
                            "SAN 检点触发词为空：建议给宽触发词（范例连「看」都写）",
                            hint="README 规则 3",
                        )
                    )
            elif chk.get("clue") or chk.get("damage_on_fail"):
                if not once:
                    issues.append(
                        _issue(
                            SEVERITY_WARNING,
                            "场景",
                            where,
                            "发放线索/伤害的检点务必 once: true",
                            hint="README 规则 4",
                        )
                    )
            if not chk.get("failure_text"):
                issues.append(
                    _issue(
                        SEVERITY_WARNING,
                        "场景",
                        where,
                        "failure_text 为空：失败文案也请写全",
                        hint="README 规则 4",
                    )
                )
            if skill == "cthulhu_mythos":
                issues.append(
                    _issue(
                        SEVERITY_WARNING,
                        "场景",
                        where,
                        "克苏鲁神话不能由 KP 主动检定，写进检定点无意义",
                        hint="README checks 节",
                    )
                )


def _exit_rules_lint(data: dict[str, Any], issues: list[Issue]) -> None:
    """规则 5：上锁出口配开锁文案；auto 与 condition 不同时用。"""
    for scene in get_list(data, "scenes"):
        if not isinstance(scene, dict):
            continue
        label = f"场景{_tag(scene)}"
        for i, exit_ in enumerate(get_list(scene, "exits")):
            if not isinstance(exit_, dict):
                continue
            where = f"{label} › 出口 #{i + 1}"
            if exit_.get("condition") and not exit_.get("narration"):
                issues.append(
                    _issue(
                        SEVERITY_WARNING,
                        "场景",
                        where,
                        "上锁出口建议写 narration（开锁瞬间的过渡描述）",
                        hint="README 规则 5",
                    )
                )
            if exit_.get("auto") and exit_.get("condition"):
                issues.append(
                    _issue(
                        SEVERITY_WARNING,
                        "场景",
                        where,
                        "auto 出口只用于无条件序幕走廊；"
                        "带 condition 的自动切景易生歧义",
                        hint="README exits 节",
                    )
                )


def _schedule_rules_lint(data: dict[str, Any], issues: list[Issue]) -> None:
    """规则 6：行程以无条件兜底条目收尾；全天条目之后不得有条目。"""
    scheduled_ids: set[str] = set()
    for npc in get_list(data, "npcs"):
        if not isinstance(npc, dict):
            continue
        schedule = get_list(npc, "schedule")
        if not schedule:
            continue
        scheduled_ids.add(str(npc.get("id", "")))
        label = f"NPC{_tag(npc)}"
        last = schedule[-1] if isinstance(schedule[-1], dict) else None
        if last is not None and last.get("condition"):
            issues.append(
                _issue(
                    SEVERITY_WARNING,
                    "NPC",
                    f"{label} › 行程",
                    "行程未以无条件兜底条目收尾：全部条目不匹配时 NPC 不在场",
                    hint="README 规则 6",
                )
            )
        for i, entry in enumerate(schedule[:-1]):
            if not isinstance(entry, dict):
                continue
            frm = str(entry.get("from", entry.get("frm", "")))
            to = str(entry.get("to", ""))
            # from==to 且无条件 = 全天恒匹配，其后条目永不可达
            if frm and frm == to and not entry.get("condition"):
                issues.append(
                    _issue(
                        SEVERITY_WARNING,
                        "NPC",
                        f"{label} › 行程 #{i + 1}",
                        f"全天窗口（from == to == {frm}）且无条件：其后条目永不可达",
                        hint="README 规则 6",
                    )
                )
    # 静态在场列表对有行程的 NPC 无效
    for scene in get_list(data, "scenes"):
        if not isinstance(scene, dict):
            continue
        issues.extend(
            _issue(
                SEVERITY_WARNING,
                "场景",
                f"场景{_tag(scene)} › npcs",
                f"NPC {npc_id} 已有行程表：scene.npcs 对其不生效",
                hint="README scenes 节",
            )
            for npc_id in get_list(scene, "npcs")
            if isinstance(npc_id, str) and npc_id in scheduled_ids
        )


def _ending_rules_lint(data: dict[str, Any], issues: list[Issue]) -> None:
    """规则 7：结局 id 查重（补 schema 缺口）+ 名称齐全 + 时间兜底居末。"""
    endings = get_list(data, "endings")
    seen: dict[str, int] = {}
    for i, ending in enumerate(endings):
        if isinstance(ending, dict):
            ident = str(ending.get("id", ""))
            if ident in seen:
                issues.append(
                    _issue(
                        SEVERITY_ERROR,
                        "结局",
                        f"结局 #{i + 1}{_tag(ending)}",
                        f"结局 id 重复：{ident}（与结局 #{seen[ident] + 1} 冲突）",
                    )
                )
            else:
                seen[ident] = i
    time_term = re.compile(r"\btime_(after|before|between):")
    for i, ending in enumerate(endings):
        if not isinstance(ending, dict):
            continue
        where = f"结局 #{i + 1}{_tag(ending)}"
        if not ending.get("name") or not ending.get("summary"):
            issues.append(
                _issue(
                    SEVERITY_WARNING,
                    "结局",
                    where,
                    "结局应写全 name 与 summary"
                    "（summary 按导演指引写，仅 query_story 可见）",
                    hint="README 规则 7",
                )
            )
        condition = str(ending.get("condition", ""))
        if time_term.search(condition) and i < len(endings) - 1:
            issues.append(
                _issue(
                    SEVERITY_WARNING,
                    "结局",
                    where,
                    "时间兜底结局应声明在最后：声明序即优先级，会遮蔽其后声明的结局",
                    hint="README 规则 7",
                )
            )


def _graph_lint(  # noqa: C901,PLR0912
    data: dict[str, Any], issues: list[Issue]
) -> None:
    """可达性 / 线索引用图：不可达场景、未使用线索。"""
    scenes = get_list(data, "scenes")
    scene_ids = [str(s.get("id", "")) for s in scenes if isinstance(s, dict)]
    start = data.get("start_scene")
    if isinstance(start, str) and start in scene_ids:
        adjacency: dict[str, list[str]] = {sid: [] for sid in scene_ids}
        for scene in scenes:
            if not isinstance(scene, dict):
                continue
            for exit_ in get_list(scene, "exits"):
                if isinstance(exit_, dict):
                    target = str(exit_.get("to_scene", ""))
                    if target in adjacency:
                        adjacency[str(scene.get("id", ""))].append(target)
        reachable: set[str] = set()
        queue = [start]
        while queue:
            current = queue.pop()
            if current in reachable:
                continue
            reachable.add(current)
            queue.extend(adjacency.get(current, []))
        for scene in scenes:
            if isinstance(scene, dict):
                sid = str(scene.get("id", ""))
                if sid not in reachable:
                    issues.append(
                        _issue(
                            SEVERITY_WARNING,
                            "场景",
                            f"场景{_tag(scene)}",
                            "自起始场景经出口不可达：玩家永远到不了这里",
                        )
                    )
    # 未使用线索
    referenced: set[str] = set()
    for scene in scenes:
        if not isinstance(scene, dict):
            continue
        for chk in get_list(scene, "checks"):
            if isinstance(chk, dict) and chk.get("clue"):
                referenced.add(str(chk["clue"]))
    for key in ("monsters", "npcs"):
        for item in get_list(data, key):
            if isinstance(item, dict) and item.get("on_death_clue"):
                referenced.add(str(item["on_death_clue"]))
    clue_term = re.compile(r"\bclues?:([a-z0-9_+]+)")
    for _where, host in condition_fields(data):
        condition = host.get("condition")
        if isinstance(condition, str):
            for match in clue_term.finditer(condition):
                referenced.update(part for part in match.group(1).split("+") if part)
    for clue in get_list(data, "clues"):
        if isinstance(clue, dict):
            ident = str(clue.get("id", ""))
            if ident and ident not in referenced:
                issues.append(
                    _issue(
                        SEVERITY_WARNING,
                        "线索",
                        f"线索{_tag(clue)}",
                        "该线索无任何获取途径"
                        "（检定点奖励 / 死亡线索 / 条件引用均未见）",
                    )
                )


def _misc_lint(data: dict[str, Any], issues: list[Issue]) -> None:
    """杂项：通用结局开关、序幕事件、引擎 flag 提示。"""
    if data.get("generic_endings") is False:
        issues.append(
            _issue(
                SEVERITY_INFO,
                "模组",
                "顶层 › generic_endings",
                "已关闭通用结局安全网：谋杀/纵火/全员倒地等极端行为将无兜底结局",
            )
        )
    issues.extend(
        _issue(
            SEVERITY_INFO,
            "事件",
            f"事件{_tag(event)}",
            "条件为空 = 序幕事件：开局首轮即记入（这是合法用法）",
        )
        for event in get_list(data, "events")
        if isinstance(event, dict) and not str(event.get("condition", "")).strip()
    )
    flag_term = re.compile(r"\bflag:([a-z0-9_]+)")
    for where, host in condition_fields(data):
        condition = host.get("condition")
        if not isinstance(condition, str):
            continue
        for match in flag_term.finditer(condition):
            name = match.group(1)
            if not name.startswith(_ENGINE_FLAGS[:5]) and not name.startswith(
                "npc_dead"
            ):
                issues.append(
                    _issue(
                        SEVERITY_WARNING,
                        "通用",
                        f"{where} › condition",
                        f"flag:{name} 非引擎写入的 flag（引擎只写 arson/threat/destroy/"
                        "assault/murder/npc_dead:*），该条件可能永不成立",
                    )
                )


def run_lint(data: dict[str, Any]) -> list[Issue]:
    """全部规范检查；调用方自行与 validate 结果合并排序。"""
    issues: list[Issue] = []
    _check_id_lint(data, issues)
    _unknown_keys_lint(data, issues)
    _narration_digits_lint(data, issues)
    _check_rules_lint(data, issues)
    _exit_rules_lint(data, issues)
    _schedule_rules_lint(data, issues)
    _ending_rules_lint(data, issues)
    _graph_lint(data, issues)
    _misc_lint(data, issues)
    return issues
