# ruff: noqa: C901,TRY003
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
from urllib.parse import urlparse

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
_PROVIDER_ID_RE = re.compile(r"^[a-z][a-z0-9_-]{0,31}$")
_MAX_CUSTOM_PROVIDERS = 16
_PROVIDER_STORAGE_KEYS = {
    "AI_BASE_URL",
    "AI_API_KEY",
    "AI_PROVIDERS",
    "AI_PROVIDER_API_KEYS",
}
_PROFILE_PROVIDER_KEYS = {
    "AI_DEFAULT_PROVIDER",
    "AI_LIGHT_PROVIDER",
    "AI_VISION_PROVIDER",
}


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


def _effective_value(
    key: str,
    root_values: dict[str, str | None],
    environment_values: dict[str, str | None],
    default: str | None = None,
) -> tuple[str | None, str]:
    if key in os.environ:
        return os.environ[key], "process"
    if key in environment_values:
        return environment_values[key], "environment"
    if key in root_values:
        return root_values[key], "env"
    return default, "default"


def _json_list(value: str | None, key: str) -> list[dict[str, Any]]:
    if not value:
        return []
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError as exc:
        raise EnvironmentValidationError(f"{key} 不是有效 JSON") from exc
    if not isinstance(parsed, list) or not all(
        isinstance(item, dict) for item in parsed
    ):
        raise EnvironmentValidationError(f"{key} 必须是 JSON 对象数组")
    return parsed


def _json_string_map(value: str | None, key: str) -> dict[str, str]:
    if not value:
        return {}
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError as exc:
        raise EnvironmentValidationError(f"{key} 不是有效 JSON") from exc
    if not isinstance(parsed, dict) or not all(
        isinstance(item_key, str) and isinstance(item_value, str)
        for item_key, item_value in parsed.items()
    ):
        raise EnvironmentValidationError(f"{key} 必须是字符串到字符串的 JSON 对象")
    return parsed


def _valid_base_url(value: str) -> bool:
    parsed = urlparse(value.strip())
    return parsed.scheme in {"http", "https"} and bool(parsed.netloc)


def _provider_state(
    root_values: dict[str, str | None],
    environment_values: dict[str, str | None],
) -> tuple[list[dict[str, Any]], dict[str, str]]:
    base_url, base_source = _effective_value(
        "AI_BASE_URL",
        root_values,
        environment_values,
        "https://token-plan-cn.xiaomimimo.com/v1",
    )
    default_key, default_key_source = _effective_value(
        "AI_API_KEY", root_values, environment_values
    )
    metadata_raw, metadata_source = _effective_value(
        "AI_PROVIDERS", root_values, environment_values, "[]"
    )
    keys_raw, keys_source = _effective_value(
        "AI_PROVIDER_API_KEYS", root_values, environment_values, "{}"
    )
    metadata = _json_list(metadata_raw, "AI_PROVIDERS")
    provider_keys = _json_string_map(keys_raw, "AI_PROVIDER_API_KEYS")
    providers: list[dict[str, Any]] = [
        {
            "id": "default",
            "baseUrl": str(base_url or ""),
            "builtIn": True,
            "apiKeyConfigured": bool(default_key),
            "apiKeyRootConfigured": bool(root_values.get("AI_API_KEY")),
            "baseUrlSource": base_source,
            "apiKeySource": default_key_source,
            "overridden": base_source in {"process", "environment"}
            or default_key_source in {"process", "environment"},
        }
    ]
    secrets = {"default": str(default_key or "")}
    for item in metadata:
        provider_id = str(item.get("id", ""))
        provider_base_url = str(item.get("base_url", ""))
        provider_key = provider_keys.get(provider_id, "")
        providers.append(
            {
                "id": provider_id,
                "baseUrl": provider_base_url,
                "builtIn": False,
                "apiKeyConfigured": bool(provider_key),
                "apiKeyRootConfigured": bool(
                    _json_string_map(
                        root_values.get("AI_PROVIDER_API_KEYS"),
                        "AI_PROVIDER_API_KEYS",
                    ).get(provider_id)
                ),
                "baseUrlSource": metadata_source,
                "apiKeySource": keys_source,
                "overridden": metadata_source in {"process", "environment"}
                or keys_source in {"process", "environment"},
            }
        )
        secrets[provider_id] = provider_key
    return providers, secrets


