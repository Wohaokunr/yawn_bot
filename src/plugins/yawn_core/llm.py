# ruff: noqa: E501,PLR0913,TRY003
"""共享 LLM 客户端：OpenAI 兼容端点的配置、路由与非流式补全。

ai_chat 的流式对话与各 AI 子插件共用同一客户端、三级模型档位与
任务路由配置。非流式 complete()
面向"一次调用拿完整结果"的场景（如狼人杀 AI 决策），带总超时
与并发上限：失败一律返回 None，由调用方决定降级策略。
complete_with_tools() 面向 agentic 场景（如跑团 KP 经 tool_call
驱动系统判定），返回完整 message 供调用方续接工具循环。
"""

import asyncio
import hashlib
import re
import time
from dataclasses import dataclass
from typing import Any, Literal, Optional
from urllib.parse import urlparse

from nonebot import get_plugin_config, logger
from openai import AsyncOpenAI, OpenAIError
from openai.types.chat import (
    ChatCompletionMessage,
    ChatCompletionMessageParam,
    ChatCompletionToolParam,
)
from pydantic import BaseModel, Field, SecretStr, field_validator, model_validator

LLMProfile = Literal["default", "light", "vision"]
ThinkingMode = Literal["auto", "enabled", "disabled"]
TaskThinkingMode = Literal["inherit", "auto", "enabled", "disabled"]
MultimodalMode = Literal["auto", "supported", "unsupported"]
LLMTask = Literal[
    "core_chat",
    "agent_dialogue",
    "agent_proactive",
    "agent_memory",
    "agent_image",
    "rpg_kp",
    "rpg_npc_router",
    "rpg_npc",
    "ww_decision",
    "ww_speech",
]

_PROVIDER_ID_RE = re.compile(r"^[a-z][a-z0-9_-]{0,31}$")
_MAX_CUSTOM_PROVIDERS = 16


class LLMProviderConfig(BaseModel):
    """一个命名 OpenAI-compatible 提供商的非敏感配置。"""

    id: str = Field(pattern=r"^[a-z][a-z0-9_-]{0,31}$")
    base_url: str

    @field_validator("base_url")
    @classmethod
    def validate_base_url(cls, value: str) -> str:
        normalized = value.strip().rstrip("/")
        parsed = urlparse(normalized)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ValueError("base_url 必须是绝对 HTTP/HTTPS 地址")
        return normalized


