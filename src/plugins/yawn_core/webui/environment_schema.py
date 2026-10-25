# ruff: noqa: PLR2004,PLR0911,PLR0912,PLR0913,C901
"""环境配置项的展示元数据、写入校验与配置体检。

元数据从配置目录的默认值和说明里推断（范围、单位、格式），前后端共用同一套
规则：前端据此选择编辑器并做即时校验，后端在写入前再校验一次，保证面板永远
不会把配置写成让 Bot 启动失败的样子。
"""

from __future__ import annotations

import json
import re
from typing import Any

_RANGE_RE = re.compile(
    r"[（(][^）)]*?(?P<lo>-?\d+(?:\.\d+)?)\s*(?:-|~|到|至)\s*(?P<hi>-?\d+(?:\.\d+)?)[^）)]*[）)]"
)
_PLAIN_RANGE_RE = re.compile(
    r"(?P<lo>-?\d+\.\d+|-?\d+)\s*(?:到|至)\s*(?P<hi>-?\d+\.\d+|-?\d+)"
)
_MAX_RE = re.compile(r"最大\s*(?P<hi>\d+(?:\.\d+)?)")
_MIN_RE = re.compile(r"至少\s*(?P<lo>\d+(?:\.\d+)?)")
_INTEGER_RE = re.compile(r"^[+-]?\d+$")
_NUMBER_RE = re.compile(r"^[+-]?(?:\d+(?:\.\d*)?|\.\d+)$")
_BOOLEAN_VALUES = {"true", "false"}

# 有固定取值、说明里列举过的普通配置
EXTRA_ENUM_OPTIONS: dict[str, list[str]] = {
    "LOG_LEVEL": ["TRACE", "DEBUG", "INFO", "SUCCESS", "WARNING", "ERROR", "CRITICAL"],
    "AGENT_RECEIVED_MEDIA_REUSE": ["deny", "same_group"],
    "AGENT_IMAGE_AUTO_CAPTION": ["off", "triggered", "all"],
    "AGENT_REPLY_SPLIT": ["auto", "marked", "off"],
}
# 只给候选、不限制取值
SUGGESTIONS: dict[str, list[str]] = {
    "ENVIRONMENT": ["prod", "dev", "test"],
    "DRIVER": ["~fastapi", "~fastapi+~websockets", "~fastapi+~httpx"],
    "AGENT_REMOTE_MEDIA_PROVIDER": ["auto"],
}
# 逗号分隔的列表
CSV_LIST_KEYS = {
    "AGENT_MEDIA_ALLOWED_HOSTS",
    "AGENT_FILE_ALLOWED_HOSTS",
    "AGENT_OPTIONAL_MESSAGE_SEGMENTS",
    "AGENT_SHARE_ALLOWED_HOSTS",
}
# JSON 字符串数组
JSON_STRING_LIST_KEYS = {"SUPERUSERS", "ONEBOT_V11_WS_URLS"}
DEPRECATED_KEYS = {"RPG_NPC_CONTEXT_LINES"}
# 必须留在面板里但写坏会锁门的键
LOCKOUT_KEYS = {"WEBUI_ENABLED", "WEBUI_ADMIN_TOKEN"}
# 说明里没写范围、但取负数或 0 没意义的数值
_POSITIVE_SUFFIXES = (
    "_TIMEOUT",
    "_TTL",
    "_MAX_TOKENS",
    "_MAX_BYTES",
    "_LIMIT",
    "_HOURS",
    "_DAYS",
)
_NON_NEGATIVE_SUFFIXES = ("_MAX", "_RETRIES", "_DELAY", "_WARN_REMAIN", "_INTERVAL")
# 允许为负数的整数（好感/态度变化）
_SIGNED_HINTS = ("_DELTA",)


def _number(text: str) -> float | int:
    return float(text) if "." in text else int(text)


def value_range(
    key: str, description: str
) -> tuple[float | int | None, float | int | None]:
    """从说明里解析取值范围；没写时按键名后缀给保守下限。"""

    lo: float | int | None = None
    hi: float | int | None = None
    match = _RANGE_RE.search(description) or _PLAIN_RANGE_RE.search(description)
    if match:
        lo, hi = _number(match.group("lo")), _number(match.group("hi"))
        if lo > hi:
            lo, hi = hi, lo
    else:
        max_match = _MAX_RE.search(description)
        if max_match:
            hi = _number(max_match.group("hi"))
        min_match = _MIN_RE.search(description)
        if min_match:
            lo = _number(min_match.group("lo"))
    if lo is None and not key.endswith(_SIGNED_HINTS):
        if key.endswith(_POSITIVE_SUFFIXES) or key == "PORT":
            lo = 1
        elif key.endswith(_NON_NEGATIVE_SUFFIXES) or "TEMPERATURE" in key:
            lo = 0
    if key == "PORT":
        hi = 65535
    if "TEMPERATURE" in key and hi is None:
        hi = 2
    return lo, hi


