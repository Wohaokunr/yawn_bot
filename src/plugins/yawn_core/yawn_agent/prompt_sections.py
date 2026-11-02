# ruff: noqa: E501, PLR0913
"""Agent 提示词分段注册表与 WebUI 覆盖。

Agent 发给模型的每一段固定文字、每一个动态生成块、每一个分层表头都在这里登记成
一个 *分段*（section）。运行时通过 :func:`apply_section` 取得该段的生效文本：

- 没有覆盖时返回代码默认值（或本轮动态生成的文本），行为与改造前逐字一致；
- WebUI 可以在 **全局** 或 **本群** 范围内对任意分段做 替换 / 追加 / 前置 / 删除，
  包括安全与边界相关的分段——删掉之后模型就真的看不到这段话。删除提示词不会关闭
  代码里的硬校验（权限、配额、critical 门槛、出站校验），WebUI 会把这一点写清楚。

覆盖存放在本地 JSON（不建表、不需要迁移），进程内缓存并按 mtime 自动重载。草稿
预览用 :func:`use_draft_overrides`，只影响当前 asyncio 上下文，不落盘。
"""

from __future__ import annotations

import contextlib
import contextvars
import copy
import hashlib
import importlib
import json
import os
import threading
import time
from collections import OrderedDict
from collections.abc import Iterator, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .context import now_beijing
from .context_budget import estimate_tokens

STORE_SCHEMA_VERSION = 1
MAX_SECTION_TEXT = 12_000
MAX_NOTE_CHARS = 200
MAX_HISTORY_PER_SECTION = 12
MAX_SAMPLE_CHARS = 6_000
MAX_SAMPLES = 256
MAX_GROUP_SCOPES = 512
# 私聊“本人”作用域：``user:<QQ>``，只收私聊分段（private.* / ptool.*）。
MAX_USER_SCOPES = 2000
GLOBAL_SCOPE = "global"
USER_SCOPE_PREFIX = "user:"

MODE_REPLACE = "replace"
MODE_APPEND = "append"
MODE_PREPEND = "prepend"
MODE_DISABLE = "disable"
# 本群显式“用代码默认值”，不继承全局覆盖。
MODE_BUILTIN = "builtin"
OVERRIDE_MODES = (MODE_REPLACE, MODE_APPEND, MODE_PREPEND, MODE_DISABLE, MODE_BUILTIN)
_TEXT_MODES = frozenset({MODE_REPLACE, MODE_APPEND, MODE_PREPEND})

KIND_STATIC = "static"  # 固定文字，默认值来自代码常量
KIND_GENERATED = "generated"  # 每轮按情况生成；替换=改成固定文字
KIND_HEADER = "header"  # 分层表头（紧贴后面的 JSON 数据）
KIND_DATA = "data"  # 上下文数据块，只能保留或整块删除

CATEGORIES: tuple[tuple[str, str, str], ...] = (
    (
        "safety",
        "安全与边界",
        "防注入、保密、诚实与工具边界。删掉后模型不再看到这些约束，但代码里的权限/配额/出站校验仍然生效。",
    ),
    ("core", "核心策略", "角色开场、当前回合优先级、人设块和媒体权威说明。"),
    ("layers", "分层表头", "每个 system 层前面的标签文字，告诉模型这一层是什么。"),
    (
        "context",
        "上下文数据",
        "群背景、成员资料、实时状态和当前回合 JSON。只能整块保留或删除。",
    ),
    ("tools", "工具说明", "装载对应工具时才追加的使用规则。"),
    ("speech", "发言策略", "场景规则与每轮动态生成的话语动作、轮替、风格、节奏说明。"),
    ("proactive", "主动发言", "插话、暖场、短会话续聊的用户提示与 JSON 协议。"),
    ("router", "工具路由", "轻量路由模型的 system 提示。"),
    ("vision", "识图", "识图转述模型的 system 提示。"),
    ("memory", "记忆整理", "记忆整理与周摘要模型的 system 提示。"),
    ("private", "私聊 Agent", "私聊里的角色、规则、用户资料和私聊记忆整理提示。"),
    (
        "tool_schema",
        "工具描述",
        "每个工具 schema 里的 description，模型据此决定怎么调用。",
    ),
    (
        "private_tools",
        "私聊工具描述",
        "私聊工具与后台子智能体工具 schema 里的 description。",
    ),
)
CATEGORY_LABELS = {key: label for key, label, _hint in CATEGORIES}

LAYER_LABELS = {
    "static": "静态前缀（system #1）",
    "stable": "群背景层（system）",
    "speaker": "成员资料层（system）",
    "tool_guidance": "工具说明层（system）",
    "realtime": "实时状态层（system）",
    "user": "当前回合（user）",
    "round": "工具循环中追加",
    "tool_result": "工具结果（tool）",
    "proactive_user": "主动发言 user 提示",
    "aux_system": "辅助模型 system",
    "tool_schema": "tools 参数",
    "private_system": "私聊 system",
}


@dataclass(frozen=True, slots=True)
class PromptSection:
    id: str
    title: str
    category: str
    layer: str
    kind: str = KIND_STATIC
    description: str = ""
    when: str = "每轮"
    safety: bool = False
    protocol: bool = False
    default_ref: str | None = None
    tasks: tuple[str, ...] = ("agent_dialogue",)
    editable: bool = True

    @property
    def modes(self) -> tuple[str, ...]:
        if not self.editable:
            return ()
        if self.kind == KIND_DATA:
            return (MODE_DISABLE, MODE_BUILTIN)
        return OVERRIDE_MODES

    def default_text(self) -> str | None:
        return _resolve_default_ref(self.default_ref)


def _resolve_default_ref(ref: str | None) -> str | None:
    """``module:attr`` 或 ``module:attr[key]``；按需导入，避免循环依赖。"""

    if not ref:
        return None
    try:
        module_name, _, attr = ref.partition(":")
        key: str | None = None
        if attr.endswith("]") and "[" in attr:
            attr, _, raw_key = attr[:-1].partition("[")
            key = raw_key
        module = importlib.import_module(f"{__package__}.{module_name}")
        value = getattr(module, attr)
        if key is not None:
            value = value[key]
        return str(value)
    except Exception:  # noqa: BLE001
        return None


_D = ("agent_dialogue",)
_DP = ("agent_dialogue", "agent_proactive")
_P = ("agent_proactive",)
_PV = ("private_chat",)