class LLMRoutingConfig(BaseModel):
    """共享模型档位、能力以及各子插件任务的路由配置。"""

    ai_light_model: Optional[str] = None
    ai_vision_model: Optional[str] = None
    ai_providers: list[LLMProviderConfig] = Field(default_factory=list)
    ai_provider_api_keys: dict[str, SecretStr] = Field(
        default_factory=dict, repr=False
    )
    ai_default_provider: str = "default"
    ai_light_provider: str = "default"
    ai_vision_provider: str = "default"
    ai_default_thinking: ThinkingMode = "auto"
    ai_light_thinking: ThinkingMode = "disabled"
    ai_vision_thinking: ThinkingMode = "disabled"
    ai_default_multimodal: MultimodalMode = "auto"
    ai_light_multimodal: MultimodalMode = "auto"

    agent_dialogue_llm_profile: LLMProfile = "default"
    agent_dialogue_thinking: TaskThinkingMode = "inherit"
    agent_proactive_llm_profile: LLMProfile = "light"
    agent_proactive_thinking: TaskThinkingMode = "inherit"
    agent_memory_llm_profile: LLMProfile = "light"
    agent_memory_thinking: TaskThinkingMode = "inherit"
    agent_image_llm_profile: LLMProfile = "vision"
    agent_image_thinking: TaskThinkingMode = "inherit"

    rpg_kp_llm_profile: LLMProfile = "default"
    rpg_kp_thinking: TaskThinkingMode = "inherit"
    rpg_npc_router_llm_profile: LLMProfile = "light"
    rpg_npc_router_thinking: TaskThinkingMode = "inherit"
    rpg_npc_llm_profile: LLMProfile = "light"
    rpg_npc_thinking: TaskThinkingMode = "inherit"

    ww_decision_llm_profile: LLMProfile = "default"
    ww_decision_thinking: TaskThinkingMode = "inherit"
    ww_speech_llm_profile: LLMProfile = "light"
    ww_speech_thinking: TaskThinkingMode = "inherit"

    agent_media_cache_ttl: int = 86400
    agent_media_cache_dir: str = "data/agent_media"
    # NapCat/QQNT received-image segments expose signed image URLs on these
    # QQ-owned CDN hosts.  This remains an exact allowlist: explicitly setting
    # AGENT_MEDIA_ALLOWED_HOSTS= still disables remote image downloads.
    agent_media_allowed_hosts: str = "gchat.qpic.cn,multimedia.nt.qq.com.cn"

    @model_validator(mode="after")
    def validate_providers(self) -> "LLMRoutingConfig":
        if len(self.ai_providers) > _MAX_CUSTOM_PROVIDERS:
            raise ValueError(f"AI_PROVIDERS 最多 {_MAX_CUSTOM_PROVIDERS} 个")
        provider_ids = [item.id for item in self.ai_providers]
        if "default" in provider_ids:
            raise ValueError("default 是内置提供商 ID，不能在 AI_PROVIDERS 中重复定义")
        if len(provider_ids) != len(set(provider_ids)):
            raise ValueError("AI_PROVIDERS 中的提供商 ID 必须唯一")
        unknown_keys = set(self.ai_provider_api_keys) - set(provider_ids)
        if unknown_keys:
            raise ValueError(
                "AI_PROVIDER_API_KEYS 包含未定义的提供商："
                + ", ".join(sorted(unknown_keys))
            )
        known = {"default", *provider_ids}
        for field_name in (
            "ai_default_provider",
            "ai_light_provider",
            "ai_vision_provider",
        ):
            provider_id = getattr(self, field_name)
            if not _PROVIDER_ID_RE.fullmatch(provider_id) or provider_id not in known:
                raise ValueError(f"{field_name.upper()} 引用了未知提供商：{provider_id}")
        return self


class AIChatConfig(LLMRoutingConfig):
    """AI 服务配置，字段从 .env / 环境变量读取。"""

    # AI 是可选能力，确定性 RPG 部署无需配置密钥也可启动。
    # SecretStr keeps NoneBot's startup configuration dump from printing the
    # raw credential while remaining backward compatible with tests and
    # callers that assign a plain string at runtime.
    ai_api_key: Optional[SecretStr] = Field(default=None, repr=False)
    ai_base_url: str = "https://token-plan-cn.xiaomimimo.com/v1"
    ai_model: str = "mimo-v2.5-pro"
    ai_max_tokens: int = 1024  # 单次生成的最大 token 数


ai_config = get_plugin_config(AIChatConfig)

# 仅在实际发起 AI 请求时构造 SDK 客户端。缓存键只保留密钥摘要。
_client_pool: dict[tuple[str, str, str], AsyncOpenAI] = {}


def _record_ai_metric(operation: str, outcome: str, started: float) -> None:
    """把 LLM 调用观测写入进程指标；指标故障不影响调用方。"""

    try:
        from .metrics import record_ai_degradation, record_ai_request

        record_ai_request(
            operation,
            outcome,
            max(time.perf_counter() - started, 0.0),
        )
        if outcome != "success":
            record_ai_degradation("llm", outcome)
    except Exception:  # noqa: BLE001
        logger.debug("AI 指标更新失败", exc_info=True)


def _usage_value(value: object, name: str) -> object:
    if isinstance(value, dict):
        return value.get(name)
    return getattr(value, name, None)


