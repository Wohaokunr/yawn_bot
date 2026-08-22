# ruff: noqa: PLR2004,TRY003
"""WebUI 配置与静态资源定位。"""

from pathlib import Path

from nonebot import get_plugin_config
from pydantic import BaseModel, Field, SecretStr


class WebUIConfig(BaseModel):
    webui_enabled: bool = False
    webui_admin_token: SecretStr = SecretStr("")
    webui_session_ttl_hours: int = Field(default=12, ge=1, le=168)
    webui_cookie_secure: bool = False


config = get_plugin_config(WebUIConfig)
BASE_PATH = "/webui"
API_PATH = f"{BASE_PATH}/api/v1"
COOKIE_NAME = "yawn_webui_session"
DIST_DIR = Path(__file__).resolve().parents[4] / "webui" / "dist"


def validate_enabled_config() -> None:
    """启用时快速失败，避免暴露无认证或残缺的管理入口。"""

    if not config.webui_enabled:
        return
    token = config.webui_admin_token.get_secret_value()
    if len(token) < 32:
        raise RuntimeError("WEBUI_ADMIN_TOKEN 启用时必须至少 32 个字符")
    if not (DIST_DIR / "index.html").is_file():
        raise RuntimeError("WebUI 前端产物缺失，请先在 webui 目录执行 npm run build")
