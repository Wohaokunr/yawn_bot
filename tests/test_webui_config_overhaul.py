"""配置页整理：Token 放宽、启动不崩、写入校验、配置体检与 WebUI 内重启。"""

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
webui_config = importlib.import_module("src.plugins.yawn_core.webui.config")
environment = importlib.import_module("src.plugins.yawn_core.webui.environment")
environment_routes = importlib.import_module(
    "src.plugins.yawn_core.webui.environment_routes"
)
schema = importlib.import_module("src.plugins.yawn_core.webui.environment_schema")

CATALOG = """# ── 运行 ──────────
# FastAPI 监听端口
PORT=8080
# 日志级别：TRACE / DEBUG / INFO / WARNING / ERROR / CRITICAL
LOG_LEVEL=INFO
# 超级用户 QQ 号，JSON 字符串数组
SUPERUSERS=["123"]
# 采样率，0.0 到 1.0
SENTRY_SAMPLE_RATE=1.0
# 媒体缓存保留秒数
AGENT_MEDIA_CACHE_TTL=86400
# 允许下载群图片的主机白名单（逗号分隔）
AGENT_MEDIA_ALLOWED_HOSTS=a.test,b.test
# 旧版 NPC 群聊窗口配置，已不再读取，仅为兼容保留
RPG_NPC_CONTEXT_LINES=8
# Agent 海量调试日志
AGENT_DEBUG_LOG=false
# ── WebUI ──────────
# 是否启用 /webui 管理端
WEBUI_ENABLED=false
# 运维管理员登录令牌
WEBUI_ADMIN_TOKEN=
# 登录会话有效时长（小时，1-168）
WEBUI_SESSION_TTL_HOURS=12
# HTTPS 访问时设为 true
WEBUI_COOKIE_SECURE=false
# ── AI ──────────
# API 密钥
AI_API_KEY=
"""