def _prompt_cache_usage(usage: object | None) -> tuple[int | None, int | None]:
    """Return provider-reported prompt cache hit/miss token counts.

    DeepSeek exposes ``prompt_cache_hit_tokens`` / ``prompt_cache_miss_tokens``
    directly on ``usage``. OpenAI-compatible endpoints commonly expose only
    ``prompt_tokens_details.cached_tokens``; in that case the miss count can be
    derived from total prompt tokens without changing billing semantics.
    """

    if usage is None:
        return None, None
    prompt_tokens = _usage_value(usage, "prompt_tokens")
    deepseek_hit = _usage_value(usage, "prompt_cache_hit_tokens")
    deepseek_miss = _usage_value(usage, "prompt_cache_miss_tokens")
    if isinstance(deepseek_hit, int) or isinstance(deepseek_miss, int):
        return (
            int(deepseek_hit) if isinstance(deepseek_hit, int) else None,
            int(deepseek_miss) if isinstance(deepseek_miss, int) else None,
        )
    details = _usage_value(usage, "prompt_tokens_details")
    cached = _usage_value(details, "cached_tokens") if details else None
    hit = int(cached) if isinstance(cached, int) else None
    miss = (
        max(int(prompt_tokens) - hit, 0)
        if isinstance(prompt_tokens, int) and hit is not None
        else None
    )
    return hit, miss


def _record_ai_usage(operation: str, response: object) -> None:
    """记录兼容端点可选的 usage；字段缺失时保持静默。"""

    try:
        from .metrics import record_ai_tokens

        usage = _usage_value(response, "usage")
        if usage is None:
            return
        prompt_tokens = _usage_value(usage, "prompt_tokens")
        completion_tokens = _usage_value(usage, "completion_tokens")
        cached_tokens, cache_miss_tokens = _prompt_cache_usage(usage)
        for source, value in (
            ("input", prompt_tokens),
            ("output", completion_tokens),
            ("cached", cached_tokens),
            ("cache_miss", cache_miss_tokens),
        ):
            if isinstance(value, int) and value > 0:
                record_ai_tokens(operation, source, value)
    except Exception:  # noqa: BLE001
        logger.debug("AI token 指标更新失败", exc_info=True)


def _secret_value(value: object) -> str | None:
    if isinstance(value, SecretStr):
        return value.get_secret_value()
    if value is None:
        return None
    text = str(value).strip()
    return text or None


@dataclass(frozen=True, slots=True)
class LLMRequestConfig:
    """一次任务调用最终使用的模型档位和请求参数。"""

    task: LLMTask
    profile: LLMProfile
    provider: str
    model: str
    thinking: ThinkingMode
    multimodal: MultimodalMode

    @property
    def extra_body(self) -> dict[str, Any] | None:
        if self.thinking == "auto":
            return None
        return {"thinking": {"type": self.thinking}}


def _configured_text(field_name: str) -> str:
    return str(getattr(ai_config, field_name, "") or "").strip()


def resolve_llm_request(task: LLMTask = "core_chat") -> LLMRequestConfig:
    """按任务解析模型档位、推理策略和多模态能力。"""

    if task == "core_chat":
        profile: LLMProfile = "default"
        task_thinking: TaskThinkingMode = "inherit"
    else:
        profile = getattr(ai_config, f"{task}_llm_profile")
        task_thinking = getattr(ai_config, f"{task}_thinking")

    if profile == "light":
        configured_model = _configured_text("ai_light_model")
        model = configured_model or ai_config.ai_model
        provider = (
            ai_config.ai_light_provider
            if configured_model
            else ai_config.ai_default_provider
        )
        global_thinking = ai_config.ai_light_thinking
        multimodal = ai_config.ai_light_multimodal
    elif profile == "vision":
        configured_model = _configured_text("ai_vision_model")
        model = configured_model or ai_config.ai_model
        provider = (
            ai_config.ai_vision_provider
            if configured_model
            else ai_config.ai_default_provider
        )
        global_thinking = ai_config.ai_vision_thinking
        multimodal = "supported"
    else:
        model = ai_config.ai_model
        provider = ai_config.ai_default_provider
        global_thinking = ai_config.ai_default_thinking
        multimodal = ai_config.ai_default_multimodal

    thinking = global_thinking if task_thinking == "inherit" else task_thinking
    return LLMRequestConfig(
        task=task,
        profile=profile,
        provider=provider,
        model=model,
        thinking=thinking,
        multimodal=multimodal,
    )


def vision_model_configured() -> bool:
    """识图任务所选档位是否有可用且未声明不支持图片的模型。"""

    request = resolve_llm_request("agent_image")
    if request.profile == "vision":
        return bool(_configured_text("ai_vision_model")) and get_client(
            request.provider
        ) is not None
    return (
        request.multimodal != "unsupported"
        and bool(request.model.strip())
        and get_client(request.provider) is not None
    )


