# ruff: noqa: I001
"""Core / Agent 管理 WebUI。"""

from .config import config, validate_enabled_config


if validate_enabled_config():
    from .app import install

    install()

__all__ = ["config"]