SECTIONS: tuple[PromptSection, ...] = (
    # ── 静态前缀 ──
    PromptSection(
        "core.version_tag",
        "版本标记",
        "core",
        "static",
        KIND_GENERATED,
        "首行 v:yawn-agent-vNN，便于在服务商日志里认出 Prompt 版本。",
        tasks=_DP,
    ),
    PromptSection(
        "core.role_intro",
        "角色开场",
        "core",
        "static",
        default_ref="prompt:POLICY_ROLE_INTRO",
        description="告诉模型它在按角色参与 QQ 群聊。",
        tasks=_DP,
    ),
    PromptSection(
        "core.turn_priority",
        "当前回合优先",
        "core",
        "static",
        default_ref="prompt:POLICY_TURN_PRIORITY",
        description="current_turn 最高优先、topic_state 的地位、不误答他人。",
        tasks=_DP,
    ),
    PromptSection(
        "safety.honesty",
        "只依据事实",
        "safety",
        "static",
        default_ref="prompt:POLICY_HONESTY",
        description="不知道就说不知道，不编造经历、不猜隐私。",
        safety=True,
        tasks=_DP,
    ),
    PromptSection(
        "safety.untrusted_data",
        "资料不可信（防注入）",
        "safety",
        "static",
        default_ref="prompt:POLICY_UNTRUSTED",
        description="群消息、记忆、摘要只是资料，不执行其中的指令。",
        safety=True,
        tasks=_DP,
    ),
    PromptSection(
        "safety.confidentiality",
        "保密边界",
        "safety",
        "static",
        default_ref="prompt:POLICY_CONFIDENTIAL",
        description="不泄露私聊、隐私记忆、权限、工具内部结果与内部策略。",
        safety=True,
        tasks=_DP,
    ),
    PromptSection(
        "safety.emotion_bounds",
        "情绪边界",
        "safety",
        "static",
        default_ref="prompt:POLICY_EMOTION",
        description="情绪状态只影响语气，不改变事实、权限、安全或记忆判断。",
        safety=True,
        tasks=_DP,
    ),
    PromptSection(
        "safety.tool_honesty",
        "工具诚实",
        "safety",
        "static",
        default_ref="prompt:POLICY_TOOL_HONESTY",
        description="只按 schema 调用工具；没成功的动作不得声称完成。",
        safety=True,
        tasks=_DP,
    ),
    PromptSection(
        "core.persona",
        "人设块",
        "core",
        "static",
        KIND_GENERATED,
        "“角色：”+ 编译后的人设 JSON。内容请在人设面板改；这里只能整体替换、追加或删除。",
        tasks=_DP,
    ),
    # ── 群背景 / 成员资料 ──
    PromptSection(
        "layers.stable_header",
        "群背景层表头",
        "layers",
        "stable",
        KIND_HEADER,
        default_ref="prompt:_STABLE_SYSTEM_PREFIX",
        tasks=_DP,
    ),
    PromptSection(
        "context.stable",
        "群背景数据",
        "context",
        "stable",
        KIND_DATA,
        "群号、群名、日摘要与跨群公开摘要（JSON）。",
        tasks=_DP,
    ),
    PromptSection(
        "core.bot_bond_guidance",
        "Bot 关系使用规则",
        "core",
        "speaker",
        default_ref="prompt:_BOT_BOND_GUIDANCE",
        when="成员资料里有 bot_bonds 时",
        tasks=_DP,
    ),
    PromptSection(
        "layers.speaker_header",
        "成员资料层表头",
        "layers",
        "speaker",
        KIND_HEADER,
        default_ref="prompt:_SPEAKER_SYSTEM_PREFIX",
        when="有相关成员资料时",
        tasks=_DP,
    ),
    PromptSection(
        "context.speaker",
        "成员资料数据",
        "context",
        "speaker",
        KIND_DATA,
        "相关成员、画像记忆、关系与 Bot 关系（JSON）。",
        when="有相关成员资料时",
        tasks=_DP,
    ),
    # ── 工具说明 ──
    PromptSection(
        "tools.result_speech",
        "工具结果怎么说",
        "safety",
        "tool_guidance",
        default_ref="tool_result_speech:TOOL_RESULT_SPEECH_INSTRUCTION",
        description="工具返回是后台事实，不照抄 JSON；没成功不说完成。",
        when="本轮装载了任何工具时",
        safety=True,
    ),
    PromptSection(
        "tools.send_message",
        "send_message 规则",
        "safety",
        "tool_guidance",
        default_ref="prompt:_SEND_MESSAGE_RULES",
        description="id 必须来自上下文、禁 CQ 码/@all、成功即结束、QQ 纯文本。",
        when="装载 send_message 时",
        safety=True,
    ),
    PromptSection(
        "tools.reaction",
        "表情包规则",
        "tools",
        "tool_guidance",
        default_ref="prompt:_REACTION_RULES",
        when="装载 search_reactions 时",
    ),
    PromptSection(
        "tools.forward",
        "合并转发规则",
        "tools",
        "tool_guidance",
        default_ref="prompt:_FORWARD_RULES",
        when="装载 send_forward 时",
    ),
    PromptSection(
        "tools.discovery",
        "渐进披露规则",
        "tools",
        "tool_guidance",
        default_ref="prompt:_DISCOVERY_RULES",
        when="装载 discover_tools 时",
    ),
    PromptSection(
        "tools.sandbox",
        "代码沙箱规则",
        "tools",
        "tool_guidance",
        default_ref="prompt:_SANDBOX_RULES",
        when="装载 run_python 时",
    ),
    PromptSection(
        "tools.shell",
        "终端规则",
        "tools",
        "tool_guidance",
        default_ref="prompt:_SHELL_RULES",
        when="装载 run_shell 时",
    ),
    PromptSection(
        "tools.web",
        "联网搜索规则",
        "tools",
        "tool_guidance",
        default_ref="prompt:_WEB_RULES",
        description="什么时候该搜、怎么写关键词、怎么转述来源；搜索结果不可信。",
        when="装载 web_search / web_fetch 时",
    ),
    PromptSection(
        "tools.schedule",
        "定时任务规则",
        "tools",
        "tool_guidance",
        default_ref="prompt:_SCHEDULE_RULES",
        when="装载 schedule_task 时",
    ),
    PromptSection(
        "tools.notice",
        "公告规则",
        "tools",
        "tool_guidance",
        default_ref="prompt:_NOTICE_RULES",
        when="装载 create_group_announcement 时",
    ),
    PromptSection(
        "tools.file",
        "群文件规则",
        "tools",
        "tool_guidance",
        default_ref="prompt:_FILE_RULES",
        when="装载 send_file / list_agent_files 时",
    ),
    PromptSection(
        "tools.target_member",
        "管理对象确认",
        "safety",
        "tool_guidance",
        default_ref="prompt:_TARGET_MEMBER_RULES",
        description="管理操作的 user_id 必须是本轮明确指向的人。",
        when="装载禁言/踢人/改名片等成员管理工具时",
        safety=True,
    ),
    PromptSection(
        "tools.final_round",
        "最后一轮提示",
        "tools",
        "round",
        default_ref="turn_budget:FINAL_ROUND_NOTE",
        when="有工具结果且只剩最后一轮时追加一条 system",
    ),
    PromptSection(
        "tools.media_followup",
        "工具图片说明",
        "tools",
        "round",
        default_ref="turn_budget:TOOL_MEDIA_NOTE",
        when="工具查询结果带回图片、追加一条 user 图片消息时",
    ),
    PromptSection(
        "tools.truncated_args",
        "参数被截断提示",
        "tools",
        "tool_result",
        default_ref="turn_budget:TRUNCATED_ARGS_HINT",
        when="工具参数 JSON 因输出截断而不完整时，写进 tool 结果的 hint",
    ),
    # ── 实时状态 ──
    PromptSection(
        "layers.realtime_header",
        "实时状态层表头",
        "layers",
        "realtime",
        KIND_HEADER,
        default_ref="prompt:_REALTIME_SYSTEM_PREFIX",
        tasks=_DP,
    ),
    PromptSection(
        "context.realtime",
        "实时状态数据",
        "context",
        "realtime",
        KIND_DATA,
        "最近消息、话题、情绪、时钟、Bot 身份等（JSON）。",
        tasks=_DP,
    ),
    PromptSection(
        "layers.media_header",
        "媒体状态表头",
        "layers",
        "realtime",
        KIND_HEADER,
        default_ref="prompt:_MEDIA_SYSTEM_PREFIX",
        when="本轮 user 消息附带媒体块时",
    ),
    PromptSection(
        "core.media_policy",
        "本轮媒体权威",
        "core",
        "realtime",
        default_ref="prompt:_CURRENT_MEDIA_POLICY",
        description="有图就看图，别被历史里“看不到图”的旧回复带偏。",
        when="本轮 user 消息附带媒体块时",
    ),
    PromptSection(
        "core.scheduled_wake",
        "定时任务唤醒",
        "core",
        "realtime",
        default_ref="prompt:_SCHEDULED_WAKE_RULES",
        description="定时任务到点执行时，说明这一轮的来历和边界。",
        when="定时任务（agent 模式）到点唤醒时",
    ),
    PromptSection(
        "layers.speech_header",
        "发言策略表头",
        "safety",
        "realtime",
        KIND_HEADER,
        default_ref="prompt:_SPEECH_SYSTEM_PREFIX",
        description="声明发言策略只约束表达，不改变事实/权限/工具边界。",
        safety=True,
        tasks=_DP,
    ),
    # ── 发言策略 ──
    PromptSection(
        "speech.scene_label",
        "场景标签",
        "speech",
        "realtime",
        KIND_GENERATED,
        "“发言场景=xxx。”这一小句。",
        tasks=_DP,
    ),
    *(
        PromptSection(
            f"speech.scene.{scene}",
            f"场景：{label}",
            "speech",
            "realtime",
            default_ref=f"speech_policy:SCENE_RULES[{scene}]",
            when=f"发言场景={scene} 时",
            tasks=_DP,
        )
        for scene, label in (
            ("direct_reply", "明确呼叫"),
            ("reply_thread", "回复链承接"),
            ("conversation", "普通承接"),
            ("active_interject", "插话"),
            ("warmup", "冷场暖场"),
            ("followup", "短会话续聊"),
            ("tool_result", "工具结果反馈"),
            ("reaction", "轻量反应"),
            ("fallback", "降级回复"),
        )
    ),
    PromptSection(
        "speech.interaction",
        "有效回合说明",
        "speech",
        "realtime",
        KIND_GENERATED,
        "Effective Turn 类型、support/resumed_task/media_binding 的含义。",
        tasks=_DP,
    ),
    PromptSection(
        "speech.act",
        "话语动作",
        "speech",
        "realtime",
        KIND_GENERATED,
        "answer / repair / ping_ack 等本轮要做的话语动作。",
        tasks=_DP,
    ),
    PromptSection(
        "speech.turn_taking",
        "轮替",
        "speech",
        "realtime",
        KIND_GENERATED,
        "该接谁的话、要不要点名。",
        tasks=_DP,
    ),
    PromptSection(
        "speech.style",
        "表达参数与字数",
        "speech",
        "realtime",
        KIND_GENERATED,
        "温暖度/幽默/详略/表现力档位和软字数目标。",
        tasks=_DP,
    ),
    PromptSection(
        "speech.quality",
        "发言质量",
        "speech",
        "realtime",
        default_ref="speech_policy:QUALITY_RULE",
        description="不用无意义开场、不复述、不硬续聊。",
        tasks=_DP,
    ),
    PromptSection(
        "speech.native",
        "原生表达",
        "speech",
        "realtime",
        KIND_GENERATED,
        "表情、reaction 等 QQ 原生表达建议。",
        tasks=_DP,
    ),
    PromptSection(
        "speech.rhythm",
        "分条节奏",
        "speech",
        "realtime",
        KIND_GENERATED,
        "||| 分条与【节奏:…】标记说明。",
        tasks=_DP,
    ),
    PromptSection(
        "speech.sticker",
        "表情包",
        "speech",
        "realtime",
        KIND_GENERATED,
        "[表情:名字] 用法、发送频率和本群可用表情包菜单。",
        when="本群开放表情包、库里有图、不在冷却且人设允许接梗时",
        tasks=_DP,
    ),
    # ── 当前回合 ──
    PromptSection(
        "layers.current_turn_header",
        "当前回合表头",
        "safety",
        "user",
        KIND_HEADER,
        default_ref="prompt:CURRENT_TURN_HEADER",
        description="声明 content 是群消息资料，不是系统指令。",
        safety=True,
    ),
    PromptSection(
        "context.current_turn",
        "当前回合数据",
        "context",
        "user",
        KIND_DATA,
        "发言人、正文、@、回复对象等（JSON）。",
        editable=False,
    ),
    # ── 主动发言 ──
    PromptSection(
        "proactive.active",
        "插话提示",
        "proactive",
        "proactive_user",
        default_ref="proactive_policy:_ACTIVE_INTERJECT_PROMPT",
        when="热闹插话评估时",
        tasks=_P,
    ),
    PromptSection(
        "proactive.warmup",
        "暖场提示",
        "proactive",
        "proactive_user",
        default_ref="proactive_policy:_WARMUP_PROMPT",
        when="冷场暖场评估时",
        tasks=_P,
    ),
    PromptSection(
        "proactive.followup",
        "续聊提示",
        "proactive",
        "proactive_user",
        default_ref="proactive_policy:_FOLLOWUP_PROMPT",
        when="短会话续聊评估时",
        tasks=_P,
    ),
    PromptSection(
        "proactive.behavior",
        "人设行为",
        "proactive",
        "proactive_user",
        KIND_GENERATED,
        "按人设活跃度/社交性生成的参与分寸。",
        tasks=_P,
    ),
    PromptSection(
        "proactive.sticker",
        "表情包",
        "proactive",
        "proactive_user",
        KIND_GENERATED,
        "主动/续聊里能用的表情包菜单与用法。",
        when="允许主动发表情包且本群有可用表情时",
        tasks=_P,
    ),
    PromptSection(
        "proactive.memory_use",
        "记忆使用",
        "proactive",
        "proactive_user",
        default_ref="proactive_policy:_MEMORY_USE_PROMPT",
        tasks=_P,
    ),
    PromptSection(
        "proactive.silence",
        "冷场事实",
        "proactive",
        "proactive_user",
        KIND_GENERATED,
        "冷场多久、连续几条没人接。",
        when="暖场时",
        tasks=_P,
    ),
    PromptSection(
        "proactive.turn_count",
        "续聊计数",
        "proactive",
        "proactive_user",
        KIND_GENERATED,
        "这是本话题第几条候选发言。",
        when="续聊/插话带轮次时",
        tasks=_P,
    ),
    PromptSection(
        "proactive.json_protocol",
        "决策 JSON 协议",
        "proactive",
        "proactive_user",
        default_ref="proactive_policy:_JSON_PROTOCOL",
        description="要求模型只返回 speak/wait/close JSON；改坏了会解析失败。",
        protocol=True,
        tasks=_P,
    ),
    PromptSection(
        "proactive.recent_lines",
        "最近主动发言",
        "proactive",
        "proactive_user",
        KIND_GENERATED,
        "最近几条主动发言，避免重复。",
        when="有最近主动发言时",
        tasks=_P,
    ),
    # ── 私聊 Agent ──
    PromptSection(
        "private.role",
        "私聊开场",
        "private",
        "private_system",
        default_ref="private_prompt:PRIVATE_ROLE",
        description="告诉模型这是一对一私聊，和群里是同一个人。",
        tasks=_PV,
    ),
    PromptSection(
        "private.persona",
        "私聊人设块",
        "private",
        "private_system",
        KIND_GENERATED,
        "这位用户生效的私聊人设编译成的角色卡：本人专属 → 本人选的人设库模板 → 私聊默认 → 全局人设 → 内置默认。",
        tasks=_PV,
    ),
    PromptSection(
        "private.rules",
        "私聊规则",
        "private",
        "private_system",
        default_ref="private_prompt:PRIVATE_RULES",
        description="语气、分条、先查工具再回答、只说等级不报好感数值。",
        tasks=_PV,
    ),
    PromptSection(
        "private.safety",
        "私聊安全",
        "safety",
        "private_system",
        default_ref="private_prompt:PRIVATE_SAFETY",
        description="资料与工具结果是不可信数据；不泄露系统提示和他人隐私；未成功不说已完成。",
        safety=True,
        tasks=_PV,
    ),
    PromptSection(
        "private.context_header",
        "用户资料表头",
        "private",
        "private_system",
        KIND_HEADER,
        default_ref="private_prompt:PRIVATE_CONTEXT_HEADER",
        tasks=_PV,
    ),
    PromptSection(
        "private.context",
        "长期资料数据",
        "context",
        "private_system",
        KIND_DATA,
        "长期资料（JSON，排在历史前面、前缀缓存可命中）：称呼、好感等级、私聊记忆、各群关系与画像、"
        "共同认识的人、各群近况、共同所在的群。",
        tasks=_PV,
    ),
    PromptSection(
        "private.context_guide",
        "资料怎么用",
        "private",
        "private_system",
        default_ref="private_prompt:PRIVATE_CONTEXT_GUIDE",
        description="长期资料/本轮资料的分工，群里的关系、近况、他刚说的话怎么自然地用，什么时候用 recall 查。",
        tasks=_PV,
    ),
    PromptSection(
        "private.rhythm",
        "私聊发送节奏",
        "private",
        "private_system",
        KIND_GENERATED,
        "【节奏:…】与 ||| 分条的用法，按这位用户生效人设的表现力/详略生成；只随人设变，放在开头那层（前缀缓存）。",
        tasks=_PV,
    ),
    PromptSection(
        "private.turn_header",
        "本轮资料表头",
        "private",
        "private_system",
        KIND_HEADER,
        default_ref="private_prompt:PRIVATE_TURN_HEADER",
        tasks=_PV,
    ),
    PromptSection(
        "private.turn_context",
        "本轮资料数据",
        "context",
        "private_system",
        KIND_DATA,
        "本轮资料（JSON，排在最后一条消息后面）：现在的时间、签到、待办提醒、后台任务、刚做完的结果、"
        "附件、他最近在群里说的话、之前聊到哪。",
        tasks=_PV,
    ),
    PromptSection(
        "private.time_note",
        "久别提示",
        "private",
        "private_system",
        KIND_GENERATED,
        "距离上次私聊超过 20 分钟时附上当前时间。",
        when="久别重聊时",
        tasks=_PV,
    ),
    PromptSection(
        "private.final_round",
        "私聊最后一轮",
        "private",
        "round",
        default_ref="private_prompt:PRIVATE_FINAL_ROUND",
        when="工具轮次用完时",
        tasks=_PV,
    ),
    PromptSection(
        "private.repeat_guard",
        "别复读上一条",
        "private",
        "round",
        default_ref="private_prompt:PRIVATE_REPEAT_GUARD",
        description="想好的回复和上一条几乎一样时追加一次（每轮最多一次）：换个说法或接着聊。",
        when="回复与上一条相似度过高时",
        tasks=_PV,
    ),
    PromptSection(
        "private.media",
        "私聊发图发文件",
        "private",
        "private_system",
        default_ref="private_prompt:PRIVATE_MEDIA",
        description="[图片:sandbox:…]/[文件:sandbox:…] 标记的用法，他发来的附件在虚拟电脑里。",
        when="虚拟电脑可用时",
        tasks=_PV,
    ),
    PromptSection(
        "private.delegation",
        "后台做事",
        "private",
        "private_system",
        default_ref="private_prompt:PRIVATE_DELEGATION",
        description="什么时候用 delegate_task 放到后台做（判断标准偏向放后台）、只需 goal、follow_up_of 接着做；对外永远用“我”说话。",
        when="小助手任务开启时",
        tasks=_PV,
    ),
    PromptSection(
        "private.turn_style",
        "私聊发言方式",
        "private",
        "private_system",
        KIND_GENERATED,
        "每轮生成：私聊可用的表情包菜单（额度、冷却每轮都可能变）+ 附件提示；节奏说明在 private.rhythm。",
        tasks=_PV,
    ),
    PromptSection(
        "private.turn_taking",
        "要不要说话",
        "private",
        "private_system",
        default_ref="private_prompt:PRIVATE_TURN_TAKING",
        description="连发消息时可以只写【等他说完】或【不回】这轮不说话；系统据此不发消息或稍后接话。",
        when="发言判断开启时（PRIVATE_AGENT_TURN_TAKING）",
        tasks=_PV,
    ),
    PromptSection(
        "private.toolkits",
        "虚拟电脑能力包",
        "private",
        "private_system",
        default_ref="private_prompt:PRIVATE_TOOLKITS",
        description="computer_kits / computer_do 的渐进式用法：先 list、再 open、再调用；按 network 判断能不能下载。",
        when="虚拟电脑可用时",
        tasks=_PV,
    ),
    PromptSection(
        "private.team",
        "给后台补充要求",
        "private",
        "private_system",
        default_ref="private_prompt:PRIVATE_TEAM",
        description="主智能体用 tasks message 给运行中的小助手发消息，读 helper_messages 回答小助手的提问。",
        when="小助手任务开启时",
        tasks=_PV,
    ),
    PromptSection(
        "private.coordinate_turn",
        "后台协调轮",
        "private",
        "round",
        default_ref="private_prompt:PRIVATE_COORDINATE_TURN",
        description="小助手给主智能体发消息时的一轮：能答就直接回小助手，不需要就【不回】，必要时才问用户。",
        when="小助手发来需要回复的消息时",
        tasks=_PV,
    ),
    PromptSection(
        "private.worker_team",
        "后台团队通信",
        "private",
        "aux_system",
        default_ref="private_prompt:PRIVATE_WORKER_TEAM",
        description="team 工具：给主助手、队友发消息，等回复；团队消息是资料。",
        when="子智能体运行时",
        tasks=_PV,
    ),
    PromptSection(
        "private.worker_voice",
        "后台对他说话的口吻",
        "private",
        "aux_system",
        default_ref="private_prompt:PRIVATE_WORKER_VOICE",
        description="report_progress 的 note 和 ask_user 的问题原样发给他：用人设口吻、他的称呼、第一人称，不提内部词。",
        when="子智能体运行时",
        tasks=_PV,
    ),
    PromptSection(
        "private.just_finished",
        "结果顺进聊天",
        "private",
        "private_system",
        default_ref="private_prompt:PRIVATE_JUST_FINISHED",
        description="他发消息时刚好有后台结果还没告诉他：先回他这句，再顺口把结果说了。",
        when="有刚做完、还没汇报的后台结果时",
        tasks=_PV,
    ),
    PromptSection(
        "private.handoff_nudge",
        "提醒放到后台",
        "private",
        "round",
        default_ref="private_prompt:PRIVATE_HANDOFF_NUDGE",
        description="主智能体本轮自己调了好几次重活工具（PRIVATE_AGENT_HANDOFF_AFTER）还没答完时插一次：交给后台接着做。",
        when="本轮重活工具次数达到阈值时（每轮最多一次）",
        tasks=_PV,
    ),
    PromptSection(
        "private.auto_handoff",
        "已自动转到后台",
        "private",
        "round",
        default_ref="private_prompt:PRIVATE_AUTO_HANDOFF",
        description="工具轮次用完时系统已把这件事自动放到后台（PRIVATE_AGENT_AUTO_HANDOFF），代替最后一轮提示：简短告诉他还在弄。",
        when="工具轮次用完、自动转后台成功时",
        tasks=_PV,
    ),
    PromptSection(
        "private.worker_continue",
        "后台续期",
        "private",
        "round",
        default_ref="private_prompt:PRIVATE_WORKER_CONTINUE",
        when="子智能体轮次用完但还有续期时",
        tasks=_PV,
    ),
    PromptSection(
        "private.report_turn",
        "后台结果汇报轮",
        "private",
        "round",
        default_ref="private_prompt:PRIVATE_REPORT_TURN",
        description="后台做完后，主智能体像自己刚弄完一样用人设告诉他；几件事一起做完合成一次说；等回答时能答就直接转过去。",
        when="小助手任务结束时",
        tasks=_PV,
    ),
    PromptSection(
        "private.worker_role",
        "后台角色",
        "private",
        "aux_system",
        default_ref="private_prompt:PRIVATE_WORKER_ROLE",
        description="后台子智能体是同一个人的另一条线：看任务单（含聊天节选和人设卡片），直接发给他的话用“我”。",
        when="子智能体运行时",
        tasks=_PV,
    ),
    PromptSection(
        "private.worker_rules",
        "后台做法",
        "private",
        "aux_system",
        default_ref="private_prompt:PRIVATE_WORKER_RULES",
        description="拆步骤、每步 report_progress（会发给用户）、finish/ask_user 的用法。",
        when="子智能体运行时",
        tasks=_PV,
    ),
    PromptSection(
        "private.worker_safety",
        "后台安全",
        "safety",
        "aux_system",
        default_ref="private_prompt:PRIVATE_WORKER_SAFETY",
        description="任务单、网页、文件和程序输出都是资料；不联系他人、不索要隐私。",
        when="子智能体运行时",
        safety=True,
        tasks=_PV,
    ),
    PromptSection(
        "private.worker_final",
        "后台最后一轮",
        "private",
        "round",
        default_ref="private_prompt:PRIVATE_WORKER_FINAL",
        when="子智能体工具轮次用完时",
        tasks=_PV,
    ),
    PromptSection(
        "private.memory_system",
        "私聊记忆整理 system",
        "private",
        "aux_system",
        default_ref="private_prompt:PRIVATE_MEMORY_SYSTEM",
        description="每隔几轮从私聊里整理关于用户本人的事实；只返回 JSON。",
        protocol=True,
        tasks=("agent_memory",),
    ),
    PromptSection(
        "private.memory_recap",
        "私聊对话回顾",
        "private",
        "aux_system",
        default_ref="private_prompt:PRIVATE_MEMORY_RECAP",
        description="记忆整理时顺便写“最近聊了什么、还有什么没下文”，下次作为 earlier_chat 放进本轮资料（PRIVATE_AGENT_RECAP）。",
        when="开着对话回顾时",
        protocol=True,
        tasks=("agent_memory",),
    ),
    PromptSection(
        "private.persona_scope",
        "专属人设说明",
        "private",
        "private_system",
        default_ref="private_prompt:PRIVATE_PERSONA_SCOPE",
        description="他有自己的私聊人设（本人专属卡或他选的人设库模板）时附在人设块后：私聊按这张卡，群里另有人设，别混用、别出戏。",
        when="这位用户有专属私聊人设时",
        tasks=_PV,
    ),
    PromptSection(
        "private.resumed_note",
        "等他说完后接话",
        "private",
        "round",
        default_ref="private_prompt:PRIVATE_RESUMED_NOTE",
        description="上一轮写了【等他说完】、他停下来一会儿后系统自动接话时追加：把连着的几条当一件事回应。",
        when="【等他说完】计时结束自动接话时",
        tasks=_PV,
    ),
    PromptSection(
        "private.files_hint",
        "发文件提示",
        "private",
        "private_system",
        default_ref="private_prompt:PRIVATE_FILES_HINT",
        description="他这次发了文件且后台可用时，放进本轮发言方式里：要加工文件就放到后台做。",
        when="他发了文件且小助手任务开启时",
        tasks=_PV,
    ),
    PromptSection(
        "private.attachments_unread",
        "文件没进电脑的说明",
        "private",
        "private_system",
        default_ref="private_prompt:PRIVATE_ATTACHMENTS_UNREAD",
        description="写在用户资料 attachments_note：文件没进虚拟电脑、只读到了文字节选时怎么处理。",
        when="附件没进虚拟电脑但读出了文字时",
        tasks=_PV,
    ),
    PromptSection(
        "private.attachments_missing",
        "附件没能入箱的说明",
        "private",
        "private_system",
        default_ref="private_prompt:PRIVATE_ATTACHMENTS_MISSING",
        description="写在用户资料 attachments_note：附件完全没能放进虚拟电脑时。",
        when="附件没能放进虚拟电脑时",
        tasks=_PV,
    ),
    PromptSection(
        "private.attachment_excerpt",
        "历史里的文件节选",
        "private",
        "round",
        default_ref="private_prompt:PRIVATE_ATTACHMENT_EXCERPT",
        description="文件没进电脑时写回他那条历史消息的节选注释；{title} 换成文件名，{text} 换成节选，删掉占位符就不带对应内容。",
        when="文件没进虚拟电脑但读出了文字时（写进历史）",
        tasks=_PV,
    ),
    PromptSection(
        "private.report_notice",
        "汇报轮通知表头",
        "private",
        "round",
        KIND_HEADER,
        default_ref="private_prompt:PRIVATE_REPORT_NOTICE",
        description="后台结果汇报轮里，放在结果 JSON 前的 user 消息表头。",
        when="小助手任务结束时",
        tasks=_PV,
    ),
    PromptSection(
        "private.coordinate_notice",
        "协调轮通知表头",
        "private",
        "round",
        KIND_HEADER,
        default_ref="private_prompt:PRIVATE_COORDINATE_NOTICE",
        description="后台协调轮里，放在 helper_messages JSON 前的 user 消息表头。",
        when="小助手发来需要回复的消息时",
        tasks=_PV,
    ),
    PromptSection(
        "private.handoff_findings",
        "自动转后台的素材表头",
        "private",
        "aux_system",
        KIND_HEADER,
        default_ref="private_prompt:PRIVATE_HANDOFF_FINDINGS",
        description="工具轮次用完自动放到后台时，任务单 background 里“前台已经查到的结果”的开头。",
        when="自动转后台时",
        tasks=_PV,
    ),
    PromptSection(
        "private.worker_brief",
        "任务单表头",
        "private",
        "aux_system",
        KIND_HEADER,
        default_ref="private_prompt:PRIVATE_WORKER_BRIEF",
        description="子智能体第一条 user 消息里任务单 JSON 前面的表头。",
        when="子智能体运行时",
        tasks=_PV,
    ),
    PromptSection(
        "private.worker_answer",
        "用户回答前缀",
        "private",
        "round",
        KIND_HEADER,
        default_ref="private_prompt:PRIVATE_WORKER_ANSWER",
        description="子智能体 ask_user 之后，用户的回答转回去时的前缀。",
        when="子智能体收到用户回答时",
        tasks=_PV,
    ),
    PromptSection(
        "private.worker_workbench",
        "工作台表头",
        "private",
        "round",
        KIND_HEADER,
        default_ref="private_prompt:PRIVATE_WORKER_WORKBENCH",
        description="上下文压缩或续期后重新附上的计划与备忘的表头。",
        when="子智能体上下文压缩或续期时",
        tasks=_PV,
    ),
    PromptSection(
        "private.worker_inbox",
        "团队消息表头",
        "private",
        "round",
        KIND_HEADER,
        default_ref="private_prompt:PRIVATE_WORKER_INBOX",
        description="子智能体每轮开始前收到的未读团队消息的表头。",
        when="子智能体有未读团队消息时",
        tasks=_PV,
    ),
    # ── 辅助模型 ──
    PromptSection(
        "router.system",
        "路由器 system",
        "router",
        "aux_system",
        default_ref="tool_planner:_SYSTEM_PROMPT",
        description="判断要不要做事、预装哪些工具包；只返回 JSON。",
        protocol=True,
        safety=True,
        tasks=("agent_router",),
    ),
    PromptSection(
        "vision.caption",
        "识图 system",
        "vision",
        "aux_system",
        default_ref="media_caption:_VISION_SYSTEM_PROMPT",
        when="模型不支持多模态、改走识图转述时",
        tasks=("agent_image",),
    ),
    PromptSection(
        "vision.auto_caption",
        "自动转述 system",
        "vision",
        "aux_system",
        default_ref="turn_media:_AUTO_CAPTION_PROMPT",
        when="后台给群图片做缓存转述时",
        tasks=("agent_image",),
    ),
    PromptSection(
        "memory.consolidation",
        "记忆整理 system",
        "memory",
        "aux_system",
        KIND_GENERATED,
        "提取事实/关系/Bot 关系并合并摘要；只返回 JSON。改坏了整理会失败并保留游标重试。",
        protocol=True,
        safety=True,
        when="记忆整理任务",
        tasks=("agent_memory",),
    ),
    PromptSection(
        "memory.weekly",
        "周摘要 system",
        "memory",
        "aux_system",
        KIND_GENERATED,
        "把一周日摘要合并成周摘要；只返回 JSON。",
        protocol=True,
        when="周摘要任务",
        tasks=("agent_memory",),
    ),
)