def value_format(key: str, description: str, kind: str) -> str | None:
    """前端用来挑选编辑器/显示辅助的格式提示。"""

    if key in JSON_STRING_LIST_KEYS:
        return "json-string-list"
    if key in CSV_LIST_KEYS:
        return "csv-list"
    if kind in {"integer", "number"}:
        if "字节" in description or key.endswith("_BYTES"):
            return "bytes"
        if "（秒" in description or "(秒" in description or "秒数" in description:
            return "seconds"
        if "分钟" in description:
            return "minutes"
        if "小时" in description:
            return "hours"
        if key.endswith("_DAYS") or "天数" in description:
            return "days"
    if key.endswith(("_URL", "_BASE", "_ENDPOINT")):
        return "url"
    return None


def field_label(key: str, description: str) -> str:
    """中文短名：取说明的第一个分句，太长就退回配置键。"""

    if not description or description == "根 .env 中的自定义配置项":
        return key
    head = re.split(r"[，,；;。（(：:]", description, maxsplit=1)[0].strip()
    return head if 0 < len(head) <= 24 else key


def field_meta(key: str, description: str, kind: str) -> dict[str, Any]:
    numeric = kind in {"integer", "number"}
    lo, hi = value_range(key, description) if numeric else (None, None)
    return {
        "label": field_label(key, description),
        "min": lo,
        "max": hi,
        "format": value_format(key, description, kind),
        "suggestions": SUGGESTIONS.get(key, []),
        "deprecated": key in DEPRECATED_KEYS or "已不再读取" in description,
    }


def sample_kind(sample: str | None) -> str | None:
    """只根据目录默认值推断类型；当前值写错也不会让类型跟着变。"""

    value = (sample or "").strip()
    if not value:
        return None
    if value.lower() in _BOOLEAN_VALUES:
        return "boolean"
    if _INTEGER_RE.fullmatch(value):
        return "integer"
    if _NUMBER_RE.fullmatch(value):
        return "number"
    if value.startswith(("[", "{")):
        return "json"
    return "string"


def validate_value(
    key: str,
    value: str,
    *,
    kind: str,
    sample: str | None,
    meta: dict[str, Any],
    options: list[str],
) -> str | None:
    """返回错误说明；合法时返回 None。"""

    stripped = value.strip()
    if options and value not in options:
        return f"仅支持 {', '.join(options)}"
    if kind == "boolean":
        if stripped.lower() not in _BOOLEAN_VALUES:
            return "只能是 true 或 false"
        return None
    if kind in {"integer", "number"}:
        if not stripped:
            return "不能为空；想用默认值请点「恢复默认」"
        pattern = _INTEGER_RE if kind == "integer" else _NUMBER_RE
        if not pattern.fullmatch(stripped):
            return "必须是整数" if kind == "integer" else "必须是数字"
        number = float(stripped)
        lo, hi = meta.get("min"), meta.get("max")
        if lo is not None and number < lo:
            return f"不能小于 {lo}"
        if hi is not None and number > hi:
            return f"不能大于 {hi}"
        return None
    if kind == "json":
        if not stripped:
            return "不能为空；想用默认值请点「恢复默认」"
        try:
            parsed = json.loads(stripped)
        except json.JSONDecodeError as exc:
            return f"JSON 格式错误（第 {exc.lineno} 行第 {exc.colno} 列）"
        try:
            expected = json.loads(sample) if sample else None
        except json.JSONDecodeError:
            expected = None
        if isinstance(expected, list) and not isinstance(parsed, list):
            return "必须是 JSON 数组"
        if isinstance(expected, dict) and not isinstance(parsed, dict):
            return "必须是 JSON 对象"
        if key in JSON_STRING_LIST_KEYS and not all(
            isinstance(item, str) for item in parsed
        ):
            return "数组里每一项都必须是字符串（加引号）"
        return None
    if key == "WEBUI_ADMIN_TOKEN":
        if not value:
            return "Token 不能为空"
        if value != stripped:
            return "Token 首尾不能有空格"
    return None


# 低于这个长度只提示偏弱，不再拒绝启动：Token 强度是运维自己的取舍，
# 崩掉整个 Bot 只会让人连改回来的入口都没有。
RECOMMENDED_TOKEN_LENGTH = 16
_COMMON_TOKEN_PARTS = (
    "12345",
    "54321",
    "password",
    "admin",
    "qwerty",
    "yawnbot",
    "abc123",
    "111111",
    "000000",
)


def admin_token_warnings(token: str) -> list[str]:
    """返回 Token 的强度提示；只描述问题，绝不回显 Token 本身。"""

    if not token:
        return []
    warnings: list[str] = []
    if len(token) < RECOMMENDED_TOKEN_LENGTH:
        warnings.append(f"长度少于建议的 {RECOMMENDED_TOKEN_LENGTH} 个字符")
    classes = sum(
        (
            any(char.islower() for char in token),
            any(char.isupper() for char in token),
            any(char.isdigit() for char in token),
            any(not char.isalnum() for char in token),
        )
    )
    if classes < 2:
        warnings.append("只用了一类字符")
    lowered = token.lower()
    if any(part in lowered for part in _COMMON_TOKEN_PARTS):
        warnings.append("包含常见弱口令片段")
    if len(set(token)) <= max(2, len(token) // 4):
        warnings.append("重复字符过多")
    return warnings
