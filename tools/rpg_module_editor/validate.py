"""校验层：把引擎 schema 的 pydantic 校验结果翻译成编辑器诊断。

数据面仍以 dict 为准：``ModuleDef.model_validate`` 只产出错误列表
与（通过时的）只读模型实例，绝不回写 dict。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional

from pydantic import ValidationError

from .schema_loader import (
    ModuleDef,
    is_trivially_true,
    validate_condition,
)

SEVERITY_ERROR = "ERROR"
SEVERITY_WARNING = "WARNING"
SEVERITY_INFO = "INFO"


@dataclass
class Issue:
    """一条校验 / 规范诊断。"""

    severity: str
    section: str  # 归属 Tab（模组/场景/NPC/怪物/线索/结局/事件/通用）
    path_label: str  # 中文定位（「场景〈porch 门廊〉 › 检定点 #2 › san_loss」）
    message: str
    hint: str = ""  # README 出处或修复提示


@dataclass
class ValidationReport:
    """一次完整校验的结果。"""

    issues: list[Issue] = field(default_factory=list)
    module: Optional[ModuleDef] = None  # 全部结构错误通过时才有值

    @property
    def errors(self) -> list[Issue]:
        return [i for i in self.issues if i.severity == SEVERITY_ERROR]

    @property
    def warnings(self) -> list[Issue]:
        return [i for i in self.issues if i.severity == SEVERITY_WARNING]

    @property
    def infos(self) -> list[Issue]:
        return [i for i in self.issues if i.severity == SEVERITY_INFO]


# loc 段 → 中文（字段级标签；实体容器段在 _describe_loc 里再装饰 id/name）
_SEGMENT_LABELS = {
    "scenes": "场景",
    "npcs": "NPC",
    "monsters": "怪物",
    "clues": "线索",
    "endings": "结局",
    "events": "事件",
    "checks": "检定点",
    "exits": "出口",
    "schedule": "行程条目",
    "facts": "私人情报",
    "social_nodes": "社交节点",
    "strategies": "社交策略",
    "time": "时钟",
    "costs": "耗时覆写",
    "frm": "from",
}

# pydantic 内置英文消息 → 中文（schema 自定义错误本就是中文）
_MESSAGE_TRANSLATIONS = {
    "Field required": "必填字段缺失",
    "Input should be a valid string": "应为文本",
    "Input should be a valid integer": "应为整数",
    "Input should be a valid boolean": "应为 true / false",
    "Input should be a valid list": "应为列表",
    "Input should be a valid dictionary": "应为键值映射",
    "Input should be a valid set": "应为集合",
}

_VALUE_ERROR_PREFIX = "Value error, "


def _translate_message(msg: str) -> str:
    if msg.startswith(_VALUE_ERROR_PREFIX):
        return msg[len(_VALUE_ERROR_PREFIX) :]
    for english, chinese in _MESSAGE_TRANSLATIONS.items():
        if msg.startswith(english):
            extra = msg[len(english) :]
            return f"{chinese}{extra}"
    return msg


def _entity_tag(entity: Any) -> str:
    """实体 id/name 装饰：〈porch 门廊〉。"""
    if not isinstance(entity, dict):
        return ""
    ident = entity.get("id", "")
    name = entity.get("name", "")
    if ident and name:
        return f"〈{ident} {name}〉"
    if ident or name:
        return f"〈{ident or name}〉"
    return ""


def describe_loc(loc: tuple[Any, ...], data: Any) -> str:
    """把 pydantic 错误 loc 渲染成中文路径。"""
    parts: list[str] = []
    node = data
    for seg in loc:
        if isinstance(seg, int):
            tag = ""
            child = None
            if isinstance(node, list) and 0 <= seg < len(node):
                child = node[seg]
                tag = _entity_tag(child)
            parts.append(f"#{seg + 1}{tag}")
            node = child
        else:
            key = str(seg)
            parts.append(_SEGMENT_LABELS.get(key, key))
            node = node.get(seg) if isinstance(node, dict) else None
    return " › ".join(parts) if parts else "顶层"


def _section_of(loc: tuple[Any, ...]) -> str:
    """错误归属的 Tab。"""
    if not loc:
        return "模组"
    first = loc[0]
    return {
        "scenes": "场景",
        "npcs": "NPC",
        "monsters": "怪物",
        "clues": "线索",
        "endings": "结局",
        "events": "事件",
        "time": "模组",
    }.get(str(first), "模组")


def validate_structure(data: dict[str, Any]) -> ValidationReport:
    """结构校验（引擎口径）；错误逐条中文化。"""
    report = ValidationReport()
    try:
        report.module = ModuleDef.model_validate(data)
    except ValidationError as e:
        for err in e.errors():
            loc = tuple(err.get("loc", ()))
            report.issues.append(
                Issue(
                    severity=SEVERITY_ERROR,
                    section=_section_of(loc),
                    path_label=describe_loc(loc, data),
                    message=_translate_message(str(err.get("msg", ""))),
                )
            )
    except Exception as e:  # noqa: BLE001 —— 数据极端畸形时给个说法
        report.issues.append(
            Issue(
                severity=SEVERITY_ERROR,
                section="模组",
                path_label="顶层",
                message=f"数据结构无法校验：{e}",
            )
        )
    return report


# ── 条件表达式实时反馈（供 ConditionInput 使用）────────────


def id_sets(data: dict[str, Any]) -> tuple[set[str], set[str], set[str]]:
    """从 dict 现取 (场景 id, 怪物 id, 线索 id) 集合。"""

    def collect(key: str) -> set[str]:
        found = set()
        for item in data.get(key, []) or []:
            if isinstance(item, dict) and isinstance(item.get("id"), str):
                found.add(item["id"])
        return found

    return collect("scenes"), collect("monsters"), collect("clues")


def check_condition(condition: str, data: dict[str, Any]) -> Optional[str]:
    """条件表达式引用校验；合法返回 None。"""
    scenes, monsters, clues = id_sets(data)
    return validate_condition(condition, scenes, monsters, clues)


def check_ending_condition(condition: str, data: dict[str, Any]) -> Optional[str]:
    """结局条件：引用校验 + 恒真拒绝（加载期会拒载）。"""
    err = check_condition(condition, data)
    if err is not None:
        return err
    if is_trivially_true(condition):
        return "条件恒真（空或仅 always）：开局即触发结局安全网，加载会被拒绝"
    return None