_SECTION_INDEX: dict[str, PromptSection] = {section.id: section for section in SECTIONS}
TOOL_SECTION_PREFIX = "tool."


def _tool_definitions() -> list[Any]:
    try:
        from .tool_registry import TOOL_DEFINITIONS

        return list(TOOL_DEFINITIONS)
    except Exception:  # noqa: BLE001
        return []


def tool_section(name: str) -> PromptSection:
    return PromptSection(
        f"{TOOL_SECTION_PREFIX}{name}",
        f"工具 {name}",
        "tool_schema",
        "tool_schema",
        description="tools[].function.description",
        when=f"本轮装载 {name} 时",
    )


# 私聊工具（private_tools.ALL_TOOLS）与后台子智能体的工作台/控制工具的描述。
PRIVATE_TOOL_SECTION_PREFIX = "ptool."
# 只有这些前缀的分段可以按单个私聊用户覆盖（作用域 ``user:<QQ>``）。
PRIVATE_SECTION_PREFIXES = ("private.", PRIVATE_TOOL_SECTION_PREFIX)


def is_private_section(section_id: str) -> bool:
    return str(section_id).startswith(PRIVATE_SECTION_PREFIXES)


def _private_tool_definitions() -> dict[str, tuple[str, str]]:
    """``{name: (description, where)}``；where = main（私聊主回合）/ worker（只给后台）。"""

    found: dict[str, tuple[str, str]] = {}
    try:
        from .private_tools import ALL_TOOLS

        for tool in ALL_TOOLS:
            found[tool.name] = (str(tool.description), "main")
    except Exception:  # noqa: BLE001
        pass
    try:
        from .private_worker import CONTROL_SPECS, WORKBENCH_SPECS

        for spec in (*WORKBENCH_SPECS, *CONTROL_SPECS):
            name = str(spec.get("name") or "")
            if name and name not in found:
                found[name] = (str(spec.get("description") or ""), "worker")
    except Exception:  # noqa: BLE001
        pass
    return found


