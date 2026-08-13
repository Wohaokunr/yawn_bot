from __future__ import annotations

import importlib
import sys
from pathlib import Path
from typing import Any

import nonebot
import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="module")
def llm_module() -> Any:
    if str(PROJECT_ROOT) not in sys.path:
        sys.path.insert(0, str(PROJECT_ROOT))
    try:
        nonebot.get_driver()
    except ValueError:
        nonebot.init()
    if nonebot.get_plugin("yawn_core") is None:
        nonebot.load_from_toml("pyproject.toml")
    return importlib.import_module("src.plugins.yawn_core.llm")


def test_config_allows_ai_api_key_to_be_omitted(llm_module: Any) -> None:
    config = llm_module.AIChatConfig()

    assert config.ai_api_key is None


@pytest.mark.asyncio
async def test_missing_key_degrades_all_completions_without_client(
    llm_module: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail_constructor(**_kwargs: object) -> None:
        pytest.fail("AsyncOpenAI must not be created without an API key")

    monkeypatch.setattr(llm_module, "client", None)
    monkeypatch.setattr(llm_module.ai_config, "ai_api_key", None)
    monkeypatch.setattr(llm_module, "AsyncOpenAI", fail_constructor)

    result = await llm_module.complete([{"role": "user", "content": "hello"}])
    tool_result = await llm_module.complete_with_tools(
        [{"role": "user", "content": "hello"}],
        [],
    )

    assert result is None
    assert tool_result is None


def test_client_is_created_once_on_first_use(
    llm_module: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    created: list[dict[str, object]] = []
    sentinel = object()

    def constructor(**kwargs: object) -> object:
        created.append(kwargs)
        return sentinel

    monkeypatch.setattr(llm_module, "client", None)
    monkeypatch.setattr(llm_module.ai_config, "ai_api_key", "test-key")
    monkeypatch.setattr(llm_module.ai_config, "ai_base_url", "https://example.test/v1")
    monkeypatch.setattr(llm_module, "AsyncOpenAI", constructor)

    assert llm_module.get_client() is sentinel
    assert llm_module.get_client() is sentinel
    assert created == [
        {
            "api_key": "test-key",
            "base_url": "https://example.test/v1",
        }
    ]
