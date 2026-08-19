"""模组 YAML 的加载与序列化管线。

原则：**dict 是唯一持久状态**（YAML 侧键名，行程条目用 ``from``），
pydantic 模型只做校验、永不回写序列化——schema 未开 ``extra="forbid"``，
经模型往返会静默丢掉未知键。目标语义等价而非字节一致：

- 保存即按模型默认值紧凑化（``once: false`` / ``hp: 10`` 等省略）；
- 多行文本一律 ``|`` 字面块（加载时先剥尾随换行，保证往返稳定）；
- 短标量列表走 flow 风格（对齐范例的 ``triggers: [搜索, 查看]``）；
- 行内注释不可避免会丢；开头成块的 ``#`` 头部注释原样保留。
"""

from __future__ import annotations

from enum import Enum
from typing import TYPE_CHECKING, Any, Optional

import yaml
from pydantic import BaseModel

if TYPE_CHECKING:
    from pathlib import Path

from .schema_loader import (
    NPC,
    CheckPoint,
    Clue,
    Deduction,
    Ending,
    Exit,
    ModuleDef,
    Monster,
    NPCFact,
    PlotEvent,
    Scene,
    ScheduleEntry,
    SocialNode,
    SocialStrategy,
    TimeConfig,
)

# (父模型, 字段名) → 列表元素 / 嵌套 dict 的模型类
_CHILD_MODELS: dict[tuple[type, str], type] = {
    (ModuleDef, "scenes"): Scene,
    (ModuleDef, "npcs"): NPC,
    (ModuleDef, "monsters"): Monster,
    (ModuleDef, "clues"): Clue,
    (ModuleDef, "deductions"): Deduction,
    (ModuleDef, "endings"): Ending,
    (ModuleDef, "events"): PlotEvent,
    (ModuleDef, "time"): TimeConfig,
    (Scene, "checks"): CheckPoint,
    (Scene, "exits"): Exit,
    (NPC, "schedule"): ScheduleEntry,
    (NPC, "facts"): NPCFact,
    (NPC, "social_nodes"): SocialNode,
    (SocialNode, "strategies"): SocialStrategy,
}

# flow 风格列表的阈值（对齐范例：triggers / keywords / npcs 等短列表）
_MAX_FLOW_ITEMS = 8
_MAX_FLOW_ITEM_LEN = 16


class ModuleParseError(ValueError):
    """YAML 解析失败（中文诊断）。"""


# ── 加载 ──────────────────────────────────────────────────


def split_header(text: str) -> tuple[str, str]:
    """分离文件开头的连续 ``#`` 注释块；返回 (头部, 正文)。"""
    lines = text.split("\n")
    cut = 0
    while cut < len(lines) and lines[cut].lstrip().startswith("#"):
        cut += 1
    return "\n".join(lines[:cut]), "\n".join(lines[cut:])


def parse_yaml_text(text: str) -> dict[str, Any]:
    """解析 YAML 正文；失败抛 ModuleParseError（中文行列提示）。"""
    try:
        raw = yaml.safe_load(text)
    except yaml.MarkedYAMLError as e:
        mark = e.problem_mark
        pos = f"第 {mark.line + 1} 行第 {mark.column + 1} 列" if mark else "位置未知"
        msg = f"YAML 解析失败（{pos}）：{e.problem}"
        raise ModuleParseError(msg) from e
    except yaml.YAMLError as e:  # pragma: no cover - 非 Marked 类罕见
        msg = f"YAML 解析失败：{e}"
        raise ModuleParseError(msg) from e
    if not isinstance(raw, dict):
        msg = "顶层必须是 YAML 映射（键值对），当前不是"
        raise ModuleParseError(msg)
    return raw


def _coerce_time_value(value: Any) -> Any:
    """YAML 1.1 六十进制陷阱修复：裸写 21:00 会被读成整数 1260。"""
    if isinstance(value, int) and not isinstance(value, bool):
        hours, minutes = divmod(value, 60)
        return f"{hours}:{minutes:02d}"
    return value


def normalize_data(data: dict[str, Any]) -> dict[str, Any]:  # noqa: C901
    """加载后的规范化（幂等）：

    - 多行字符串剥尾随换行（``|`` 块往返稳定的前提）；
    - 已知时间槽的整数还原为 HH:MM（time.start、行程 from/to）。
    """

    def walk(value: Any) -> Any:
        if isinstance(value, dict):
            return {k: walk(v) for k, v in value.items()}
        if isinstance(value, list):
            return [walk(v) for v in value]
        if isinstance(value, str) and "\n" in value:
            return value.rstrip("\n")
        return value

    result = walk(data)
    time_block = result.get("time")
    if isinstance(time_block, dict) and "start" in time_block:
        time_block["start"] = _coerce_time_value(time_block["start"])
    for npc in result.get("npcs", []):
        if not isinstance(npc, dict):
            continue
        for entry in npc.get("schedule", []):
            if not isinstance(entry, dict):
                continue
            for key in ("from", "frm", "to"):
                if key in entry:
                    entry[key] = _coerce_time_value(entry[key])
    return result