def private_tool_section(name: str, where: str = "main") -> PromptSection:
    return PromptSection(
        f"{PRIVATE_TOOL_SECTION_PREFIX}{name}",
        f"私聊工具 {name}",
        "private_tools",
        "tool_schema",
        description="私聊 tools[].function.description"
        if where == "main"
        else "后台子智能体 tools[].function.description",
        when=f"私聊本轮装载 {name} 时" if where == "main" else "后台子智能体运行时",
        tasks=_PV,
    )


def get_section(section_id: str) -> PromptSection | None:
    section = _SECTION_INDEX.get(section_id)
    if section is not None:
        return section
    if section_id.startswith(TOOL_SECTION_PREFIX):
        name = section_id[len(TOOL_SECTION_PREFIX) :]
        if any(getattr(item, "name", None) == name for item in _tool_definitions()):
            return tool_section(name)
    if section_id.startswith(PRIVATE_TOOL_SECTION_PREFIX):
        name = section_id[len(PRIVATE_TOOL_SECTION_PREFIX) :]
        found = _private_tool_definitions().get(name)
        if found is not None:
            return private_tool_section(name, found[1])
    return None


def private_tool_sections() -> list[PromptSection]:
    return [
        private_tool_section(name, where)
        for name, (_desc, where) in _private_tool_definitions().items()
    ]


