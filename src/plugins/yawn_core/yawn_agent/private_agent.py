# ruff: noqa: C901, TID252, TC003, E501, PLR0912, PLR0913, PLR0915, PLR0911
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

发送走 ``private_delivery``（与群聊同一套节奏分条，外加表情包、虚拟电脑里的图片/文件）；
耗时多步的活由 ``delegate_task`` 派给后台子智能体（``private_tasks`` / ``private_worker``），
做完后 ``report_task`` 拿同一把回合锁跑一轮“汇报轮”，用人设把结果转述给他。
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime
from typing import Any

from nonebot import get_driver, logger
from nonebot.adapters.onebot.v11 import MessageSegment

from .. import affinity, llm
from ..private_chat_bridge import (
    PrivateTurnRequest,
    private_agent_config,
    turns_today,
)
from . import private_turn_taking as turn_taking
from .context import now_beijing
from .log import dbg, dbg_exc
from .persona import prompt_persona
from .private_context import build_private_context
from .private_memory import (
    consolidate_in_background,
    should_consolidate,
    user_message_count,
)
from .private_persona import persona_scope_needed, resolve_private_persona
from .private_prompt import (
    PRIVATE_ATTACHMENT_EXCERPT,
    PRIVATE_ATTACHMENTS_MISSING,
    PRIVATE_ATTACHMENTS_UNREAD,
    PRIVATE_AUTO_HANDOFF,
    PRIVATE_CONTEXT_GUIDE,
    PRIVATE_CONTEXT_HEADER,
    PRIVATE_COORDINATE_NOTICE,
    PRIVATE_COORDINATE_TURN,
    PRIVATE_DELEGATION,
    PRIVATE_FILES_HINT,
    PRIVATE_FINAL_ROUND,
    PRIVATE_HANDOFF_FINDINGS,
    PRIVATE_HANDOFF_NUDGE,
    PRIVATE_JUST_FINISHED,
    PRIVATE_MEDIA,
    PRIVATE_PERSONA_SCOPE,
    PRIVATE_REPEAT_GUARD,
    PRIVATE_REPORT_NOTICE,
    PRIVATE_REPORT_TURN,
    PRIVATE_RESUMED_NOTE,
    PRIVATE_ROLE,
    PRIVATE_RULES,
    PRIVATE_SAFETY,
    PRIVATE_TEAM,
    PRIVATE_TOOLKITS,
    PRIVATE_TURN_HEADER,
    PRIVATE_TURN_TAKING,
)
from .private_tools import (
    WRITE_TOOLS,
    PrivateToolContext,
    execute_private_tool,
    tool_schemas,
)
from .prompt_sections import (
    ContentBuilder,
    Span,
    apply_section,
    remember_manifest,
    use_private_user,
)
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
# 模型没给回复时（超时/空回复/上游报错）同一轮重试一次前等多久。
RETRY_PAUSE_SECONDS = 1.0
_RETRY_OUTCOMES = frozenset({"timeout", "empty", "error"})
# 和上一条回复的相似度超过这个就算“复读”（只看 12 字以上的回复）。
REPEAT_SIMILARITY = 0.88
REPEAT_MIN_CHARS = 12
# 单个工具结果进上下文的字符上限（超过的截掉尾部并注明），防止一次搜索把上下文撑爆。
TOOL_RESULT_CHAR_LIMIT = 12000
# 私聊工具名（或“工具.动作”）→ 声明核验用的群聊工具名（共用 speech_claims 的声明族）。
_CLAIM_ALIASES: dict[str, tuple[str, ...]] = {
    "delegate_task": ("delegate_task",),
    "delegate_project": ("delegate_task",),
    "checkin": ("checkin_for_me",),
    "checkin_status": ("get_my_checkin",),
    "checkin_board": ("get_my_checkin",),
    "reminders.add": ("schedule_task",),
    "reminders.snooze": ("update_scheduled_task",),
    "reminders.cancel": ("cancel_scheduled_task",),
    "my_data.wipe": ("forget_about_me",),
}
# 模型这轮做了写操作却没说话时，用兜底句子告知结果。
_WRITE_RESULTS = frozenset(
    {
        "checkin",
        "remember_about_me",
        "forget_about_me",
        "notes.add",
        "todos.add",
        "todos.done",
        "reminders.add",
        "reminders.cancel",
        "countdowns.add",
        "mood.log",
        "chat_prefs.set",
        "delegate_task",
        "delegate_project",
        "tasks.cancel",
        "tasks.answer",
        "tasks.message",
    }
)
# 私聊里用户找 Bot 聊一轮给的好感；每日上限见 affinity.SOURCE_RULES。
PRIVATE_CHAT_AFFINITY = 1

_background: set[asyncio.Task[Any]] = set()


def _trim_history(history: list[dict[str, str]]) -> list[dict[str, str]]:
    """不记锚点的历史裁剪（按字符预算从新往旧装，保证以 user 开头）。"""

    return _history_window(None, history).messages


def _history_window(user_id: int | None, history: list[dict[str, str]]) -> Any:
    from .private_cache import anchored_window

    config = private_agent_config
    return anchored_window(
        user_id,
        history,
        budget=int(getattr(config, "private_agent_history_chars", HISTORY_CHAR_BUDGET)),
        max_count=max(
            int(getattr(config, "private_agent_history_messages", 40)) - 4, 2
        ),
        drop_ratio=float(getattr(config, "private_agent_history_drop_ratio", 0.4)),
        message_chars=int(getattr(config, "private_agent_history_message_chars", 1500)),
    )


