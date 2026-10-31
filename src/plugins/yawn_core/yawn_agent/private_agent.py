# ruff: noqa: C901, TID252, TC003
"""私聊 Agent 的一轮：人设 + 用户资料 + 工具循环 → 清洗 → 分条发送 → 好感与记忆。

由 ``ai_chat`` 经 ``private_chat_bridge`` 调用（父插件串行队列保证同一用户不并发）。
返回实际送达的文本，ai_chat 负责把它写进对话历史；返回 None 表示放弃本轮，
ai_chat 会退回原来的纯对话，所以这里任何“不确定”的情况都应返回 None 而不是硬答。

约束（AGENTS.md「私聊 Agent」）：

- 所有模型请求不传 max_tokens（``llm.NO_OUTPUT_LIMIT``）；
- 提示词每一段都登记在 ``prompt_sections``（类别 private）；
- 工具只作用于用户本人；写操作成功后立即提交，避免后面模型失败把签到回滚掉；
- “已完成”声明用 ``speech_claims`` 核验；
- 每人每天轮数有上限，超出退回纯对话。
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime
from typing import Any

from nonebot import get_driver
from nonebot.adapters.onebot.v11 import MessageSegment
from sqlalchemy import func, select

from .. import affinity, llm
from ..data_models.chat_message import ChatMessage
from ..data_models.chat_session import ChatSession
from ..private_chat_bridge import PrivateTurnRequest, private_agent_config
from .context import now_beijing
from .log import dbg, dbg_exc
from .persona import prompt_persona, resolve_persona
from .private_context import build_private_context
from .private_memory import (
    consolidate_in_background,
    should_consolidate,
    user_message_count,
)
from .private_prompt import (
    PRIVATE_CONTEXT_HEADER,
    PRIVATE_FINAL_ROUND,
    PRIVATE_ROLE,
    PRIVATE_RULES,
    PRIVATE_SAFETY,
)
from .private_tools import PrivateToolContext, execute_private_tool, tool_schemas
from .prompt_sections import ContentBuilder, apply_section
from .speech_claims import check_action_claims
from .speech_format import normalize_qq_text

# 私聊里分条的标记，与群聊一致。
SPLIT_MARK = "|||"
MAX_PARTS = 4
# 分条之间的停顿（秒），像真人打字。
PART_PAUSE_SECONDS = 0.8
# 历史最多带多少条（ai_chat 已经按 20 条截断，这里再按字符兜底）。
HISTORY_CHAR_BUDGET = 6000
COMPLETION_TIMEOUT = 60.0
# 私聊工具名 → 声明核验用的工具名（与群聊共用 speech_claims 的声明族）。
_CLAIM_ALIASES = {"checkin": "checkin_for_me"}
# 私聊里用户找 Bot 聊一轮给的好感；每日上限见 affinity.SOURCE_RULES。
PRIVATE_CHAT_AFFINITY = 1

_background: set[asyncio.Task[Any]] = set()


async def turns_today(session: Any, user_id: int, now: datetime) -> int:
    """今天 Bot 在私聊里回了这个人几次（assistant 消息数）。"""

    start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    return int(
        await session.scalar(
            select(func.count())
            .select_from(ChatMessage)
            .join(ChatSession, ChatSession.id == ChatMessage.session_id)
            .where(
                ChatSession.user_id == user_id,
                ChatSession.group_id.is_(None),
                ChatMessage.role == "assistant",
                ChatMessage.created_at >= start,
            )
        )
        or 0
    )


def _trim_history(history: list[dict[str, str]]) -> list[dict[str, str]]:
    """从新往旧装，超出字符预算就停；保证以 user 开头。"""

    kept: list[dict[str, str]] = []
    used = 0
    for item in reversed(history):
        content = str(item.get("content") or "")
        if used + len(content) > HISTORY_CHAR_BUDGET and kept:
            break
        kept.append({"role": item["role"], "content": content})
        used += len(content)
    kept.reverse()
    while kept and kept[0]["role"] != "user":
        kept.pop(0)
    return kept


def build_messages(
    persona: dict[str, Any], context_json: str, request: PrivateTurnRequest
) -> list[dict[str, Any]]:
    """system（角色、人设、规则、安全）+ system（用户资料）+ 历史。"""

    core = ContentBuilder()
    core.add("private.role", apply_section("private.role", PRIVATE_ROLE))
    core.add(
        "private.persona",
        apply_section("private.persona", f"\n角色：{prompt_persona(persona)}"),
    )
    core.add("private.rules", apply_section("private.rules", "\n" + PRIVATE_RULES))
    core.add("private.safety", apply_section("private.safety", "\n" + PRIVATE_SAFETY))
    core_text = core.build()[0].strip()
    profile = ContentBuilder()
    profile.add(
        "private.context_header",
        apply_section("private.context_header", PRIVATE_CONTEXT_HEADER),
    )
    profile.add("private.context", apply_section("private.context", context_json))
    if request.time_note:
        profile.add(
            "private.time_note",
            apply_section("private.time_note", "\n" + request.time_note),
        )
    profile_text = profile.build()[0].strip()
    messages: list[dict[str, Any]] = []
    if core_text:
        messages.append({"role": "system", "content": core_text})
    if profile_text:
        messages.append({"role": "system", "content": profile_text})
    messages.extend(_trim_history(request.history))
    if not messages or messages[-1].get("role") != "user":
        messages.append({"role": "user", "content": request.user_input})
    return messages


def split_reply(text: str) -> list[str]:
    """按 ||| 分条，清洗每条；空条丢弃，最多 MAX_PARTS 条（多的并进最后一条）。"""

    parts = [
        normalize_qq_text(part).text.strip()
        for part in str(text or "").split(SPLIT_MARK)
    ]
    parts = [part for part in parts if part]
    if len(parts) > MAX_PARTS:
        parts = [*parts[: MAX_PARTS - 1], "\n".join(parts[MAX_PARTS - 1 :])]
    return parts


def verify_claims(text: str, succeeded: set[str]) -> str:
    verified = set(succeeded) | {
        _CLAIM_ALIASES[name] for name in succeeded if name in _CLAIM_ALIASES
    }
    check = check_action_claims(text, frozenset(verified))
    if check.changed:
        dbg(f"私聊回复去掉了没有依据的完成声明: {check.families}")
    return check.text


def _assistant_with_calls(message: Any) -> dict[str, Any]:
    calls = []
    for call in getattr(message, "tool_calls", None) or []:
        function = getattr(call, "function", None)
        if function is None:
            continue
        calls.append(
            {
                "id": str(getattr(call, "id", "") or ""),
                "type": "function",
                "function": {
                    "name": str(getattr(function, "name", "") or ""),
                    "arguments": str(getattr(function, "arguments", "") or "{}"),
                },
            }
        )
    payload: dict[str, Any] = {
        "role": "assistant",
        "content": getattr(message, "content", None) or None,
        "tool_calls": calls,
    }
    reasoning = getattr(message, "reasoning_content", None)
    if reasoning:
        payload["reasoning_content"] = reasoning
    return payload


def _parse_args(raw: str) -> Any:
    try:
        value = json.loads(raw or "{}")
    except json.JSONDecodeError:
        return {"__invalid__": True}
    return value if isinstance(value, dict) else {"__invalid__": True}


async def _run_tools(
    message: Any, messages: list[dict[str, Any]], ctx: PrivateToolContext
) -> None:
    messages.append(_assistant_with_calls(message))
    for call in messages[-1]["tool_calls"]:
        name = call["function"]["name"]
        args = _parse_args(call["function"]["arguments"])
        if args.get("__invalid__"):
            payload: dict[str, Any] = {
                "ok": False,
                "error": "参数不是合法的 JSON 对象",
                "error_code": "invalid_args",
                "hint": "按 schema 重新给出参数",
            }
        else:
            payload = await execute_private_tool(name, args, ctx)
        dbg(f"私聊用户 {ctx.user_id} 工具 {name} ok={payload.get('ok')}")
        messages.append(
            {
                "role": "tool",
                "tool_call_id": call["id"],
                "content": json.dumps(payload, ensure_ascii=False, default=str),
            }
        )
    if ctx.mutated:
        # 写操作立刻落库：后面模型失败、退回纯对话时签到/记忆不能跟着丢。
        await ctx.session.commit()
        ctx.mutated = False


async def _send_parts(request: PrivateTurnRequest, parts: list[str]) -> list[str]:
    delivered: list[str] = []
    for index, part in enumerate(parts):
        if index:
            await asyncio.sleep(PART_PAUSE_SECONDS)
        try:
            await request.bot.send(request.event, MessageSegment.text(part))
        except Exception:  # noqa: BLE001
            dbg_exc(
                f"私聊用户 {request.user_id} 分条 {index + 1} 发送失败，停止后续分条"
            )
            break
        delivered.append(part)
    return delivered


def _is_superuser(user_id: int) -> bool:
    try:
        return str(user_id) in get_driver().config.superusers
    except Exception:  # noqa: BLE001
        return False


async def handle_private_turn(request: PrivateTurnRequest) -> str | None:
    """私聊 Agent 一轮；返回送达的文本，放弃时返回 None。"""

    session = request.session
    user_id = request.user_id
    now = now_beijing()
    cap = int(private_agent_config.private_agent_daily_turns)
    if cap and await turns_today(session, user_id, now) >= cap:
        dbg(f"私聊用户 {user_id} 今日 Agent 轮数已达 {cap}，退回纯对话")
        return None
    try:
        bot_id: int | None = int(request.bot.self_id)
    except (TypeError, ValueError):
        bot_id = None

    context = await build_private_context(
        session, user_id, bot_id=bot_id, nickname=request.nickname, now=now
    )
    persona = resolve_persona(None)
    messages = build_messages(persona, context.render(), request)
    tools = tool_schemas()
    ctx = PrivateToolContext(
        session=session,
        user_id=user_id,
        bot_id=bot_id,
        groups=context.groups,
        now=now,
        nickname=request.nickname,
        is_superuser=_is_superuser(user_id),
    )
    max_rounds = int(private_agent_config.private_agent_max_tool_rounds)
    text = ""
    for round_index in range(max_rounds + 1):
        final = round_index == max_rounds
        if final and round_index > 0:
            messages.append(
                {
                    "role": "system",
                    "content": apply_section(
                        "private.final_round", PRIVATE_FINAL_ROUND
                    ),
                }
            )
        result = await llm.complete_with_tools_result(
            messages,  # pyright: ignore[reportArgumentType]
            [] if final else tools,  # pyright: ignore[reportArgumentType]
            task="agent_dialogue",
            max_tokens=llm.NO_OUTPUT_LIMIT,
            timeout=COMPLETION_TIMEOUT,
        )
        message = result.message
        if message is None:
            dbg(f"私聊用户 {user_id} 模型无回复（{result.outcome}）")
            break
        if getattr(message, "tool_calls", None) and not final:
            await _run_tools(message, messages, ctx)
            continue
        text = str(getattr(message, "content", "") or "")
        break

    if not text.strip():
        if ctx.succeeded & {"checkin", "remember_about_me", "forget_about_me"}:
            text = "好啦，已经帮你处理好了~"
        else:
            return None
    text = verify_claims(text, ctx.succeeded)
    parts = split_reply(text)
    if not parts:
        return None
    delivered = await _send_parts(request, parts)
    if not delivered:
        return None
    await _after_turn(session, user_id, now)
    return "\n".join(delivered)


async def _after_turn(session: Any, user_id: int, now: datetime) -> None:
    """好感 +1（有日上限）、到点触发私聊记忆整理。都不能影响已发出的回复。"""

    try:
        await affinity.grant(
            session,
            user_id,
            "private_chat",
            PRIVATE_CHAT_AFFINITY,
            note="私聊聊天",
            now=now,
        )
        await session.commit()
    except Exception:  # noqa: BLE001
        dbg_exc(f"私聊用户 {user_id} 好感记录失败(忽略)")
        try:
            await session.rollback()
        except Exception:  # noqa: BLE001
            dbg_exc("私聊好感回滚失败(忽略)")
    try:
        count = await user_message_count(session, user_id)
    except Exception:  # noqa: BLE001
        return
    if should_consolidate(count):
        task = asyncio.create_task(consolidate_in_background(user_id, now))
        _background.add(task)
        task.add_done_callback(_background.discard)


__all__ = [
    "MAX_PARTS",
    "SPLIT_MARK",
    "build_messages",
    "handle_private_turn",
    "split_reply",
    "turns_today",
    "verify_claims",
]