def all_sections(*, include_private: bool = True) -> list[PromptSection]:
    tools = sorted(
        {str(getattr(item, "name", "")) for item in _tool_definitions()} - {""}
    )
    sections = [*SECTIONS, *(tool_section(name) for name in tools)]
    if include_private:
        return [*sections, *private_tool_sections()]
    return [section for section in sections if not is_private_section(section.id)]


def section_default(section: PromptSection) -> str | None:
    if section.category == "tool_schema":
        name = section.id[len(TOOL_SECTION_PREFIX) :]
        for item in _tool_definitions():
            if getattr(item, "name", None) == name:
                return str(getattr(item, "description", "") or "")
        return None
    if section.category == "private_tools":
        found = _private_tool_definitions().get(
            section.id[len(PRIVATE_TOOL_SECTION_PREFIX) :]
        )
        return found[0] if found is not None else None
    return section.default_text()


# ── 覆盖存储 ─────────────────────────────────────────────────────────────


@dataclass(slots=True)
class Override:
    mode: str
    text: str = ""
    note: str = ""
    updated_at: str = ""
    updated_by: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "mode": self.mode,
            "text": self.text,
            "note": self.note,
            "updatedAt": self.updated_at,
            "updatedBy": self.updated_by,
        }

    @classmethod
    def from_raw(cls, raw: Any) -> Override | None:
        if not isinstance(raw, Mapping):
            return None
        mode = str(raw.get("mode") or "")
        if mode not in OVERRIDE_MODES:
            return None
        return cls(
            mode=mode,
            text=str(raw.get("text") or "")[:MAX_SECTION_TEXT],
            note=str(raw.get("note") or "")[:MAX_NOTE_CHARS],
            updated_at=str(raw.get("updatedAt") or raw.get("updated_at") or ""),
            updated_by=str(raw.get("updatedBy") or raw.get("updated_by") or ""),
        )


@dataclass(slots=True)
class _StoreState:
    revision: int = 0
    scopes: dict[str, dict[str, Override]] = field(default_factory=dict)
    history: dict[str, dict[str, list[dict[str, Any]]]] = field(default_factory=dict)
    updated_at: str = ""
    loaded_mtime: float | None = None
    load_error: str | None = None


class PromptStoreConflictError(RuntimeError):
    """写入时 revision 不匹配：别处（另一个标签页）刚改过。"""

    def __init__(self, current: int) -> None:
        super().__init__(f"提示词覆盖已被修改（当前版本 {current}），请刷新后重试")
        self.current = current


class PromptStoreValidationError(ValueError):
    def __init__(self, message: str, *, field_name: str = "text") -> None:
        super().__init__(message)
        self.field_name = field_name


def _default_store_path() -> Path:
    raw = os.environ.get("AGENT_PROMPT_OVERRIDES_PATH", "").strip()
    if raw:
        return Path(raw)
    return Path("data") / "yawn_agent" / "prompt_overrides.json"


_store_path: Path | None = None
_state = _StoreState()
_lock = threading.RLock()


def store_path() -> Path:
    return _store_path or _default_store_path()


def set_store_path(path: Path | None) -> None:
    """测试/部署用：切换覆盖文件并清空缓存。"""

    global _store_path, _state  # noqa: PLW0603
    with _lock:
        _store_path = path
        _state = _StoreState()


def _scope_key(group_id: int | None, user_id: int | None = None) -> str:
    if user_id is not None:
        return f"{USER_SCOPE_PREFIX}{int(user_id)}"
    return GLOBAL_SCOPE if group_id is None else str(int(group_id))


def user_scope_key(user_id: int) -> str:
    return _scope_key(None, user_id)


def is_user_scope(scope: str) -> bool:
    return str(scope).startswith(USER_SCOPE_PREFIX)


def scope_user_id(scope: str) -> int | None:
    if not is_user_scope(scope):
        return None
    raw = str(scope)[len(USER_SCOPE_PREFIX) :]
    return int(raw) if raw.isdigit() else None


def _check_scope(section_id: str, group_id: int | None, user_id: int | None) -> None:
    """本人作用域只收私聊分段；群和用户不能同时指定。"""

    if user_id is None:
        return
    if group_id is not None:
        raise PromptStoreValidationError("不能同时按群和按用户覆盖", field_name="scope")
    if int(user_id) <= 0:
        raise PromptStoreValidationError("用户 QQ 号无效", field_name="userId")  # noqa: TRY003
    if section_id and not is_private_section(section_id):
        raise PromptStoreValidationError(
            "按用户覆盖只适用于私聊分段", field_name="sectionId"
        )