@pytest.fixture
def env_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    catalog = tmp_path / "catalog.dotenv"
    catalog.write_text(CATALOG, encoding="utf-8")
    path = tmp_path / ".env"
    path.write_text(
        "WEBUI_ENABLED=true\nWEBUI_ADMIN_TOKEN=Whk12345\nPORT=abc\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(environment, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(environment, "ENV_PATH", path)
    monkeypatch.setattr(environment, "CATALOG_PATH", catalog)
    monkeypatch.setattr(environment, "EXAMPLE_PATH", tmp_path / "missing")
    for key in (
        "ENVIRONMENT",
        "YAWNBOT_DOTENV_KEYS",
        "YAWNBOT_PINNED_ENV_KEYS",
        "YAWNBOT_SUPERVISED",
        "PORT",
        "HOST",
        "AI_API_KEY",
        "ONEBOT_V11_ACCESS_TOKEN",
        "WEBUI_ENABLED",
        "WEBUI_ADMIN_TOKEN",
        "WEBUI_COOKIE_SECURE",
        "AGENT_DEBUG_LOG",
    ):
        monkeypatch.delenv(key, raising=False)
    return path


# ── Token 与启动 ──


def test_short_token_starts_webui_with_warning(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    (tmp_path / "index.html").write_text("ok", encoding="utf-8")
    monkeypatch.setattr(webui_config, "DIST_DIR", tmp_path)
    monkeypatch.setattr(webui_config.config, "webui_enabled", True)
    monkeypatch.setattr(
        webui_config.config, "webui_admin_token", SecretStr("Whk@12345")
    )
    assert webui_config.validate_enabled_config() is True


@pytest.mark.parametrize(
    ("token", "has_dist"), [("", True), ("   ", True), ("long-enough-token", False)]
)
def test_unusable_webui_config_is_skipped_instead_of_crashing(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    token: str,
    has_dist: bool,  # noqa: FBT001
) -> None:
    if has_dist:
        (tmp_path / "index.html").write_text("ok", encoding="utf-8")
    monkeypatch.setattr(webui_config, "DIST_DIR", tmp_path)
    monkeypatch.setattr(webui_config.config, "webui_enabled", True)
    monkeypatch.setattr(webui_config.config, "webui_admin_token", SecretStr(token))
    assert webui_config.validate_enabled_config() is False


def test_disabled_webui_is_not_mounted(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(webui_config.config, "webui_enabled", False)
    assert webui_config.validate_enabled_config() is False


@pytest.mark.parametrize(
    ("raw", "expected"), [("0", 1), ("999", 168), ("abc", 12), ("24", 24)]
)
def test_session_ttl_is_clamped_instead_of_failing(raw: str, expected: int) -> None:
    parsed = webui_config.WebUIConfig.model_validate({"webui_session_ttl_hours": raw})
    assert parsed.webui_session_ttl_hours == expected


def test_token_warnings_describe_without_echoing() -> None:
    warnings = schema.admin_token_warnings("Whk@12345")
    assert any("16" in item for item in warnings)
    assert any("弱口令" in item for item in warnings)
    assert all("Whk" not in item for item in warnings)
    assert schema.admin_token_warnings("aaaaaaaaaaaaaaaaaaaa")
    assert schema.admin_token_warnings("") == []
    assert schema.admin_token_warnings("Zq7-mP2v_Xr9!Lk4sT8w") == []


def test_empty_token_never_logs_in(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(auth.config, "webui_admin_token", SecretStr(""))
    auth.reset_login_failures_for_tests()
    assert auth.check_admin_token("ip", "") is False


def test_session_signature_depends_on_server_key(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(auth.config, "webui_admin_token", SecretStr("short"))
    monkeypatch.setattr(auth, "SESSION_KEY_PATH", tmp_path / "k" / "session.key")
    auth._server_key.cache_clear()
    try:
        cookie, _session = auth.create_session()
        assert auth.verify_session(cookie) is not None
        key_file = tmp_path / "k" / "session.key"
        assert key_file.stat().st_mode & 0o777 == 0o600  # noqa: PLR2004
        # 换一把本地密钥（等同另一台机器），同一个短 Token 签出的 Cookie 无效
        key_file.write_bytes(b"x" * 32)
        auth._server_key.cache_clear()
        assert auth.verify_session(cookie) is None
    finally:
        auth._server_key.cache_clear()


# ── 元数据与校验 ──


@pytest.mark.parametrize(
    ("key", "description", "expected"),
    [
        ("WEBUI_SESSION_TTL_HOURS", "登录会话有效时长（小时，1-168）", (1, 168)),
        ("FANQIE_REQUEST_RETRIES", "请求失败重试次数（0-5）", (0, 5)),
        ("FANQIE_REQUEST_TIMEOUT", "单次请求超时（秒，最大 120）", (1, 120)),
        ("FANQIE_REQUEST_DELAY", "相邻请求的最小间隔（秒，至少 0.2）", (0.2, None)),
        ("SENTRY_SAMPLE_RATE", "采样率，0.0 到 1.0", (0.0, 1.0)),
        ("PORT", "FastAPI 监听端口", (1, 65535)),
        ("RPG_SOCIAL_NEGATIVE_RAPPORT_DELTA", "负向情绪微调", (None, None)),
        ("RPG_KP_TEMPERATURE", "KP 生成温度", (0, 2)),
    ],
)
def test_value_range_parses_descriptions(
    key: str, description: str, expected: tuple[object, object]
) -> None:
    assert schema.value_range(key, description) == expected


def test_field_meta_formats_and_labels() -> None:
    meta = schema.field_meta("AGENT_MEDIA_CACHE_TTL", "媒体缓存保留秒数", "integer")
    assert meta["format"] == "seconds"
    assert schema.field_meta("X_MAX_BYTES", "大小上限（字节）", "integer")[
        "format"
    ] == "bytes"
    assert schema.field_meta("SUPERUSERS", "超级用户", "json")["format"] == (
        "json-string-list"
    )
    assert schema.field_meta("AGENT_MEDIA_ALLOWED_HOSTS", "白名单", "string")[
        "format"
    ] == "csv-list"
    assert schema.field_label("PORT", "FastAPI 监听端口") == "FastAPI 监听端口"
    assert schema.field_label("X", "很长" * 20) == "X"
    assert schema.field_meta("RPG_NPC_CONTEXT_LINES", "已不再读取", "integer")[
        "deprecated"
    ]


def test_snapshot_keeps_catalog_kind_and_reports_invalid_current_value(
    env_path: Path,
) -> None:
    _ = env_path
    snapshot = environment.load_environment()
    entries = {item["key"]: item for item in snapshot["entries"]}
    # 当前值写错也仍按整数编辑，并标出问题
    assert entries["PORT"]["kind"] == "integer"
    assert entries["PORT"]["issue"] == "必须是整数"
    assert entries["LOG_LEVEL"]["kind"] == "enum"
    assert entries["SUPERUSERS"]["meta"]["format"] == "json-string-list"
    assert entries["WEBUI_ADMIN_TOKEN"]["secret"] is True
    assert entries["WEBUI_ADMIN_TOKEN"]["issue"] is None
    assert "Whk12345" not in str(snapshot)
    assert snapshot["restartSupported"] is False
    titles = [item["title"] for item in snapshot["diagnostics"]]
    assert "PORT 的当前值无效" in titles
    assert "运维 Token 偏弱" in titles
    assert "default 提供商没有 API Key" in titles
    assert snapshot["diagnostics"][0]["level"] == "error"


def test_pinned_keys_are_not_reported_as_overrides(
    env_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _ = env_path
    monkeypatch.setenv("PORT", "8080")
    monkeypatch.setenv("YAWNBOT_PINNED_ENV_KEYS", "HOST,PORT")
    entries = {
        item["key"]: item for item in environment.load_environment()["entries"]
    }
    assert entries["PORT"]["source"] == "pinned"
    diagnostics = environment.load_environment()["diagnostics"]
    assert not any("外部环境覆盖" in item["title"] for item in diagnostics)


@pytest.mark.parametrize(
    ("key", "value", "message"),
    [
        ("PORT", "eighty", "必须是整数"),
        ("PORT", "70000", "不能大于 65535"),
        ("WEBUI_SESSION_TTL_HOURS", "0", "不能小于 1"),
        ("SENTRY_SAMPLE_RATE", "1.5", "不能大于 1.0"),
        ("WEBUI_COOKIE_SECURE", "yes please", "只能是 true 或 false"),
        ("SUPERUSERS", "[123]", "字符串"),
        ("SUPERUSERS", "{}", "JSON 数组"),
        ("SUPERUSERS", "[", "JSON 格式错误"),
        ("LOG_LEVEL", "LOUD", "仅支持"),
        ("WEBUI_ADMIN_TOKEN", " padded ", "首尾不能有空格"),
        ("WEBUI_ADMIN_TOKEN", "", "不能为空"),
    ],
)
def test_update_rejects_values_that_would_break_startup(
    env_path: Path, key: str, value: str, message: str
) -> None:
    version = environment.load_environment()["version"]
    with pytest.raises(environment.EnvironmentValidationError, match=message):
        environment.update_environment(version, [(key, value)])
    assert "PORT=abc" in env_path.read_text(encoding="utf-8")


def test_update_accepts_short_token_and_valid_values(env_path: Path) -> None:
    version = environment.load_environment()["version"]
    environment.update_environment(
        version,
        [
            ("WEBUI_ADMIN_TOKEN", "Whk@12345"),
            ("PORT", "8081"),
            ("SUPERUSERS", '["1","2"]'),
            ("SENTRY_SAMPLE_RATE", "0.5"),
        ],
    )
    text = env_path.read_text(encoding="utf-8")
    assert 'WEBUI_ADMIN_TOKEN="Whk@12345"' in text
    assert 'PORT="8081"' in text


def test_update_refuses_to_lock_out_admin(env_path: Path) -> None:
    version = environment.load_environment()["version"]
    with pytest.raises(environment.EnvironmentValidationError, match="无法登录"):
        environment.update_environment(version, [("WEBUI_ADMIN_TOKEN", None)])
    # 先关 WebUI 再移除 Token 是允许的
    environment.update_environment(
        version, [("WEBUI_ENABLED", "false"), ("WEBUI_ADMIN_TOKEN", None)]
    )
    assert "WEBUI_ADMIN_TOKEN" not in env_path.read_text(encoding="utf-8")


def test_pending_restart_tracks_file_changes(
    env_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(environment, "PROCESS_STARTED_AT", 0.0)
    assert environment.load_environment()["pendingRestart"] is True
    monkeypatch.setattr(
        environment, "PROCESS_STARTED_AT", env_path.stat().st_mtime + 10
    )
    assert environment.load_environment()["pendingRestart"] is False


# ── 重启接口 ──


def _client(monkeypatch: pytest.MonkeyPatch) -> tuple[TestClient, str]:
    monkeypatch.setattr(auth.config, "webui_admin_token", SecretStr("Whk@12345"))
    monkeypatch.setattr(auth.config, "webui_session_ttl_hours", 12)
    monkeypatch.setattr(auth.config, "webui_cookie_secure", False)
    auth.reset_login_failures_for_tests()
    app = FastAPI()
    app_module.register(app, include_spa=False)
    client = TestClient(app)
    login = client.post("/webui/api/v1/auth/login", json={"token": "Whk@12345"})
    assert login.status_code == 200, login.text  # noqa: PLR2004
    return client, login.json()["data"]["csrfToken"]


def test_restart_requires_supervisor_and_csrf(
    env_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _ = env_path
    client, csrf = _client(monkeypatch)
    status = client.get("/webui/api/v1/system/status").json()["data"]
    assert status["restartSupported"] is False
    assert client.post("/webui/api/v1/system/restart").status_code == 403  # noqa: PLR2004
    refused = client.post(
        "/webui/api/v1/system/restart", headers={"X-CSRF-Token": csrf}
    )
    assert refused.status_code == 409  # noqa: PLR2004

    killed: list[bool] = []
    monkeypatch.setenv("YAWNBOT_SUPERVISED", "1")
    monkeypatch.setattr(environment_routes, "_restart_scheduled", False)
    monkeypatch.setattr(environment_routes, "_RESTART_DELAY_SECONDS", 0.0)
    monkeypatch.setattr(
        environment_routes, "_terminate_for_restart", lambda: killed.append(True)
    )
    accepted = client.post(
        "/webui/api/v1/system/restart", headers={"X-CSRF-Token": csrf}
    )
    assert accepted.status_code == 200, accepted.text  # noqa: PLR2004
    assert accepted.json()["data"]["scheduled"] is True
    assert environment_routes._restart_scheduled is True
