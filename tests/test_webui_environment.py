from __future__ import annotations

import importlib
import sys
from pathlib import Path

import nonebot
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import SecretStr

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))
try:
    nonebot.get_driver()
except ValueError:
    nonebot.init()
if nonebot.get_plugin("yawn_core") is None:
    nonebot.load_from_toml("pyproject.toml")

auth = importlib.import_module("src.plugins.yawn_core.webui.auth")
app_module = importlib.import_module("src.plugins.yawn_core.webui.app")
environment_routes = importlib.import_module(
    "src.plugins.yawn_core.webui.environment_routes"
)
environment = importlib.import_module("src.plugins.yawn_core.webui.environment")


@pytest.fixture
def env_files(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, Path]:
    env_path = tmp_path / ".env"
    example_path = tmp_path / ".env.example"
    example_path.write_text(
        """# ── AI 配置 ──────────
# 默认模型
AI_MODEL=default-model
# 默认提供商地址
AI_BASE_URL=https://default.test/v1
# API 密钥
AI_API_KEY=
# 自定义提供商
# AI_PROVIDERS=[]
# 自定义提供商密钥
# AI_PROVIDER_API_KEYS={}
# 默认档位提供商
AI_DEFAULT_PROVIDER=default
# 轻量档位提供商
AI_LIGHT_PROVIDER=default
# 识图档位提供商
AI_VISION_PROVIDER=default
# 轻量模型推理
AI_LIGHT_THINKING=disabled
# 默认模型图片能力
AI_DEFAULT_MULTIMODAL=auto
# 主动任务模型
AGENT_PROACTIVE_LLM_PROFILE=light
# 主动任务思考
AGENT_PROACTIVE_THINKING=inherit

# prose with lower_case=value is not a setting
""",
        encoding="utf-8",
    )
    env_path.write_text(
        "# keep this comment\n"
        "ENVIRONMENT=prod\n"
        "AI_MODEL=root-model\n"
        "AI_API_KEY=secret-value\n"
        "CUSTOM_FLAG=true\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(environment, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(environment, "ENV_PATH", env_path)
    monkeypatch.setattr(environment, "EXAMPLE_PATH", example_path)
    monkeypatch.delenv("ENVIRONMENT", raising=False)
    monkeypatch.delenv("AI_MODEL", raising=False)
    return env_path, example_path


def test_environment_catalog_is_complete_and_secrets_are_masked(
    env_files: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    env_path, _example_path = env_files
    (env_path.parent / ".env.prod").write_text(
        "AI_MODEL=prod-model\n", encoding="utf-8"
    )
    snapshot = environment.load_environment()
    entries = {item["key"]: item for item in snapshot["entries"]}

    assert {
        "AI_MODEL",
        "AI_API_KEY",
        "AI_LIGHT_THINKING",
        "AI_DEFAULT_MULTIMODAL",
        "AGENT_PROACTIVE_LLM_PROFILE",
        "AGENT_PROACTIVE_THINKING",
        "CUSTOM_FLAG",
    } <= entries.keys()
    assert "lower_case" not in entries
    assert entries["CUSTOM_FLAG"]["section"] == "自定义配置"
    assert entries["AI_API_KEY"]["value"] is None
    assert "secret-value" not in str(snapshot)
    assert entries["AI_MODEL"]["value"] == "root-model"
    assert entries["AI_MODEL"]["source"] == "environment"
    assert entries["AI_MODEL"]["overridden"] is True
    assert snapshot["llmProviders"] == [
        {
            "id": "default",
            "baseUrl": "https://token-plan-cn.xiaomimimo.com/v1",
            "builtIn": True,
            "apiKeyConfigured": True,
            "apiKeyRootConfigured": True,
            "baseUrlSource": "default",
            "apiKeySource": "env",
            "overridden": False,
        }
    ]
    assert "secret-value" not in str(snapshot["llmProviders"])

    monkeypatch.setenv("AI_MODEL", "process-model")
    overridden = environment.load_environment()
    model = next(item for item in overridden["entries"] if item["key"] == "AI_MODEL")
    assert model["source"] == "process"
    assert "process-model" not in str(overridden)


def test_environment_update_preserves_comments_and_round_trips_values(
    env_files: tuple[Path, Path],
) -> None:
    env_path, _example_path = env_files
    version = environment.load_environment()["version"]
    result = environment.update_environment(
        version,
        [
            ("AI_MODEL", 'model with spaces and "quotes"'),
            ("AI_API_KEY", None),
            ("AGENT_PROACTIVE_LLM_PROFILE", "vision"),
            ("AGENT_PROACTIVE_THINKING", "disabled"),
        ],
    )

    content = env_path.read_text(encoding="utf-8")
    assert "# keep this comment" in content
    assert "AI_API_KEY=" not in content
    assert result["restartRequired"] is True
    snapshot = environment.load_environment()
    entries = {item["key"]: item for item in snapshot["entries"]}
    assert entries["AI_MODEL"]["value"] == 'model with spaces and "quotes"'
    assert entries["AGENT_PROACTIVE_LLM_PROFILE"]["value"] == "vision"
    assert entries["AGENT_PROACTIVE_THINKING"]["value"] == "disabled"

    with pytest.raises(environment.EnvironmentConflictError):
        environment.update_environment(version, [("AI_MODEL", "stale")])
    with pytest.raises(environment.EnvironmentValidationError):
        environment.update_environment(
            result["version"], [("AGENT_PROACTIVE_THINKING", "sometimes")]
        )


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("AI_LIGHT_THINKING", "inherit"),
        ("AI_DEFAULT_MULTIMODAL", "yes"),
        ("AGENT_PROACTIVE_LLM_PROFILE", "cheap"),
    ],
)
def test_environment_rejects_invalid_llm_enums(
    env_files: tuple[Path, Path], key: str, value: str
) -> None:
    _ = env_files
    version = environment.load_environment()["version"]
    with pytest.raises(environment.EnvironmentValidationError):
        environment.update_environment(version, [(key, value)])