def _load_locked() -> _StoreState:  # noqa: C901
    global _state  # noqa: PLW0603
    path = store_path()
    try:
        mtime = path.stat().st_mtime
    except OSError:
        if _state.loaded_mtime is not None:
            _state = _StoreState()
        return _state
    if _state.loaded_mtime == mtime:
        return _state
    fresh = _StoreState(loaded_mtime=mtime)
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        # 文件损坏时保持“没有覆盖”，不能让 Agent 因为一个 JSON 起不来。
        fresh.load_error = f"{type(exc).__name__}: {str(exc)[:160]}"
        _state = fresh
        return _state
    if isinstance(raw, Mapping):
        fresh.revision = int(raw.get("revision") or 0)
        fresh.updated_at = str(raw.get("updatedAt") or "")
        for scope, entries in dict(raw.get("scopes") or {}).items():
            if not isinstance(entries, Mapping):
                continue
            parsed = {
                str(section_id): override
                for section_id, value in entries.items()
                if (override := Override.from_raw(value)) is not None
            }
            if parsed:
                fresh.scopes[str(scope)] = parsed
        for scope, sections in dict(raw.get("history") or {}).items():
            if isinstance(sections, Mapping):
                fresh.history[str(scope)] = {
                    str(key): [
                        dict(item)
                        for item in list(value)[-MAX_HISTORY_PER_SECTION:]
                        if isinstance(item, Mapping)
                    ]
                    for key, value in sections.items()
                    if isinstance(value, list)
                }
    _state = fresh
    return _state


def _save_locked(state: _StoreState) -> None:
    path = store_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "schemaVersion": STORE_SCHEMA_VERSION,
        "revision": state.revision,
        "updatedAt": state.updated_at,
        "scopes": {
            scope: {key: value.as_dict() for key, value in sorted(entries.items())}
            for scope, entries in sorted(state.scopes.items())
            if entries
        },
        "history": state.history,
    }
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, path)  # noqa: PTH105
    with contextlib.suppress(OSError):
        state.loaded_mtime = path.stat().st_mtime


def store_snapshot() -> dict[str, Any]:
    with _lock:
        state = _load_locked()
        return {
            "revision": state.revision,
            "updatedAt": state.updated_at or None,
            "path": str(store_path().name),
            "loadError": state.load_error,
            "scopes": {
                scope: {key: value.as_dict() for key, value in entries.items()}
                for scope, entries in state.scopes.items()
            },
        }


def store_revision() -> int:
    with _lock:
        return _load_locked().revision


def scope_overrides(
    group_id: int | None, *, user_id: int | None = None
) -> dict[str, Override]:
    with _lock:
        return dict(_load_locked().scopes.get(_scope_key(group_id, user_id), {}))


def user_scopes() -> dict[int, dict[str, Override]]:
    """所有有本人覆盖的私聊用户：``{QQ: {section_id: Override}}``。"""

    with _lock:
        state = _load_locked()
        result: dict[int, dict[str, Override]] = {}
        for scope, entries in state.scopes.items():
            uid = scope_user_id(scope)
            if uid is not None and entries:
                result[uid] = dict(entries)
        return result


def section_history(
    section_id: str, group_id: int | None, *, user_id: int | None = None
) -> list[dict[str, Any]]:
    with _lock:
        state = _load_locked()
        return list(
            reversed(
                state.history.get(_scope_key(group_id, user_id), {}).get(section_id, [])
            )
        )


def validate_override(
    section: PromptSection,
    mode: str,
    text: str,
    *,
    confirm_safety: bool = False,
) -> str:
    """返回规范化后的文本；不合法时抛 :class:`PromptStoreValidationError`。"""

    if not section.editable:
        raise PromptStoreValidationError(
            "这一段是本轮数据，不能在这里改", field_name="mode"
        )
    if mode not in section.modes:
        raise PromptStoreValidationError(f"这一段不支持“{mode}”", field_name="mode")
    cleaned = str(text or "").replace("\x00", "").replace("\r\n", "\n")
    if len(cleaned) > MAX_SECTION_TEXT:
        raise PromptStoreValidationError(f"文本最长 {MAX_SECTION_TEXT} 字")  # noqa: TRY003
    if mode in _TEXT_MODES and not cleaned.strip():
        raise PromptStoreValidationError(
            "替换/追加/前置需要填写文本；想删除这段请选“删除”"
        )
    if mode not in _TEXT_MODES:
        cleaned = ""
    if section.safety and mode in {MODE_DISABLE, MODE_REPLACE} and not confirm_safety:
        raise PromptStoreValidationError(
            "这是安全相关分段，删除或替换前需要确认", field_name="confirmSafety"
        )
    return cleaned


def _check_scope_capacity(state: _StoreState, scope: str) -> None:
    if scope == GLOBAL_SCOPE or scope in state.scopes:
        return
    if is_user_scope(scope):
        used = sum(1 for key in state.scopes if is_user_scope(key))
        if used >= MAX_USER_SCOPES:
            raise PromptStoreValidationError(
                "单独覆盖提示词的私聊用户太多了", field_name="userId"
            )
        return
    used = sum(
        1 for key in state.scopes if key != GLOBAL_SCOPE and not is_user_scope(key)
    )
    if used >= MAX_GROUP_SCOPES:
        raise PromptStoreValidationError(
            "单独覆盖提示词的群太多了", field_name="groupId"
        )


def _history_entry(
    scope_entry: Override | None, action: str, actor: str, revision: int
) -> dict[str, Any]:
    return {
        "action": action,
        "revision": revision,
        "at": now_beijing().isoformat(timespec="seconds"),
        "by": actor[:32],
        "override": scope_entry.as_dict() if scope_entry is not None else None,
    }


def _check_revision(state: _StoreState, expected: int | None) -> None:
    if expected is not None and int(expected) != state.revision:
        raise PromptStoreConflictError(state.revision)


def _commit(state: _StoreState) -> int:
    state.revision += 1
    state.updated_at = now_beijing().isoformat(timespec="seconds")
    _save_locked(state)
    return state.revision


def set_override(
    section_id: str,
    *,
    group_id: int | None,
    mode: str,
    text: str = "",
    note: str = "",
    actor: str = "",
    expected_revision: int | None = None,
    confirm_safety: bool = False,
    user_id: int | None = None,
) -> dict[str, Any]:
    section = get_section(section_id)
    if section is None:
        raise PromptStoreValidationError("未知的提示词分段", field_name="sectionId")
    _check_scope(section_id, group_id, user_id)
    cleaned = validate_override(section, mode, text, confirm_safety=confirm_safety)
    if mode == MODE_BUILTIN and group_id is None and user_id is None:
        # 全局的“用默认值”就是删除全局覆盖。
        return clear_override(
            section_id, group_id=None, actor=actor, expected_revision=expected_revision
        )
    with _lock:
        state = _load_locked()
        _check_revision(state, expected_revision)
        scope = _scope_key(group_id, user_id)
        _check_scope_capacity(state, scope)
        override = Override(
            mode=mode,
            text=cleaned,
            note=str(note or "").strip()[:MAX_NOTE_CHARS],
            updated_at=now_beijing().isoformat(timespec="seconds"),
            updated_by=str(actor or "")[:32],
        )
        state.scopes.setdefault(scope, {})[section_id] = override
        revision = state.revision + 1
        bucket = state.history.setdefault(scope, {}).setdefault(section_id, [])
        bucket.append(_history_entry(override, "set", actor, revision))
        del bucket[:-MAX_HISTORY_PER_SECTION]
        _commit(state)
        return {"revision": state.revision, "override": override.as_dict()}


def clear_override(
    section_id: str,
    *,
    group_id: int | None,
    actor: str = "",
    expected_revision: int | None = None,
    user_id: int | None = None,
) -> dict[str, Any]:
    _check_scope(section_id, group_id, user_id)
    with _lock:
        state = _load_locked()
        _check_revision(state, expected_revision)
        scope = _scope_key(group_id, user_id)
        entries = state.scopes.get(scope, {})
        if section_id not in entries:
            return {"revision": state.revision, "override": None, "changed": False}
        entries.pop(section_id, None)
        if not entries:
            state.scopes.pop(scope, None)
        bucket = state.history.setdefault(scope, {}).setdefault(section_id, [])
        bucket.append(_history_entry(None, "clear", actor, state.revision + 1))
        del bucket[:-MAX_HISTORY_PER_SECTION]
        _commit(state)
        return {"revision": state.revision, "override": None, "changed": True}


