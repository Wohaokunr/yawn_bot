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
    assert config.ai_providers == []
    assert config.ai_default_provider == "default"
    assert config.ai_light_model is None
    assert config.ai_vision_model is None
    assert config.ai_default_thinking == "auto"
    assert config.ai_light_thinking == "disabled"
    assert config.agent_proactive_llm_profile == "light"
    assert config.agent_proactive_thinking == "inherit"


def test_config_repr_masks_api_key_and_resolves_profile_defaults(
    llm_module: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = llm_module.AIChatConfig(
        ai_api_key="secret",
        ai_model="fallback",
        ai_providers=[{"id": "fast", "base_url": "https://fast.test/v1"}],
        ai_provider_api_keys={"fast": "named-secret"},
    )
    assert "secret" not in repr(config)
    assert "named-secret" not in repr(config)
    monkeypatch.setattr(llm_module.ai_config, "ai_model", "fallback")
    monkeypatch.setattr(llm_module.ai_config, "ai_light_model", None)
    assert llm_module.resolve_llm_request("agent_dialogue").model == "fallback"
    assert llm_module.resolve_llm_request("agent_memory").model == "fallback"


def test_task_override_controls_profile_thinking_and_multimodal(
    llm_module: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(llm_module.ai_config, "ai_model", "default-model")
    monkeypatch.setattr(llm_module.ai_config, "ai_light_model", "light-model")
    monkeypatch.setattr(llm_module.ai_config, "ai_light_thinking", "disabled")
    monkeypatch.setattr(llm_module.ai_config, "ai_light_multimodal", "unsupported")
    monkeypatch.setattr(llm_module.ai_config, "agent_dialogue_llm_profile", "light")
    monkeypatch.setattr(llm_module.ai_config, "agent_dialogue_thinking", "enabled")

    request = llm_module.resolve_llm_request("agent_dialogue")

    assert request.profile == "light"
    assert request.model == "light-model"
    assert request.thinking == "enabled"
    assert request.multimodal == "unsupported"
    assert request.extra_body == {"thinking": {"type": "enabled"}}


@pytest.mark.asyncio
async def test_missing_key_degrades_all_completions_without_client(
    llm_module: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail_constructor(**_kwargs: object) -> None:
        pytest.fail("AsyncOpenAI must not be created without an API key")

    llm_module._client_pool.clear()
    monkeypatch.setattr(llm_module.ai_config, "ai_api_key", None)
    monkeypatch.setattr(llm_module, "AsyncOpenAI", fail_constructor)

    result = await llm_module.complete([{"role": "user", "content": "hello"}])
    tool_result = await llm_module.complete_with_tools(
        [{"role": "user", "content": "hello"}],
        [],
    )

    assert result is None
    assert tool_result is None


@pytest.mark.asyncio
async def test_tool_completion_result_exposes_finish_usage_and_duration(
    llm_module: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from types import SimpleNamespace

    class Completions:
        async def create(self, **_kwargs: object) -> Any:
            message = SimpleNamespace(content="调试回复", tool_calls=[])
            return SimpleNamespace(
                choices=[
                    SimpleNamespace(message=message, finish_reason="stop")
                ],
                usage=SimpleNamespace(
                    prompt_tokens=120,
                    completion_tokens=18,
                    prompt_tokens_details=SimpleNamespace(cached_tokens=40),
                ),
            )

    fake = SimpleNamespace(
        chat=SimpleNamespace(completions=Completions())
    )
    monkeypatch.setattr(llm_module, "get_client", lambda _provider="default": fake)

    result = await llm_module.complete_with_tools_result(
        [{"role": "user", "content": "hello"}], [], task="agent_dialogue"
    )

    assert result.outcome == "success"
    assert result.message is not None
    assert result.finish_reason == "stop"
    assert result.prompt_tokens == 120  # noqa: PLR2004
    assert result.completion_tokens == 18  # noqa: PLR2004
    assert result.cached_tokens == 40  # noqa: PLR2004
    assert result.duration_ms >= 0


def test_client_is_created_once_on_first_use(
    llm_module: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    created: list[dict[str, object]] = []
    sentinel = object()

    def constructor(**kwargs: object) -> object:
        created.append(kwargs)
        return sentinel

    llm_module._client_pool.clear()
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


@pytest.mark.asyncio
async def test_tasks_select_profiles_and_resolve_thinking(
    llm_module: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[dict[str, object]] = []

    class Completions:
        async def create(self, **kwargs: object) -> Any:
            calls.append(kwargs)
            message = type("Message", (), {"content": "ok", "tool_calls": []})()
            choice = type("Choice", (), {"message": message, "finish_reason": "stop"})()
            return type("Response", (), {"choices": [choice]})()

    fake = type(
        "Client", (), {"chat": type("Chat", (), {"completions": Completions()})()}
    )()
    monkeypatch.setattr(llm_module, "get_client", lambda _provider="default": fake)
    monkeypatch.setattr(llm_module.ai_config, "ai_api_key", "fixture-key")
    monkeypatch.setattr(llm_module.ai_config, "ai_model", "advanced-model")
    monkeypatch.setattr(llm_module.ai_config, "ai_light_model", "ordinary-light")
    monkeypatch.setattr(llm_module.ai_config, "ai_vision_model", "vision-model")
    monkeypatch.setattr(llm_module.ai_config, "ai_default_thinking", "auto")
    monkeypatch.setattr(llm_module.ai_config, "ai_light_thinking", "disabled")
    monkeypatch.setattr(llm_module.ai_config, "ai_vision_thinking", "enabled")
    monkeypatch.setattr(llm_module.ai_config, "rpg_kp_thinking", "enabled")

    await llm_module.complete(
        [{"role": "user", "content": "dialogue"}], task="agent_dialogue"
    )
    await llm_module.complete(
        [{"role": "user", "content": "memory"}], task="agent_memory"
    )
    await llm_module.complete(
        [{"role": "user", "content": "image"}], task="agent_image"
    )
    await llm_module.complete_with_tools(
        [{"role": "user", "content": "kp"}], [], task="rpg_kp"
    )

    assert [item["model"] for item in calls] == [
        "advanced-model",
        "ordinary-light",
        "vision-model",
        "advanced-model",
    ]
    assert "extra_body" not in calls[0]
    assert calls[1]["extra_body"] == {"thinking": {"type": "disabled"}}
    assert calls[2]["extra_body"] == {"thinking": {"type": "enabled"}}
    assert calls[3]["extra_body"] == {"thinking": {"type": "enabled"}}


def test_named_provider_is_selected_and_reused_without_fallback(
    llm_module: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    created: list[dict[str, object]] = []

    def constructor(**kwargs: object) -> object:
        created.append(kwargs)
        return object()

    llm_module._client_pool.clear()
    monkeypatch.setattr(
        llm_module.ai_config,
        "ai_providers",
        [llm_module.LLMProviderConfig(id="fast", base_url="https://fast.test/v1")],
    )
    monkeypatch.setattr(
        llm_module.ai_config,
        "ai_provider_api_keys",
        {"fast": llm_module.SecretStr("fast-secret")},
    )
    monkeypatch.setattr(llm_module.ai_config, "ai_light_provider", "fast")
    monkeypatch.setattr(llm_module.ai_config, "ai_light_model", "fast-model")
    monkeypatch.setattr(llm_module.ai_config, "agent_memory_llm_profile", "light")
    monkeypatch.setattr(llm_module, "AsyncOpenAI", constructor)

    request = llm_module.resolve_llm_request("agent_memory")

    assert request.provider == "fast"
    assert request.model == "fast-model"
    assert llm_module.get_client(request.provider) is llm_module.get_client("fast")
    assert created == [
        {"api_key": "fast-secret", "base_url": "https://fast.test/v1"}
    ]


def test_empty_light_model_inherits_default_provider_and_model(
    llm_module: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(llm_module.ai_config, "ai_model", "default-model")
    monkeypatch.setattr(llm_module.ai_config, "ai_default_provider", "default")
    monkeypatch.setattr(llm_module.ai_config, "ai_light_model", None)
    monkeypatch.setattr(llm_module.ai_config, "ai_light_provider", "fast")
    monkeypatch.setattr(llm_module.ai_config, "agent_memory_llm_profile", "light")

    request = llm_module.resolve_llm_request("agent_memory")

    assert request.provider == "default"
    assert request.model == "default-model"
