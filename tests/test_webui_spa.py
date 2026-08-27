from __future__ import annotations

import importlib
import re
import sys
from pathlib import Path

import nonebot
from fastapi import FastAPI
from fastapi.testclient import TestClient

try:
    nonebot.get_driver()
except ValueError:
    nonebot.init()

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))
if nonebot.get_plugin("yawn_core") is None:
    nonebot.load_from_toml("pyproject.toml")

app_module = importlib.import_module("src.plugins.yawn_core.webui.app")
config_module = importlib.import_module("src.plugins.yawn_core.webui.config")


def test_built_spa_and_assets_are_served() -> None:
    index_path = config_module.DIST_DIR / "index.html"
    assert index_path.is_file(), "CI must download the webui-dist artifact first"

    app = FastAPI()
    app_module.register(app)
    client = TestClient(app)

    response = client.get("/webui")
    assert response.status_code == 200  # noqa: PLR2004
    assert "text/html" in response.headers["content-type"]
    assert '<div id="root"></div>' in response.text

    fallback = client.get("/webui/agent")
    assert fallback.status_code == 200  # noqa: PLR2004
    assert '<div id="root"></div>' in fallback.text

    asset_match = re.search(r'["\'](/webui/assets/[^"\']+)["\']', response.text)
    assert asset_match is not None
    asset = client.get(asset_match.group(1))
    assert asset.status_code == 200  # noqa: PLR2004
    assert asset.content
