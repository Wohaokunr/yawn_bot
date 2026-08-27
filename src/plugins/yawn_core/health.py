"""Small process health endpoint used by containers and reverse proxies."""

from collections.abc import Callable
from typing import Any

from fastapi import FastAPI
from nonebot import get_driver, logger


def install_healthcheck(
    get_sub_plugin_report: Callable[[], tuple[Any, ...]],
) -> None:
    """Register a dependency-free liveness endpoint on the NoneBot FastAPI app."""

    app = getattr(get_driver(), "server_app", None)
    if not isinstance(app, FastAPI):
        logger.warning("未注册 /healthz：当前 NoneBot Driver 不是 FastAPI")
        return

    @app.get("/healthz", include_in_schema=False)
    async def healthz() -> dict[str, object]:
        report = get_sub_plugin_report()
        failed = [item.label for item in report if item.state == "failed"]
        return {
            "status": "ok",
            "service": "yawnbot",
            "subplugins": {
                "loaded": sum(item.state == "loaded" for item in report),
                "missing": sum(item.state == "missing" for item in report),
                "failed": len(failed),
            },
        }