def build_messages(
    persona: dict[str, Any],
    context: Any,
    request: PrivateTurnRequest,
    *,
    computer: bool = False,
    tasks: bool = False,
    turn_style: str = "",
    allow_silence: bool = False,
    just_finished: bool = False,
    round_note: tuple[str, str] | None = None,
    spans_out: list[dict[str, Any]] | None = None,
    meta_out: dict[str, Any] | None = None,
    anchor: bool = True,
) -> list[dict[str, Any]]:
    """按“越稳定越靠前”拼一轮请求（服务商的前缀缓存只认从头开始完全相同的部分）::

        system#1 角色、人设、规则、能力、资料用法、安全   ← 只随配置/人设变
        system#2 长期资料（称呼、关系、记忆、群里的关系与近况）← 记忆整理后才变
        历史（锚定窗口，起点很多轮不动）                      ← 只在末尾追加
        本条消息
        system#3 本轮资料（时间、签到、待办、附件、刚做完的事）+ 本轮发言方式 ← 每轮都变

    ``context`` 是 ``PrivateContext``（按键分成长期/本轮两层）；传 JSON 字符串时整段当长期资料
    （兼容旧调用）。``round_note`` 是汇报轮/协调轮的说明（分段 id, 文字），放进 system#3。
    ``spans_out`` 写入每个分段的位置（WebUI 高亮）；``meta_out`` 写入分层指纹与历史窗口，
    用来诊断缓存为什么没命中。
    """

    from .private_cache import layer_digest

    core = ContentBuilder()
    core.add("private.role", apply_section("private.role", PRIVATE_ROLE))
    core.add(
        "private.persona",
        apply_section("private.persona", f"\n角色：{prompt_persona(persona)}"),
    )
    if persona_scope_needed(persona):
        core.add(
            "private.persona_scope",
            apply_section("private.persona_scope", "\n" + PRIVATE_PERSONA_SCOPE),
        )
    core.add("private.rules", apply_section("private.rules", "\n" + PRIVATE_RULES))
    if allow_silence:
        core.add(
            "private.turn_taking",
            apply_section("private.turn_taking", "\n" + PRIVATE_TURN_TAKING),
        )
    if computer:
        core.add("private.media", apply_section("private.media", "\n" + PRIVATE_MEDIA))
        core.add(
            "private.toolkits", apply_section("private.toolkits", "\n" + PRIVATE_TOOLKITS)
        )
    if tasks:
        core.add(
            "private.delegation",
            apply_section("private.delegation", "\n" + PRIVATE_DELEGATION),
        )
        core.add("private.team", apply_section("private.team", "\n" + PRIVATE_TEAM))
    core.add(
        "private.context_guide",
        apply_section("private.context_guide", "\n" + PRIVATE_CONTEXT_GUIDE),
    )
    core.add("private.rhythm", apply_section("private.rhythm", "\n" + rhythm_style(persona)))
    core.add("private.safety", apply_section("private.safety", "\n" + PRIVATE_SAFETY))
    core_text, core_spans = _stripped(core)

    if isinstance(context, str):
        stable_json, volatile_json = context, ""
    else:
        stable_json, volatile_json = context.render_stable(), context.render_volatile()
    profile = ContentBuilder()
    if stable_json:
        profile.add(
            "private.context_header",
            apply_section("private.context_header", PRIVATE_CONTEXT_HEADER),
        )
        profile.add("private.context", apply_section("private.context", stable_json))
    profile_text, profile_spans = _stripped(profile)

    turn = ContentBuilder()
    if volatile_json:
        turn.add(
            "private.turn_header",
            apply_section("private.turn_header", PRIVATE_TURN_HEADER),
        )
        turn.add("private.turn_context", apply_section("private.turn_context", volatile_json))
    if request.time_note:
        turn.add(
            "private.time_note",
            apply_section("private.time_note", "\n" + request.time_note),
        )
    if tasks and just_finished:
        turn.add(
            "private.just_finished",
            apply_section("private.just_finished", "\n" + PRIVATE_JUST_FINISHED),
        )
    if round_note:
        section_id, note = round_note
        turn.add(section_id, apply_section(section_id, "\n" + note))
    if turn_style:
        turn.add(
            "private.turn_style", apply_section("private.turn_style", "\n" + turn_style)
        )
    turn_text, turn_spans = _stripped(turn)

    window = _history_window(_anchor_user(request) if anchor else None, request.history)
    history = list(window.messages)
    if not history or history[-1].get("role") != "user":
        history.append({"role": "user", "content": request.user_input})

    messages: list[dict[str, Any]] = []
    spans: list[dict[str, Any]] = []
    for text, parts in ((core_text, core_spans), (profile_text, profile_spans)):
        if text:
            spans.extend(span.as_dict(len(messages)) for span in parts)
            messages.append({"role": "system", "content": text})
    messages.extend(history)
    if turn_text:
        spans.extend(span.as_dict(len(messages)) for span in turn_spans)
        messages.append({"role": "system", "content": turn_text})
    remember_manifest(messages, spans)
    if spans_out is not None:
        spans_out.extend(spans)
    if meta_out is not None:
        meta_out.update(
            {
                "layers": {
                    "system": layer_digest(core_text),
                    "profile": layer_digest(profile_text),
                    "history": layer_digest(history[0]) if history else "",
                },
                "history": {
                    "kept": len(history),
                    "dropped": window.dropped,
                    "chars": window.chars,
                    "moved": window.moved,
                    "reason": window.reason,
                },
                "chars": {
                    "system": len(core_text),
                    "profile": len(profile_text),
                    "turn": len(turn_text),
                },
            }
        )
    return messages


def _stable_tools() -> bool:
    return bool(getattr(private_agent_config, "private_agent_stable_tools", True))


def _round_tools(
    tools: list[dict[str, Any]],
    *,
    final: bool = False,
    allowed: frozenset[str] | None = None,
) -> dict[str, Any]:
    """这一轮的 tools/tool_choice。

    开着 ``PRIVATE_AGENT_STABLE_TOOLS`` 时工具表永远是同一份（它是请求前缀的一部分，换了
    整段缓存失效）：最后一轮用 ``tool_choice="none"`` 不许再调；只放行部分工具的轮
    （汇报/协调）靠执行时拦截。关着时退回旧做法：最后一轮不带工具、只放行的轮只带那几个。
    """

    if _stable_tools():
        if final:
            return {"tools": tools, "tool_choice": "none"}
        return {"tools": tools}
    if final:
        return {"tools": []}
    if allowed is not None:
        return {
            "tools": [
                tool
                for tool in tools
                if str((tool.get("function") or {}).get("name")) in allowed
            ]
        }
    return {"tools": tools}


def _trace_prompt(
    user_id: int, pipeline: str, meta: dict[str, Any], tools: list[dict[str, Any]]
) -> str | None:
    """记下分层指纹，和上一轮比出第一处变化的层；写进 Trace。返回变化的层名。"""

    from .private_cache import layer_digest, note_prefix

    layers = dict(meta.get("layers") or {})
    layers["tools"] = layer_digest(tools)
    diff = note_prefix(user_id, pipeline, layers)
    try:
        from .execution_trace import trace_event

        trace_event(
            "prompt",
            "私聊提示词分层",
            status="success",
            output={
                "pipeline": pipeline,
                "tool_count": len(tools),
                "history": meta.get("history"),
                "chars": meta.get("chars"),
                "prefix_changed": diff.get("changed"),
                "stable_layers": diff.get("stable_layers"),
                "first": diff.get("first"),
            },
        )
    except Exception:  # noqa: BLE001
        dbg_exc("私聊提示词分层记录失败(忽略)")
    history = meta.get("history") or {}
    dbg(
        f"私聊用户 {user_id} {pipeline} 提示词: 历史 {history.get('kept')} 条"
        f"（{history.get('reason')}），前缀变化层={diff.get('changed') or '无'}"
    )
    return diff.get("changed")