def resolve_provider(provider_id: str = "default") -> tuple[str, str | None]:
    """解析提供商的 Base URL 和密钥；密钥缺失时返回 ``None``。"""

    if provider_id == "default":
        return ai_config.ai_base_url, _secret_value(ai_config.ai_api_key)
    provider = next(
        (item for item in ai_config.ai_providers if item.id == provider_id), None
    )
    if provider is None:
        return "", None
    return provider.base_url, _secret_value(
        ai_config.ai_provider_api_keys.get(provider_id)
    )


def get_client(provider_id: str = "default") -> Optional[AsyncOpenAI]:
    """返回指定提供商的共享客户端；未配置密钥时返回 ``None``。"""

    resolved_base_url, resolved_key = resolve_provider(provider_id)
    if not resolved_key:
        return None
    key_digest = hashlib.sha256(resolved_key.encode("utf-8")).hexdigest()
    pool_key = (provider_id, resolved_base_url, key_digest)
    pooled = _client_pool.get(pool_key)
    if pooled is not None:
        return pooled
    created = AsyncOpenAI(api_key=resolved_key, base_url=resolved_base_url)
    _client_pool[pool_key] = created
    return created


async def test_llm_connection(
    *, base_url: str, api_key: str, model: str, timeout: float = 10.0
) -> float:
    """用极短补全验证一组草稿配置，返回毫秒耗时。"""

    started = time.perf_counter()
    temporary = AsyncOpenAI(api_key=api_key, base_url=base_url)
    try:
        await asyncio.wait_for(
            temporary.chat.completions.create(
                model=model,
                messages=[{"role": "user", "content": "Reply with OK."}],
                stream=False,
                max_tokens=8,
            ),
            timeout=timeout,
        )
    finally:
        await temporary.close()
    return max(time.perf_counter() - started, 0.0) * 1000


# 非流式补全的并发上限：防止满桌 AI 的并发决策饿死 /对话 等交互调用
_COMPLETION_CONCURRENCY = asyncio.Semaphore(6)


async def complete(  # noqa: PLR0911
    messages: list[ChatCompletionMessageParam],
    *,
    max_tokens: Optional[int] = None,
    temperature: Optional[float] = None,
    timeout: float = 25.0,
    task: LLMTask = "core_chat",
    response_format: dict[str, Any] | None = None,
    multimodal: bool = False,
) -> Optional[str]:
    """非流式补全：返回完整回复文本；失败/超时/空回复返回 None。"""
    _ = multimodal  # reserved for role-specific provider capability handling
    started = time.perf_counter()
    outcome = "error"
    result: Optional[str] = None
    # 部分 OpenAI 兼容端点会把显式 null 的 temperature 拒成 400，
    # 仅在调用方显式给值时才加入请求参数
    extra: dict[str, Any] = {}
    if temperature is not None:
        extra["temperature"] = temperature
    try:
        request = resolve_llm_request(task)
        llm_client = get_client(request.provider)
        if llm_client is None:
            outcome = "not_configured"
            logger.warning(
                f"LLM 提供商未配置密钥（task={task}, provider={request.provider}）"
            )
            return None
        async with _COMPLETION_CONCURRENCY:
            try:
                response = await asyncio.wait_for(
                    llm_client.chat.completions.create(
                        model=request.model,
                        messages=messages,
                        stream=False,
                        max_tokens=(
                            max_tokens
                            if max_tokens is not None
                            else ai_config.ai_max_tokens
                        ),
                        **(
                            {"response_format": response_format}
                            if response_format
                            else {}
                        ),
                        **(
                            {"extra_body": request.extra_body}
                            if request.extra_body is not None
                            else {}
                        ),
                        **extra,
                    ),
                    timeout=timeout,
                )
                _record_ai_usage(task, response)
            except asyncio.TimeoutError:
                outcome = "timeout"
                logger.warning(
                    f"LLM 非流式补全超时（task={task}, model={request.model}, "
                    f"timeout={timeout}s）",
                    exc_info=True,
                )
                return None
            except OpenAIError:
                logger.warning(
                    f"LLM 非流式补全失败（task={task}, model={request.model}, "
                    f"timeout={timeout}s）",
                    exc_info=True,
                )
                return None
        if not response.choices:
            outcome = "empty"
            logger.warning(f"LLM 未返回任何 choice（task={task}, model={request.model}）")
            return None
        choice = response.choices[0]
        content = (choice.message.content or "").strip()
        if not content:
            outcome = "empty"
            # 推理模型可能把 token 全部耗在 reasoning 上，或被 max_tokens
            # 截断（finish_reason="length"）：留下诊断信息而不是静默 None
            logger.warning(
                f"LLM 返回空内容"
                f"（task={task}, model={request.model}, finish_reason={choice.finish_reason}）"
            )
            return None
        outcome = "success"
        logger.debug(
            f"LLM 补全成功（task={task}, model={request.model}, 长度={len(content)}）"
        )
        result = content
    except asyncio.CancelledError:
        outcome = "cancelled"
        raise
    except Exception:  # noqa: BLE001
        outcome = "error"
        logger.warning(
            "LLM 非流式补全发生未预期异常（task=%s）",
            task,
            exc_info=True,
        )
        return None
    finally:
        _record_ai_metric(task, outcome, started)
    return result


