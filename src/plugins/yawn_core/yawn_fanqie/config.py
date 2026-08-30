"""番茄小说子插件配置。"""

from pydantic import BaseModel, Field


class Config(BaseModel):
    """公开页面抓取、可选本机 helper、队列和正文保留策略。"""

    fanqie_request_timeout: float = Field(default=30.0, gt=0, le=120)
    fanqie_request_retries: int = Field(default=2, ge=0, le=5)
    fanqie_request_delay: float = Field(default=0.5, ge=0.2, le=60)
    fanqie_queue_max: int = Field(default=20, ge=1, le=1000)
    fanqie_user_active_max: int = Field(default=1, ge=1, le=10)
    fanqie_group_active_max: int = Field(default=3, ge=1, le=50)
    fanqie_max_chapters: int = Field(default=500, ge=1, le=5000)
    fanqie_max_file_bytes: int = Field(default=32 * 1024 * 1024, ge=1024)
    fanqie_file_retention_hours: int = Field(default=24, ge=1, le=720)
    fanqie_search_limit: int = Field(default=5, ge=1, le=5)
    fanqie_rank_limit: int = Field(default=10, ge=1, le=10)
    fanqie_browser_timeout: float = Field(default=30.0, gt=0, le=120)
    fanqie_browser_headless: bool = True
    fanqie_browser_profile_dir: str = ""
    fanqie_browser_ws_endpoint: str = ""
    fanqie_app_protocol_enabled: bool = True
    fanqie_third_party_api_base: str = "http://101.35.133.34:5000"
    fanqie_third_party_api_timeout: float = Field(default=30.0, gt=0, le=120)
    fanqie_third_party_api_retries: int = Field(default=1, ge=0, le=3)
    # The public fanqietc frontend uses this proxy for App-decrypted free text.
    # Keep the token configurable because the frontend may rotate it.
    fanqie_third_party_fallback_base: str = "https://api.fanqietc.com"
    fanqie_third_party_fallback_token: str = (
        "fqtc_7nKp2mQ8xR4vL6wT1yZ3bC5dF0hJ8aE9uI3kM7"
    )
    fanqie_mobile_helper_path: str = ""
    fanqie_mobile_helper_startup_timeout: float = Field(default=15.0, gt=0, le=60)
    fanqie_mobile_helper_timeout: float = Field(default=120.0, gt=0, le=600)
    fanqie_user_agent: str = (
        "YawnBot/0.1 (+https://github.com/Wohaokunr/yawn_bot; public-pages-only)"
    )