def bulk_apply(  # noqa: C901, PLR0912
    changes: list[dict[str, Any]],
    *,
    group_id: int | None,
    actor: str = "",
    expected_revision: int | None = None,
    confirm_safety: bool = False,
    replace_scope: bool = False,
    user_id: int | None = None,
) -> dict[str, Any]:
    """一次提交多处改动（批量删除安全段、导入、整组恢复默认）；全部校验通过才写。"""

    _check_scope("", group_id, user_id)
    prepared: list[tuple[str, Override | None]] = []
    for item in changes:
        section_id = str(item.get("sectionId") or "")
        section = get_section(section_id)
        if section is None:
            raise PromptStoreValidationError(
                f"未知的提示词分段：{section_id[:64]}", field_name="sectionId"
            )
        _check_scope(section_id, group_id, user_id)
        mode = str(item.get("mode") or "")
        if mode in {"", "inherit", "default"}:
            prepared.append((section_id, None))
            continue
        cleaned = validate_override(
            section, mode, str(item.get("text") or ""), confirm_safety=confirm_safety
        )
        if mode == MODE_BUILTIN and group_id is None and user_id is None:
            prepared.append((section_id, None))
            continue
        prepared.append(
            (
                section_id,
                Override(
                    mode=mode,
                    text=cleaned,
                    note=str(item.get("note") or "").strip()[:MAX_NOTE_CHARS],
                    updated_at=now_beijing().isoformat(timespec="seconds"),
                    updated_by=str(actor or "")[:32],
                ),
            )
        )
    with _lock:
        state = _load_locked()
        _check_revision(state, expected_revision)
        scope = _scope_key(group_id, user_id)
        if any(override is not None for _sid, override in prepared):
            _check_scope_capacity(state, scope)
        entries = {} if replace_scope else dict(state.scopes.get(scope, {}))
        revision = state.revision + 1
        touched = 0
        for section_id, override in prepared:
            before = entries.get(section_id)
            if override is None:
                if before is None:
                    continue
                entries.pop(section_id, None)
            else:
                entries[section_id] = override
            touched += 1
            bucket = state.history.setdefault(scope, {}).setdefault(section_id, [])
            bucket.append(_history_entry(override, "bulk", actor, revision))
            del bucket[:-MAX_HISTORY_PER_SECTION]
        if replace_scope:
            touched = max(touched, len(state.scopes.get(scope, {})))
        if entries:
            state.scopes[scope] = entries
        else:
            state.scopes.pop(scope, None)
        if touched:
            _commit(state)
        return {"revision": state.revision, "changed": touched}


def export_scope(group_id: int | None, *, user_id: int | None = None) -> dict[str, Any]:
    return {
        "kind": "yawnbot.prompt-overrides",
        "schemaVersion": STORE_SCHEMA_VERSION,
        "scope": _scope_key(group_id, user_id),
        "exportedAt": now_beijing().isoformat(timespec="seconds"),
        "overrides": {
            key: value.as_dict()
            for key, value in sorted(scope_overrides(group_id, user_id=user_id).items())
        },
    }


# ── 解析与应用 ───────────────────────────────────────────────────────────

_DRAFT: contextvars.ContextVar[dict[str, dict[str, Override]] | None] = (
    contextvars.ContextVar("yawn_agent_prompt_draft", default=None)
)
# 当前私聊回合的用户：私聊分段按 ``user:<QQ>`` → 全局 解析。
_PRIVATE_USER: contextvars.ContextVar[int | None] = contextvars.ContextVar(
    "yawn_agent_private_prompt_user", default=None
)


# 当前私聊链路：turn / report / coordinate / worker / memory（抓取请求时标注用）。
_PRIVATE_PIPELINE: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "yawn_agent_private_prompt_pipeline", default=None
)


@contextlib.contextmanager
def use_private_user(
    user_id: int | None, *, pipeline: str | None = None
) -> Iterator[None]:
    """私聊回合（主回合、汇报、协调、子智能体、记忆整理）期间绑定用户与链路。"""

    token = _PRIVATE_USER.set(int(user_id) if user_id else None)
    pipe_token = _PRIVATE_PIPELINE.set(pipeline if user_id else None)
    try:
        yield
    finally:
        _PRIVATE_PIPELINE.reset(pipe_token)
        _PRIVATE_USER.reset(token)


def current_private_user() -> int | None:
    return _PRIVATE_USER.get()


def current_private_pipeline() -> str | None:
    return _PRIVATE_PIPELINE.get()


@contextlib.contextmanager
def use_draft_overrides(
    drafts: Mapping[str, Mapping[str, Any]] | None,
    *,
    group_id: int | None,
    user_id: int | None = None,
) -> Iterator[None]:
    """在当前上下文里叠加未保存的草稿（预览用），不落盘。

    ``drafts`` 为 ``{section_id: {"mode":..., "text":...}}``；``mode=inherit`` 表示
    预览“去掉这一层覆盖”的效果。
    """

    if not drafts:
        yield
        return
    with use_draft_scopes({_scope_key(group_id, user_id): drafts}):
        yield


def _draft_layer(drafts: Mapping[str, Mapping[str, Any]]) -> dict[str, Override]:
    layer: dict[str, Override] = {}
    for section_id, raw in drafts.items():
        if str(raw.get("mode") or "") in {"inherit", "default"}:
            layer[str(section_id)] = Override(mode="inherit")
            continue
        parsed = Override.from_raw(raw)
        if parsed is not None and get_section(str(section_id)) is not None:
            layer[str(section_id)] = parsed
    return layer


@contextlib.contextmanager
def use_draft_scopes(
    layers: Mapping[str, Mapping[str, Mapping[str, Any]]],
) -> Iterator[None]:
    """同时叠加多个作用域的草稿（私聊预览：全局草稿 + 本人草稿），``{scope: drafts}``。"""

    merged = {scope: _draft_layer(drafts) for scope, drafts in layers.items() if drafts}
    if not merged:
        yield
        return
    token = _DRAFT.set(merged)
    try:
        yield
    finally:
        _DRAFT.reset(token)


def _current_group_id() -> int | None:
    try:
        from .execution_trace import current_execution_trace

        trace = current_execution_trace()
        return int(trace.group_id) if trace is not None else None
    except Exception:  # noqa: BLE001
        return None


@dataclass(frozen=True, slots=True)
class ResolvedOverride:
    override: Override | None
    scope: str | None  # "draft" / "group" / "global" / None


def resolve_override(
    section_id: str, group_id: int | None = None, *, user_id: int | None = None
) -> ResolvedOverride:
    """本群（私聊分段：本人）草稿 → 覆盖 → 全局草稿 → 全局覆盖；``builtin``/``inherit`` 停止查找。"""

    drafts = _DRAFT.get() or {}
    with _lock:
        state = _load_locked()
    if is_private_section(section_id):
        uid = user_id if user_id is not None else _PRIVATE_USER.get()
        scopes = ([_scope_key(None, uid)] if uid else []) + [GLOBAL_SCOPE]
    else:
        gid = group_id if group_id is not None else _current_group_id()
        scopes = ([str(int(gid))] if gid is not None else []) + [GLOBAL_SCOPE]
    for scope in scopes:
        label = (
            "global"
            if scope == GLOBAL_SCOPE
            else "user"
            if is_user_scope(scope)
            else "group"
        )
        draft = drafts.get(scope, {}).get(section_id)
        if draft is not None:
            if draft.mode == "inherit":
                continue
            if draft.mode == MODE_BUILTIN:
                return ResolvedOverride(None, "draft")
            return ResolvedOverride(draft, "draft")
        stored = state.scopes.get(scope, {}).get(section_id)
        if stored is not None:
            if stored.mode == MODE_BUILTIN:
                return ResolvedOverride(None, label)
            return ResolvedOverride(stored, label)
    return ResolvedOverride(None, None)


def compose(base: str, override: Override | None) -> str:
    if override is None:
        return base
    if override.mode == MODE_REPLACE:
        return override.text
    if override.mode == MODE_DISABLE:
        return ""
    if override.mode == MODE_APPEND:
        return f"{base}\n{override.text}" if base else override.text
    if override.mode == MODE_PREPEND:
        return f"{override.text}\n{base}" if base else override.text
    return base


@dataclass(slots=True)
class _Sample:
    text: str
    chars: int
    seen: int
    last_seen: float
    last_group: int | None
    effective_chars: int


_SAMPLES: OrderedDict[str, _Sample] = OrderedDict()
_SAMPLE_LOCK = threading.Lock()


def _record_sample(
    section_id: str, generated: str, effective: str, group_id: int | None
) -> None:
    with _SAMPLE_LOCK:
        sample = _SAMPLES.get(section_id)
        seen = (sample.seen if sample is not None else 0) + 1
        _SAMPLES[section_id] = _Sample(
            text=generated[:MAX_SAMPLE_CHARS],
            chars=len(generated),
            seen=seen,
            last_seen=time.time(),
            last_group=group_id,
            effective_chars=len(effective),
        )
        _SAMPLES.move_to_end(section_id)
        while len(_SAMPLES) > MAX_SAMPLES:
            _SAMPLES.popitem(last=False)


def section_sample(section_id: str) -> dict[str, Any] | None:
    with _SAMPLE_LOCK:
        sample = _SAMPLES.get(section_id)
        if sample is None:
            return None
        return {
            "text": sample.text,
            "chars": sample.chars,
            "truncated": sample.chars > len(sample.text),
            "seen": sample.seen,
            "lastSeenAt": time.strftime(
                "%Y-%m-%dT%H:%M:%S", time.localtime(sample.last_seen)
            ),
            "lastGroupId": str(sample.last_group)
            if sample.last_group is not None
            else None,
            "effectiveChars": sample.effective_chars,
        }


def apply_section(
    section_id: str,
    generated: str,
    *,
    group_id: int | None = None,
    user_id: int | None = None,
) -> str:
    """返回分段的生效文本；任何异常都退回代码默认，绝不影响对话。"""

    try:
        gid = group_id if group_id is not None else _current_group_id()
        resolved = resolve_override(section_id, gid, user_id=user_id)
        effective = compose(generated, resolved.override)
        section = _SECTION_INDEX.get(section_id)
        # 数据块（群消息、成员资料）不进进程级样例缓存：它们只该出现在有界的 Trace 里。
        if section is None or section.kind != KIND_DATA:
            _record_sample(section_id, generated, effective, gid)
        return effective  # noqa: TRY300
    except Exception:  # noqa: BLE001
        return generated