def _record_usage(
    pipeline: str, result: Any, user_id: int, round_index: int, changed: str | None
) -> None:
    from .private_cache import record_usage

    try:
        entry = record_usage(
            pipeline,
            result,
            user_id=user_id,
            round_index=round_index,
            changed=changed if round_index == 0 else None,
        )
    except Exception:  # noqa: BLE001
        dbg_exc("私聊缓存统计失败(忽略)")
        return
    if entry.get("hit_rate") is not None:
        dbg(
            f"私聊用户 {user_id} {pipeline} 第 {round_index + 1} 次请求: 缓存命中 "
            f"{entry.get('cached')}/{entry.get('cached', 0) + entry.get('miss', 0)} tokens"
        )


def _anchor_user(request: PrivateTurnRequest) -> int | None:
    try:
        return int(request.user_id)
    except (TypeError, ValueError):
        return None


def _stripped(builder: ContentBuilder) -> tuple[str, list[Span]]:
    """拼好一层并去掉首尾空白，分段位置跟着平移、截到文本范围内。"""

    raw, spans = builder.build()
    lead = len(raw) - len(raw.lstrip())
    text = raw.strip()
    moved = []
    for span in spans:
        start = max(span.start - lead, 0)
        end = min(span.end - lead, len(text))
        if end > start:
            moved.append(Span(span.section_id, start, end))
    return text, moved


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
    verified = set(succeeded)
    for name in succeeded:
        verified.update(_CLAIM_ALIASES.get(name, ()))
    check = check_action_claims(text, frozenset(verified))
    if check.changed:
        dbg(f"私聊回复去掉了没有依据的完成声明: {check.families}")
    return check.text


def _normalized_reply(text: str) -> str:
    import re

    plain = re.sub(r"【[^】]{1,12}】", "", str(text or ""))
    plain = plain.replace(SPLIT_MARK, "")
    return re.sub(r"[\s，。！？!?,.~\uff5e…]+", "", plain)


def repeats_last_reply(text: str, history: list[dict[str, str]]) -> bool:
    """这条回复是不是和他看到的上一条回复几乎一样（复读）。只比较足够长的回复。"""

    from difflib import SequenceMatcher

    current = _normalized_reply(text)
    if len(current) < REPEAT_MIN_CHARS:
        return False
    previous = next(
        (
            str(item.get("content") or "")
            for item in reversed(history)
            if item.get("role") == "assistant"
        ),
        "",
    )
    before = _normalized_reply(previous)
    if len(before) < REPEAT_MIN_CHARS:
        return False
    return SequenceMatcher(None, current, before).ratio() >= REPEAT_SIMILARITY


def _tool_content(payload: dict[str, Any]) -> str:
    """工具结果转 JSON；太长时截掉尾部并标明，模型知道结果不完整。"""

    content = json.dumps(payload, ensure_ascii=False, default=str)
    if len(content) <= TOOL_RESULT_CHAR_LIMIT:
        return content
    omitted = len(content) - TOOL_RESULT_CHAR_LIMIT
    return (
        content[:TOOL_RESULT_CHAR_LIMIT]
        + f"…（结果太长，后面 {omitted} 字省略；需要细节就缩小范围再查，或放到后台做）"
    )


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
                "content": _tool_content(payload),
            }
        )
    if ctx.mutated:
        # 写操作立刻落库：后面模型失败、退回纯对话时签到/记忆不能跟着丢。
        await ctx.session.commit()
        ctx.mutated = False


# 兼容旧引用：文字已登记为 private.files_hint。
FILES_HINT = PRIVATE_FILES_HINT


def rhythm_style(persona: dict[str, Any] | None = None) -> str:
    """节奏标签用法（按他人设的表现力/详略）。只随人设变，所以放在开头那层（前缀缓存）。"""

    from types import SimpleNamespace

    from .persona import persona_style_levels
    from .speech_rhythm import rhythm_instruction

    levels = persona_style_levels(persona)
    style = SimpleNamespace(expressiveness=levels.get("expressiveness", 1)) if levels else None
    # 详略档位最低（只说一句）的人设：按“短回应”给节奏说明。
    act = "acknowledge" if levels.get("verbosity", 1) <= 0 else "continue"
    return rhythm_instruction(scene="private", act=act, complexity="normal", style=style)


def turn_style(
    user_id: int,
    now: datetime,
    *,
    emoji_pref: str | None = None,
    files: bool = False,
    persona: dict[str, Any] | None = None,
    include_rhythm: bool = False,
) -> str:
    """本轮的发言方式：表情包菜单（额度、冷却每轮都可能变）+ 附件提示。

    节奏说明默认不在这里：它只随人设变，由 ``build_messages`` 放进开头那层
    （``private.rhythm``）；``include_rhythm=True`` 时仍拼在最前（兼容旧调用）。
    """

    from .private_delivery import private_sticker_instruction

    parts = [rhythm_style(persona)] if include_rhythm else []
    if files:
        parts.append(apply_section("private.files_hint", PRIVATE_FILES_HINT))
    try:
        parts.append(
            private_sticker_instruction(
                user_id, now.date().isoformat(), allow=emoji_pref != "none"
            )
        )
    except Exception:  # noqa: BLE001
        dbg_exc("私聊表情包菜单生成失败(忽略)")
    return "\n".join(part for part in parts if part)


def one_voice(text: str, user_id: int | None = None) -> str:
    """对外只有一个“我”：清掉小助手/子智能体/任务编号这类内部说法（他私聊人设的名字不动）。"""

    from .persona import resolve_persona
    from .private_voice import persona_names, scrub_internal

    try:
        persona = (
            resolve_private_persona(user_id) if user_id else resolve_persona(None)
        )
        names = persona_names(persona)
    except Exception:  # noqa: BLE001
        names = ()
    return scrub_internal(text, persona_names=names)


async def _deliver_text(
    bot: Any, user_id: int, text: str, send: Any, now: datetime
) -> list[str]:
    from .private_delivery import deliver, file_uploader

    text = one_voice(text, user_id)
    result = await deliver(
        user_id,
        text,
        send,
        upload_file=file_uploader(bot, user_id) if bot is not None else None,
        today=now.date().isoformat(),
        sleep=asyncio.sleep,
    )
    return result.delivered


async def _send_parts(request: PrivateTurnRequest, text: str, now: datetime) -> list[str]:
    async def send(message: Any) -> None:
        if request.event is None:
            # 没有触发事件的回合（等他说完后自己接话）：直接发私聊。
            await request.bot.send_private_msg(user_id=request.user_id, message=message)
            return
        await request.bot.send(request.event, message)

    return await _deliver_text(request.bot, request.user_id, text, send, now)


