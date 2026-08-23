# ruff: noqa: E501,PLR0913
"""共享 LLM 客户端：OpenAI 兼容端点的配置、单例与非流式补全。

ai_chat 的流式对话与各 AI 子插件共用同一客户端、三级模型档位与
任务路由配置。非流式 complete()
面向"一次调用拿完整结果"的场景（如狼人杀 AI 决策），带总超时
与并发上限：失败一律返回 None，由调用方决定降级策略。
complete_with_tools() 面向 agentic 场景（如跑团 KP 经 tool_call
驱动系统判定），返回完整 message 供调用方续接工具循环。
"""

import asyncio
import time
from dataclasses import dataclass
from typing import Any, Literal, Optional

from nonebot import get_plugin_config, logger
from openai import AsyncOpenAI, OpenAIError
from openai.types.chat import (
    ChatCompletionMessage,
    ChatCompletionMessageParam,
    ChatCompletionToolParam,
)
from pydantic import BaseModel, Field, SecretStr

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


class LLMRoutingConfig(BaseModel):
    """共享模型档位、能力以及各子插件任务的路由配置。"""

    ai_light_model: Optional[str] = None
    ai_vision_model: Optional[str] = None
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
    agent_media_allowed_hosts: str = ""


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

# 仅在实际发起 AI 请求时构造 SDK 客户端。
client: Optional[AsyncOpenAI] = None
_client_pool: dict[tuple[str, str], AsyncOpenAI] = {}


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
        model = _configured_text("ai_light_model") or ai_config.ai_model
        global_thinking = ai_config.ai_light_thinking
        multimodal = ai_config.ai_light_multimodal
    elif profile == "vision":
        model = _configured_text("ai_vision_model") or ai_config.ai_model
        global_thinking = ai_config.ai_vision_thinking
        multimodal = "supported"
    else:
        model = ai_config.ai_model
        global_thinking = ai_config.ai_default_thinking
        multimodal = ai_config.ai_default_multimodal

    thinking = global_thinking if task_thinking == "inherit" else task_thinking
    return LLMRequestConfig(
        task=task,
        profile=profile,
        model=model,
        thinking=thinking,
        multimodal=multimodal,
    )


def vision_model_configured() -> bool:
    """识图任务所选档位是否有可用且未声明不支持图片的模型。"""

    request = resolve_llm_request("agent_image")
    if request.profile == "vision":
        return bool(_configured_text("ai_vision_model"))
    return request.multimodal != "unsupported" and bool(request.model.strip())


def get_client(
    *, base_url: str | None = None, api_key: object | None = None
) -> Optional[AsyncOpenAI]:
    """返回按端点和密钥复用的共享客户端；未配置 AI 时返回 ``None``。"""
    global client  # noqa: PLW0603
    resolved_key = _secret_value(ai_config.ai_api_key if api_key is None else api_key)
    if not resolved_key:
        return None
    resolved_base_url = str(base_url or ai_config.ai_base_url)
    pool_key = (resolved_base_url, resolved_key)
    if base_url is None and api_key is None and client is not None:
        return client
    pooled = _client_pool.get(pool_key)
    if pooled is not None:
        if base_url is None and api_key is None:
            client = pooled
        return pooled
    created = AsyncOpenAI(api_key=resolved_key, base_url=resolved_base_url)
    _client_pool[pool_key] = created
    if base_url is None and api_key is None:
        client = created
    return created


# 非流式补全的并发上限：防止满桌 AI 的并发决策饿死 /对话 等交互调用
_COMPLETION_CONCURRENCY = asyncio.Semaphore(6)


async def complete(
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
    request = resolve_llm_request(task)
    started = time.perf_counter()
    outcome = "error"
    result: Optional[str] = None
    # 部分 OpenAI 兼容端点会把显式 null 的 temperature 拒成 400，
    # 仅在调用方显式给值时才加入请求参数
    extra: dict[str, Any] = {}
    if temperature is not None:
        extra["temperature"] = temperature
    try:
        llm_client = get_client()
        if llm_client is None:
            outcome = "not_configured"
            logger.warning("LLM 未配置 AI_API_KEY，跳过非流式补全")
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
    finally:
        _record_ai_metric(task, outcome, started)
    return result


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
    """带工具调用的非流式补全：返回完整 message（含 tool_calls）。

    面向 agentic 场景（如跑团 KP 通过 tool_call 驱动系统判定）。
    与 complete() 相同的失败语义：任何失败/超时/空回复返回 None，
    由调用方决定降级策略；不支持 tools 参数的端点会以 OpenAIError
    落入 None 分支。返回原始 message 以便调用方读取 content 与
    tool_calls 并把工具结果回填对话继续循环。
    """
    started = time.perf_counter()
    outcome = "error"
    result: Optional[ChatCompletionMessage] = None
    request = resolve_llm_request(task)
    extra: dict[str, Any] = {}
    if temperature is not None:
        extra["temperature"] = temperature
    try:
        llm_client = get_client()
        if llm_client is None:
            outcome = "not_configured"
            logger.warning("LLM 未配置 AI_API_KEY，跳过工具补全")
            return None
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
            except asyncio.TimeoutError:
                outcome = "timeout"
                logger.warning(
                    f"LLM 工具补全超时（task={task}, model={request.model}, "
                    f"timeout={timeout}s）",
                    exc_info=True,
                )
                return None
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
                return None
        if not response.choices:
            outcome = "empty"
            logger.warning(f"LLM 未返回任何 choice（task={task}, model={request.model}）")
            return None
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
            return None
        outcome = "success"
        logger.debug(
            f"LLM 工具补全成功（task={task}, model={request.model}, "
            f"tool_calls={len(message.tool_calls or [])}, 文本长度={len(content)}）"
        )
        result = message
    except asyncio.CancelledError:
        outcome = "cancelled"
        raise
    finally:
        _record_ai_metric(task, outcome, started)
    return result


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
