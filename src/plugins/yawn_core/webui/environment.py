# ruff: noqa: TRY003
"""根 ``.env`` 的脱敏读取与原子更新。"""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
from io import StringIO
from pathlib import Path
from typing import Any

from dotenv import dotenv_values

PROJECT_ROOT = Path(__file__).resolve().parents[4]
ENV_PATH = PROJECT_ROOT / ".env"
EXAMPLE_PATH = PROJECT_ROOT / ".env.example"

_KEY_RE = re.compile(r"^[A-Z][A-Z0-9_]*$")
_ASSIGNMENT_RE = re.compile(
    r"^\s*(?:export\s+)?(?P<key>[A-Z][A-Z0-9_]*)\s*=\s*(?P<value>.*)$"
)
_COMMENTED_ASSIGNMENT_RE = re.compile(
    r"^\s*#\s*(?P<key>[A-Z][A-Z0-9_]*)\s*=\s*(?P<value>.*)$"
)
_SECTION_RE = re.compile(r"^\s*#\s*[─━=\-]{2,}\s*(.*?)\s*[─━=\-]{2,}\s*$")
_SECRET_RE = re.compile(
    r"(?:TOKEN|KEY|SECRET|PASSWORD|PASSWD|COOKIE|DSN|CREDENTIAL|AUTH)",
    re.IGNORECASE,
)
_INTEGER_RE = re.compile(r"^[+-]?\d+$")
_NUMBER_RE = re.compile(r"^[+-]?(?:\d+\.\d*|\d*\.\d+)$")
_GLOBAL_THINKING_KEYS = {
    "AI_DEFAULT_THINKING",
    "AI_LIGHT_THINKING",
    "AI_VISION_THINKING",
}
_TASK_THINKING_KEYS = {
    "AGENT_DIALOGUE_THINKING",
    "AGENT_PROACTIVE_THINKING",
    "AGENT_MEMORY_THINKING",
    "AGENT_IMAGE_THINKING",
    "RPG_KP_THINKING",
    "RPG_NPC_ROUTER_THINKING",
    "RPG_NPC_THINKING",
    "WW_DECISION_THINKING",
    "WW_SPEECH_THINKING",
}
_PROFILE_KEYS = {
    "AGENT_DIALOGUE_LLM_PROFILE",
    "AGENT_PROACTIVE_LLM_PROFILE",
    "AGENT_MEMORY_LLM_PROFILE",
    "AGENT_IMAGE_LLM_PROFILE",
    "RPG_KP_LLM_PROFILE",
    "RPG_NPC_ROUTER_LLM_PROFILE",
    "RPG_NPC_LLM_PROFILE",
    "WW_DECISION_LLM_PROFILE",
    "WW_SPEECH_LLM_PROFILE",
}
_MULTIMODAL_KEYS = {"AI_DEFAULT_MULTIMODAL", "AI_LIGHT_MULTIMODAL"}
_ENUM_OPTIONS: dict[str, list[str]] = {
    **{key: ["auto", "enabled", "disabled"] for key in _GLOBAL_THINKING_KEYS},
    **{
        key: ["inherit", "auto", "enabled", "disabled"]
        for key in _TASK_THINKING_KEYS
    },
    **{key: ["default", "light", "vision"] for key in _PROFILE_KEYS},
    **{
        key: ["auto", "supported", "unsupported"]
        for key in _MULTIMODAL_KEYS
    },
}
_MAX_VALUE_LENGTH = 16384


class EnvironmentConflictError(RuntimeError):
    """文件已在读取后被其他操作修改。"""


class EnvironmentValidationError(ValueError):
    """待写入的键或值不符合 dotenv 约束。"""


def _read_text(path: Path) -> str:
    if not path.is_file():
        return ""
    return path.read_text(encoding="utf-8-sig")


def _file_version(path: Path | None = None) -> str:
    resolved = ENV_PATH if path is None else path
    payload = resolved.read_bytes() if resolved.is_file() else b""
    return hashlib.sha256(payload).hexdigest()


def _parsed_values(path: Path) -> dict[str, str | None]:
    if not path.is_file():
        return {}
    return dict(dotenv_values(path))


def _parse_sample(raw: str) -> str | None:
    return dotenv_values(stream=StringIO(f"VALUE={raw}\n")).get("VALUE")


def _value_kind(key: str, sample: str | None, current: str | None) -> str:
    if key in _ENUM_OPTIONS:
        return "enum"
    value = (current if current is not None else sample or "").strip()
    if value.lower() in {"true", "false"}:
        return "boolean"
    if _INTEGER_RE.fullmatch(value):
        return "integer"
    if _NUMBER_RE.fullmatch(value):
        return "number"
    if value.startswith(("[", "{")):
        return "json"
    return "string"


def _catalog() -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    positions: set[str] = set()
    section = "基础配置"
    comments: list[str] = []
    for line in _read_text(EXAMPLE_PATH).splitlines():
        section_match = _SECTION_RE.match(line)
        if section_match:
            section = section_match.group(1).strip() or "其他配置"
            comments.clear()
            continue
        commented = _COMMENTED_ASSIGNMENT_RE.match(line)
        active = _ASSIGNMENT_RE.match(line)
        match = commented or active
        if match:
            key = match.group("key")
            if key not in positions:
                raw_sample = match.group("value")
                items.append(
                    {
                        "key": key,
                        "section": section,
                        "description": " ".join(comments).strip(),
                        "sample": _parse_sample(raw_sample),
                    }
                )
                positions.add(key)
            comments.clear()
            continue
        stripped = line.strip()
        if stripped.startswith("#"):
            comment = stripped.removeprefix("#").strip()
            if comment:
                comments.append(comment)
        elif not stripped:
            comments.clear()
    return items