def _is_superuser(user_id: int) -> bool:
    try:
        return str(user_id) in get_driver().config.superusers
    except Exception:  # noqa: BLE001
        return False


async def handle_private_turn(request: PrivateTurnRequest) -> str | None:
    """私聊 Agent 一轮；返回送达的文本，放弃时返回 None。

    整轮绑定这位用户：私聊分段按“本人覆盖 → 全局”解析，真实请求按用户抓取。
    """

    with use_private_user(request.user_id, pipeline="turn"):
        return await _handle_private_turn(request)


async def _handle_private_turn(request: PrivateTurnRequest) -> str | None:
    session = request.session
    user_id = request.user_id
    now = now_beijing()
    resumed = bool(request.extra.get("resumed"))
    silence_ok = turn_taking.enabled()
    if not resumed:
        # 他又说话了：之前“等他说完”的计时器作废，由这一轮接着聊。
        turn_taking.cancel_wait(user_id)
    arrivals_at_start = turn_taking.arrivals(user_id)
    cap = int(private_agent_config.private_agent_daily_turns)
    if cap and await turns_today(session, user_id, now) >= cap:
        dbg(f"私聊用户 {user_id} 今日 Agent 轮数已达 {cap}，退回纯对话")
        return None
    try:
        bot_id: int | None = int(request.bot.self_id)
    except (TypeError, ValueError):
        bot_id = None

    from .private_computer import computer_status, import_media
    from .private_tasks import tasks_enabled

    computer, computer_reason = await computer_status()
    attachments: list[dict[str, Any]] = []
    if request.media and computer:
        try:
            attachments = await import_media(request.bot, user_id, request.media)
        except Exception:  # noqa: BLE001
            logger.exception(f"私聊用户 {user_id} 附件入箱失败(忽略)")
    unread_files = [
        item
        for index, item in enumerate(request.media or [], start=1)
        if str(item.get("type")) == "file"
        and not any(row.get("id") == f"m{index}" and row.get("ref") for row in attachments)
    ]
    if unread_files:
        # 文件没能进虚拟电脑（电脑没开/离线/入箱失败）：Bot 端直接读文字兜底。
        from . import private_file_text

        try:
            direct = await private_file_text.read_files(request.bot, request.media)
        except Exception:  # noqa: BLE001
            logger.exception(f"私聊用户 {user_id} 直接读取文件失败(忽略)")
            direct = []
        known = {row.get("id") for row in attachments if row.get("ref")}
        attachments = [row for row in attachments if row.get("id") in known] + [
            {**row, "in_computer": False} for row in direct if row.get("id") not in known
        ]
    if request.media and not any(row.get("ref") for row in attachments):
        # 生产排查用：附件没进电脑时日志里要看得出是电脑没开还是入箱被关。
        logger.warning(
            f"私聊用户 {user_id} 发来 {len(request.media)} 个附件但没有放进虚拟电脑"
            f"（电脑可用={computer}，原因={computer_reason or '入箱关闭或无结果'}）"
        )
    context = await build_private_context(
        session, user_id, bot_id=bot_id, nickname=request.nickname, now=now
    )
    from . import private_vision

    # 之前的图片转述已经写回发图那条历史消息（见 _annotate_turn_message），这里不再放进
    # 资料：放在资料里模型分不清新旧，会把一张旧表情包当成每轮新发的。
    vision = await private_vision.prepare_private_vision(
        request.bot, user_id, request.media
    )
    if tasks_enabled():
        from . import private_team

        try:
            helper_messages = private_team.unread(user_id, private_team.MAIN)
        except Exception:  # noqa: BLE001
            helper_messages = []
        if helper_messages:
            context.payload["helper_messages"] = helper_messages
    if attachments:
        # 本条消息新发的附件。之前发过的附件写在历史消息里（“已放进他的虚拟电脑：sandbox:…”）。
        context.payload["attachments"] = attachments
        if any(row.get("in_computer") is False for row in attachments):
            context.payload["attachments_note"] = apply_section(
                "private.attachments_unread", PRIVATE_ATTACHMENTS_UNREAD
            )
    elif request.media:
        context.payload["attachments_note"] = apply_section(
            "private.attachments_missing", PRIVATE_ATTACHMENTS_MISSING
        )
    tasks = tasks_enabled()
    from . import private_store
    from .private_voice import dialogue_excerpt, voice_card

    just_finished: list[dict[str, Any]] = []
    if tasks:
        # 刚做完、还没告诉他的结果：他正好在说话，就顺着这一轮一起说，不另起一条汇报。
        from .private_tasks import unreported_results

        try:
            just_finished = unreported_results(user_id)[:3]
        except Exception:  # noqa: BLE001
            just_finished = []
        if just_finished:
            context.payload["just_finished"] = [
                report_payload(item) for item in just_finished
            ]
    emoji_pref = (private_store.load(user_id).get("prefs") or {}).get("emoji")
    persona = resolve_private_persona(user_id)
    meta: dict[str, Any] = {}
    messages = build_messages(
        persona,
        context,
        request,
        computer=computer,
        tasks=tasks,
        turn_style=turn_style(
            user_id,
            now,
            emoji_pref=emoji_pref,
            files=bool(tasks and attachments),
            persona=persona,
        ),
        allow_silence=silence_ok,
        just_finished=bool(just_finished),
        meta_out=meta,
    )
    if resumed:
        resumed_note = apply_section("private.resumed_note", PRIVATE_RESUMED_NOTE)
        if resumed_note:
            messages.append({"role": "system", "content": resumed_note})
    original_users = {
        index: message["content"]
        for index, message in enumerate(messages)
        if message.get("role") == "user"
    }
    private_vision.attach_to_messages(messages, vision)
    is_superuser = _is_superuser(user_id)
    tools = tool_schemas(is_superuser=is_superuser, computer=computer, tasks=tasks)
    changed = _trace_prompt(user_id, "turn", meta, tools)
    ctx = PrivateToolContext(
        session=session,
        user_id=user_id,
        bot_id=bot_id,
        groups=context.groups,
        now=now,
        nickname=request.nickname,
        is_superuser=is_superuser,
        bot=request.bot,
        dialogue=dialogue_excerpt(request.history),
        user_input=str(request.user_input or "")[:200],
        voice=voice_card(persona),
        call_them=str((context.payload.get("user") or {}).get("call_them") or ""),
        computer=bool(computer),
        attachment_refs=[str(row["ref"]) for row in attachments if row.get("ref")],
    )
    max_rounds = int(private_agent_config.private_agent_max_tool_rounds)
    handoff_after = int(private_agent_config.private_agent_handoff_after)
    nudged = False
    retried = False
    repeat_checked = False
    text = ""
    for round_index in range(max_rounds + 1):
        final = round_index == max_rounds
        if final and round_index > 0:
            handed = tasks and _auto_handoff(ctx, messages)
            messages.append(
                {
                    "role": "system",
                    "content": apply_section("private.auto_handoff", PRIVATE_AUTO_HANDOFF)
                    if handed
                    else apply_section("private.final_round", PRIVATE_FINAL_ROUND),
                }
            )
        elif (
            tasks
            and not nudged
            and handoff_after
            and not ctx.delegated
            and ctx.heavy_calls >= handoff_after
            and round_index < max_rounds - 1
        ):
            # 自己一路调工具调了好几次还没完：提醒一次交给后台，别让他干等。
            nudged = True
            dbg(f"私聊用户 {user_id} 本轮已调 {ctx.heavy_calls} 次重活工具，提醒放到后台")
            messages.append(
                {
                    "role": "system",
                    "content": apply_section("private.handoff_nudge", PRIVATE_HANDOFF_NUDGE),
                }
            )
        try:
            result = await llm.complete_with_tools_result(
                messages,  # pyright: ignore[reportArgumentType]
                **_round_tools(tools, final=final),
                task="agent_dialogue",
                max_tokens=llm.NO_OUTPUT_LIMIT,
                timeout=COMPLETION_TIMEOUT,
                multimodal=bool(vision.blocks),
                raise_on_unsupported=bool(vision.blocks),
            )
        except llm.LLMMultimodalUnsupportedError:
            # 对话模型拒收图片：改走识图转述，同一轮再请求一次。
            dbg(f"私聊用户 {user_id} 对话模型不收图片，改用识图转述")
            vision = await private_vision.fallback_to_captions(user_id, vision)
            # 重附的旧图挂在更早的历史消息上，也要一起还原。
            for index, content in original_users.items():
                messages[index]["content"] = content
            private_vision.attach_to_messages(messages, vision)
            result = await llm.complete_with_tools_result(
                messages,  # pyright: ignore[reportArgumentType]
                **_round_tools(tools, final=final),
                task="agent_dialogue",
                max_tokens=llm.NO_OUTPUT_LIMIT,
                timeout=COMPLETION_TIMEOUT,
            )
        _record_usage("turn", result, user_id, round_index, changed)
        if result.message is None and not retried and result.outcome in _RETRY_OUTCOMES:
            # 上游抖了一下（超时/空回复）：同样的请求再试一次（前缀相同，基本全命中缓存），
            # 而不是直接退回没有人设的纯对话。
            retried = True
            dbg(f"私聊用户 {user_id} 模型无回复（{result.outcome}），重试一次")
            await asyncio.sleep(RETRY_PAUSE_SECONDS)
            result = await llm.complete_with_tools_result(
                messages,  # pyright: ignore[reportArgumentType]
                **_round_tools(tools, final=final),
                task="agent_dialogue",
                max_tokens=llm.NO_OUTPUT_LIMIT,
                timeout=COMPLETION_TIMEOUT,
                multimodal=bool(vision.blocks),
            )
            _record_usage("turn", result, user_id, round_index, None)
        message = result.message
        if message is None:
            dbg(f"私聊用户 {user_id} 模型无回复（{result.outcome}）")
            break
        if getattr(message, "tool_calls", None) and not final:
            await _run_tools(message, messages, ctx)
            continue
        text = str(getattr(message, "content", "") or "")
        if (
            not final
            and not repeat_checked
            and repeats_last_reply(text, request.history)
        ):
            # 和上一条几乎一字不差：提醒一次换个说法（常见于模型卡在同一句客套话上）。
            repeat_checked = True
            dbg(f"私聊用户 {user_id} 回复和上一条几乎一样，要求换个说法")
            note = apply_section("private.repeat_guard", PRIVATE_REPEAT_GUARD)
            if note:
                messages.append({"role": "system", "content": note})
                text = ""
                continue
        break

    if not resumed:
        await _annotate_turn_message(
            session,
            user_id,
            [*private_vision.history_notes(vision), *attachment_notes(attachments)],
        )
    wrote = bool(ctx.succeeded & (_WRITE_RESULTS | WRITE_TOOLS))
    silence = turn_taking.parse_silence(text)
    text = silence.text
    if silence.silent and silence_ok and not (ctx.succeeded & _WRITE_RESULTS):
        if silence.kind == "wait" and not resumed:
            turn_taking.schedule_wait(
                request.bot,
                user_id,
                seconds=float(private_agent_config.private_agent_wait_seconds),
                since=arrivals_at_start,
            )
            dbg(f"私聊用户 {user_id} 本轮不说话：等他说完")
        else:
            dbg(f"私聊用户 {user_id} 本轮不说话：不需要回应")
        _trace_silence(user_id, silence.kind)
        return ""
    if not text.strip():
        if ctx.succeeded & _WRITE_RESULTS:
            text = "好啦，已经帮你处理好了~"
        else:
            return None
    if (
        silence_ok
        and not wrote
        and turn_taking.newer_message_arrived(user_id, arrivals_at_start)
        and turn_taking.may_yield(user_id)
    ):
        # 想好了还没发，他又发来新消息：这条让给下一轮，看到全部消息再一起回。
        turn_taking.record_yield(user_id)
        dbg(f"私聊用户 {user_id} 有新消息排队，本轮回复让给下一轮")
        _trace_silence(user_id, "yield")
        return ""
    turn_taking.record_spoke(user_id)
    text = verify_claims(text, ctx.succeeded)
    if not split_reply(text):
        return None
    delivered = await _send_parts(request, text, now)
    if not delivered:
        return None
    if just_finished:
        from .private_tasks import mark_reported

        try:
            mark_reported(user_id, [str(item["id"]) for item in just_finished], via="chat")
        except Exception:  # noqa: BLE001
            dbg_exc(f"私聊用户 {user_id} 标记已顺带告知的结果失败(忽略)")
    await _after_turn(session, user_id, now)
    return "\n".join(delivered)