@dataclass(frozen=True, slots=True)
class LLMToolCompletionResult:
    """工具补全的消息与只读诊断元数据。"""

    message: ChatCompletionMessage | None
    finish_reason: str | None
    prompt_tokens: int | None
    completion_tokens: int | None
    cached_tokens: int | None
    cache_miss_tokens: int | None
    outcome: str
    duration_ms: float


def _tool_completion_result(
    *,
    message: ChatCompletionMessage | None,
    finish_reason: object = None,
    response: object = None,
    outcome: str,
    started: float,
) -> LLMToolCompletionResult:
    usage = _usage_value(response, "usage") if response is not None else None
    cached_tokens, cache_miss_tokens = _prompt_cache_usage(usage)

    def optional_int(value: object) -> int | None:
        return int(value) if isinstance(value, int) else None

    return LLMToolCompletionResult(
        message=message,
        finish_reason=str(finish_reason) if finish_reason is not None else None,
        prompt_tokens=optional_int(_usage_value(usage, "prompt_tokens")),
        completion_tokens=optional_int(_usage_value(usage, "completion_tokens")),
        cached_tokens=cached_tokens,
        cache_miss_tokens=cache_miss_tokens,
        outcome=outcome,
        duration_ms=max(time.perf_counter() - started, 0.0) * 1000,
    )