def load_module_file(path: Path) -> tuple[dict[str, Any], str]:
    """读取模组文件；返回 (规范化后的数据, 头部注释)。"""
    text = path.read_text(encoding="utf-8")
    header, body = split_header(text)
    data = normalize_data(parse_yaml_text(body))
    return data, header


# ── 默认值裁剪 ────────────────────────────────────────────


def _default_of(info: Any) -> Any:
    """字段默认值的 YAML 侧形态（枚举取值 / 嵌套模型按其自身裁剪）。"""
    default = info.default
    if isinstance(default, BaseModel):
        return strip_defaults(default.model_dump(by_alias=True), type(default))
    if isinstance(default, Enum):
        return default.value
    return default


def strip_defaults(data: dict[str, Any], model_cls: type) -> dict[str, Any]:
    """按模型默认值裁剪实体 dict；未知键原样保留（由 lint 报 ERROR）。"""
    if not isinstance(data, dict):
        return data
    fields = model_cls.model_fields
    result: dict[str, Any] = {}
    for key, raw in data.items():
        info = fields.get(key)
        if info is None:  # 未知键：保留原值，绝不静默丢
            result[key] = raw
            continue
        child_cls = _CHILD_MODELS.get((model_cls, key))
        if child_cls is not None and isinstance(raw, list):
            value: Any = [
                strip_defaults(v, child_cls) if isinstance(v, dict) else v for v in raw
            ]
        elif child_cls is not None and isinstance(raw, dict):
            value = strip_defaults(raw, child_cls)
        else:
            value = raw
        if info.is_required():
            result[key] = value
            continue
        if value == _default_of(info):
            continue  # 与默认一致：省略，输出向范例的紧凑风格看齐
        result[key] = value
    return result


# ── 输出 ──────────────────────────────────────────────────


class _FlowList(list):
    """标记为 flow 风格渲染的标量列表。"""


class ModuleDumper(yaml.SafeDumper):
    """模组专用 Dumper：中文不转义、多行文本字面块、短列表 flow。"""


def _represent_str(dumper: ModuleDumper, data: str) -> yaml.Node:
    if "\n" in data:
        return dumper.represent_scalar("tag:yaml.org,2002:str", data, style="|")
    return dumper.represent_scalar("tag:yaml.org,2002:str", data)


def _represent_flow_list(dumper: ModuleDumper, data: list) -> yaml.Node:
    return dumper.represent_sequence("tag:yaml.org,2002:seq", data, flow_style=True)


ModuleDumper.add_representer(str, _represent_str)
ModuleDumper.add_representer(_FlowList, _represent_flow_list)


def _flowable(value: list) -> bool:
    if not value or len(value) > _MAX_FLOW_ITEMS:
        return False
    for item in value:
        if isinstance(item, str):
            if "\n" in item or len(item) > _MAX_FLOW_ITEM_LEN:
                return False
        elif not isinstance(item, (int, float, bool)):
            return False
    return True


def _prepare_styles(value: Any) -> Any:
    if isinstance(value, dict):
        return {k: _prepare_styles(v) for k, v in value.items()}
    if isinstance(value, list):
        items = [_prepare_styles(v) for v in value]
        return _FlowList(items) if _flowable(items) else items
    return value


def dump_module_text(data: dict[str, Any], header: str = "") -> str:
    """序列化模组数据为 YAML 文本（默认值裁剪 + 风格化处理）。"""
    if not isinstance(data, dict):
        msg = "模组数据顶层必须是映射"
        raise ModuleParseError(msg)
    prepared = _prepare_styles(strip_defaults(data, ModuleDef))
    body = yaml.dump(
        prepared,
        Dumper=ModuleDumper,
        allow_unicode=True,
        sort_keys=False,
        default_flow_style=False,
        indent=2,
        width=10**9,  # 防止长中文句被折行
    )
    if header.strip():
        return f"{header.rstrip()}\n\n{body}"
    return body


def save_module_file(path: Path, data: dict[str, Any], header: str = "") -> None:
    """保存模组（UTF-8 + LF，与仓库格式约定一致）。"""
    text = dump_module_text(data, header)
    with path.open("w", encoding="utf-8", newline="\n") as f:
        f.write(text)


def default_header(name: str, module_id: str) -> str:
    """新建模组的默认头部注释。"""
    return f"# 本模组由 YawnBot 跑团模组编辑器生成：{name}（{module_id}）"


def load_or_error(path: Path) -> tuple[Optional[dict[str, Any]], Optional[str]]:
    """读取文件；失败返回 (None, 错误描述)。"""
    try:
        data, header = load_module_file(path)
    except ModuleParseError as e:
        return None, str(e)
    except OSError as e:
        return None, f"读取文件失败：{e}"
    return data, header