def _auto_handoff(ctx: PrivateToolContext, messages: list[dict[str, Any]]) -> bool:
    """工具轮次用完还在调工具：把这件事自动放到后台接着做（带上已经查到的）。

    只在开着 ``PRIVATE_AGENT_AUTO_HANDOFF``、本轮没派过、确实调过重活工具时做；
    额度满了或出错就返回 False，照旧走最后一轮硬答。
    """

    if (
        not private_agent_config.private_agent_auto_handoff
        or ctx.delegated
        or ctx.heavy_calls <= 0
        or not ctx.user_input.strip()
    ):
        return False
    from . import private_tasks
    from .private_voice import handoff_findings, infer_toolsets, short_title

    findings = handoff_findings(messages)
    brief = (
        apply_section("private.handoff_findings", PRIVATE_HANDOFF_FINDINGS) + "\n" + findings
    )
    if ctx.attachment_refs:
        brief += f"\n他这次发来的文件：{' '.join(ctx.attachment_refs[:6])}"
    goal = f"把他这次要的做完并交付：{ctx.user_input.strip()}"[:600]
    try:
        private_tasks.start_task(
            ctx.bot,
            ctx.user_id,
            title=short_title(ctx.user_input),
            goal=goal,
            brief=brief[:4000],
            toolsets=infer_toolsets(computer=ctx.computer, has_refs=bool(ctx.attachment_refs)),
            save_to_notes=False,
            now=ctx.now,
            dialogue=ctx.dialogue,
            asked=ctx.user_input,
            voice=ctx.voice,
            call_them=ctx.call_them,
            origin="auto_handoff",
        )
    except Exception as exc:  # noqa: BLE001
        dbg(f"私聊用户 {ctx.user_id} 自动放到后台失败，照旧直接回答: {exc}")
        return False
    ctx.delegated += 1
    ctx.succeeded.add("delegate_task")
    dbg(f"私聊用户 {ctx.user_id} 工具轮次用完，已自动放到后台接着做")
    _trace_handoff(ctx.user_id, ctx.heavy_calls)
    return True