def section_text(
    section_id: str,
    default: str,
    *,
    group_id: int | None = None,
    user_id: int | None = None,
) -> str:
    """静态分段的便捷入口：``default`` 就是代码常量。"""

    return apply_section(section_id, default, group_id=group_id, user_id=user_id)


def system_messages(
    section_id: str, default: str, *, group_id: int | None = None
) -> list[dict[str, Any]]:
    """单条 system 提示：被 WebUI 删除时返回空列表，不给服务商发空 system。"""

    text = section_text(section_id, default, group_id=group_id)
    return [{"role": "system", "content": text}] if text else []


# ── 片段拼装：生效文本 + 每段在消息里的位置 ─────────────────────────────


@dataclass(slots=True)
class Span:
    section_id: str
    start: int
    end: int

    def as_dict(self, message_index: int) -> dict[str, Any]:
        return {
            "message": message_index,
            "sectionId": self.section_id,
            "start": self.start,
            "end": self.end,
        }


@dataclass(slots=True)
class ContentBuilder:
    """把若干分段拼成一条消息，并记下每段落在哪个字符区间。"""

    parts: list[tuple[str | None, str]] = field(default_factory=list)

    def add(self, section_id: str | None, text: str) -> None:
        if text:
            self.parts.append((section_id, text))

    def build(self) -> tuple[str, list[Span]]:
        content = ""
        spans: list[Span] = []
        for section_id, text in self.parts:
            start = len(content)
            content += text
            if section_id:
                spans.append(Span(section_id, start, len(content)))
        return content, spans


def join_builders(builders: list[ContentBuilder], separator: str) -> ContentBuilder:
    """按行拼接若干子块，空块整块跳过（避免删掉一段后留下空行）。"""

    joined = ContentBuilder()
    first = True
    for builder in builders:
        if not builder.parts:
            continue
        if not first:
            joined.add(None, separator)
        joined.parts.extend(builder.parts)
        first = False
    return joined


_MANIFEST: contextvars.ContextVar[dict[str, Any] | None] = contextvars.ContextVar(
    "yawn_agent_prompt_manifest", default=None
)


def remember_manifest(
    messages: list[dict[str, Any]], spans: list[dict[str, Any]]
) -> None:
    """记住最近一次构建的分段位置；捕获请求时按首条消息内容配对。"""

    try:
        first = messages[0].get("content") if messages else ""
        digest = hashlib.sha256(str(first).encode("utf-8")).hexdigest()[:16]
        _MANIFEST.set(
            {"anchor": digest, "count": len(messages), "spans": copy.deepcopy(spans)}
        )
    except Exception:  # noqa: BLE001
        return


def manifest_for(messages: list[Any]) -> list[dict[str, Any]] | None:
    manifest = _MANIFEST.get()
    if manifest is None or not messages:
        return None
    first = messages[0].get("content") if isinstance(messages[0], Mapping) else ""
    digest = hashlib.sha256(str(first).encode("utf-8")).hexdigest()[:16]
    if digest != manifest.get("anchor"):
        return None
    count = int(manifest.get("count") or 0)
    return [
        span
        for span in list(manifest.get("spans") or [])
        if int(span.get("message", -1)) < count
    ]


def locate_sections(  # noqa: C901
    messages: list[Any], *, known: list[dict[str, Any]] | None = None
) -> list[dict[str, Any]]:
    """没有构建清单的消息（路由、识图、工具循环追加的 system）按生效文本定位分段。"""

    spans = list(known or [])
    covered = {int(span["message"]) for span in spans}
    candidates: list[tuple[str, str]] = []
    for section in SECTIONS:
        if section.kind in {KIND_DATA, KIND_GENERATED}:
            continue
        default = section.default_text()
        if not default:
            continue
        effective = apply_section_preview(section.id, default)
        if len(effective) >= 8:  # noqa: PLR2004
            candidates.append((section.id, effective))
    for index, message in enumerate(messages):
        if index in covered or not isinstance(message, Mapping):
            continue
        content = message.get("content")
        if isinstance(content, list):
            text = "".join(
                str(part.get("text") or "")
                for part in content
                if isinstance(part, Mapping) and part.get("type") == "text"
            )
        else:
            text = str(content or "")
        if not text:
            continue
        for section_id, effective in candidates:
            start = text.find(effective)
            if start >= 0:
                spans.append(
                    {
                        "message": index,
                        "sectionId": section_id,
                        "start": start,
                        "end": start + len(effective),
                    }
                )
    spans.sort(key=lambda span: (int(span["message"]), int(span["start"])))
    return spans


def apply_section_preview(
    section_id: str,
    default: str,
    *,
    group_id: int | None = None,
    user_id: int | None = None,
) -> str:
    """和 :func:`apply_section` 同口径，但不记样例（只读查询用）。"""

    try:
        return compose(
            default, resolve_override(section_id, group_id, user_id=user_id).override
        )
    except Exception:  # noqa: BLE001
        return default


# ── WebUI 目录 ───────────────────────────────────────────────────────────


def _override_payload(override: Override | None) -> dict[str, Any] | None:
    return override.as_dict() if override is not None else None


def catalog(group_id: int | None) -> dict[str, Any]:
    """返回全部分段：默认值、全局/本群覆盖、生效文本、最近样例与 Token 估算。"""

    with _lock:
        state = _load_locked()
        global_entries = dict(state.scopes.get(GLOBAL_SCOPE, {}))
        group_entries = (
            dict(state.scopes.get(str(int(group_id)), {}))
            if group_id is not None
            else {}
        )
        revision = state.revision
        load_error = state.load_error
        updated_at = state.updated_at
    rows: list[dict[str, Any]] = []
    # 私聊分段在「私聊 Agent」页按全局/本人管理，群聊工作台不再列出。
    private_count = sum(
        1 for section in all_sections() if is_private_section(section.id)
    )
    for section in all_sections(include_private=False):
        default = section_default(section)
        sample = section_sample(section.id)
        base = default if default is not None else (sample or {}).get("text") or ""
        resolved = resolve_override(section.id, group_id)
        effective = compose(base, resolved.override)
        rows.append(
            {
                "id": section.id,
                "title": section.title,
                "category": section.category,
                "categoryLabel": CATEGORY_LABELS.get(
                    section.category, section.category
                ),
                "layer": section.layer,
                "layerLabel": LAYER_LABELS.get(section.layer, section.layer),
                "kind": section.kind,
                "description": section.description,
                "when": section.when,
                "safety": section.safety,
                "protocol": section.protocol,
                "editable": section.editable,
                "modes": list(section.modes),
                "tasks": list(section.tasks),
                "defaultText": default,
                "defaultTokens": estimate_tokens(default) if default else 0,
                "sample": sample,
                "globalOverride": _override_payload(global_entries.get(section.id)),
                "groupOverride": _override_payload(group_entries.get(section.id)),
                "effectiveScope": resolved.scope,
                "effectiveMode": resolved.override.mode
                if resolved.override is not None
                else "default",
                "effectiveText": effective if (default is not None or sample) else None,
                "effectiveTokens": estimate_tokens(effective) if effective else 0,
            }
        )
    return {
        "revision": revision,
        "updatedAt": updated_at or None,
        "loadError": load_error,
        "storeFile": store_path().name,
        "groupId": str(group_id) if group_id is not None else None,
        "categories": [
            {"key": key, "label": label, "hint": hint}
            for key, label, hint in CATEGORIES
            if key not in {"private", "private_tools"}
        ],
        "privateSections": private_count,
        "sections": rows,
        "summary": {
            "total": len(rows),
            "globalOverrides": sum(
                1 for key in global_entries if not is_private_section(key)
            ),
            "groupOverrides": len(group_entries),
            "disabled": sum(1 for row in rows if row["effectiveMode"] == MODE_DISABLE),
            "safetyDisabled": sum(
                1
                for row in rows
                if row["safety"]
                and row["effectiveMode"] in {MODE_DISABLE, MODE_REPLACE}
            ),
        },
    }


__all__ = [
    "CATEGORIES",
    "GLOBAL_SCOPE",
    "MODE_APPEND",
    "MODE_BUILTIN",
    "MODE_DISABLE",
    "MODE_PREPEND",
    "MODE_REPLACE",
    "OVERRIDE_MODES",
    "PRIVATE_TOOL_SECTION_PREFIX",
    "SECTIONS",
    "USER_SCOPE_PREFIX",
    "ContentBuilder",
    "Override",
    "PromptSection",
    "PromptStoreConflictError",
    "PromptStoreValidationError",
    "apply_section",
    "bulk_apply",
    "catalog",
    "clear_override",
    "current_private_pipeline",
    "current_private_user",
    "export_scope",
    "get_section",
    "is_private_section",
    "join_builders",
    "locate_sections",
    "manifest_for",
    "remember_manifest",
    "resolve_override",
    "scope_user_id",
    "section_history",
    "section_text",
    "set_override",
    "set_store_path",
    "store_revision",
    "store_snapshot",
    "system_messages",
    "use_draft_overrides",
    "use_draft_scopes",
    "use_private_user",
    "user_scope_key",
    "user_scopes",
]
