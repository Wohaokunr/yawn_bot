# ruff: noqa: E501, TID252
"""私聊工具的渐进式披露：请求里只放族目录 + 少数常用工具，其余按需展开。

以前每轮请求都带上全部私聊工具的完整 schema（四十来个、近三万字符），绝大多数轮次
一个都不用，却要为它们付输入 token（缓存命中也要付钱，没命中时更贵）。现在：

- **第一层（每轮都在，前缀缓存）**：``open_tools``（描述里是工具族目录：族名、什么时候用、
  里面有哪些工具）+ ``use_tool``（按名字调用族里的工具）+ 少数直达工具
  （``PRIVATE_AGENT_DIRECT_TOOLS``，默认派活、后台任务、跑代码、联网搜索、回想）。
- **第二层（按需）**：``open_tools(family=…)`` 返回该族每个工具的用途和**紧凑参数签名**
  （比 JSON schema 短一半以上），结果只在本轮的工具消息里，不进历史。
- **第三层**：``use_tool(tool=…, args={…})`` 执行；参数错了把签名附在错误里，一步改对。

工具表对同一位用户逐字稳定（只随电脑/后台任务/群管/超管这些可用性变化），所以仍然满足
私聊前缀缓存“工具表不换”的硬规则；最后一轮/汇报/协调轮照旧用 ``tool_choice``。
``PRIVATE_AGENT_TOOL_DISCLOSURE=full`` 退回旧做法（全部完整 schema）。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from .prompt_sections import PRIVATE_TOOL_SECTION_PREFIX, apply_section

if TYPE_CHECKING:
    from .private_tools import PrivateTool, PrivateToolContext

OPEN_TOOL = "open_tools"
USE_TOOL = "use_tool"
META_TOOLS = frozenset({OPEN_TOOL, USE_TOOL})
DEFAULT_DIRECT = ("delegate_task", "tasks", "run_code", "web_search", "recall")
# 签名里每个参数说明最多保留多少字（详细用法在工具用途里）。
_PARAM_NOTE_CHARS = 40


@dataclass(frozen=True, slots=True)
class Family:
    key: str
    label: str
    when: str
    tools: tuple[str, ...]


FAMILIES: tuple[Family, ...] = (
    Family(
        "status",
        "他的状态",
        "签到、积分、关系等级、好感进度、共同的群；好感有什么用、送礼、商店道具、补签、每日任务、专属称呼；涉及这些先查再说",
        ("my_status", "checkin", "affinity"),
    ),
    Family(
        "memory",
        "记忆",
        "记住/忘掉关于他的事、你记得他什么、群里的你和私聊的你怎么共用记忆",
        ("recall", "remember_about_me", "forget_about_me", "memory_link"),
    ),
    Family(
        "assistant",
        "个人助理",
        "笔记、待办、心情记录、他对私聊的偏好（称呼、长短、免打扰、早安、生日）",
        ("notes", "todos", "mood", "chat_prefs"),
    ),
    Family(
        "stickers",
        "表情包",
        "他发来的表情包想收藏/你喜欢想偷走、按情绪找能发的表情包、看他存过哪些",
        ("stickers",),
    ),
    Family(
        "time",
        "提醒与定时",
        "到点提醒他、到点替他去办、倒数日、群定时任务",
        ("reminders", "schedules", "countdowns"),
    ),
    Family(
        "web",
        "联网",
        "搜新闻、实时信息、拿不准的事实，打开网页读正文",
        ("web_search", "web_fetch"),
    ),
    Family(
        "computer",
        "虚拟电脑",
        "精确计算、单位换算、日期差、随机、别处几点都现场写代码跑；处理文件、画图、做表格/文档，一百多个现成能力包",
        ("run_code", "computer_files", "computer_kits", "computer_do", "toolbox"),
    ),
    Family(
        "tasks",
        "后台做事",
        "费时间的活放到后台做、组队做大活、看进度/结果、改要求、取消",
        ("delegate_task", "delegate_project", "tasks"),
    ),
    Family(
        "groups",
        "群",
        "群里最近聊什么、替他在群里转告/提问、替群主/管理员管群",
        ("group_recent", "group_bridge", "group_manage"),
    ),
    Family(
        "account",
        "设置与账号",
        "私聊人设、网页账号与登录、主动提议的开关、他的数据与额度、命令列表、反馈给管理员",
        (
            "my_persona",
            "webui_account",
            "offers",
            "my_data",
            "list_commands",
            "feedback",
            "bot_status",
        ),
    ),
)
FAMILY_BY_KEY = {family.key: family for family in FAMILIES}
FAMILY_OF = {name: family.key for family in FAMILIES for name in family.tools}

OPEN_DESCRIPTION = (
    "展开一个工具族，看里面每个工具的用途和参数（* 必填）。下面是全部工具族；直达工具（工具表里单独列出的）直接调用。"
    "族里的工具用 use_tool 调；用法已经清楚（比如本轮展开过、目录里看得懂）就直接 use_tool，不必先展开。"
    "要不要用工具照常判断：闲聊、接情绪不用工具。"
)
USE_DESCRIPTION = "调用族里的工具：tool 写工具名，args 写参数对象（按 open_tools 给的签名）。参数不对会返回正确用法，照着改一次。"


def disclosure_mode() -> str:
    from ..private_chat_bridge import private_agent_config

    mode = str(
        getattr(private_agent_config, "private_agent_tool_disclosure", "progressive")
    )
    return mode if mode in {"progressive", "full"} else "progressive"


def direct_names() -> tuple[str, ...]:
    from ..private_chat_bridge import private_agent_config

    raw = getattr(private_agent_config, "private_agent_direct_tools", None)
    if raw is None:
        return DEFAULT_DIRECT
    return tuple(
        item.strip() for item in str(raw).replace("，", ",").split(",") if item.strip()
    )


def family_line(family: Family, names: list[str]) -> str:
    when = apply_section(
        f"{PRIVATE_TOOL_SECTION_PREFIX}family.{family.key}", family.when
    )
    return f"{family.key}（{family.label}）：{when}｜{'、'.join(names)}"


def families_for(tools: list[PrivateTool]) -> list[tuple[Family, list[str]]]:
    """能用的工具按族分组（族顺序固定、族内按工具表顺序，渲染逐字稳定）。"""

    names = [tool.name for tool in tools]
    grouped: list[tuple[Family, list[str]]] = []
    for family in FAMILIES:
        members = [name for name in family.tools if name in names]
        if members:
            grouped.append((family, members))
    return grouped


def request_tools(tools: list[PrivateTool]) -> list[dict[str, Any]]:
    """发给模型的工具表。``tools`` 是 ``private_tools.available_tools`` 的结果。"""

    if disclosure_mode() == "full":
        return [tool.schema() for tool in tools]
    direct = set(direct_names())
    families = families_for(tools)
    lines = [family_line(family, names) for family, names in families]
    hidden = [tool.name for tool in tools if tool.name not in direct]
    schemas = [tool.schema() for tool in tools if tool.name in direct]
    if not hidden:
        return schemas
    schemas.append(
        {
            "type": "function",
            "function": {
                "name": OPEN_TOOL,
                "description": apply_section(
                    f"{PRIVATE_TOOL_SECTION_PREFIX}{OPEN_TOOL}", OPEN_DESCRIPTION
                )
                + "\n"
                + "\n".join(lines),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "family": {
                            "type": "string",
                            "enum": [family.key for family, _names in families],
                        },
                        "tool": {
                            "type": "string",
                            "description": "只看一个工具时写工具名",
                        },
                    },
                    "additionalProperties": False,
                },
            },
        }
    )
    schemas.append(
        {
            "type": "function",
            "function": {
                "name": USE_TOOL,
                "description": apply_section(
                    f"{PRIVATE_TOOL_SECTION_PREFIX}{USE_TOOL}", USE_DESCRIPTION
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "tool": {"type": "string", "enum": hidden},
                        "args": {"type": "object"},
                    },
                    "required": ["tool"],
                    "additionalProperties": False,
                },
            },
        }
    )
    return schemas


# ── 紧凑签名 ─────────────────────────────────────────────


def _type_text(spec: dict[str, Any]) -> str:  # noqa: PLR0911
    if "enum" in spec:
        return "|".join(str(item) for item in spec["enum"])
    kind = spec.get("type")
    if kind == "string":
        limit = spec.get("maxLength")
        return f"文本≤{limit}" if limit else "文本"
    if kind == "integer":
        low, high = spec.get("minimum"), spec.get("maximum")
        return f"整数{low}~{high}" if low is not None and high is not None else "整数"
    if kind == "number":
        return "数字"
    if kind == "boolean":
        return "true|false"
    if kind == "array":
        item = spec.get("items") or {}
        inner = (
            "{"
            + params_text(item.get("properties") or {}, item.get("required") or ())
            + "}"
            if item.get("type") == "object" and item.get("properties")
            else _type_text(item)
        )
        top = spec.get("maxItems")
        return f"[{inner}]" + (f"≤{top}项" if top else "")
    if kind == "object":
        props = spec.get("properties") or {}
        return (
            "{" + params_text(props, spec.get("required") or ()) + "}"
            if props
            else "对象"
        )
    return str(kind or "任意")


def params_text(properties: dict[str, Any], required: Any = ()) -> str:
    """``action*: add|list; content: 文本≤300; id: 整数1~1000000(条目编号)``。"""

    must = set(required or ())
    parts = []
    for key, raw in properties.items():
        spec = raw if isinstance(raw, dict) else {}
        text = f"{key}{'*' if key in must else ''}: {_type_text(spec)}"
        note = str(spec.get("description") or "").strip()
        if note and spec.get("type") != "object":
            text += f"({note[:_PARAM_NOTE_CHARS]})"
        parts.append(text)
    return "; ".join(parts) or "无参数"


def usage(tool: PrivateTool) -> dict[str, str]:
    description = apply_section(
        f"{PRIVATE_TOOL_SECTION_PREFIX}{tool.name}", tool.description
    )
    return {
        "tool": tool.name,
        "use": description,
        "args": params_text(tool.properties, tool.required),
    }


def _available_by_name(ctx: PrivateToolContext) -> dict[str, PrivateTool]:
    from .private_tools import TOOLS_BY_NAME

    if ctx.available is None:
        return dict(TOOLS_BY_NAME)
    return {name: tool for name, tool in TOOLS_BY_NAME.items() if name in ctx.available}


def open_tools(raw_args: Any, ctx: PrivateToolContext) -> dict[str, Any]:
    """第二层：展开一个族（或一个工具）。只读，不算工具成功，不进历史。"""

    from .private_tools import resolve_alias

    args = raw_args if isinstance(raw_args, dict) else {}
    pool = _available_by_name(ctx)
    wanted = str(args.get("tool") or "").strip()
    key = str(args.get("family") or "").strip()
    if wanted:
        wanted, _ = resolve_alias(wanted, {})
        if wanted not in pool:
            # 写成族名也认。
            if wanted in FAMILY_BY_KEY and not key:
                key = wanted
            else:
                return {
                    "ok": False,
                    "error": f"没有可用的工具 {wanted}",
                    "error_code": "not_found",
                    "hint": "看 open_tools 描述里的目录",
                }
        else:
            ctx.opened.add(wanted)
            _record("open", wanted)
            return {
                "ok": True,
                "result": {
                    "how": "use_tool(tool, args)；* 必填",
                    "tools": [usage(pool[wanted])],
                },
            }
    if not key:
        key = FAMILY_OF.get(wanted, "")
    family = FAMILY_BY_KEY.get(key)
    if family is None:
        return {
            "ok": False,
            "error": f"没有工具族 {key or '（没写）'}",
            "error_code": "invalid_args",
            "hint": "family 写目录里的族名",
        }
    members = [pool[name] for name in family.tools if name in pool]
    if not members:
        return {
            "ok": False,
            "error": f"{family.label}现在用不了",
            "error_code": "not_allowed",
            "hint": "照实告诉他，别编结果",
        }
    _record("open", key)
    if f"family:{key}" in ctx.opened:
        return {
            "ok": True,
            "result": {
                "family": key,
                "note": "这一轮已经展开过，照上面的用法直接 use_tool",
            },
        }
    ctx.opened.add(f"family:{key}")
    ctx.opened.update(tool.name for tool in members)
    return {
        "ok": True,
        "result": {
            "family": key,
            "how": "use_tool(tool, args)；* 必填",
            "tools": [usage(tool) for tool in members],
        },
    }


def unwrap_use(
    raw_args: Any, ctx: PrivateToolContext
) -> tuple[str, Any, dict[str, Any] | None]:
    """第三层：拆出 use_tool 里的真实工具名和参数；格式不对时给出可改正的错误。"""

    del ctx
    args = raw_args if isinstance(raw_args, dict) else {}
    name = str(args.get("tool") or args.get("name") or "").strip()
    if not name:
        return (
            "",
            {},
            {
                "ok": False,
                "error": "use_tool 要写 tool（工具名）",
                "error_code": "invalid_args",
                "hint": "例如 use_tool(tool=notes, args={action: add, content: …})",
            },
        )
    if name in META_TOOLS:
        return (
            "",
            {},
            {
                "ok": False,
                "error": f"{name} 直接调用，不用经过 use_tool",
                "error_code": "invalid_args",
            },
        )
    inner = args.get("args")
    if isinstance(inner, str):
        # 有的模型把参数写成 JSON 字符串。
        try:
            inner = json.loads(inner or "{}")
        except json.JSONDecodeError:
            inner = None
    if inner is None:
        # 参数直接平铺在外层（{"tool": "notes", "action": "add"}）也认。
        inner = {
            key: value
            for key, value in args.items()
            if key not in {"tool", "name", "args"}
        }
    if not isinstance(inner, dict):
        return (
            "",
            {},
            {
                "ok": False,
                "error": "args 必须是对象",
                "error_code": "invalid_args",
                "hint": "args 写成 {参数名: 值}",
            },
        )
    return name, inner, None


def annotate_failure(
    name: str, payload: dict[str, Any], ctx: PrivateToolContext
) -> dict[str, Any]:
    """参数错了：把这个工具的签名附上（等于顺手展开），下一步就能改对。"""

    from .private_tools import resolve_alias

    if payload.get("ok") or payload.get("error_code") != "invalid_args":
        return payload
    canonical, _ = resolve_alias(name, {})
    tool = _available_by_name(ctx).get(canonical)
    if tool is None or canonical in ctx.opened:
        return payload
    ctx.opened.add(canonical)
    _record("fixed", canonical)
    return {**payload, "usage": usage(tool)}


def _record(kind: str, name: str) -> None:
    try:
        from .private_cache import record_tool

        record_tool(kind, name)
        if kind in {"open", "fixed"}:
            # Trace 里看得到这一轮展开了哪些族、哪些参数被纠正（诊断“为什么多绕了一轮”）。
            from .execution_trace import trace_event

            trace_event(
                "decision",
                "私聊工具展开" if kind == "open" else "私聊工具参数纠正",
                status="success",
                output={"what": name},
            )
    except Exception:  # noqa: BLE001 - 统计失败不影响工具
        pass


def record_call(call_name: str, arguments: Any) -> None:
    """主回合每次工具调用记一笔：直达工具记 direct，use_tool 记里面的真实工具。"""

    if call_name == OPEN_TOOL:
        return
    if call_name == USE_TOOL:
        _record("use", inner_name(call_name, arguments))
    else:
        _record("direct", call_name)


def inner_name(call_name: str, arguments: Any) -> str:
    """Trace/转后台摘要里显示真实工具名（use_tool 包着的那个）。"""

    if call_name != USE_TOOL:
        return call_name
    data = arguments
    if isinstance(data, str):
        try:
            data = json.loads(data or "{}")
        except json.JSONDecodeError:
            return call_name
    if isinstance(data, dict) and data.get("tool"):
        return str(data["tool"])
    return call_name


def catalog_sections() -> dict[str, tuple[str, str]]:
    """登记到 prompt_sections 的私聊工具目录文字（ptool.open_tools / use_tool / family.*）。"""

    found = {
        OPEN_TOOL: (OPEN_DESCRIPTION, "main"),
        USE_TOOL: (USE_DESCRIPTION, "main"),
    }
    for family in FAMILIES:
        found[f"family.{family.key}"] = (family.when, "main")
    return found


__all__ = [
    "DEFAULT_DIRECT",
    "FAMILIES",
    "FAMILY_BY_KEY",
    "FAMILY_OF",
    "META_TOOLS",
    "OPEN_TOOL",
    "USE_TOOL",
    "Family",
    "annotate_failure",
    "catalog_sections",
    "direct_names",
    "disclosure_mode",
    "families_for",
    "inner_name",
    "open_tools",
    "params_text",
    "record_call",
    "request_tools",
    "unwrap_use",
    "usage",
]
