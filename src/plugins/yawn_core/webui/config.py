"""WebUI 配置与静态资源定位。"""

from pathlib import Path

from nonebot import get_plugin_config, logger
from pydantic import BaseModel, SecretStr, ValidationError, field_validator

from .environment_schema import admin_token_warnings

SESSION_TTL_DEFAULT_HOURS = 12
SESSION_TTL_MIN_HOURS = 1
SESSION_TTL_MAX_HOURS = 168


class WebUIConfig(BaseModel):
    webui_enabled: bool = False
    webui_admin_token: SecretStr = SecretStr("")
    webui_session_ttl_hours: int = SESSION_TTL_DEFAULT_HOURS
    webui_cookie_secure: bool = False
    # QQ 用户用私聊里设的密码登录 WebUI 的自助页面（管理的群、自己的私聊人设）
    webui_user_login_enabled: bool = True
    # 对外访问地址（如 https://bot.example.com/webui），私聊里发登录链接用
    webui_public_url: str = ""

    @field_validator("webui_session_ttl_hours", mode="before")
    @classmethod
    def _clamp_session_ttl(cls, value: object) -> int:
        """越界或写错的时长回退到可用值，而不是让插件加载失败。"""

        try:
            hours = int(str(value).strip())
        except (TypeError, ValueError):
            logger.warning(
                "WEBUI_SESSION_TTL_HOURS 不是整数，已使用默认 "
                f"{SESSION_TTL_DEFAULT_HOURS} 小时"
            )
            return SESSION_TTL_DEFAULT_HOURS
        clamped = max(SESSION_TTL_MIN_HOURS, min(SESSION_TTL_MAX_HOURS, hours))
        if clamped != hours:
            logger.warning(
                f"WEBUI_SESSION_TTL_HOURS={hours} 超出 "
                f"{SESSION_TTL_MIN_HOURS}-{SESSION_TTL_MAX_HOURS}，已按 {clamped} 处理"
            )
        return clamped


def _load_config() -> WebUIConfig:
    try:
        return get_plugin_config(WebUIConfig)
    except ValidationError as exc:
        # 例如 WEBUI_ENABLED=maybe：管理端关掉即可，Bot 本体照常启动
        logger.error(f"WebUI 配置无效，已按未启用处理：{exc.errors()}")
        return WebUIConfig()


config = _load_config()
BASE_PATH = "/webui"
API_PATH = f"{BASE_PATH}/api/v1"
COOKIE_NAME = "yawn_webui_session"
DIST_DIR = Path(__file__).resolve().parents[4] / "webui" / "dist"


def enabled_config_problems() -> list[str]:
    """列出让 WebUI 无法挂载的问题；空列表表示可以安全启用。"""

    problems: list[str] = []
    token = config.webui_admin_token.get_secret_value()
    if not token.strip():
        problems.append("WEBUI_ADMIN_TOKEN 为空，未挂载 WebUI 以免暴露无认证的管理入口")
    if not (DIST_DIR / "index.html").is_file():
        problems.append("WebUI 前端产物缺失，请先在 webui 目录执行 npm run build")
    return problems


def validate_enabled_config() -> bool:
    """检查启用条件；有问题只记日志并跳过 WebUI，绝不让整个 Bot 启动失败。"""

    if not config.webui_enabled:
        return False
    problems = enabled_config_problems()
    for problem in problems:
        logger.error(f"WebUI 未启用：{problem}")
    if problems:
        return False
    warnings = admin_token_warnings(config.webui_admin_token.get_secret_value())
    if warnings:
        logger.warning(
            "WEBUI_ADMIN_TOKEN 偏弱（" + "；".join(warnings) + "），"
            "公网访问时建议换成随机长 Token"
        )
    return True