def resolve_llm_provider(provider_id: str) -> tuple[str, str | None]:
    """读取当前有效提供商配置，供连接测试复用且不向响应暴露密钥。"""

    root_values = _parsed_values(ENV_PATH)
    environment_name = (
        os.environ.get("ENVIRONMENT") or root_values.get("ENVIRONMENT") or "prod"
    )
    environment_values = _parsed_values(PROJECT_ROOT / f".env.{environment_name}")
    providers, secrets = _provider_state(root_values, environment_values)
    provider = next((item for item in providers if item["id"] == provider_id), None)
    if provider is None:
        raise EnvironmentValidationError(f"未知 LLM 提供商：{provider_id}")
    return str(provider["baseUrl"]), secrets.get(provider_id) or None


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
    providers, _secrets = _provider_state(root_values, environment_values)
    return {
        "file": ".env",
        "version": _file_version(),
        "environment": str(environment_name),
        "environmentFile": (
            environment_path.name if environment_path.is_file() else None
        ),
        "entries": entries,
        "llmProviders": providers,
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


def _provider_changes(
    providers: list[dict[str, Any]], root_values: dict[str, str | None]
) -> list[tuple[str, str | None]]:
    if not providers or providers[0].get("id") != "default":
        raise EnvironmentValidationError("提供商列表必须以不可删除的 default 开头")
    if len(providers) > _MAX_CUSTOM_PROVIDERS + 1:
        raise EnvironmentValidationError(
            f"自定义提供商最多 {_MAX_CUSTOM_PROVIDERS} 个"
        )
    root_keys = _json_string_map(
        root_values.get("AI_PROVIDER_API_KEYS"), "AI_PROVIDER_API_KEYS"
    )
    seen: set[str] = set()
    metadata: list[dict[str, str]] = []
    next_keys: dict[str, str] = {}
    result: list[tuple[str, str | None]] = []
    for index, provider in enumerate(providers):
        provider_id = str(provider.get("id", "")).strip()
        base_url = str(provider.get("baseUrl", "")).strip().rstrip("/")
        if not _PROVIDER_ID_RE.fullmatch(provider_id):
            raise EnvironmentValidationError(f"提供商 ID 格式不正确：{provider_id}")
        if provider_id in seen:
            raise EnvironmentValidationError(f"提供商 ID 重复：{provider_id}")
        if (index == 0) != (provider_id == "default"):
            raise EnvironmentValidationError("default 是保留提供商且必须位于首项")
        if not _valid_base_url(base_url):
            raise EnvironmentValidationError(
                f"提供商 {provider_id} 的 Base URL 必须是绝对 HTTP/HTTPS 地址"
            )
        seen.add(provider_id)
        api_key_supplied = "apiKey" in provider
        api_key = provider.get("apiKey")
        if api_key_supplied and api_key is not None:
            api_key = str(api_key).strip()
            if not api_key:
                raise EnvironmentValidationError(
                    f"提供商 {provider_id} 的新密钥不能为空；删除请提交 null"
                )
        if provider_id == "default":
            result.append(("AI_BASE_URL", base_url))
            if api_key_supplied:
                result.append(("AI_API_KEY", api_key))
            continue
        metadata.append({"id": provider_id, "base_url": base_url})
        existing_key = root_keys.get(provider_id)
        resolved_key = api_key if api_key_supplied else existing_key
        if resolved_key:
            next_keys[provider_id] = str(resolved_key)
    result.extend(
        [
            (
                "AI_PROVIDERS",
                json.dumps(metadata, ensure_ascii=False, separators=(",", ":"))
                if metadata
                else None,
            ),
            (
                "AI_PROVIDER_API_KEYS",
                json.dumps(next_keys, ensure_ascii=False, separators=(",", ":"))
                if next_keys
                else None,
            ),
        ]
    )
    return result


def _validate_llm_candidate(candidate: str) -> None:
    root_values = dict(dotenv_values(stream=StringIO(candidate)))
    environment_name = (
        os.environ.get("ENVIRONMENT") or root_values.get("ENVIRONMENT") or "prod"
    )
    environment_values = _parsed_values(PROJECT_ROOT / f".env.{environment_name}")
    providers, secrets = _provider_state(root_values, environment_values)
    ids = {str(item["id"]) for item in providers}
    if len(ids) != len(providers):
        raise EnvironmentValidationError("AI_PROVIDERS 中的提供商 ID 必须唯一")
    for item in providers:
        provider_id = str(item["id"])
        if provider_id != "default" and not secrets.get(provider_id):
            raise EnvironmentValidationError(f"提供商 {provider_id} 尚未配置 API Key")
    for key in _PROFILE_PROVIDER_KEYS:
        provider_id, _source = _effective_value(
            key, root_values, environment_values, "default"
        )
        if provider_id not in ids:
            raise EnvironmentValidationError(f"{key} 引用了未知提供商：{provider_id}")


def update_environment(
    expected_version: str,
    changes: list[tuple[str, str | None]],
    providers: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """按版本更新根 .env；只重写目标键并以原子替换提交。"""

    if not changes and providers is None:
        raise EnvironmentValidationError("至少提交一个配置变更")
    if providers is not None and any(
        key in _PROVIDER_STORAGE_KEYS for key, _ in changes
    ):
        raise EnvironmentValidationError("提供商配置不能同时通过普通字段和专用表单提交")
    if providers is not None:
        changes = [*changes, *_provider_changes(providers, _parsed_values(ENV_PATH))]
    _validate_changes(changes)
    if _file_version() != expected_version:
        raise EnvironmentConflictError

    original = _read_text(ENV_PATH)
    candidate = _updated_text(original, changes)
    if providers is not None or any(
        key in _PROFILE_PROVIDER_KEYS for key, _ in changes
    ):
        _validate_llm_candidate(candidate)
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