def _trace_handoff(user_id: int, heavy_calls: int) -> None:
    try:
        from .execution_trace import trace_event

        trace_event(
            "decision",
            "私聊自动放到后台",
            status="success",
            output={"user_id": user_id, "heavy_calls": heavy_calls},
        )
    except Exception:  # noqa: BLE001
        dbg_exc("私聊自动转后台记录失败(忽略)")


def _trace_silence(user_id: int, kind: str) -> None:
    try:
        from .execution_trace import trace_event

        trace_event(
            "decision",
            "私聊发言判断",
            status="success",
            output={"user_id": user_id, "silent": kind},
        )
    except Exception:  # noqa: BLE001
        dbg_exc("私聊发言判断记录失败(忽略)")


async def resume_after_wait(bot: Any, user_id: int) -> None:
    """“等他说完”到时间他没再说话：拿回合锁，用最近的聊天记录自己接上话。"""

    from nonebot_plugin_orm import get_session

    from ..private_chat_bridge import user_turn_lock
    from .private_scheduler import record_outgoing

    bot = bot or _any_bot()
    if bot is None:
        return
    async with user_turn_lock(user_id):
        if turn_taking.pending(user_id):
            return
        async with get_session() as session:
            history = await _recent_history(session, user_id)
            if not history or history[-1]["role"] != "user":
                return
            request = PrivateTurnRequest(
                bot=bot,
                event=None,
                user_id=user_id,
                user_input=history[-1]["content"],
                raw_text="",
                history=history,
                session=session,
                extra={"resumed": True},
            )
            text = await handle_private_turn(request)
        if text:
            await record_outgoing(user_id, text, now_beijing())


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


# ── 子任务汇报轮与进度 ────────────────────────────────────────────────────

REPORT_HISTORY_MESSAGES = 20


def _any_bot() -> Any:
    from nonebot import get_bots

    bots = [bot for bot in get_bots().values() if bot is not None]
    return bots[0] if bots else None


async def _private_session_id(session: Any, user_id: int) -> int | None:
    from sqlalchemy import select

    from ..data_models.chat_session import ChatSession

    return await session.scalar(
        select(ChatSession.id)
        .where(
            ChatSession.user_id == user_id,
            ChatSession.group_id.is_(None),
            ChatSession.is_deleted.is_(False),
        )
        .order_by(ChatSession.updated_at.desc().nullslast())
        .limit(1)
    )


def attachment_notes(attachments: list[dict[str, Any]]) -> list[str]:
    """本轮附件入箱结果写进历史的一句话：后几轮他说“这个文件”时模型知道它在哪。"""

    notes: list[str] = []
    for row in attachments or []:
        label = {"image": "图片", "file": "文件", "record": "语音", "video": "视频"}.get(
            str(row.get("type")), "附件"
        )
        name = str(row.get("name") or "").strip()
        title = f"{label}「{name[:60]}」" if name else label
        if row.get("ref"):
            size = f"，{row['size']}" if row.get("size") else ""
            notes.append(f"（这条里的{title}已放进他的虚拟电脑：{row['ref']}{size}）")
        elif row.get("text_excerpt"):
            # 没进电脑但读出了文字：节选写进历史，后几轮还能接着用。
            from .private_file_text import HISTORY_EXCERPT_CHARS, excerpt

            text = excerpt(str(row["text_excerpt"]), HISTORY_EXCERPT_CHARS)
            template = apply_section("private.attachment_excerpt", PRIVATE_ATTACHMENT_EXCERPT)
            if template:
                notes.append(template.replace("{title}", title).replace("{text}", text))
        elif row.get("failed"):
            notes.append(f"（这条里的{title}没能放进虚拟电脑：{row['failed']}）")
    return notes


async def _annotate_turn_message(session: Any, user_id: int, notes: list[str]) -> None:
    """把识图转述、附件位置追加到他这条消息的入库内容上（历史里原位可见）。

    出错只记日志：不能影响这一轮回复。
    """

    notes = [note for note in notes if note]
    if not notes:
        return
    from sqlalchemy import select, update

    from ..data_models.chat_message import ChatMessage

    try:
        session_id = await _private_session_id(session, user_id)
        if session_id is None:
            return
        row = (
            await session.execute(
                select(ChatMessage.id, ChatMessage.content)
                .where(
                    ChatMessage.session_id == session_id,
                    ChatMessage.role == "user",
                    ChatMessage.is_deleted.is_(False),
                )
                .order_by(ChatMessage.id.desc())
                .limit(1)
            )
        ).first()
        if row is None:
            return
        message_id, content = row
        content = str(content or "")
        extra = [note for note in notes if note not in content]
        if not extra:
            return
        await session.execute(
            update(ChatMessage)
            .where(ChatMessage.id == message_id)
            .values(content="\n".join([content, *extra]).strip())
        )
        await session.commit()
    except Exception:  # noqa: BLE001
        dbg_exc(f"私聊用户 {user_id} 写回识图/附件说明失败(忽略)")
        try:
            await session.rollback()
        except Exception:  # noqa: BLE001
            dbg_exc("私聊写回说明回滚失败(忽略)")


async def _recent_history(session: Any, user_id: int) -> list[dict[str, str]]:
    from sqlalchemy import select

    from ..data_models.chat_message import ChatMessage

    session_id = await _private_session_id(session, user_id)
    if session_id is None:
        return []
    rows = (
        await session.execute(
            select(ChatMessage.role, ChatMessage.content)
            .where(ChatMessage.session_id == session_id)
            .order_by(ChatMessage.id.desc())
            .limit(
                int(
                    getattr(
                        private_agent_config,
                        "private_agent_history_messages",
                        REPORT_HISTORY_MESSAGES,
                    )
                )
            )
        )
    ).all()
    return [
        {"role": str(role), "content": str(content or "")}
        for role, content in reversed(rows)
        if role in {"user", "assistant"}
    ]