def test_environment_round_trips_named_providers_without_exposing_keys(
    env_files: tuple[Path, Path],
) -> None:
    env_path, _example_path = env_files
    version = environment.load_environment()["version"]
    result = environment.update_environment(
        version,
        [
            ("AI_LIGHT_MODEL", "fast-model"),
            ("AI_LIGHT_PROVIDER", "fast"),
        ],
        [
            {"id": "default", "baseUrl": "https://default.test/v1"},
            {
                "id": "fast",
                "baseUrl": "https://fast.test/v1/",
                "apiKey": "fast-secret",
            },
        ],
    )

    content = env_path.read_text(encoding="utf-8")
    assert "AI_PROVIDERS=" in content
    assert "AI_PROVIDER_API_KEYS=" in content
    snapshot = environment.load_environment()
    assert [item["id"] for item in snapshot["llmProviders"]] == ["default", "fast"]
    assert snapshot["llmProviders"][1]["baseUrl"] == "https://fast.test/v1"
    assert snapshot["llmProviders"][1]["apiKeyConfigured"] is True
    assert "fast-secret" not in str(snapshot)

    preserved = environment.update_environment(
        result["version"],
        [],
        [
            {"id": "default", "baseUrl": "https://default.test/v1"},
            {"id": "fast", "baseUrl": "https://fast-v2.test/v1"},
        ],
    )
    assert "fast-secret" in env_path.read_text(encoding="utf-8")

    with pytest.raises(
        environment.EnvironmentValidationError, match="引用了未知提供商"
    ):
        environment.update_environment(
            preserved["version"],
            [],
            [{"id": "default", "baseUrl": "https://default.test/v1"}],
        )


def test_environment_routes_require_csrf_and_report_conflicts(
    env_files: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    _ = env_files
    monkeypatch.setattr(auth.config, "webui_admin_token", SecretStr("x" * 40))
    monkeypatch.setattr(auth.config, "webui_session_ttl_hours", 12)
    monkeypatch.setattr(auth.config, "webui_cookie_secure", False)
    auth.reset_login_failures_for_tests()
    app = FastAPI()
    app_module.register(app, include_spa=False)
    client = TestClient(app)
    login = client.post("/webui/api/v1/auth/login", json={"token": "x" * 40})
    csrf = login.json()["data"]["csrfToken"]
    response = client.get("/webui/api/v1/environment")
    assert response.status_code == 200, response.text  # noqa: PLR2004
    snapshot = response.json()["data"]

    tested: dict[str, object] = {}

    async def fake_test_connection(**kwargs: object) -> float:
        tested.update(kwargs)
        return 12.5

    monkeypatch.setattr(environment_routes, "test_llm_connection", fake_test_connection)
    connection = client.post(
        "/webui/api/v1/llm/test",
        headers={"X-CSRF-Token": csrf},
        json={
            "providerId": "default",
            "baseUrl": "https://draft.test/v1",
            "model": "draft-model",
        },
    )
    assert connection.status_code == 200, connection.text  # noqa: PLR2004
    assert connection.json()["data"] == {"success": True, "latencyMs": 12.5}
    assert tested == {
        "base_url": "https://draft.test/v1",
        "api_key": "secret-value",
        "model": "draft-model",
        "timeout": 10.0,
    }
    assert "secret-value" not in connection.text

    async def failing_test_connection(**_kwargs: object) -> float:
        raise RuntimeError("upstream rejected secret-value")  # noqa: TRY003

    monkeypatch.setattr(
        environment_routes, "test_llm_connection", failing_test_connection
    )
    failed_connection = client.post(
        "/webui/api/v1/llm/test",
        headers={"X-CSRF-Token": csrf},
        json={"providerId": "default", "model": "draft-model"},
    )
    assert failed_connection.status_code == 502  # noqa: PLR2004
    assert "secret-value" not in failed_connection.text
    assert "[REDACTED]" in failed_connection.text

    rejected = client.patch(
        "/webui/api/v1/environment",
        json={
            "version": snapshot["version"],
            "changes": [{"key": "AI_MODEL", "value": "next"}],
        },
    )
    assert rejected.status_code == 403  # noqa: PLR2004

    invalid = client.patch(
        "/webui/api/v1/environment",
        headers={"X-CSRF-Token": csrf},
        json={
            "version": snapshot["version"],
            "changes": [
                {"key": "AGENT_PROACTIVE_LLM_PROFILE", "value": "cheap"}
            ],
        },
    )
    assert invalid.status_code == 422  # noqa: PLR2004

    changed = client.patch(
        "/webui/api/v1/environment",
        headers={"X-CSRF-Token": csrf},
        json={
            "version": snapshot["version"],
            "changes": [{"key": "AI_API_KEY", "value": "new-secret"}],
        },
    )
    assert changed.status_code == 200  # noqa: PLR2004
    assert "new-secret" not in changed.text
    assert changed.json()["data"]["restartRequired"] is True

    conflict = client.patch(
        "/webui/api/v1/environment",
        headers={"X-CSRF-Token": csrf},
        json={
            "version": snapshot["version"],
            "changes": [{"key": "AI_MODEL", "value": "stale"}],
        },
    )
    assert conflict.status_code == 409  # noqa: PLR2004