async def complete_with_tools_result(  # noqa: C901,PLR0911
    messages: list[ChatCompletionMessageParam],
    tools: list[ChatCompletionToolParam],
    *,
    max_tokens: Optional[int] = None,
    temperature: Optional[float] = None,
    timeout: float = 40.0,
    task: LLMTask = "core_chat",
    response_format: dict[str, Any] | None = None,
    multimodal: bool = False,
    raise_on_unsupported: bool = False,
) -> LLMToolCompletionResult:
    """带工具调用的非流式补全，并返回 WebUI 可展示的诊断元数据。

    面向 agentic 场景（如跑团 KP 通过 tool_call 驱动系统判定）。失败、
    超时或空回复通过 outcome 和空 message 表达；成功时保留原始 message，
    供调用方读取 content/tool_calls，并额外返回结束原因、Token 与耗时。
    """
    started = time.perf_counter()
    outcome = "error"
    extra: dict[str, Any] = {}
    if temperature is not None:
        extra["temperature"] = temperature
    try:
        request = resolve_llm_request(task)
        llm_client = get_client(request.provider)
        if llm_client is None:
            outcome = "not_configured"
            logger.warning(
                f"LLM 提供商未配置密钥（task={task}, provider={request.provider}）"
            )
            return _tool_completion_result(
                message=None, outcome=outcome, started=started
            )
        async with _COMPLETION_CONCURRENCY:
            try:
                response = await asyncio.wait_for(
                    llm_client.chat.completions.create(
                        model=request.model,
                        messages=messages,
                        # OpenAI 官方及多数兼容端点对空 tools 数组直接 400；
                        # 无工具场景（如主动发言）不传该参数。
                        **({"tools": tools} if tools else {}),
                        stream=False,
                        max_tokens=(
                            max_tokens
                            if max_tokens is not None
                            else ai_config.ai_max_tokens
                        ),
                        **(
                            {"response_format": response_format}
                            if response_format
                            else {}
                        ),
                        **(
                            {"extra_body": request.extra_body}
                            if request.extra_body is not None
                            else {}
                        ),
                        **extra,
                    ),
                    timeout=timeout,
                )
                _record_ai_usage(task, response)
            except asyncio.TimeoutError:
                outcome = "timeout"
                logger.warning(
                    f"LLM 工具补全超时（task={task}, model={request.model}, "
                    f"timeout={timeout}s）",
                    exc_info=True,
                )
                return _tool_completion_result(
                    message=None, outcome=outcome, started=started
                )
            except OpenAIError as exc:
                if multimodal and _looks_like_multimodal_unsupported(exc):
                    outcome = "unsupported_multimodal"
                    if raise_on_unsupported:
                        raise LLMMultimodalUnsupportedError(str(exc)) from exc
                logger.warning(
                    f"LLM 工具补全失败（task={task}, model={request.model}, "
                    f"timeout={timeout}s）",
                    exc_info=True,
                )
                return _tool_completion_result(
                    message=None, outcome=outcome, started=started
                )
        if not response.choices:
            outcome = "empty"
            logger.warning(f"LLM 未返回任何 choice（task={task}, model={request.model}）")
            return _tool_completion_result(
                message=None, outcome=outcome, started=started, response=response
            )
        choice = response.choices[0]
        message = choice.message
        content = (message.content or "").strip()
        if not content and not message.tool_calls:
            outcome = "empty"
            # 既无文本又无工具调用：推理截断或端点异常，留诊断信息
            logger.warning(
                f"LLM 工具补全返回空内容"
                f"（task={task}, model={request.model}, finish_reason={choice.finish_reason}）"
            )
            return _tool_completion_result(
                message=None,
                finish_reason=choice.finish_reason,
                response=response,
                outcome=outcome,
                started=started,
            )
        outcome = "success"
        logger.debug(
            f"LLM 工具补全成功（task={task}, model={request.model}, "
            f"tool_calls={len(message.tool_calls or [])}, 文本长度={len(content)}）"
        )
        return _tool_completion_result(
            message=message,
            finish_reason=choice.finish_reason,
            response=response,
            outcome=outcome,
            started=started,
        )
    except LLMMultimodalUnsupportedError:
        raise
    except asyncio.CancelledError:
        outcome = "cancelled"
        raise
    except Exception:  # noqa: BLE001
        outcome = "error"
        logger.warning(
            "LLM 工具补全发生未预期异常（task=%s）",
            task,
            exc_info=True,
        )
        return _tool_completion_result(message=None, outcome=outcome, started=started)
    finally:
        _record_ai_metric(task, outcome, started)
    return _tool_completion_result(message=None, outcome=outcome, started=started)


async def complete_with_tools(
    messages: list[ChatCompletionMessageParam],
    tools: list[ChatCompletionToolParam],
    *,
    max_tokens: Optional[int] = None,
    temperature: Optional[float] = None,
    timeout: float = 40.0,
    task: LLMTask = "core_chat",
    response_format: dict[str, Any] | None = None,
    multimodal: bool = False,
    raise_on_unsupported: bool = False,
) -> Optional[ChatCompletionMessage]:
    """兼容现有调用方，只返回完整 message（含 tool_calls）。"""

    result = await complete_with_tools_result(
        messages,
        tools,
        max_tokens=max_tokens,
        temperature=temperature,
        timeout=timeout,
        task=task,
        response_format=response_format,
        multimodal=multimodal,
        raise_on_unsupported=raise_on_unsupported,
    )
    return result.message


class LLMMultimodalUnsupportedError(RuntimeError):
    """The selected endpoint/model explicitly rejected image input."""


def _looks_like_multimodal_unsupported(error: BaseException) -> bool:
    text = str(error).lower()
    markers = (
        "multimodal",
        "vision",
        "image_url",
        "image input",
        "does not support image",
        "unsupported content type",
    )
    return any(marker in text for marker in markers)
