"""共享 LLM 客户端：OpenAI 兼容端点的配置、单例与非流式补全。

ai_chat 的流式对话与 yawn_werewolf 的 AI 玩家共用同一客户端与
配置（字段名沿用 ai_* 前缀，.env 无需改动）。非流式 complete()
面向"一次调用拿完整结果"的场景（如狼人杀 AI 决策），带总超时
与并发上限：失败一律返回 None，由调用方决定降级策略。
complete_with_tools() 面向 agentic 场景（如跑团 KP 经 tool_call
驱动系统判定），返回完整 message 供调用方续接工具循环。
"""

import asyncio
from typing import Any, Optional

from nonebot import get_plugin_config, logger
from openai import AsyncOpenAI, OpenAIError
from openai.types.chat import (
    ChatCompletionMessage,
    ChatCompletionMessageParam,
    ChatCompletionToolParam,
)
from pydantic import BaseModel


class AIChatConfig(BaseModel):
    """AI 服务配置，字段从 .env / 环境变量读取。"""

    # AI 是可选能力，确定性 RPG 部署无需配置密钥也可启动。
    ai_api_key: Optional[str] = None
    ai_base_url: str = "https://token-plan-cn.xiaomimimo.com/v1"
    ai_model: str = "mimo-v2.5-pro"
    ai_max_tokens: int = 1024  # 单次生成的最大 token 数


ai_config = get_plugin_config(AIChatConfig)

# 仅在实际发起 AI 请求时构造 SDK 客户端。
client: Optional[AsyncOpenAI] = None


def get_client() -> Optional[AsyncOpenAI]:
    """返回共享客户端；未配置 AI 时返回 ``None``。"""
    global client  # noqa: PLW0603
    if client is not None:
        return client
    api_key = ai_config.ai_api_key
    if not api_key or not api_key.strip():
        return None
    client = AsyncOpenAI(
        api_key=api_key,
        base_url=ai_config.ai_base_url,
    )
    return client

# 非流式补全的并发上限：防止满桌 AI 的并发决策饿死 /对话 等交互调用
_COMPLETION_CONCURRENCY = asyncio.Semaphore(6)


async def complete(
    messages: list[ChatCompletionMessageParam],
    *,
    max_tokens: Optional[int] = None,
    temperature: Optional[float] = None,
    timeout: float = 25.0,
) -> Optional[str]:
    """非流式补全：返回完整回复文本；失败/超时/空回复返回 None。"""
    # 部分 OpenAI 兼容端点会把显式 null 的 temperature 拒成 400，
    # 仅在调用方显式给值时才加入请求参数
    extra: dict[str, Any] = {}
    if temperature is not None:
        extra["temperature"] = temperature
    llm_client = get_client()
    if llm_client is None:
        logger.warning("LLM 未配置 AI_API_KEY，跳过非流式补全")
        return None
    async with _COMPLETION_CONCURRENCY:
        try:
            response = await asyncio.wait_for(
                llm_client.chat.completions.create(
                    model=ai_config.ai_model,
                    messages=messages,
                    stream=False,
                    max_tokens=(
                        max_tokens
                        if max_tokens is not None
                        else ai_config.ai_max_tokens
                    ),
                    **extra,
                ),
                timeout=timeout,
            )
        except (OpenAIError, asyncio.TimeoutError):
            logger.warning(
                f"LLM 非流式补全失败（model={ai_config.ai_model}, timeout={timeout}s）",
                exc_info=True,
            )
            return None
    if not response.choices:
        logger.warning(f"LLM 未返回任何 choice（model={ai_config.ai_model}）")
        return None
    choice = response.choices[0]
    content = (choice.message.content or "").strip()
    if not content:
        # 推理模型可能把 token 全部耗在 reasoning 上，或被 max_tokens
        # 截断（finish_reason="length"）：留下诊断信息而不是静默 None
        logger.warning(
            f"LLM 返回空内容"
            f"（model={ai_config.ai_model}, finish_reason={choice.finish_reason}）"
        )
        return None
    logger.debug(f"LLM 补全成功（model={ai_config.ai_model}, 长度={len(content)}）")
    return content


async def complete_with_tools(
    messages: list[ChatCompletionMessageParam],
    tools: list[ChatCompletionToolParam],
    *,
    max_tokens: Optional[int] = None,
    temperature: Optional[float] = None,
    timeout: float = 40.0,
) -> Optional[ChatCompletionMessage]:
    """带工具调用的非流式补全：返回完整 message（含 tool_calls）。

    面向 agentic 场景（如跑团 KP 通过 tool_call 驱动系统判定）。
    与 complete() 相同的失败语义：任何失败/超时/空回复返回 None，
    由调用方决定降级策略；不支持 tools 参数的端点会以 OpenAIError
    落入 None 分支。返回原始 message 以便调用方读取 content 与
    tool_calls 并把工具结果回填对话继续循环。
    """
    extra: dict[str, Any] = {}
    if temperature is not None:
        extra["temperature"] = temperature
    llm_client = get_client()
    if llm_client is None:
        logger.warning("LLM 未配置 AI_API_KEY，跳过工具补全")
        return None
    async with _COMPLETION_CONCURRENCY:
        try:
            response = await asyncio.wait_for(
                llm_client.chat.completions.create(
                    model=ai_config.ai_model,
                    messages=messages,
                    tools=tools,
                    stream=False,
                    max_tokens=(
                        max_tokens
                        if max_tokens is not None
                        else ai_config.ai_max_tokens
                    ),
                    **extra,
                ),
                timeout=timeout,
            )
        except (OpenAIError, asyncio.TimeoutError):
            logger.warning(
                f"LLM 工具补全失败（model={ai_config.ai_model}, timeout={timeout}s）",
                exc_info=True,
            )
            return None
    if not response.choices:
        logger.warning(f"LLM 未返回任何 choice（model={ai_config.ai_model}）")
        return None
    choice = response.choices[0]
    message = choice.message
    content = (message.content or "").strip()
    if not content and not message.tool_calls:
        # 既无文本又无工具调用：推理截断或端点异常，留诊断信息
        logger.warning(
            f"LLM 工具补全返回空内容"
            f"（model={ai_config.ai_model}, finish_reason={choice.finish_reason}）"
        )
        return None
    logger.debug(
        f"LLM 工具补全成功（model={ai_config.ai_model}, "
        f"tool_calls={len(message.tool_calls or [])}, 文本长度={len(content)}）"
    )
    return message