def report_payload(task: dict[str, Any]) -> dict[str, Any]:
    """汇报轮/顺带告知用的结果视图；``batch`` 时合成 ``results``。"""

    batch = task.get("batch") or []
    if batch:
        return {
            "results": [
                _single_payload(item) for item in [task, *batch]
            ],
            "note": "这几件事前后脚做完了，合成一段话一次告诉他",
        }
    return _single_payload(task)


def _single_payload(task: dict[str, Any]) -> dict[str, Any]:
    status = str(task.get("status"))
    payload: dict[str, Any] = {
        "task": task.get("title"),
        "status": {"done": "done", "blocked": "waiting_for_you", "failed": "failed"}.get(
            status, status
        ),
    }
    if task.get("asked"):
        payload["asked"] = str(task.get("asked"))[:200]
    if task.get("told"):
        payload["told_him"] = list(task.get("told") or [])[-4:]
    if status == "done":
        payload["summary"] = task.get("summary")
        if task.get("details"):
            payload["details"] = task.get("details")
        if task.get("files"):
            payload["files"] = task.get("files")
        if task.get("saved_note"):
            payload["saved_to_notes"] = True
    elif status == "blocked":
        payload["question"] = task.get("question")
        payload["task_id"] = task.get("id")
    else:
        payload["reason"] = task.get("error") or task.get("summary") or "没有说明原因"
    return payload


_IMAGE_SUFFIXES = (".png", ".jpg", ".jpeg", ".gif", ".webp")


def fallback_report(task: dict[str, Any]) -> str:
    """汇报轮模型失败时的兜底：第一人称、不带「标题」，几件事一起时逐件说。"""

    items = [task, *(task.get("batch") or [])]
    parts: list[str] = []
    for item in items:
        status = item.get("status")
        title = str(item.get("title") or "").strip()
        about = f"{title}，" if len(items) > 1 and title else ""
        if status == "done":
            parts.append(f"{about}弄好了：{item.get('summary') or ''}".strip("：，"))
            for ref in item.get("files") or []:
                kind = "图片" if str(ref).lower().endswith(_IMAGE_SUFFIXES) else "文件"
                parts.append(f"[{kind}:{ref}]")
        elif status == "blocked":
            parts.append(f"{about}我想先跟你确认一下：{item.get('question') or ''}")
        else:
            parts.append(f"{about}这个没弄成：{item.get('error') or '出了点问题'}")
    return "|||".join(part for part in parts if part)


REPORT_MAX_ROUNDS = 2


def _report_tools(user_id: int, computer: bool) -> list[dict[str, Any]]:  # noqa: FBT001
    """汇报/协调轮的工具表：和主回合同一份（只放行 tasks，执行时拦截），保住前缀缓存。

    关着 ``PRIVATE_AGENT_STABLE_TOOLS`` 时只给 tasks 一个（旧做法）。
    """

    from .private_tools import TOOLS_BY_NAME

    if not _stable_tools():
        return [TOOLS_BY_NAME["tasks"].schema()]
    from .private_tasks import tasks_enabled

    return tool_schemas(
        is_superuser=_is_superuser(user_id), computer=computer, tasks=tasks_enabled()
    )


def _already_told(user_id: int, task: dict[str, Any]) -> dict[str, Any] | None:
    """拿到回合锁后再看一眼：已经顺着聊天告诉他的就不重复汇报；返回还要汇报的部分。"""

    from .private_tasks import get_task

    if task.get("project") is True or not task.get("id"):
        return task

    def fresh(item: dict[str, Any]) -> dict[str, Any] | None:
        current = get_task(user_id, str(item.get("id"))) or item
        if current.get("reported_state") == current.get("status"):
            return None
        return {**item, **current}

    head = fresh(task)
    rest = [row for row in (fresh(item) for item in task.get("batch") or []) if row]
    if head is None:
        if not rest:
            return None
        head, rest = rest[0], rest[1:]
    if rest:
        head["batch"] = rest
    else:
        head.pop("batch", None)
    return head


async def report_task(bot: Any, user_id: int, task: dict[str, Any]) -> bool:
    """绑定这位用户后执行（本人提示词覆盖、按用户抓取请求）。"""

    with use_private_user(user_id, pipeline="report"):
        return await _report_task(bot, user_id, task)