def load_environment() -> dict[str, Any]:
    """返回根 .env 的全部已知键，敏感值只报告是否已配置。"""

    root_values = _parsed_values(ENV_PATH)
    environment_name = (
        os.environ.get("ENVIRONMENT") or root_values.get("ENVIRONMENT") or "prod"
    )
    environment_path = PROJECT_ROOT / f".env.{environment_name}"
    environment_values = _parsed_values(environment_path)

    catalog = _catalog()
    catalog_keys = {item["key"] for item in catalog}
    for key in root_values:
        if _KEY_RE.fullmatch(key) and key not in catalog_keys:
            catalog.append(
                {
                    "key": key,
                    "section": "自定义配置",
                    "description": "根 .env 中的自定义配置项",
                    "sample": None,
                }
            )

    entries: list[dict[str, Any]] = []
    for item in catalog:
        key = str(item["key"])
        secret = bool(_SECRET_RE.search(key)) or key.endswith("DATABASE_URL")
        configured = key in root_values
        if key in os.environ:
            source = "process"
        elif key in environment_values:
            source = "environment"
        elif configured:
            source = "env"
        else:
            source = "default"
        current = root_values.get(key)
        sample = item.get("sample")
        entries.append(
            {
                "key": key,
                "section": item["section"],
                "description": item["description"],
                "value": None if secret else current,
                "defaultValue": None if secret else sample,
                "configured": configured,
                "effectiveConfigured": source != "default",
                "secret": secret,
                "kind": _value_kind(key, sample, current),
                "options": _ENUM_OPTIONS.get(key, []),
                "source": source,
                "overridden": source in {"process", "environment"},
            }
        )
    return {
        "file": ".env",
        "version": _file_version(),
        "environment": str(environment_name),
        "environmentFile": (
            environment_path.name if environment_path.is_file() else None
        ),
        "entries": entries,
    }


def _render_assignment(key: str, value: str) -> str:
    return f"{key}={json.dumps(value, ensure_ascii=False)}"


def _validate_changes(changes: list[tuple[str, str | None]]) -> None:
    seen: set[str] = set()
    for key, value in changes:
        if not _KEY_RE.fullmatch(key):
            raise EnvironmentValidationError(f"配置键格式不正确：{key}")
        if key in seen:
            raise EnvironmentValidationError(f"配置键重复提交：{key}")
        seen.add(key)
        if value is not None and ("\r" in value or "\n" in value):
            raise EnvironmentValidationError(f"配置值必须为单行文本：{key}")
        if value is not None and len(value) > _MAX_VALUE_LENGTH:
            raise EnvironmentValidationError(f"配置值过长：{key}")
        options = _ENUM_OPTIONS.get(key)
        if options is not None and value is not None and value not in options:
            raise EnvironmentValidationError(
                f"{key} 仅支持 {', '.join(options)}"
            )


def _updated_text(original: str, changes: list[tuple[str, str | None]]) -> str:
    changed = dict(changes)
    lines = original.splitlines()
    last_positions: dict[str, int] = {}
    for index, line in enumerate(lines):
        match = _ASSIGNMENT_RE.match(line)
        if match and match.group("key") in changed:
            last_positions[match.group("key")] = index

    output: list[str] = []
    for index, line in enumerate(lines):
        match = _ASSIGNMENT_RE.match(line)
        if not match or match.group("key") not in changed:
            output.append(line)
            continue
        key = match.group("key")
        value = changed[key]
        if index == last_positions[key] and value is not None:
            output.append(_render_assignment(key, value))

    additions = [
        (key, value)
        for key, value in changes
        if value is not None and key not in last_positions
    ]
    if additions:
        if output and output[-1].strip():
            output.append("")
        if "# WebUI 管理的配置覆盖" not in output:
            output.append("# WebUI 管理的配置覆盖")
        output.extend(_render_assignment(key, value) for key, value in additions)

    newline = "\r\n" if "\r\n" in original else "\n"
    return newline.join(output) + (newline if output else "")


def update_environment(
    expected_version: str, changes: list[tuple[str, str | None]]
) -> dict[str, Any]:
    """按版本更新根 .env；只重写目标键并以原子替换提交。"""

    if not changes:
        raise EnvironmentValidationError("至少提交一个配置变更")
    _validate_changes(changes)
    if _file_version() != expected_version:
        raise EnvironmentConflictError

    original = _read_text(ENV_PATH)
    candidate = _updated_text(original, changes)
    parsed = dict(dotenv_values(stream=StringIO(candidate)))
    for key, value in changes:
        if value is None:
            if key in parsed:
                raise EnvironmentValidationError(f"移除配置失败：{key}")
        elif parsed.get(key) != value:
            raise EnvironmentValidationError(f"配置值解析结果不一致：{key}")

    ENV_PATH.parent.mkdir(parents=True, exist_ok=True)
    temp_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb",
            dir=ENV_PATH.parent,
            prefix=".env.",
            suffix=".tmp",
            delete=False,
        ) as handle:
            handle.write(candidate.encode("utf-8"))
            handle.flush()
            os.fsync(handle.fileno())
            temp_path = Path(handle.name)
        if _file_version() != expected_version:
            raise EnvironmentConflictError
        temp_path.replace(ENV_PATH)
        temp_path = None
    finally:
        if temp_path is not None:
            temp_path.unlink(missing_ok=True)

    return {
        "version": _file_version(),
        "restartRequired": True,
        "updatedKeys": [key for key, _value in changes],
    }
