# ruff: noqa: TRY003,C901,PLR0912,PLR0911,E501
"""Schema-driven tool argument validation, error taxonomy and per-turn ledger.

模型给出的参数先在这里按注册表 schema 校验和规范化，然后才进入权限、
配额和副作用流程。这样非法参数不会预扣配额，也不会出现 ``bool("false")``
被当成 True 这类反向执行。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any

ERROR_INVALID_ARGS = "invalid_args"
ERROR_PERMISSION = "permission"
ERROR_NOT_FOUND = "not_found"
ERROR_QUOTA = "quota"
ERROR_UNSUPPORTED = "unsupported"
ERROR_UPSTREAM = "upstream"
ERROR_DUPLICATE = "duplicate"
ERROR_EXPIRED = "expired"

# 每个明确触发回合最多执行的 privileged/critical 副作用次数。
MAX_CONTROLLED_CALLS_PER_TURN = 3

_TRUE_WORDS = frozenset({"true", "1", "yes", "y", "on", "是", "开", "开启", "打开"})
_FALSE_WORDS = frozenset({"false", "0", "no", "n", "off", "否", "关", "关闭"})
_INT_RE = re.compile(r"^[+-]?\d+$")
_URL_RE = re.compile(r"https?://[^\s'\"<>]+", re.IGNORECASE)
_POSIX_PATH_RE = re.compile(r"(?<![\w.])/(?:[\w.-]+/)+[\w.-]*")
_WINDOWS_PATH_RE = re.compile(r"\b[A-Za-z]:\\(?:[^\s\\'\"<>]+\\)*[^\s\\'\"<>]*")
_AUDIT_TEXT_LIMIT = 200


class ToolError(Exception):
    """带错误分类和修正建议的工具错误；message 可以直接给模型。"""

    def __init__(self, message: str, *, code: str, hint: str | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.hint = hint


class ToolArgumentError(ToolError, ValueError):
    """参数不符合 schema；保持 ValueError 兼容旧的 except 分支。"""

    def __init__(self, message: str, *, hint: str | None = None) -> None:
        super().__init__(message, code=ERROR_INVALID_ARGS, hint=hint)


def parse_strict_bool(value: Any, *, field_name: str) -> bool:
    """只接受明确的真假表达；``"false"`` 必须是 False。"""

    if isinstance(value, bool):
        return value
    if isinstance(value, int) and value in {0, 1}:
        return bool(value)
    if isinstance(value, str):
        folded = value.strip().casefold()
        if folded in _TRUE_WORDS:
            return True
        if folded in _FALSE_WORDS:
            return False
    raise ToolArgumentError(
        f"{field_name} 必须是布尔值",
        hint=f"{field_name} 只能传 true 或 false",
    )


def parse_int(value: Any, *, field_name: str) -> int:
    if isinstance(value, bool):
        raise ToolArgumentError(f"{field_name} 必须是整数", hint="不要传布尔值")
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    if isinstance(value, str) and _INT_RE.match(value.strip()):
        return int(value.strip())
    raise ToolArgumentError(
        f"{field_name} 必须是整数",
        hint=f"{field_name} 需要纯数字，例如 123456",
    )


def _parse_number(value: Any, *, field_name: str) -> float:
    if isinstance(value, bool):
        raise ToolArgumentError(f"{field_name} 必须是数字")
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value.strip())
        except ValueError:
            pass
    raise ToolArgumentError(f"{field_name} 必须是数字")


def _check_range(number: float, schema: dict[str, Any], field_name: str) -> None:
    minimum = schema.get("minimum")
    maximum = schema.get("maximum")
    if minimum is not None and number < minimum:
        raise ToolArgumentError(
            f"{field_name} 不能小于 {minimum}",
            hint=f"{field_name} 允许范围 {minimum}~{maximum if maximum is not None else '不限'}",
        )
    if maximum is not None and number > maximum:
        raise ToolArgumentError(
            f"{field_name} 不能大于 {maximum}",
            hint=f"{field_name} 允许范围 {minimum if minimum is not None else '不限'}~{maximum}",
        )


def _coerce_value(value: Any, schema: dict[str, Any], field_name: str) -> Any:
    expected = schema.get("type")
    if expected == "boolean":
        coerced: Any = parse_strict_bool(value, field_name=field_name)
    elif expected == "integer":
        coerced = parse_int(value, field_name=field_name)
        _check_range(coerced, schema, field_name)
    elif expected == "number":
        coerced = _parse_number(value, field_name=field_name)
        _check_range(coerced, schema, field_name)
    elif expected == "string":
        if isinstance(value, bool) or not isinstance(value, (str, int, float)):
            raise ToolArgumentError(f"{field_name} 必须是字符串")
        coerced = str(value).strip()
        min_length = schema.get("minLength")
        max_length = schema.get("maxLength")
        if min_length is not None and len(coerced) < int(min_length):
            raise ToolArgumentError(
                f"{field_name} 不能为空" if int(min_length) <= 1 else f"{field_name} 至少 {min_length} 个字符"
            )
        if max_length is not None and len(coerced) > int(max_length):
            raise ToolArgumentError(
                f"{field_name} 超过 {max_length} 字符上限（当前 {len(coerced)}）",
                hint=f"把 {field_name} 精简到 {max_length} 字以内再调用",
            )
        pattern = schema.get("pattern")
        if pattern and not re.fullmatch(str(pattern), coerced):
            raise ToolArgumentError(f"{field_name} 格式不正确")
    elif expected == "array":
        if not isinstance(value, list):
            raise ToolArgumentError(f"{field_name} 必须是数组")
        min_items = schema.get("minItems")
        max_items = schema.get("maxItems")
        if min_items is not None and len(value) < int(min_items):
            raise ToolArgumentError(f"{field_name} 至少需要 {min_items} 项")
        if max_items is not None and len(value) > int(max_items):
            raise ToolArgumentError(f"{field_name} 最多 {max_items} 项")
        # 数组元素（消息段、转发节点）由 outbound 的专用校验器负责。
        coerced = value
    elif expected == "object":
        if not isinstance(value, dict):
            raise ToolArgumentError(f"{field_name} 必须是对象")
        coerced = value
    else:
        coerced = value
    enum = schema.get("enum")
    if enum is not None and coerced not in enum:
        raise ToolArgumentError(
            f"{field_name} 取值无效",
            hint=f"{field_name} 只能是 {'/'.join(str(item) for item in enum)}",
        )
    return coerced


def validate_tool_arguments(
    name: str,
    properties: dict[str, Any],
    required: tuple[str, ...] | list[str],
    args: dict[str, Any],
) -> dict[str, Any]:
    """按工具 schema 校验并返回规范化后的参数副本。"""

    if not isinstance(args, dict):
        raise ToolArgumentError("工具参数必须是对象")
    unknown = sorted(key for key in args if key not in properties)
    if unknown:
        allowed = "、".join(properties) or "无参数"
        raise ToolArgumentError(
            f"{name} 不支持参数 {'、'.join(unknown)}",
            hint=f"{name} 可用参数：{allowed}",
        )
    cleaned: dict[str, Any] = {}
    for key, value in args.items():
        if value is None:
            # 显式 null 等同于省略；必填项在下面统一报错。
            continue
        cleaned[key] = _coerce_value(value, properties[key], key)
    missing = [key for key in required if key not in cleaned]
    if missing:
        raise ToolArgumentError(
            f"{name} 缺少必填参数 {'、'.join(missing)}",
            hint=_missing_hint(missing),
        )
    return cleaned


def _missing_hint(missing: list[str]) -> str:
    if "user_id" in missing:
        return "只知道昵称时先用 find_group_member 查 user_id"
    if "message_id" in missing:
        return "message_id 要来自当前上下文或 get_recent_group_messages 的结果"
    if "notice_id" in missing:
        return "先用 list_group_notices 找到 notice_id"
    if "file_id" in missing or "folder_id" in missing:
        return "先用 list_group_files 或 search_group_files 找到文件/文件夹 id"
    return f"补上 {'、'.join(missing)} 后再调用"


def sanitize_error_text(text: object, *, limit: int = 300) -> str:
    """剥掉报错里的 URL 和本机路径，避免进模型 Prompt 和审计。"""

    cleaned = _URL_RE.sub("[url]", str(text or ""))
    cleaned = _WINDOWS_PATH_RE.sub("[path]", cleaned)
    cleaned = _POSIX_PATH_RE.sub("[path]", cleaned)
    return cleaned.strip()[:limit]


_ERROR_HINTS = {
    ERROR_PERMISSION: "这是权限限制，如实告诉对方需要管理员操作或在 WebUI 开启",
    ERROR_QUOTA: "今天的额度用完了，如实说明，不要换别的工具绕过",
    ERROR_NOT_FOUND: "目标不存在或已过期，先重新查询再决定",
    ERROR_UNSUPPORTED: "当前 QQ 后端不支持，换一种不需要该能力的方式回答",
    ERROR_UPSTREAM: "QQ 后端返回失败，告诉对方没有成功，不要假装完成",
    ERROR_DUPLICATE: "同样的操作已经做过了，不要重复执行",
    ERROR_EXPIRED: "触发已过期，本轮不要再发送",
}


def classify_tool_error(exc: BaseException) -> str:
    if isinstance(exc, ToolError):
        return exc.code
    message = str(exc)
    if isinstance(exc, PermissionError):
        if "配额" in message or "上限" in message:
            return ERROR_QUOTA
        if "不支持" in message:
            return ERROR_UNSUPPORTED
        if "必须来自" in message or "不属于" in message or "不是当前群成员" in message:
            return ERROR_NOT_FOUND
        return ERROR_PERMISSION
    if isinstance(exc, LookupError):
        return ERROR_NOT_FOUND
    if isinstance(exc, (ValueError, TypeError, KeyError)):
        if "不支持" in message:
            return ERROR_UNSUPPORTED
        return ERROR_INVALID_ARGS
    return ERROR_UPSTREAM


def tool_error_payload(exc: BaseException) -> dict[str, Any]:
    code = classify_tool_error(exc)
    payload: dict[str, Any] = {
        "ok": False,
        "error": sanitize_error_text(exc) or "执行失败",
        "error_code": code,
    }
    hint = exc.hint if isinstance(exc, ToolError) and exc.hint else _ERROR_HINTS.get(code)
    if hint:
        payload["hint"] = hint
    return payload


def audit_safe_value(value: object) -> object:
    """审计参数里的长文本截断；结构保持 JSON 可序列化。"""

    if isinstance(value, str):
        return value if len(value) <= _AUDIT_TEXT_LIMIT else value[:_AUDIT_TEXT_LIMIT] + "…"
    if isinstance(value, (bool, int, float)) or value is None:
        return value
    try:
        rendered = json.dumps(value, ensure_ascii=False)
    except (TypeError, ValueError):
        rendered = str(value)
    if len(rendered) <= _AUDIT_TEXT_LIMIT * 4:
        return value if not isinstance(value, (set, frozenset, tuple)) else list(value)
    return rendered[: _AUDIT_TEXT_LIMIT * 4] + "…"


def _ledger_key(name: str, args: dict[str, Any]) -> str:
    return f"{name}:{json.dumps(args, ensure_ascii=False, sort_keys=True, default=str)}"


@dataclass(slots=True)
class ToolCallLedger:
    """单回合内写操作的去重和次数账本；只活在当前回合内存中。"""

    successes: dict[str, dict[str, Any]] = field(default_factory=dict)
    controlled_calls: int = 0

    def duplicate_of(self, name: str, args: dict[str, Any]) -> dict[str, Any] | None:
        previous = self.successes.get(_ledger_key(name, args))
        if previous is None:
            return None
        return {**previous, "duplicate": True}

    def record_success(self, name: str, args: dict[str, Any], payload: dict[str, Any]) -> None:
        self.successes[_ledger_key(name, args)] = dict(payload)

    def reserve_controlled(self) -> None:
        if self.controlled_calls >= MAX_CONTROLLED_CALLS_PER_TURN:
            raise ToolError(
                f"本轮管理操作已达 {MAX_CONTROLLED_CALLS_PER_TURN} 次上限",
                code=ERROR_QUOTA,
                hint="先把已完成的结果告诉对方，剩下的让对方再发一条消息",
            )
        self.controlled_calls += 1

    def release_controlled(self) -> None:
        self.controlled_calls = max(self.controlled_calls - 1, 0)


__all__ = [
    "ERROR_DUPLICATE",
    "ERROR_EXPIRED",
    "ERROR_INVALID_ARGS",
    "ERROR_NOT_FOUND",
    "ERROR_PERMISSION",
    "ERROR_QUOTA",
    "ERROR_UNSUPPORTED",
    "ERROR_UPSTREAM",
    "MAX_CONTROLLED_CALLS_PER_TURN",
    "ToolArgumentError",
    "ToolCallLedger",
    "ToolError",
    "audit_safe_value",
    "classify_tool_error",
    "parse_int",
    "parse_strict_bool",
    "sanitize_error_text",
    "tool_error_payload",
    "validate_tool_arguments",
]
