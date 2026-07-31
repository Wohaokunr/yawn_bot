"""共享 LLM 客户端：OpenAI 兼容端点的配置、单例与非流式补全。

ai_chat 的流式对话与 yawn_werewolf 的 AI 玩家共用同一客户端与
配置（字段名沿用 ai_* 前缀，.env 无需改动）。非流式 complete()
面向"一次调用拿完整结果"的场景（如狼人杀 AI 决策），带总超时
与并发上限：失败一律返回 None，由调用方决定降级策略。
"""

import asyncio
from typing import Any, Optional

from nonebot import get_plugin_config, logger
from openai import AsyncOpenAI, OpenAIError
from openai.types.chat import ChatCompletionMessageParam
from pydantic import BaseModel


class AIChatConfig(BaseModel):
    """AI 服务配置，字段从 .env / 环境变量读取。"""

    ai_api_key: str  # 必填，缺失时启动即报错
    ai_base_url: str = "https://token-plan-cn.xiaomimimo.com/v1"
    ai_model: str = "mimo-v2.5-pro"
    ai_max_tokens: int = 1024  # 单次生成的最大 token 数


ai_config = get_plugin_config(AIChatConfig)

client = AsyncOpenAI(
    api_key=ai_config.ai_api_key,
    base_url=ai_config.ai_base_url,
)

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
    async with _COMPLETION_CONCURRENCY:
        try:
            response = await asyncio.wait_for(
                client.chat.completions.create(
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