async def _report_task(bot: Any, user_id: int, task: dict[str, Any]) -> bool:
    """汇报轮：拿回合锁，由主智能体像自己刚做完一样，用人设把结果告诉他。

    等回答（blocked）时带 ``tasks`` 工具：聊天里已经有答案就直接转过去、只写【不回】，不打扰他。
    """

    from nonebot_plugin_orm import get_session

    from ..private_chat_bridge import user_turn_lock
    from .private_computer import computer_ready
    from .private_scheduler import record_outgoing

    bot = bot or _any_bot()
    if bot is None:
        return False
    async with user_turn_lock(user_id):
        pending = _already_told(user_id, task)
        if pending is None:
            dbg(f"私聊用户 {user_id} 结果已经在聊天里说过了，不再单独汇报")
            return True
        task = pending
        now = now_beijing()
        text = ""
        blocked = task.get("status") == "blocked" and not task.get("batch")
        answered = False
        try:
            async with get_session() as session:
                history = await _recent_history(session, user_id)
                context = await build_private_context(
                    session, user_id, bot_id=None, nickname=None, now=now
                )
                notice = (
                    apply_section("private.report_notice", PRIVATE_REPORT_NOTICE)
                    + "\n"
                    + json.dumps(report_payload(task), ensure_ascii=False)
                )
                request = PrivateTurnRequest(
                    bot=bot,
                    event=None,
                    user_id=user_id,
                    user_input=notice,
                    raw_text="",
                    history=[*history, {"role": "user", "content": notice}],
                    session=None,
                )
                persona = resolve_private_persona(user_id)
                computer = computer_ready()
                meta: dict[str, Any] = {}
                # 和主回合同一份开头（发言判断、电脑说明都一样）、同一份工具表：
                # 汇报轮能直接命中主回合留下的前缀缓存。
                messages = build_messages(
                    persona,
                    context,
                    request,
                    computer=computer,
                    tasks=True,
                    turn_style=turn_style(user_id, now, persona=persona),
                    allow_silence=turn_taking.enabled(),
                    round_note=("private.report_turn", PRIVATE_REPORT_TURN),
                    meta_out=meta,
                )
                ctx = PrivateToolContext(
                    session=session,
                    user_id=user_id,
                    bot_id=None,
                    groups=context.groups,
                    now=now,
                    bot=bot,
                    allowed_tools=frozenset({"tasks"}) if blocked else frozenset(),
                )
                tools = _report_tools(user_id, computer)
                changed = _trace_prompt(user_id, "report", meta, tools)
                for round_index in range(REPORT_MAX_ROUNDS + 1):
                    final = round_index == REPORT_MAX_ROUNDS or not blocked
                    result = await llm.complete_with_tools_result(
                        messages,  # pyright: ignore[reportArgumentType]
                        **_round_tools(tools, final=final, allowed=ctx.allowed_tools),
                        task="agent_dialogue",
                        max_tokens=llm.NO_OUTPUT_LIMIT,
                        timeout=COMPLETION_TIMEOUT,
                    )
                    _record_usage("report", result, user_id, round_index, changed)
                    message = result.message
                    if message is None:
                        break
                    if getattr(message, "tool_calls", None) and not final:
                        await _run_tools(message, messages, ctx)
                        continue
                    text = str(getattr(message, "content", "") or "")
                    break
                answered = bool(ctx.succeeded & {"tasks.answer", "tasks.message"})
        except Exception:  # noqa: BLE001
            dbg_exc(f"私聊用户 {user_id} 汇报轮出错，改用模板")
        silence = turn_taking.parse_silence(text)
        if silence.silent and answered:
            dbg(f"私聊用户 {user_id} 等回答的事已经直接替他答了，不打扰他")
            return True
        text = "" if silence.silent else silence.text
        if not text.strip():
            text = fallback_report(task)
        if task.get("status") != "done" or any(
            item.get("status") != "done" for item in task.get("batch") or []
        ):
            # 没做成/在等回答时不许说“弄好了”。
            text = verify_claims(text, set())

        async def send(message: Any) -> None:
            await bot.send_private_msg(user_id=user_id, message=message)

        delivered = await _deliver_text(bot, user_id, text, send, now)
        if not delivered:
            return False
        try:
            await record_outgoing(user_id, "\n".join(delivered), now)
        except Exception:  # noqa: BLE001
            dbg_exc(f"私聊用户 {user_id} 汇报入库失败(忽略)")
        return True


COORDINATE_MAX_ROUNDS = 3


async def coordinate(bot: Any, user_id: int) -> None:
    """绑定这位用户后执行（本人提示词覆盖、按用户抓取请求）。"""

    with use_private_user(user_id, pipeline="coordinate"):
        return await _coordinate(bot, user_id)


async def _coordinate(bot: Any, user_id: int) -> None:
    """后台协调轮：小助手发消息问主助手。主智能体能答就直接回它，答不了才问用户。

    和普通回合共用回合锁；只给 ``tasks`` 一个工具（message/answer/list/cancel），
    最终回复写【不回】时用户什么也收不到。
    """

    from nonebot_plugin_orm import get_session

    from ..private_chat_bridge import user_turn_lock
    from . import private_team
    from .private_computer import computer_ready
    from .private_scheduler import record_outgoing

    bot = bot or _any_bot()
    if bot is None:
        return
    async with user_turn_lock(user_id):
        incoming = private_team.unread(user_id, private_team.MAIN)
        if not incoming:
            return
        now = now_beijing()
        async with get_session() as session:
            history = await _recent_history(session, user_id)
            context = await build_private_context(session, user_id, bot_id=None, nickname=None, now=now)
            notice = (
                apply_section("private.coordinate_notice", PRIVATE_COORDINATE_NOTICE)
                + "\n"
                + json.dumps({"helper_messages": incoming}, ensure_ascii=False)
            )
            request = PrivateTurnRequest(
                bot=bot,
                event=None,
                user_id=user_id,
                user_input=notice,
                raw_text="",
                history=[*history, {"role": "user", "content": notice}],
                session=session,
            )
            persona = resolve_private_persona(user_id)
            computer = computer_ready()
            meta: dict[str, Any] = {}
            messages = build_messages(
                persona,
                context,
                request,
                computer=computer,
                tasks=True,
                turn_style=turn_style(user_id, now, persona=persona),
                allow_silence=turn_taking.enabled(),
                round_note=("private.coordinate_turn", PRIVATE_COORDINATE_TURN),
                meta_out=meta,
            )
            ctx = PrivateToolContext(
                session=session,
                user_id=user_id,
                bot_id=None,
                groups=context.groups,
                now=now,
                bot=bot,
                allowed_tools=frozenset({"tasks"}),
            )
            tools = _report_tools(user_id, computer)
            changed = _trace_prompt(user_id, "coordinate", meta, tools)
            text = ""
            for round_index in range(COORDINATE_MAX_ROUNDS + 1):
                final = round_index == COORDINATE_MAX_ROUNDS
                result = await llm.complete_with_tools_result(
                    messages,  # pyright: ignore[reportArgumentType]
                    **_round_tools(tools, final=final, allowed=ctx.allowed_tools),
                    task="agent_dialogue",
                    max_tokens=llm.NO_OUTPUT_LIMIT,
                    timeout=COMPLETION_TIMEOUT,
                )
                _record_usage("coordinate", result, user_id, round_index, changed)
                message = result.message
                if message is None:
                    break
                if getattr(message, "tool_calls", None) and not final:
                    await _run_tools(message, messages, ctx)
                    continue
                text = str(getattr(message, "content", "") or "")
                break
        silence = turn_taking.parse_silence(text)
        if silence.silent or not silence.text.strip():
            dbg(f"私聊用户 {user_id} 后台协调：已直接回复小助手，不打扰他")
            return
        text = verify_claims(silence.text, ctx.succeeded)

        async def send(message: Any) -> None:
            await bot.send_private_msg(user_id=user_id, message=message)

        delivered = await _deliver_text(bot, user_id, text, send, now)
        if delivered:
            await record_outgoing(user_id, "\n".join(delivered), now)


async def notify_progress(bot: Any, user_id: int, text: str) -> bool:
    """子任务每完成一步的进度消息：模板文字，直接发，不过模型、不进会话历史。"""

    bot = bot or _any_bot()
    if bot is None:
        return False
    text = one_voice(text, user_id)
    if not text.strip():
        return False
    await bot.send_private_msg(user_id=user_id, message=MessageSegment.text(text))
    return True


__all__ = [
    "MAX_PARTS",
    "SPLIT_MARK",
    "build_messages",
    "coordinate",
    "fallback_report",
    "handle_private_turn",
    "notify_progress",
    "one_voice",
    "report_payload",
    "report_task",
    "resume_after_wait",
    "split_reply",
    "turns_today",
    "verify_claims",
]
