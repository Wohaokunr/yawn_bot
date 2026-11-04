# ruff: noqa: E501, C901, PLR0911, PLR0912, TID252, TC003, TRY003, RUF001, PERF203, TRY300
"""私聊 Agent 的工具：全部只作用于**当前私聊的这位用户本人**。

没有任何工具接受 user_id；群相关的工具只能选用户本人活跃所在的群，读 Agent 数据
（摘要、画像、关系）还要求该群开着 Agent、用户没有隐私退出。参数一律先过
``tool_args.validate_tool_arguments``，失败统一返回 ``tool_error_payload``。

签到、好感、记忆都调用已有服务（``checkin_service`` / ``affinity`` /
``memory_self`` / ``private_memory``），这里不另写业务逻辑。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field, replace
from datetime import datetime
from random import randint
from typing import Any

from sqlalchemy import select

from .. import affinity
from ..checkin_service import (
    MAX_BATCH_GROUPS,
    POINTS_MAX,
    POINTS_MIN,
    checkin_overview,
    perform_checkin,
)
from ..command_catalog import get_registered_command_groups
from ..data_models.agent_memory import AgentMemory
from ..permission import check_feature_permission
from ..superuser import superuser_full_access
from . import private_memory
from .memory_hygiene import contains_pii, looks_like_instruction, scrub_pii
from .memory_self import SELF_FACT_KEYS, forget_member_facts, remember_member_fact
from .private_computer import COMPUTER_TOOL_SPECS
from .private_context import SharedGroup, group_bonds, group_profile_facts
from .private_offers import TOOL_DESCRIPTION as _OFFERS_DESCRIPTION
from .private_offers import TOOL_PROPERTIES as _OFFERS_PROPERTIES
from .private_schedule_tool import TOOL_DESCRIPTION as _SCHEDULES_DESCRIPTION
from .private_schedule_tool import TOOL_PROPERTIES as _SCHEDULES_PROPERTIES
from .prompt_sections import PRIVATE_TOOL_SECTION_PREFIX, apply_section
from .tool_args import (
    ERROR_INVALID_ARGS,
    ERROR_NOT_FOUND,
    ERROR_PERMISSION,
    ERROR_QUOTA,
    ToolError,
    tool_error_payload,
    validate_tool_arguments,
)

_FACT_KEY_SCHEMA = {
    "type": "string",
    "enum": list(SELF_FACT_KEYS),
    "description": "display_name昵称/preferred_address希望的称呼/hobby爱好/preference喜好/dislike讨厌的/skill擅长/current_status近期状态",
}
_GROUP_SCHEMA = {
    "type": "string",
    "minLength": 1,
    "maxLength": 60,
    "description": "群名（可以只写一部分）或群号；省略表示全部",
}
_SUMMARY_DIGITS_RE = re.compile(r"(?<!\d)\d{5,12}(?!\d)")
_MAX_SUMMARIES = 3
_SUMMARY_CHARS = 400
_MAX_COMMANDS = 30


@dataclass(frozen=True, slots=True)
class PrivateTool:
    name: str
    description: str
    properties: dict[str, Any] = field(default_factory=dict)
    required: tuple[str, ...] = ()
    writes: bool = False

    def schema(self) -> dict[str, Any]:
        # 描述登记为 ptool.<名字>：WebUI 私聊提示词页可按全局/本人改。
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": apply_section(
                    f"{PRIVATE_TOOL_SECTION_PREFIX}{self.name}", self.description
                ),
                "parameters": {
                    "type": "object",
                    "properties": self.properties,
                    "required": list(self.required),
                    "additionalProperties": False,
                },
            },
        }


STATUS_VIEWS = (
    "overview",
    "checkin",
    "board",
    "relationship",
    "affinity_progress",
    "affinity_sources",
    "affinity_history",
    "groups",
)
PRIVATE_TOOLS: tuple[PrivateTool, ...] = (
    PrivateTool(
        "my_status",
        "查他在你这里的各种状态（只读，以这里为准，别凭印象说）：overview 概况（关系等级、几个群、签到合计、认识多久）；"
        "checkin 各群今天签没签、连签、积分；board 某个群今天的签到顺序和他排第几（要写 group）；"
        "relationship 你们的关系（整体等级、各群熟悉程度、他怎么叫你、你们的梗）；"
        "affinity_progress 离下一级还差多少、affinity_sources 最近 30 天好感主要来自哪、affinity_history 最近的变化（只说等级和方向，不报数值）；"
        "groups 你们共同在的群和群里的 Agent 开没开",
        {
            "view": {"type": "string", "enum": list(STATUS_VIEWS)},
            "group": _GROUP_SCHEMA,
            "limit": {"type": "integer", "minimum": 1, "maximum": 30},
        },
        ("view",),
    ),
    PrivateTool(
        "checkin",
        "替他签到（等同他在群里发 /签到）。不写 group 就在他所在的所有群里签；今天签过的返回 already",
        {"group": _GROUP_SCHEMA},
        writes=True,
    ),
    PrivateTool(
        "remember_about_me",
        "他明确让你记住关于他自己的长期特征时用（称呼、爱好、讨厌什么、近况……；一次性的事记进 notes）。"
        "also_in_groups 只有他说“群里也这样”时才为 true",
        {
            "key": _FACT_KEY_SCHEMA,
            "content": {"type": "string", "minLength": 1, "maxLength": 100},
            "also_in_groups": {"type": "boolean"},
        },
        ("key", "content"),
        writes=True,
    ),
    PrivateTool(
        "forget_about_me",
        "他让你忘掉关于他的事时用：按 key 类别或 keyword 只删一部分，都不写就是全忘；include_groups=true 连各群里的画像一起删",
        {
            "key": _FACT_KEY_SCHEMA,
            "keyword": {"type": "string", "minLength": 1, "maxLength": 40},
            "include_groups": {"type": "boolean"},
        },
        writes=True,
    ),
    PrivateTool(
        "group_recent",
        "某个他在的群最近几天聊了什么（你整理的群聊日摘要）",
        {"group": {**_GROUP_SCHEMA, "description": "群名（可以只写一部分）或群号"}},
        ("group",),
    ),
    PrivateTool(
        "recall",
        "回想：他问“你还记得…吗”“群里那天聊的…”“我上次跟你说的…”时先查再答，查不到就直说不记得。"
        "写 query 按关键词查群里的事（摘要、关于他的画像）、聊天记录窗口外的旧私聊、你记住的他的事；"
        "不写 query 就列出你记得他的全部（私聊里他告诉你的 + 群里了解到的）",
        {
            "query": {
                "type": "string",
                "minLength": 1,
                "maxLength": 40,
                "description": "关键词，如 考研、上周的聚餐",
            },
            "where": {
                "type": "string",
                "enum": ["all", "groups", "private_chat"],
                "description": "默认 all",
            },
        },
    ),
    PrivateTool(
        "memory_link",
        "群里的你和私聊里的你怎么共用记忆，由他本人定：view 看设置和最近的流动记录（他问“你从哪知道的”“群里知道我什么”）；"
        "share/unshare 让群里的你也知道/不再知道某几类他私聊说的事（keys，他明确同意才 share，group 可限定群）；"
        "mute_group/unmute_group 某个群的事别带进/可以带进私聊；group_talk on=false 别在私聊里提群里的事。"
        "私聊里聊的其他内容永远不会带到群里",
        {
            "action": {
                "type": "string",
                "enum": ["view", "share", "unshare", "mute_group", "unmute_group", "group_talk"],
            },
            "keys": {"type": "array", "items": _FACT_KEY_SCHEMA, "minItems": 1, "maxItems": 7},
            "group": {**_GROUP_SCHEMA, "description": "群名（可以只写一部分）或群号"},
            "on": {"type": "boolean"},
        },
        ("action",),
        writes=True,
    ),
    PrivateTool(
        "list_commands",
        "他问有什么命令、某个功能怎么用时查：现在私聊/群里能用的命令和说明，query 按关键词筛",
        {
            "query": {"type": "string", "minLength": 1, "maxLength": 20},
            "scene": {"type": "string", "enum": ["private", "group", "all"]},
        },
    ),
)
_ID = {"type": "integer", "minimum": 1, "maximum": 1000000, "description": "条目编号（list 返回的 id）"}
_LIMIT = {"type": "integer", "minimum": 1, "maximum": 30}
_CLOCK = {"type": "string", "maxLength": 5, "description": "HH:MM（北京时间），传 off 关闭"}

# 个人助理与扩展工具（private_organizer / private_services）。
EXTRA_TOOLS: tuple[PrivateTool, ...] = (
    PrivateTool(
        "notes",
        "他的私人笔记本：他说“帮我记一下/存一下”而且不是关于他本人的长期特征时用。"
        "add 记一条（可带 tag）、list 列出（置顶在前）、search 按 keyword 找、delete 删、pin/unpin 置顶",
        {
            "action": {"type": "string", "enum": ["add", "list", "search", "delete", "pin", "unpin"]},
            "content": {"type": "string", "minLength": 1, "maxLength": 300},
            "tag": {"type": "string", "minLength": 1, "maxLength": 12, "description": "如 书单/灵感"},
            "keyword": {"type": "string", "minLength": 1, "maxLength": 30},
            "id": _ID,
            "limit": _LIMIT,
        },
        ("action",),
        writes=True,
    ),
    PrivateTool(
        "todos",
        "他的待办清单：add 加一项（due 截止日期、priority）、list 列出、done 完成、undo 撤销完成、delete 删除、clear_done 清掉已完成的",
        {
            "action": {"type": "string", "enum": ["add", "list", "done", "undo", "delete", "clear_done"]},
            "content": {"type": "string", "minLength": 1, "maxLength": 120},
            "due": {"type": "string", "maxLength": 10, "description": "YYYY-MM-DD"},
            "priority": {"type": "string", "enum": ["high", "normal", "low"]},
            "include_done": {"type": "boolean"},
            "id": _ID,
            "limit": _LIMIT,
        },
        ("action",),
        writes=True,
    ),
    PrivateTool(
        "reminders",
        "到点私聊提醒他（简单的一次性/每天/工作日/每周）：add 设提醒（at 或 in_minutes 二选一，时间按资料里的 now 推算，说不清就先问）、"
        "list、cancel、snooze 推迟这一次、pause/resume、edit 改内容或时间。mode=agent 是到点由你去办（如“每天 8 点帮我查天气”）。"
        "每月/每年/间隔/cron、给群设、查群里的定时任务用 schedules",
        {
            "action": {"type": "string", "enum": ["add", "list", "cancel", "snooze", "pause", "resume", "edit"]},
            "content": {"type": "string", "minLength": 1, "maxLength": 300, "description": "提醒他的话；mode=agent 时是到点要办的事"},
            "mode": {"type": "string", "enum": ["notify", "agent"]},
            "at": {"type": "string", "maxLength": 19, "description": "YYYY-MM-DD HH:MM（北京时间）"},
            "in_minutes": {"type": "integer", "minimum": 1, "maximum": 527040},
            "repeat": {"type": "string", "enum": ["none", "daily", "weekdays", "weekly"]},
            "snooze_minutes": {"type": "integer", "minimum": 1, "maximum": 1440},
            "id": _ID,
        },
        ("action",),
        writes=True,
    ),
    PrivateTool(
        "countdowns",
        "倒数日/纪念日：add 加一个（title + date，notify 当天要不要私聊提醒）、list 看还有几天、delete 删",
        {
            "action": {"type": "string", "enum": ["add", "list", "delete"]},
            "title": {"type": "string", "minLength": 1, "maxLength": 40},
            "date": {"type": "string", "maxLength": 10, "description": "YYYY-MM-DD"},
            "notify": {"type": "boolean"},
            "id": _ID,
        },
        ("action",),
        writes=True,
    ),
    PrivateTool(
        "mood",
        "心情记录（他愿意记时才记）：log 记下此刻心情（score 1很差~5很好，note 一句话）；history 看最近几天的心情和趋势",
        {
            "action": {"type": "string", "enum": ["log", "history"]},
            "score": {"type": "integer", "minimum": 1, "maximum": 5},
            "note": {"type": "string", "minLength": 1, "maxLength": 60},
            "days": {"type": "integer", "minimum": 1, "maximum": 90},
        },
        ("action",),
        writes=True,
    ),
    PrivateTool(
        "chat_prefs",
        "他对私聊的偏好：get 查看；set 只改他明确提出的项（回复长短、表情多少、怎么称呼他、免打扰时段、早安问候和签到提醒时间、生日、要不要你主动发消息）",
        {
            "action": {"type": "string", "enum": ["get", "set"]},
            "reply_length": {"type": "string", "enum": ["short", "normal", "long"]},
            "emoji": {"type": "string", "enum": ["none", "some", "lots"]},
            "call_me": {"type": "string", "maxLength": 16, "description": "off 清除"},
            "quiet_start": _CLOCK,
            "quiet_end": _CLOCK,
            "morning_greeting": _CLOCK,
            "checkin_nudge": {**_CLOCK, "description": "几点还没签到就提醒他；off 关闭"},
            "birthday": {"type": "string", "maxLength": 5, "description": "MM-DD，off 清除"},
            "proactive": {"type": "boolean", "description": "false：除了他自己设的提醒，你都不主动发消息"},
        },
        ("action",),
        writes=True,
    ),
    PrivateTool(
        "toolbox",
        "（虚拟电脑用不了时的备用）小工具：calc 算式、convert 单位换算、date_diff 日期差、date_add 日期加减、dice 骰子、coin 硬币、pick 随机选一个、fortune 今日运势、world_time 某城市几点",
        {
            "op": {"type": "string", "enum": ["calc", "convert", "date_diff", "date_add", "dice", "coin", "pick", "fortune", "world_time"]},
            "expression": {"type": "string", "maxLength": 200},
            "value": {"type": "number"},
            "from_unit": {"type": "string", "maxLength": 10},
            "to_unit": {"type": "string", "maxLength": 10},
            "start": {"type": "string", "maxLength": 10, "description": "YYYY-MM-DD 或 今天/明天/昨天"},
            "end": {"type": "string", "maxLength": 10},
            "days": {"type": "integer", "minimum": -73000, "maximum": 73000},
            "sides": {"type": "integer", "minimum": 2, "maximum": 1000},
            "count": {"type": "integer", "minimum": 1, "maximum": 20},
            "options": {"type": "array", "minItems": 2, "maxItems": 30, "items": {"type": "string"}},
            "place": {"type": "string", "maxLength": 40},
        },
        ("op",),
    ),
    PrivateTool(
        "my_data",
        "他在你这里的数据：summary 存了什么（各类条数）、chat 你们聊了多久多少条、quota 今天还能聊几轮/搜几次/派几件事、"
        "wipe 清空他的全部个人数据和私聊记忆（先告诉他会删掉什么，他明确确认后才带 confirm=true）",
        {"action": {"type": "string", "enum": ["summary", "chat", "quota", "wipe"]}, "confirm": {"type": "boolean"}},
        writes=True,
    ),
    PrivateTool(
        "feedback",
        "他明确想给机器人提建议、报 bug 或投诉时，转给管理员（每天最多 3 条）",
        {
            "content": {"type": "string", "minLength": 1, "maxLength": 300},
            "kind": {"type": "string", "enum": ["bug", "suggestion", "complaint", "other"]},
        },
        ("content",),
        writes=True,
    ),
    PrivateTool(
        "web_search",
        "联网搜索新闻、实时信息、你拿不准的事实。返回标题、摘要和编号 w1、w2…；要细节再用 web_fetch 打开",
        {"query": {"type": "string", "minLength": 1, "maxLength": 100, "description": "几个关键词"}},
        ("query",),
    ),
    PrivateTool(
        "web_fetch",
        "打开搜索结果（编号如 w1）或网址读正文；正文长时用 offset 接着读",
        {
            "target": {"type": "string", "minLength": 1, "maxLength": 500},
            "offset": {"type": "integer", "minimum": 0, "maximum": 200000},
        },
        ("target",),
    ),
    PrivateTool(
        "bot_status",
        "（仅超级管理员）机器人运行状态：运行时长、今天私聊人数和回复数、后台任务、反馈条数",
    ),
)
# 虚拟电脑（private_computer）：schema 在那边定义，这里包成 PrivateTool。
COMPUTER_TOOLS: tuple[PrivateTool, ...] = tuple(
    PrivateTool(
        spec["name"],
        spec["description"],
        spec["properties"],
        tuple(spec.get("required", ())),
        writes=bool(spec.get("writes")),
    )
    for spec in COMPUTER_TOOL_SPECS
)
# 主从多智能体（private_tasks）：派活给后台小助手、查看/取消/转交回答。
TASK_TOOLS: tuple[PrivateTool, ...] = (
    PrivateTool(
        "delegate_task",
        "把费时间的活放到后台做，你马上回他一句：要联网查好几处再整理、写代码处理数据或画图、处理他发来的文件、做表格/PPT/文档、写长文，"
        "估计要三次以上工具或半分钟以上的都放这里。只写 goal 就行（最近的聊天、本轮附件、他的称呼会自动带上），brief 只补聊天里没有的要求。"
        "做的时候会用你的口吻报进度，做完你收到结果再用自己的话告诉他；对他来说都是你在做",
        {
            "goal": {"type": "string", "minLength": 1, "maxLength": 600, "description": "要交付什么、做到什么程度"},
            "brief": {"type": "string", "maxLength": 1500, "description": "聊天里没有的要求、格式、已知信息"},
            "title": {"type": "string", "minLength": 1, "maxLength": 20, "description": "短标题；不写就从 goal 截"},
            "tools": {
                "type": "array",
                "minItems": 1,
                "maxItems": 2,
                "items": {"type": "string", "enum": ["web", "computer"]},
                "description": "不写就自动选",
            },
            "save_to_notes": {"type": "boolean", "description": "他说要存下来时为 true"},
            "follow_up_of": {
                "type": "string",
                "maxLength": 12,
                "description": "接着之前做完的哪件事（编号如 t3），他说“展开第二点”“换成表格”时用",
            },
        },
        ("goal",),
        writes=True,
    ),
    PrivateTool(
        "delegate_project",
        "一个人做不完的大活：拆成 2~6 个成员分头做，成员能互相发消息、共用他的虚拟电脑；要用别人结果的成员写 depends_on 等对方做完"
        "（如 调研A、调研B 并行，汇总 depends_on 两者）。全部做完你收到合并结果再转述",
        {
            "title": {"type": "string", "minLength": 1, "maxLength": 20},
            "goal": {"type": "string", "minLength": 1, "maxLength": 600},
            "brief": {"type": "string", "maxLength": 1500, "description": "所有成员都需要的背景"},
            "members": {
                "type": "array",
                "minItems": 2,
                "maxItems": 6,
                "items": {
                    "type": "object",
                    "properties": {
                        "name": {"type": "string", "maxLength": 12, "description": "如 调研员/写手"},
                        "goal": {"type": "string", "maxLength": 600},
                        "tools": {"type": "array", "items": {"type": "string", "enum": ["web", "computer"]}},
                        "depends_on": {"type": "array", "items": {"type": "string"}, "description": "要等的成员 name"},
                    },
                    "required": ["name", "goal", "tools"],
                },
            },
            "save_to_notes": {"type": "boolean"},
        },
        ("title", "goal", "members"),
        writes=True,
    ),
    PrivateTool(
        "tasks",
        "你在后台做的事：list 看在做/做完的和进度；result 看某件的完整结果（他追问细节时）；cancel 他说不要了；"
        "answer 把他的回答转给在等回答的那件；message 给在做的那件补充要求、改方向、回答 helper_messages（id 写编号/成员名，all 发给所有在做的）",
        {
            "action": {"type": "string", "enum": ["list", "result", "cancel", "answer", "message"]},
            "id": {"type": "string", "maxLength": 12, "description": "编号如 t12、成员名或 all"},
            "answer": {"type": "string", "minLength": 1, "maxLength": 500},
            "text": {"type": "string", "minLength": 1, "maxLength": 600, "description": "message 的内容"},
        },
        ("action",),
        writes=True,
    ),
)
_LEVEL = {"type": "integer", "minimum": 0, "maximum": 4}
_PERSONA_FIELDS_SCHEMA = {
    "type": "object",
    "description": "只写要改的字段。name 名字/self_name 自称/user_title 怎么叫他/identity 一句话身份/personality 性格/speaking_style 说话方式；"
    "catchphrases 口癖、likes 喜欢的话题、avoid_topics 避开的话题、examples 示例台词 传数组（传 off 清空）；"
    "warmth 温度/humor 幽默/verbosity 详略/expressiveness 表现力/sociability 活跃度/reaction_tendency 接梗 是 0–4 档",
    "properties": {
        "name": {"type": "string", "maxLength": 32},
        "self_name": {"type": "string", "maxLength": 16},
        "user_title": {"type": "string", "maxLength": 16},
        "identity": {"type": "string", "maxLength": 160},
        "personality": {"type": "string", "maxLength": 400},
        "speaking_style": {"type": "string", "maxLength": 400},
        "catchphrases": {"type": "array", "items": {"type": "string"}, "maxItems": 6},
        "likes": {"type": "array", "items": {"type": "string"}, "maxItems": 8},
        "avoid_topics": {"type": "array", "items": {"type": "string"}, "maxItems": 8},
        "examples": {"type": "array", "items": {"type": "string"}, "maxItems": 4},
        "warmth": _LEVEL,
        "humor": _LEVEL,
        "verbosity": _LEVEL,
        "expressiveness": _LEVEL,
        "sociability": _LEVEL,
        "reaction_tendency": _LEVEL,
    },
    "additionalProperties": False,
}
# 私聊每用户人设（private_persona）：他可以在私聊里让你换个样子，只影响你们的私聊。
PERSONA_TOOLS: tuple[PrivateTool, ...] = (
    PrivateTool(
        "my_persona",
        "你在和他私聊时的人设（群里不变）。只有他明确要你换自称、称呼、说话方式、性格时才改，改完下一句就按新人设说话。"
        "view 现在的人设、来源和今天还能改几次；options 能套的模板；set 改（fields 只写要改的）；preset 套内置模板、template 用人设库的（name 写名字）；"
        "reset 回默认；undo 撤销上一次；history 改过什么",
        {
            "action": {
                "type": "string",
                "enum": ["view", "options", "set", "preset", "template", "reset", "undo", "history"],
            },
            "fields": _PERSONA_FIELDS_SCHEMA,
            "name": {"type": "string", "minLength": 1, "maxLength": 32, "description": "preset/template 的名字"},
        },
        ("action",),
        writes=True,
    ),
)
# 网页个人账号：他想登录 WebUI（管理自己的群、调私聊人设）时用。故意没有“密码”参数，
# 密码只能他自己发 /网页密码，或者在网页上用设置码设，绝不经过你。
ACCOUNT_TOOLS: tuple[PrivateTool, ...] = (
    PrivateTool(
        "webui_account",
        "他的网页（WebUI）个人账号，登录后能管自己当群主/管理员的群、调你们私聊的人设。"
        "guide 新人引导：进行到哪一步、登录网址（login_url 原样发，别自己编）、下一步做什么；view 开没开通、能管哪些群；"
        "ask_password 帮他设自己想要的密码：系统直接私聊提示他把密码发过来，下一条由系统收走加密保存，你看不到（只需简短说照提示发）；"
        "cancel 他不想设了时关掉收密码的窗口；setup_code 把一次性设置码直接私发给他，去登录页用设置码设密码；logout_all 所有设备退出。"
        "绝不让他把密码发在普通聊天里；他已经发了就提醒他撤回并换一个",
        {
            "action": {
                "type": "string",
                "enum": ["guide", "view", "ask_password", "cancel", "setup_code", "logout_all"],
            }
        },
        ("action",),
        writes=True,
    ),
)
# 群管代办（group_manage）：只给数据库里当群主/管理员的人和超级管理员；每次执行都实时确认他和 Bot 的身份。
MANAGE_ACTIONS = (
    "groups", "members", "muted", "notices", "audit",
    "mute", "unmute", "whole_mute", "announce", "delete_notice", "send", "todo",
    "todo_done", "todo_cancel", "essence", "unessence", "recall", "card", "title",
    "kick", "rename", "confirm", "cancel",
)
MANAGE_TOOLS: tuple[PrivateTool, ...] = (
    PrivateTool(
        "group_manage",
        "替他管理他当群主/管理员、你也是管理员的群（每次实时核对两边身份；超级管理员等同群主，但你不是管理员的群仍做不了）。只在他开口要时用。"
        "查：groups 能管哪些群和能做什么、members 找成员（query）、muted 谁在禁言、notices 群公告、audit 最近操作记录。"
        "做：mute（member+minutes）/unmute、whole_mute（enable）、announce 发公告（content，可 pinned、confirm_required）、delete_notice（notice_id）、"
        "send 以你的身份发一句（text）、todo 发一条并设为群待办、todo_done/todo_cancel（message_id）、essence/unessence/recall（只限你代发的 message_id）、"
        "card 改群名片（member+text，空=清空）、title 专属头衔、kick（reject 拒绝再加群）、rename 改群名。"
        "返回 needs_confirm 时把 summary 原样问他，这一轮就结束；他下一条同意再 confirm（confirm_id），不同意就 cancel。"
        "本轮资料里的 group_manage_waiting 是还在等他确认的：他同意就直接 confirm 那个 confirm_id，别重新发起。绝不替他确认；管理员不能处置群主和其他管理员",
        {
            "action": {"type": "string", "enum": list(MANAGE_ACTIONS)},
            "group": {"type": "string", "minLength": 1, "maxLength": 60, "description": "群名（可以只写一部分）或群号"},
            "member": {"type": "string", "minLength": 1, "maxLength": 40, "description": "成员群名片/昵称/QQ 号"},
            "query": {"type": "string", "maxLength": 40},
            "minutes": {"type": "integer", "minimum": 1, "maximum": 43200, "description": "禁言分钟数"},
            "enable": {"type": "boolean"},
            "content": {"type": "string", "minLength": 1, "maxLength": 600, "description": "公告正文"},
            "text": {"type": "string", "maxLength": 500, "description": "要发的消息/待办/群名片/头衔/新群名"},
            "pinned": {"type": "boolean"},
            "confirm_required": {"type": "boolean"},
            "reject": {"type": "boolean"},
            "notice_id": {"type": "string", "maxLength": 128},
            "message_id": {"type": "string", "maxLength": 24},
            "confirm_id": {"type": "string", "maxLength": 12},
            "reason": {"type": "string", "maxLength": 80, "description": "他说的理由（记进操作记录）"},
        },
        ("action",),
        writes=True,
    ),
)
MANAGE_NAMES = frozenset(tool.name for tool in MANAGE_TOOLS)
# 群聊 ↔ 私聊协作（agent_bridge）：私聊这边替他在群里转告/提问、看从群里转来的事。
BRIDGE_ACTIONS = ("post", "ask", "replies", "close", "handoffs", "done", "confirm", "cancel")
BRIDGE_TOOLS: tuple[PrivateTool, ...] = (
    PrivateTool(
        "group_bridge",
        "他明确要你去群里说/问时用（群里的你和私聊的你是同一个人）：post 替他在群里转告一句（会带“〔他的名字 让我转告〕”）、"
        "ask 替他在群里问大家（群友引用回复会攒一会儿私聊转给他）、replies 看提问收到的回复、close 不再收回复（id）、"
        "handoffs 看他从群里转到私聊的事、done 标记那件事办完了（id）。post/ask 先返回 needs_confirm：把群名和原话问他，这一轮就结束；"
        "他下一条同意再 confirm（confirm_id），不同意就 cancel。不替他编内容，不把私聊里的其他内容带到群里。"
        "本轮资料里 from_groups 是他刚从群里转来的事，asks_in_groups 是还在收回复的提问，group_relay_waiting 是等他确认的转告",
        {
            "action": {"type": "string", "enum": list(BRIDGE_ACTIONS)},
            "group": {"type": "string", "minLength": 1, "maxLength": 60, "description": "群名（可以只写一部分）或群号；他只在一个群时可省略"},
            "text": {"type": "string", "minLength": 1, "maxLength": 300, "description": "要在群里转告/问的话（他的原意）"},
            "id": {"type": "string", "maxLength": 12, "description": "提问编号（r…）或转来的事的编号（h…）"},
            "confirm_id": {"type": "string", "maxLength": 12},
        },
        ("action",),
        writes=True,
    ),
)
BRIDGE_NAMES = frozenset(tool.name for tool in BRIDGE_TOOLS)
# 私聊提醒 ↔ 群定时任务一起管（schedule_hub）：编号 p…/g…，给群设要他确认。
SCHEDULE_TOOLS: tuple[PrivateTool, ...] = (
    PrivateTool("schedules", _SCHEDULES_DESCRIPTION, _SCHEDULES_PROPERTIES, ("action",), writes=True),
)
SCHEDULE_NAMES = frozenset(tool.name for tool in SCHEDULE_TOOLS)
# 主动提议（private_offers）：他自己开关、调间隔、静音某类事，记下他对上次提议的态度。
OFFER_TOOLS: tuple[PrivateTool, ...] = (
    PrivateTool("offers", _OFFERS_DESCRIPTION, _OFFERS_PROPERTIES, ("action",), writes=True),
)
OFFER_NAMES = frozenset(tool.name for tool in OFFER_TOOLS)
# 只给超级管理员的工具：普通用户的 schema 里看不到。
SUPERUSER_TOOLS = frozenset({"bot_status"})
ALL_TOOLS: tuple[PrivateTool, ...] = (
    PRIVATE_TOOLS
    + EXTRA_TOOLS
    + PERSONA_TOOLS
    + ACCOUNT_TOOLS
    + MANAGE_TOOLS
    + BRIDGE_TOOLS
    + SCHEDULE_TOOLS
    + OFFER_TOOLS
    + COMPUTER_TOOLS
    + TASK_TOOLS
)
COMPUTER_NAMES = frozenset(tool.name for tool in COMPUTER_TOOLS)
TASK_NAMES = frozenset(tool.name for tool in TASK_TOOLS)
TOOLS_BY_NAME: dict[str, PrivateTool] = {tool.name: tool for tool in ALL_TOOLS}
WRITE_TOOLS = frozenset(tool.name for tool in ALL_TOOLS if tool.writes)
# 小工具箱只是虚拟电脑用不了时的备用：电脑在时算数/换算/日期/随机都现场写代码跑。
FALLBACK_TOOLS = frozenset({"toolbox"})
# 合并/删掉的旧工具名 → (新工具, 补上的参数)。模型按老习惯叫、WebUI 旧覆盖、测试都还能用。
LEGACY_ALIASES: dict[str, tuple[str, dict[str, Any]]] = {
    "my_overview": ("my_status", {"view": "overview"}),
    "checkin_status": ("my_status", {"view": "checkin"}),
    "checkin_board": ("my_status", {"view": "board"}),
    "list_my_groups": ("my_status", {"view": "groups"}),
    "our_relationship": ("my_status", {"view": "relationship"}),
    "affinity_detail": ("my_status", {}),
    "what_you_remember": ("recall", {}),
    "chat_history": ("recall", {"where": "private_chat"}),
    "webui_onboarding": ("webui_account", {}),
}


def resolve_alias(name: str, args: Any) -> tuple[str, Any]:
    """旧工具名换成合并后的新工具；参数按旧语义改写。不是旧名原样返回。"""

    target = LEGACY_ALIASES.get(name)
    if target is None or not isinstance(args, dict):
        return (target[0] if target else name), args
    new_name, extra = target
    data = {**args, **extra}
    if name == "affinity_detail":
        data = {
            "view": f"affinity_{args.get('view') or 'progress'}",
            **({"limit": args["limit"]} if args.get("limit") else {}),
        }
    elif name == "chat_history":
        if str(args.get("action") or "stats") != "search":
            return "my_data", {"action": "chat"}
        data = {"where": "private_chat"}
        if args.get("keyword"):
            data["query"] = args["keyword"]
    elif name == "what_you_remember":
        data = {}
    return new_name, data


def available_tools(
    *,
    is_superuser: bool = False,
    computer: bool = False,
    tasks: bool = False,
    manage: bool = False,
) -> list[PrivateTool]:
    """这位用户这一轮能用的工具（虚拟电脑、后台任务、群管代办只在可用时出现）。"""

    from .agent_bridge import enabled as bridge_enabled

    bridge = bridge_enabled()
    # 能放后台时把派活工具排在最前面：模型更容易想到“交出去”，而不是自己一路调工具。
    ordered = (
        [*TASK_TOOLS, *(tool for tool in ALL_TOOLS if tool.name not in TASK_NAMES)]
        if tasks
        else list(ALL_TOOLS)
    )
    return [
        tool
        for tool in ordered
        if (is_superuser or tool.name not in SUPERUSER_TOOLS)
        and (computer or tool.name not in COMPUTER_NAMES)
        and (not computer or tool.name not in FALLBACK_TOOLS)
        and (tasks or tool.name not in TASK_NAMES)
        and (manage or tool.name not in MANAGE_NAMES)
        and (bridge or tool.name not in BRIDGE_NAMES)
    ]


def tool_schemas(
    *,
    is_superuser: bool = False,
    computer: bool = False,
    tasks: bool = False,
    manage: bool = False,
) -> list[dict[str, Any]]:
    """能用的工具的**完整** schema（全量披露模式、WebUI 预览、子智能体取用）。

    私聊主回合默认不发这一份，而是 ``private_tool_catalog.request_tools`` 的渐进式工具表。
    """

    return [
        tool.schema()
        for tool in available_tools(
            is_superuser=is_superuser, computer=computer, tasks=tasks, manage=manage
        )
    ]


@dataclass(slots=True)
class PrivateToolContext:
    session: Any
    user_id: int
    bot_id: int | None
    groups: list[SharedGroup]
    now: datetime
    nickname: str | None = None
    is_superuser: bool = False
    # 反馈转发给管理员时用；测试里可以不给
    bot: Any = None
    # 本轮已经成功的写工具，用于去重与“已完成”声明
    succeeded: set[str] = field(default_factory=set)
    mutated: bool = False
    # 联网搜索：本轮的结果编号（w1…）和已调用次数
    web_refs: dict[str, str] = field(default_factory=dict)
    web_calls: int = 0
    # 虚拟电脑：本轮已运行次数（同一轮重跑不受冷却）
    computer_runs: int = 0
    # 本轮派出的小助手任务数（声明核验与“一轮最多派几个”）
    delegated: int = 0
    # 以下由主回合填好，派活时自动带进任务单（子智能体据此用同一个口吻、知道他原话）
    dialogue: str = ""
    user_input: str = ""
    voice: dict[str, Any] = field(default_factory=dict)
    call_them: str = ""
    computer: bool = False
    attachment_refs: list[str] = field(default_factory=list)
    # 本轮主智能体自己调用重活工具（联网/读网页/跑代码/能力包）的次数
    heavy_calls: int = 0
    # 本轮用 my_persona 改了私聊人设：发送前清词要用新的人设名
    persona_changed: bool = False
    # 超级管理员（全权开着）：他不在、但 Bot 在的群。只在点名单个群时用
    # （_single_group：群里最近在聊什么、签到榜），批量/“我所在的群”仍只看 groups
    superuser_groups: list[SharedGroup] = field(default_factory=list)
    # 只放行这些工具（汇报/协调轮发同一份工具表保前缀缓存，越界的调用在这里拦下）；None 不限
    allowed_tools: frozenset[str] | None = None
    # 本轮 group_manage 调用次数；以及“已经在等他回话，别再调工具了”的信号（主回合据此直接收尾）
    manage_calls: int = 0
    stop_tools: bool = False
    # 这一轮这位用户能用的工具名（渐进式披露：use_tool 只能调这些）；None 不限（测试、子智能体）
    available: frozenset[str] | None = None
    # 本轮已经用 open_tools 展开过的工具族/工具（重复展开直接给短答，省上下文）
    opened: set[str] = field(default_factory=set)
    # 本轮已成功执行过的只读调用（工具名+参数）：一模一样的再来一次就不重复执行、不重复塞结果
    seen_calls: set[str] = field(default_factory=set)


def _match_groups(ctx: PrivateToolContext, raw: str | None) -> list[SharedGroup]:
    if not raw:
        return list(ctx.groups)
    needle = raw.strip().casefold()
    if needle.isdigit():
        hits = [g for g in ctx.groups if str(g.group_id) == needle]
    else:
        exact = [g for g in ctx.groups if g.name.casefold() == needle]
        hits = exact or [g for g in ctx.groups if needle in g.name.casefold()]
    if not hits:
        raise ToolError(
            f"没找到你所在的群「{raw}」",
            code=ERROR_NOT_FOUND,
            hint="先用 list_my_groups 看看他在哪些群",
        )
    return hits


def _single_group(ctx: PrivateToolContext, raw: str) -> SharedGroup:
    try:
        hits = _match_groups(ctx, raw)
    except ToolError:
        if not ctx.superuser_groups:
            raise
        hits = _match_groups(replace(ctx, groups=ctx.superuser_groups), raw)
    if len(hits) > 1:
        names = "、".join(g.name for g in hits[:5])
        raise ToolError(
            f"有多个群匹配「{raw}」：{names}",
            code=ERROR_NOT_FOUND,
            hint="问用户具体是哪个群",
        )
    return hits[0]


async def _overview(ctx: PrivateToolContext) -> dict[str, Any]:
    snap = await affinity.snapshot(ctx.session, ctx.user_id, now=ctx.now)
    statuses = await checkin_overview(ctx.session, ctx.user_id, now=ctx.now)
    from ..data_models.bot_user import BotUser

    user = await ctx.session.get(BotUser, ctx.user_id)
    return {
        "relationship": snap.public_dict(),
        "groups": len(ctx.groups),
        "checked_in_today": sum(1 for s in statuses if s.checked_today),
        "best_streak_days": max((s.streak_days for s in statuses), default=0),
        "total_checkin_days": sum(s.total_days for s in statuses),
        "total_points": sum(s.points for s in statuses),
        "known_since": f"{user.first_interaction_at:%Y-%m-%d}"
        if user is not None and user.first_interaction_at
        else None,
    }


async def _status(ctx: PrivateToolContext, args: dict[str, Any]) -> dict[str, Any]:
    """my_status：原来六个只读查询（概况/签到/签到榜/关系/好感明细/共同的群）合成一个。"""

    from . import private_services as services

    view = str(args.get("view") or "overview")
    if view == "overview":
        return await _overview(ctx)
    if view == "checkin":
        return await _checkin_status(ctx, args.get("group"))
    if view == "board":
        if not str(args.get("group") or "").strip():
            if len(ctx.groups) == 1:
                return await services.checkin_board(ctx, {"group": str(ctx.groups[0].group_id)})
            raise ToolError(
                "board 要写 group（哪个群）",
                code=ERROR_INVALID_ARGS,
                hint="问他是哪个群，或先用 view=groups 看看",
            )
        return await services.checkin_board(ctx, {"group": str(args["group"])})
    if view == "relationship":
        return await _relationship(ctx)
    if view == "groups":
        return _list_groups(ctx)
    sub = view.removeprefix("affinity_")
    return await services.affinity_detail(
        ctx, {"view": sub, **({"limit": args["limit"]} if args.get("limit") else {})}
    )


async def _checkin(ctx: PrivateToolContext, group: str | None) -> dict[str, Any]:
    targets = _match_groups(ctx, group)[:MAX_BATCH_GROUPS]
    results: list[dict[str, Any]] = []
    skipped: list[str] = []
    for target in targets:
        if not await check_feature_permission(
            ctx.user_id, target.group_id, "checkin", ctx.session
        ):
            skipped.append(target.name)
            continue
        result = await perform_checkin(
            ctx.session,
            group_id=target.group_id,
            user_id=ctx.user_id,
            nickname=target.nickname,
            now=ctx.now,
            points_reward=randint(POINTS_MIN, POINTS_MAX),
        )
        payload = result.public_dict()
        payload["group_name"] = target.name
        payload.pop("group_id", None)
        results.append(payload)
        if result.ok:
            ctx.mutated = True
    if not results and not skipped:
        raise ToolError(
            "你还没有和我一起在任何群里",
            code=ERROR_NOT_FOUND,
            hint="告诉他先在群里和你说句话",
        )
    response: dict[str, Any] = {"results": results}
    if skipped:
        response["checkin_disabled_in"] = skipped
    return response


async def _checkin_status(ctx: PrivateToolContext, group: str | None) -> dict[str, Any]:
    targets = _match_groups(ctx, group)
    statuses = await checkin_overview(
        ctx.session, ctx.user_id, group_ids=[g.group_id for g in targets], now=ctx.now
    )
    items = []
    for item in statuses:
        payload = item.public_dict()
        payload.pop("group_id", None)
        items.append(payload)
    return {"groups": items}


def _list_groups(ctx: PrivateToolContext) -> dict[str, Any]:
    return {
        "groups": [
            {"name": g.name, "group_id": g.group_id, "agent_on": g.agent_readable}
            for g in ctx.groups
        ]
    }


def _link(ctx: PrivateToolContext) -> Any:
    from . import memory_link

    return memory_link.load(ctx.user_id, now=ctx.now) if memory_link.enabled() else None


def _visible(ctx: PrivateToolContext, link: Any) -> list[SharedGroup]:
    from .private_context import visible_groups

    return [g for g in visible_groups(ctx.groups, link) if g.agent_readable]


async def _remembered(ctx: PrivateToolContext) -> dict[str, Any]:
    from . import memory_link

    private = [
        private_memory.fact_view(row)
        for row in await private_memory.list_facts(
            ctx.session, ctx.user_id, now=ctx.now
        )
    ]
    link = _link(ctx)
    readable = _visible(ctx, link)
    names = {g.group_id: g.name for g in readable}
    facts = await group_profile_facts(
        ctx.session, ctx.user_id, list(names), now=ctx.now, link=link
    )
    learned = []
    for f in facts:
        item: dict[str, Any] = {
            "key": f["key"],
            "content": f["content"],
            "group": "、".join(sorted(names.get(gid, "") for gid in f.get("group_ids") or [f["group_id"]])),
        }
        item["how"] = "他在群里亲口说的" if f.get("self_told") else "群聊里看出来的"
        if f.get("as_of"):
            item["as_of"] = f["as_of"]
        learned.append(item)
    payload: dict[str, Any] = {
        "told_you_in_private": private,
        "learned_in_groups": learned,
    }
    if link is not None:
        shared = [key for key in memory_link.SHAREABLE_KEYS if key in link.share_keys]
        if shared and memory_link.share_enabled():
            payload["groups_also_know"] = [
                item["label"] for item in private if item["key"] in shared
            ] or [private_memory.KEY_LABELS.get(key, key) for key in shared]
        if link.tombstones:
            payload["asked_to_forget"] = len(link.tombstones)
        hidden = [g.name for g in ctx.groups if g.agent_readable and g not in readable and not link.group_visible_in_private(g.group_id)]
        if hidden:
            payload["not_brought_into_private"] = sorted(hidden)
    return payload


async def _remember(ctx: PrivateToolContext, args: dict[str, Any]) -> dict[str, Any]:
    key = str(args["key"])
    try:
        result = await private_memory.remember(
            ctx.session, ctx.user_id, key, str(args["content"]), now=ctx.now
        )
    except ValueError as exc:
        raise ToolError(str(exc), code=ERROR_PERMISSION) from exc
    ctx.mutated = True
    synced: list[str] = []
    if args.get("also_in_groups"):
        for group in ctx.groups:
            if not group.agent_readable:
                continue
            try:
                await remember_member_fact(
                    ctx.session,
                    group.group_id,
                    ctx.user_id,
                    key,
                    result["content"],
                    now=ctx.now,
                )
            except (ValueError, PermissionError):
                continue
            synced.append(group.name)
    payload: dict[str, Any] = {"key": key, "content": result["content"], "saved": True}
    if args.get("also_in_groups"):
        payload["synced_groups"] = synced
        if synced:
            from . import memory_link

            # 他明确说“群里也这样”：解除“各处都忘掉”的旧记录，并记一笔流动。
            memory_link.clear_tombstones_for(
                ctx.user_id, key, result["content"], now=ctx.now, everywhere=True
            )
            memory_link.log_event(ctx.user_id, "share", now=ctx.now, keys=[key], groups=synced)
    return payload


async def _forget(ctx: PrivateToolContext, args: dict[str, Any]) -> dict[str, Any]:
    key = args.get("key")
    keyword = args.get("keyword")
    result = await private_memory.forget(
        ctx.session, ctx.user_id, key=key, keyword=keyword
    )
    groups_changed: list[str] = []
    if args.get("include_groups"):
        for group in ctx.groups:
            outcome = await forget_member_facts(
                ctx.session, group.group_id, ctx.user_id, raw_key=key, keyword=keyword
            )
            if outcome.get("changed"):
                groups_changed.append(group.name)
    from . import memory_link

    everywhere = bool(args.get("include_groups"))
    tombstone = memory_link.add_tombstone(
        ctx.user_id,
        key=key,
        keyword=keyword,
        scope="everywhere" if everywhere else "private",
        now=ctx.now,
        kind="forget_everywhere" if everywhere else "forget",
        groups=groups_changed or None,
    )
    if result["changed"] or groups_changed or tombstone:
        ctx.mutated = True
    payload: dict[str, Any] = {
        "private_removed": result["removed"],
        "private_trimmed": result["trimmed"],
        "changed": bool(result["changed"] or groups_changed),
    }
    if tombstone:
        # 忘掉的事之后自动整理不会再记回来；只在私聊忘掉时，群画像在私聊里也不再提。
        payload["wont_relearn"] = True
    if result["protected"]:
        payload["kept_admin_entries"] = result["protected"]
    if everywhere:
        payload["groups_changed"] = groups_changed
    else:
        still = await _still_in_groups(ctx, key, keyword)
        if still:
            payload["still_in_groups"] = still
            payload["hint"] = "群里的画像还记着，私聊里我不会再提；他要连群里一起删，再调一次并设 include_groups=true"
    return payload


async def _still_in_groups(
    ctx: PrivateToolContext, key: str | None, keyword: str | None
) -> list[str]:
    """只在私聊忘掉时：哪些群的画像里还有这件事（只报群名）。"""

    readable = [g for g in ctx.groups if g.agent_readable]
    if not readable or not (key or keyword):
        return []
    facts = await group_profile_facts(
        ctx.session, ctx.user_id, [g.group_id for g in readable], now=ctx.now
    )
    names = {g.group_id: g.name for g in readable}
    hit: set[str] = set()
    for fact in facts:
        if key and fact["key"] != key:
            continue
        if keyword and keyword not in fact["content"]:
            continue
        for gid in fact.get("group_ids") or [fact["group_id"]]:
            if gid in names:
                hit.add(names[gid])
    return sorted(hit)


async def _memory_link_tool(ctx: PrivateToolContext, args: dict[str, Any]) -> dict[str, Any]:
    """memory_link：他本人定群聊记忆和私聊记忆怎么流动。"""

    from . import memory_link

    if not memory_link.enabled():
        raise ToolError("记忆打通功能没开", code=ERROR_PERMISSION)
    action = str(args["action"])
    if action == "view":
        state = memory_link.load(ctx.user_id, now=ctx.now)
        return {
            "settings": memory_link.settings_view(state, ctx.groups),
            "forgotten": memory_link.tombstone_view(state),
            "recent_changes": memory_link.ledger_view(state),
            "rule": "私聊里聊的内容不会带到群里；只有 shared_with_groups 里的类别，群里的我也知道",
        }
    if action in {"share", "unshare"}:
        keys = [str(key) for key in args.get("keys") or []]
        if not keys:
            raise ToolError("要说清楚是哪几类", code=ERROR_INVALID_ARGS, hint="keys 填类别，比如 preferred_address、hobby")
        if action == "share" and not memory_link.share_enabled():
            raise ToolError("管理员关掉了“群里也知道”这个功能", code=ERROR_PERMISSION)
        groups: list[SharedGroup] = []
        if action == "share" and args.get("group"):
            groups = [_single_group(ctx, str(args["group"]))]
            if not groups[0].agent_readable:
                raise ToolError(f"「{groups[0].name}」没开我的群聊功能，或你在那里关了我的记忆", code=ERROR_PERMISSION)
        result = memory_link.set_share(
            ctx.user_id,
            keys,
            on=action == "share",
            now=ctx.now,
            groups=[g.group_id for g in groups] or None,
            group_names=[g.name for g in groups] or None,
        )
        ctx.mutated = True
        names = {g.group_id: g.name for g in ctx.groups}
        return {
            "action": action,
            "shared_with_groups": [private_memory.KEY_LABELS.get(k, k) for k in result["share_keys"]],
            "only_in": [names.get(gid, str(gid)) for gid in result["share_groups"]],
            "groups_that_see_it": [
                g.name
                for g in ctx.groups
                if g.agent_readable and result["share_keys"] and (not result["share_groups"] or g.group_id in result["share_groups"])
            ],
        }
    if action in {"mute_group", "unmute_group"}:
        if not args.get("group"):
            raise ToolError("要说是哪个群", code=ERROR_INVALID_ARGS)
        group = _single_group(ctx, str(args["group"]))
        muted = memory_link.set_muted(
            ctx.user_id, group.group_id, muted=action == "mute_group", now=ctx.now, group_name=group.name
        )
        ctx.mutated = True
        return {"action": action, "group": group.name, "muted": group.group_id in muted}
    if action == "group_talk":
        if "on" not in args:
            raise ToolError("要说开还是关", code=ERROR_INVALID_ARGS, hint="on=false 表示别在私聊提群里的事")
        on = memory_link.set_groups_in_private(ctx.user_id, on=bool(args["on"]), now=ctx.now)
        ctx.mutated = True
        return {"action": action, "bring_up_groups": on}
    raise ToolError(f"不支持的动作：{action}", code=ERROR_INVALID_ARGS)


async def _relationship(ctx: PrivateToolContext) -> dict[str, Any]:
    from .private_context import visible_groups

    snap = await affinity.snapshot(ctx.session, ctx.user_id, now=ctx.now)
    # 他让私聊里别提的群，那里的关系也不在私聊里讲（与私聊上下文同一口径）。
    bonds = await group_bonds(
        ctx.session, ctx.user_id, ctx.bot_id, visible_groups(ctx.groups, _link(ctx)), now=ctx.now
    )
    return {"overall": snap.public_dict(), "in_groups": bonds}


def _clean_summary(text: str) -> str | None:
    value = str(text or "").strip()
    if not value or looks_like_instruction(value):
        return None
    value = _SUMMARY_DIGITS_RE.sub(
        "[号码]", scrub_pii(value) if contains_pii(value) else value
    )
    return value[:_SUMMARY_CHARS]


async def _group_recent(ctx: PrivateToolContext, raw: str) -> dict[str, Any]:
    group = _single_group(ctx, raw)
    if not group.agent_readable:
        raise ToolError(
            f"「{group.name}」没开我的群聊功能，或你在那个群关闭了我的记忆，我看不到那里的聊天",
            code=ERROR_PERMISSION,
        )
    rows = (
        (
            await ctx.session.execute(
                select(AgentMemory)
                .where(
                    AgentMemory.group_id == group.group_id,
                    AgentMemory.subject_user_id == 0,
                    AgentMemory.memory_type == "summary",
                    AgentMemory.memory_key.like("daily:%")
                    | AgentMemory.memory_key.like("weekly:%"),
                )
                .order_by(AgentMemory.memory_key.desc())
                .limit(_MAX_SUMMARIES * 2)
            )
        )
        .scalars()
        .all()
    )
    summaries = []
    for row in rows:
        if row.expires_at is not None and row.expires_at < ctx.now:
            continue
        text = _clean_summary(str(row.content or ""))
        if text:
            summaries.append(
                {"period": str(row.memory_key).split(":", 1)[-1], "summary": text}
            )
        if len(summaries) >= _MAX_SUMMARIES:
            break
    payload: dict[str, Any] = {"group": group.name, "summaries": summaries, "empty": not summaries}
    link = _link(ctx)
    if link is not None and not link.group_visible_in_private(group.group_id):
        # 他自己点名问的才答；平时别主动提这个群。
        payload["note"] = "他之前说过私聊里别提这个群，这次是他自己问的，答完别再主动提"
    return payload


_RECALL_PER_GROUP = 4
_RECALL_GROUP_TOTAL = 8
_RECALL_CHAT = 6
_RECALL_CHAT_CHARS = 120
_RECALL_SKIP_RECENT = 12


async def _recall(ctx: PrivateToolContext, args: dict[str, Any]) -> dict[str, Any]:
    """按关键词回想群里的事、旧私聊和私聊记忆；全部只读、只看本人能看的部分。

    叠加他本人定的规则（``memory_link``）：私聊里不提的群不查、他让我忘掉的不返回。
    """

    query = str(args.get("query") or "").strip()
    where = str(args.get("where") or "all")
    link = _link(ctx)
    result: dict[str, Any] = {"query": query}
    if where in {"all", "groups"}:
        result["in_groups"] = await _recall_groups(ctx, query, link=link)
        people = await _recall_people(ctx, query, link)
        if people:
            result["people"] = people
    if where in {"all", "private_chat"}:
        result["in_private_chat"] = await _recall_chat(ctx, query)
        result["remembered"] = await _recall_facts(ctx, query)
        earlier = _recall_recap(ctx, query)
        if earlier:
            result["earlier_chat"] = earlier
    if where == "all":
        relayed = _recall_bridge(ctx, query)
        if relayed:
            result["between_group_and_private"] = relayed
    if link is not None:
        _drop_forgotten(ctx, result, link)
    result["found"] = any(
        result.get(key)
        for key in (
            "in_groups",
            "in_private_chat",
            "remembered",
            "people",
            "earlier_chat",
            "between_group_and_private",
        )
    )
    if not result["found"]:
        result["hint"] = "没找到相关的，直接告诉他不记得了，别编"
    return result


def _drop_forgotten(ctx: PrivateToolContext, result: dict[str, Any], link: Any) -> None:
    """他让我忘掉的关键词：回想结果里含着的一律不返回。"""

    from .memory_link import text_suppressed

    for key in ("in_groups", "in_private_chat", "remembered", "people", "earlier_chat", "between_group_and_private"):
        items = result.get(key)
        if not isinstance(items, list):
            continue
        result[key] = [
            item
            for item in items
            if not text_suppressed(
                ctx.user_id,
                " ".join(str(v) for v in item.values()) if isinstance(item, dict) else str(item),
                now=ctx.now,
                state=link,
            )
        ]


def _terms(query: str) -> list[str]:
    from .memory_retrieval import query_terms

    return [term for term in dict.fromkeys([query, *query_terms(query)]) if term]


def _recall_recap(ctx: PrivateToolContext, query: str) -> list[str]:
    """记忆整理写下的“之前聊到哪”（窗口外更早的话题）。"""

    from . import private_store

    try:
        recap = private_store.load(ctx.user_id).get("recap") or {}
    except Exception:  # noqa: BLE001
        return []
    terms = _terms(query)
    texts = [str(recap.get("summary") or ""), *(str(t) for t in recap.get("open_threads") or [])]
    return [text for text in texts if text and any(term in text for term in terms)][:3]


def _recall_bridge(ctx: PrivateToolContext, query: str) -> list[dict[str, Any]]:
    """群里转到私聊的事、他让我在群里转告/问的事（agent_bridge）。"""

    from . import agent_bridge

    terms = _terms(query)
    found: list[dict[str, Any]] = []
    try:
        handoffs = agent_bridge.handoffs(ctx.user_id)
        relays = agent_bridge.relays(ctx.user_id)
    except Exception:  # noqa: BLE001
        return []
    for item in reversed(handoffs):
        text = str(item.get("topic") or "")
        if any(term in text for term in terms):
            found.append({"kind": "群里转来私聊的事", "group": item.get("group_name"), "what": text[:80], "when": item.get("created")})
    for item in reversed(relays):
        text = str(item.get("text") or "")
        if any(term in text for term in terms):
            found.append(
                {
                    "kind": "他让你在群里问的" if item.get("kind") == "ask" else "他让你在群里转告的",
                    "group": item.get("group_name"),
                    "what": text[:80],
                    "when": item.get("created"),
                    "replies": len(item.get("replies") or []),
                }
            )
    return found[:4]


async def _recall_people(ctx: PrivateToolContext, query: str, link: Any) -> list[dict[str, Any]]:
    """他在群里和谁是什么关系：名字或关系词命中关键词的（只给名字和关系词）。"""

    from .private_context import people_you_both_know, visible_groups

    try:
        people = await people_you_both_know(
            ctx.session, ctx.user_id, visible_groups(ctx.groups, link), now=ctx.now
        )
    except Exception:  # noqa: BLE001
        return []
    terms = _terms(query)
    return [
        item
        for item in people
        if any(term in item["name"] or term in item["relation"] or term in str(item.get("note") or "") for term in terms)
    ][:4]


async def _recall_groups(
    ctx: PrivateToolContext, query: str, *, link: Any = None
) -> list[dict[str, Any]]:
    """可读群里与关键词相关的群摘要、关于他本人的画像和你们在群里的共同经历（不返回别的成员的画像）。"""

    from .memory_retrieval import search_group_memories

    found: list[dict[str, Any]] = []
    for group in _visible(ctx, link):
        try:
            rows = await search_group_memories(
                ctx.session, group.group_id, query, limit=_RECALL_PER_GROUP * 3, now=ctx.now
            )
        except Exception:  # noqa: BLE001
            continue
        picked = 0
        for row in rows:
            subject = int(row.subject_user_id or 0)
            related = {int(uid) for uid in row.related_user_ids or []}
            about_him = subject == ctx.user_id
            if not about_him and subject != 0:
                continue
            if row.memory_type not in {"summary", "core", "profile", "bond"}:
                continue
            text = _clean_summary(str(row.content or ""))
            if not text:
                continue
            key = str(row.memory_key)
            item: dict[str, Any] = {"group": group.name, "content": text}
            if row.memory_type == "summary":
                item["period"] = key.split(":", 1)[-1]
                if ctx.user_id in related:
                    item["you_were_there"] = True
            elif row.memory_type == "bond":
                # 你和他在群里的共同经历（bot_bond）
                item["between_us"] = True
            else:
                item["about_you"] = key
            found.append(item)
            picked += 1
            if picked >= _RECALL_PER_GROUP:
                break
        if len(found) >= _RECALL_GROUP_TOTAL:
            break
    return found[:_RECALL_GROUP_TOTAL]


async def _recall_chat(ctx: PrivateToolContext, query: str) -> list[dict[str, Any]]:
    """他和你的旧私聊里提到关键词的消息（跳过最近十几条：那些本来就在聊天记录里）。"""

    from ..data_models.chat_message import ChatMessage
    from ..data_models.chat_session import ChatSession
    from .memory_retrieval import query_terms

    terms = [query, *query_terms(query)]
    terms = [term for term in dict.fromkeys(terms) if term]
    if not terms:
        return []
    from sqlalchemy import or_

    base = (
        select(ChatMessage.id)
        .join(ChatSession, ChatSession.id == ChatMessage.session_id)
        .where(
            ChatSession.user_id == ctx.user_id,
            ChatSession.group_id.is_(None),
            ChatMessage.is_deleted.is_(False),
        )
        .order_by(ChatMessage.id.desc())
        .limit(_RECALL_SKIP_RECENT)
    )
    recent_ids = set((await ctx.session.execute(base)).scalars().all())
    rows = (
        await ctx.session.execute(
            select(ChatMessage.id, ChatMessage.role, ChatMessage.content, ChatMessage.created_at)
            .join(ChatSession, ChatSession.id == ChatMessage.session_id)
            .where(
                ChatSession.user_id == ctx.user_id,
                ChatSession.group_id.is_(None),
                ChatMessage.is_deleted.is_(False),
                or_(*(ChatMessage.content.contains(term, autoescape=True) for term in terms[:6])),
            )
            .order_by(ChatMessage.id.desc())
            .limit(_RECALL_CHAT * 4)
        )
    ).all()
    found: list[dict[str, Any]] = []
    for message_id, role, content, created_at in rows:
        if message_id in recent_ids:
            continue
        text = _clean_summary(_snippet(str(content or ""), terms))
        if not text:
            continue
        found.append(
            {
                "who": "他" if role == "user" else "你",
                "said": text[:_RECALL_CHAT_CHARS],
                "when": f"{created_at:%Y-%m-%d}" if created_at else "",
            }
        )
        if len(found) >= _RECALL_CHAT:
            break
    found.reverse()
    return found


def _snippet(text: str, terms: list[str]) -> str:
    """长消息只截命中关键词附近的一段。"""

    if len(text) <= _RECALL_CHAT_CHARS:
        return text
    position = min(
        (text.find(term) for term in terms if term and term in text), default=0
    )
    start = max(position - _RECALL_CHAT_CHARS // 3, 0)
    piece = text[start : start + _RECALL_CHAT_CHARS]
    return ("…" if start else "") + piece


async def _recall_facts(ctx: PrivateToolContext, query: str) -> list[dict[str, Any]]:
    from .memory_retrieval import query_terms
    from .private_memory import fact_view, list_facts

    terms = [query, *query_terms(query)]
    facts = [fact_view(row) for row in await list_facts(ctx.session, ctx.user_id, now=ctx.now)]
    return [
        {"what": item["label"], "content": item["content"]}
        for item in facts
        if any(term and (term in item["content"] or term in item["label"]) for term in terms)
    ]


async def _commands(ctx: PrivateToolContext, args: dict[str, Any]) -> dict[str, Any]:
    from ..command_access import CommandAccessContext
    from ..permission import get_user_feature_status

    scene = str(args.get("scene") or "all")
    query = str(args.get("query") or "").strip().casefold()
    statuses = await get_user_feature_status(ctx.user_id, None, ctx.session)
    enabled = frozenset(key for key, _d, on, _s in statuses if on)
    access = CommandAccessContext(
        user_id=ctx.user_id,
        group_id=None,
        enabled_features=enabled,
        is_superuser=ctx.is_superuser,
    )
    items: list[dict[str, Any]] = []
    for group in get_registered_command_groups():
        for spec in group.commands:
            if spec.permission != "everyone" and not ctx.is_superuser:
                continue
            if scene != "all" and spec.scope not in {scene, "all"}:
                continue
            if not access.feature_enabled(spec.feature):
                continue
            text = f"{spec.name} {' '.join(spec.aliases)} {spec.description} {group.display_name}".casefold()
            if query and query not in text:
                continue
            items.append(
                {
                    "command": f"/{spec.name}",
                    "aliases": [f"/{alias}" for alias in spec.aliases[:3]],
                    "what": spec.description,
                    "where": {"group": "群聊", "private": "私聊"}.get(
                        spec.scope, "群聊/私聊"
                    ),
                    "module": group.display_name,
                }
            )
    return {
        "commands": items[:_MAX_COMMANDS],
        "more": max(len(items) - _MAX_COMMANDS, 0),
    }


async def execute_private_tool(  # noqa: PLR0915
    name: str, raw_args: Any, ctx: PrivateToolContext
) -> dict[str, Any]:
    """执行一个私聊工具，返回给模型的 payload（``ok`` + ``result`` 或错误）。"""

    from . import private_tool_catalog as catalog

    if name == catalog.OPEN_TOOL:
        return catalog.open_tools(raw_args, ctx)
    if name == catalog.USE_TOOL:
        inner, inner_args, problem = catalog.unwrap_use(raw_args, ctx)
        if problem is not None:
            return problem
        payload = await execute_private_tool(inner, inner_args, ctx)
        return catalog.annotate_failure(inner, payload, ctx)
    name, raw_args = resolve_alias(name, raw_args)
    tool = TOOLS_BY_NAME.get(name)
    if tool is None:
        return {
            "ok": False,
            "error": f"没有工具 {name}",
            "error_code": "invalid_args",
            "hint": "工具名看 open_tools 里的目录",
        }
    if ctx.available is not None and name not in ctx.available:
        return {
            "ok": False,
            "error": f"{name} 现在用不了",
            "error_code": "not_allowed",
            "hint": "这项能力对他没开或暂时不可用，照实说，别编结果",
        }
    if ctx.allowed_tools is not None and name not in ctx.allowed_tools:
        allowed = "、".join(sorted(ctx.allowed_tools)) or "不调用任何工具"
        return {
            "ok": False,
            "error": f"这一轮只能用：{allowed}",
            "error_code": "not_allowed",
            "hint": "别的事等他回话的那一轮再做；现在直接回复",
        }
    from .private_voice import HEAVY_TOOLS

    if name in HEAVY_TOOLS:
        ctx.heavy_calls += 1
    try:
        args = validate_tool_arguments(
            name, tool.properties, tool.required, raw_args or {}
        )
        if name == "my_status":
            result = await _status(ctx, args)
        elif name == "checkin":
            result = await _checkin(ctx, args.get("group"))
        elif name == "remember_about_me":
            result = await _remember(ctx, args)
        elif name == "forget_about_me":
            result = await _forget(ctx, args)
        elif name == "group_recent":
            result = await _group_recent(ctx, str(args["group"]))
        elif name == "recall":
            result = (
                await _recall(ctx, args)
                if str(args.get("query") or "").strip()
                else await _remembered(ctx)
            )
        elif name == "memory_link":
            result = await _memory_link_tool(ctx, args)
        elif name == "list_commands":
            result = await _commands(ctx, args)
        elif name in COMPUTER_NAMES:
            from .private_computer import execute_computer_tool

            result = await execute_computer_tool(
                name, args, ctx.user_id, rerun=ctx.computer_runs > 0
            )
            if name in {"run_code", "computer_do"}:
                ctx.computer_runs += 1
        elif name in TASK_NAMES:
            result = _task_tool(name, args, ctx)
        elif name == "my_persona":
            result = _persona_tool(args, ctx)
        elif name == "webui_account":
            result = await _webui_account_tool(args, ctx)
        elif name in MANAGE_NAMES:
            result = await _group_manage_tool(args, ctx)
        elif name in BRIDGE_NAMES:
            from .private_bridge_tool import group_bridge_tool

            result = await group_bridge_tool(args, ctx)
        elif name in SCHEDULE_NAMES:
            from .private_schedule_tool import schedules_tool

            result = await schedules_tool(args, ctx)
        elif name in OFFER_NAMES:
            result = _offers_tool(args, ctx)
        else:
            result = await _execute_extra(name, args, ctx)
    except Exception as exc:  # noqa: BLE001
        return tool_error_payload(exc)
    ctx.succeeded.add(name)
    if name == "my_status":
        # 声明核验：查过签到/签到榜才能说“你今天签了/排第几”
        ctx.succeeded.add(f"my_status.{args.get('view')}")
    if name in (MANAGE_NAMES | BRIDGE_NAMES | SCHEDULE_NAMES | OFFER_NAMES) and isinstance(result, dict) and result.get("done"):
        # 声明核验：只有真正执行了（不是“待确认”）才算办成
        ctx.succeeded.add(f"{name}.done")
        ctx.succeeded.add(f"{name}.{result.get('action')}")
    if name in _ORGANIZER_ACTIONS and str(args.get("action") or "") in _ORGANIZER_ACTIONS[name]:
        # 声明核验按“工具.动作”区分：查了待办不能说“已经帮你加上了”
        ctx.succeeded.add(f"{name}.{args['action']}")
    return {"ok": True, "result": result}


def _offers_tool(args: dict[str, Any], ctx: PrivateToolContext) -> dict[str, Any]:
    from . import private_store as store
    from .private_offer_policy import load_settings, record_usage
    from .private_offers import tool_call

    settings = load_settings()
    try:
        result = store.update(ctx.user_id, lambda rec: tool_call(rec, args, ctx.now, settings))
    except LookupError as exc:
        raise ToolError(str(exc), code=ERROR_NOT_FOUND) from exc
    except ValueError as exc:
        raise ToolError(str(exc), code=ERROR_INVALID_ARGS) from exc
    if result.get("action") == "resolve" and result.get("outcome"):
        record_usage(str(result["outcome"]))
    return result


MAX_DELEGATIONS_PER_TURN = 3


def _task_tool(
    name: str, args: dict[str, Any], ctx: PrivateToolContext
) -> dict[str, Any]:
    from . import private_tasks
    from .private_organizer import clean_text

    if name == "delegate_task":
        if ctx.delegated >= MAX_DELEGATIONS_PER_TURN:
            raise ToolError(
                "这一轮已经派了好几个任务了",
                code=ERROR_PERMISSION,
                hint="先告诉他已经安排的，别再派",
            )
        from .private_voice import infer_toolsets, short_title

        goal = clean_text(args.get("goal"), limit=600, field="目标")
        tools = [
            str(item)
            for item in args.get("tools") or []
            if str(item) in {"web", "computer", "toolbox"}
        ]
        brief = _auto_brief(ctx, str(args.get("brief") or ""))
        if not tools:
            tools = infer_toolsets(
                computer=ctx.computer, has_refs="sandbox:" in brief
            )
        if not ctx.computer:
            tools = [item for item in tools if item != "computer"] or ["toolbox"]
        title = (
            clean_text(args.get("title"), limit=20, field="标题")
            if str(args.get("title") or "").strip()
            else short_title(goal)
        )
        follow_up = str(args.get("follow_up_of") or "").strip() or None
        result = private_tasks.start_task(
            ctx.bot,
            ctx.user_id,
            title=title,
            goal=goal,
            brief=brief,
            toolsets=sorted(set(tools)),
            save_to_notes=bool(args.get("save_to_notes")),
            now=ctx.now,
            **_hand_context(ctx),
            follow_up_of=follow_up,
        )
        ctx.delegated += 1
        return result
    if name == "delegate_project":
        if ctx.delegated >= MAX_DELEGATIONS_PER_TURN:
            raise ToolError("这一轮已经派了好几个任务了", code=ERROR_PERMISSION, hint="先告诉他已经安排的，别再派")
        result = private_tasks.start_project(
            ctx.bot,
            ctx.user_id,
            title=clean_text(args.get("title"), limit=20, field="标题"),
            goal=clean_text(args.get("goal"), limit=600, field="目标"),
            brief=_auto_brief(ctx, str(args.get("brief") or "")),
            members=args.get("members"),
            save_to_notes=bool(args.get("save_to_notes")),
            now=ctx.now,
            **_hand_context(ctx),
        )
        ctx.delegated += 1
        return result
    action = str(args.get("action") or "list")
    if action == "list":
        payload: dict[str, Any] = {"tasks": private_tasks.list_tasks(ctx.user_id)}
        projects = private_tasks.list_projects(ctx.user_id)
        if projects:
            payload["projects"] = projects[:3]
        return payload
    if action == "result":
        task_id = str(args.get("id") or "").strip()
        if not task_id:
            raise ToolError("result 要写编号 id", code=ERROR_NOT_FOUND, hint="先用 tasks list 看编号")
        return private_tasks.result_detail(ctx.user_id, task_id)
    if action == "message":
        target = str(args.get("id") or "").strip()
        text = str(args.get("text") or args.get("answer") or "").strip()
        if not target or not text:
            raise ToolError("message 要写 id（任务编号/成员名/all）和 text", code=ERROR_NOT_FOUND, hint="先用 tasks list 看编号")
        return private_tasks.message_task(ctx.bot, ctx.user_id, target, text)
    task_id = str(args.get("id") or "").strip()
    if not task_id:
        raise ToolError(
            "要给任务编号 id", code=ERROR_NOT_FOUND, hint="先用 tasks list 看编号"
        )
    if action == "cancel":
        return private_tasks.cancel_task(ctx.user_id, task_id)
    answer = str(args.get("answer") or "").strip()
    if not answer:
        raise ToolError("answer 不能为空", code=ERROR_NOT_FOUND, hint="把他的原话转交")
    return private_tasks.answer_task(ctx.bot, ctx.user_id, task_id, answer)


BRIEF_LIMIT = 1500


def _auto_brief(ctx: PrivateToolContext, brief: str) -> str:
    """模型写的 brief + 本轮附件的 sandbox 引用（它没抄进去的）。聊天节选单独存，不占 brief。"""

    text = brief.strip()[:BRIEF_LIMIT]
    missing = [ref for ref in ctx.attachment_refs if ref not in text]
    if missing:
        text = f"{text}\n他这次发来的文件：{' '.join(missing[:6])}".strip()
    return text


def _hand_context(ctx: PrivateToolContext) -> dict[str, Any]:
    return {
        "dialogue": ctx.dialogue,
        "asked": ctx.user_input,
        "voice": ctx.voice,
        "call_them": ctx.call_them,
    }


# 会改个人数据的动作（其余动作只读）。
_ORGANIZER_ACTIONS: dict[str, frozenset[str]] = {
    "notes": frozenset({"add", "delete", "pin", "unpin"}),
    "todos": frozenset({"add", "done", "undo", "delete", "clear_done"}),
    "reminders": frozenset({"add", "cancel", "snooze", "pause", "resume", "edit"}),
    "countdowns": frozenset({"add", "delete"}),
    "mood": frozenset({"log"}),
    "chat_prefs": frozenset({"set"}),
    "my_data": frozenset({"wipe"}),
    "tasks": frozenset({"cancel", "answer", "message"}),
    "my_persona": frozenset({"set", "preset", "template", "reset", "undo"}),
    "webui_account": frozenset({"setup_code", "logout_all", "ask_password", "cancel"}),
    "computer_files": frozenset({"delete"}),
}


async def _webui_account_tool(
    args: dict[str, Any], ctx: PrivateToolContext
) -> dict[str, Any]:
    """网页个人账号。设置码由 Bot 直接私聊发出，不回给模型，免得进上下文和记录。"""

    from .. import webui_account_commands as wac
    from .. import webui_accounts

    blocked = wac.feature_blocked_reason()
    if blocked:
        raise ToolError(blocked, code=ERROR_PERMISSION, hint="照实告诉他现在用不了")
    action = str(args.get("action") or "view")
    if action in {"guide", "ask_password", "cancel"}:
        # 新人引导三件事（原 webui_onboarding 工具）合进来：一个账号一件工具。
        return await _webui_onboarding_tool(args, ctx)
    if action == "view":
        account = webui_accounts.get_account(ctx.user_id)
        groups = await wac.managed_group_names(ctx.user_id)
        return {
            "opened": bool(account and account.has_password and not account.disabled),
            "disabled": bool(account and account.disabled),
            "password_set_at": account.password_changed_at if account else None,
            "last_login_at": account.last_login_at if account else None,
            "how_to_login": f"{wac.login_hint()}，用他的 QQ 号和密码",
            "managed_groups": groups[:10],
            "how_to_set_password": "用 ask_password 让他直接把想要的密码发过来，或 setup_code 给他发设置码",
        }
    if action == "setup_code":
        if ctx.bot is None:
            raise ToolError("现在发不了私聊消息", code=ERROR_PERMISSION, hint="让他自己发 /网页密码")
        try:
            code = webui_accounts.issue_setup_code(ctx.user_id)
        except webui_accounts.AccountError as exc:
            raise ToolError(str(exc), code=ERROR_PERMISSION, hint="照实告诉他") from exc
        await ctx.bot.send_private_msg(
            user_id=ctx.user_id, message=wac.setup_code_text(code)
        )
        return {
            "sent": True,
            "note": "设置码已经单独私聊发给他了（你看不到），10 分钟内有效；别复述设置码",
        }
    if action == "logout_all":
        done = webui_accounts.revoke_sessions(ctx.user_id)
        return {"done": done, "note": "所有设备已退出" if done else "他还没有网页账号"}
    raise ToolError("没有这个动作", code=ERROR_INVALID_ARGS, hint="action 用 guide/view/ask_password/cancel/setup_code/logout_all")


async def _webui_onboarding_tool(
    args: dict[str, Any], ctx: PrivateToolContext
) -> dict[str, Any]:
    """新人引导：看进度、开收密码窗口。密码由 webui_account_commands 的监听器直接收，绝不经过模型。"""

    from .. import webui_account_commands as wac
    from .. import webui_accounts

    blocked = wac.feature_blocked_reason()
    if blocked:
        raise ToolError(blocked, code=ERROR_PERMISSION, hint="照实告诉他现在用不了")
    action = str(args.get("action") or "guide")
    account = webui_accounts.get_account(ctx.user_id)
    opened = bool(account and account.has_password and not account.disabled)
    if action == "guide":
        if account is not None and account.disabled:
            step, next_step = "disabled", "他的网页账号被管理员停用了，照实告诉他，让他找管理员"
        elif wac.capture_pending(ctx.user_id):
            step, next_step = "waiting_password", "正在等他把密码发过来，让他照系统提示直接发密码就行"
        elif not opened:
            step, next_step = "no_password", "先帮他设密码：问他要不要现在设，愿意就用 ask_password"
        elif account is not None and not account.last_login_at:
            step, next_step = "never_logged_in", "密码已设好但还没登录过：把 login_url 发给他，告诉他账号填 QQ 号"
        else:
            step, next_step = "ready", "已经能登录了，有需要就把 login_url 发给他；忘了密码可以 ask_password 重设"
        groups = await wac.managed_group_names(ctx.user_id)
        url = wac.login_url()
        return {
            "step": step,
            "next": next_step,
            "login_url": url or None,
            "login_url_note": None if url else "管理员没配置公网地址，告诉他打开 WebUI 网页即可，别编网址",
            "username": f"他的 QQ 号 {ctx.user_id}",
            "login_tab": "登录页选「QQ 账号」",
            "password_rules": wac.password_rules_text(),
            "password_set_at": account.password_changed_at if account else None,
            "last_login_at": account.last_login_at if account else None,
            "can_do": (
                ["Bot 在的所有群都能管（不必当群管）", "群关系图、群功能、群 Agent 和人设、群管代办", "调我们私聊里你的人设"]
                if superuser_full_access(ctx.user_id)
                else ["看自己当群主/管理员的群的关系图", "开关这些群的功能", "调我们私聊里你的人设"]
                if groups
                else ["调我们私聊里你的人设", "当了群主/管理员后还能在网页上管群"]
            ),
            "managed_groups": groups[:10],
            "managed_group_total": len(groups),
        }
    if action == "ask_password":
        if account is not None and account.disabled:
            raise ToolError("他的网页账号被管理员停用了", code=ERROR_PERMISSION, hint="照实告诉他，让他找管理员")
        if ctx.bot is None:
            raise ToolError("现在发不了私聊消息", code=ERROR_PERMISSION, hint="让他自己发 /网页密码 拿设置码")
        seconds = wac.arm_password_capture(ctx.user_id)
        try:
            await ctx.bot.send_private_msg(
                user_id=ctx.user_id, message=wac.capture_prompt_text()
            )
        except Exception as exc:
            wac.cancel_password_capture(ctx.user_id)
            raise ToolError("提示没发出去", code=ERROR_PERMISSION, hint="让他自己发 /网页密码") from exc
        return {
            "waiting": True,
            "minutes": seconds // 60,
            "replacing": opened,
            "note": "系统已经单独私聊提示他把密码发过来；他下一条消息会被系统直接收走，你看不到也不用处理。"
            "你这句简短接一下就行（比如让他照提示发），别复述规则，更别让他在别处发密码",
        }
    cancelled = wac.cancel_password_capture(ctx.user_id)
    return {"cancelled": cancelled, "note": "已关掉收密码的窗口" if cancelled else "本来就没在等密码"}


def _persona_tool(args: dict[str, Any], ctx: PrivateToolContext) -> dict[str, Any]:
    from . import private_persona as pp
    from .persona import resolve_preset_id

    action = str(args.get("action") or "view")
    try:
        if action == "view":
            return pp.tool_view(ctx.user_id)
        if action == "options":
            return pp.tool_options()
        if action == "history":
            state = pp.state_payload(ctx.user_id)
            return {"history": state["history"][:5], "current": state["sourceLabel"]}
        if action == "set":
            fields = args.get("fields")
            if not isinstance(fields, dict) or not fields:
                raise ToolError("没给要改的字段", code=ERROR_INVALID_ARGS, hint='fields 里写要改的，如 {"self_name":"本喵"}')
            result = pp.patch_fields(ctx.user_id, fields, by=pp.BY_SELF)
        elif action == "preset":
            preset_id = resolve_preset_id(str(args.get("name") or ""))
            if preset_id is None:
                raise ToolError("没有这个模板", code=ERROR_NOT_FOUND, hint="先用 options 看有哪些模板")
            result = pp.use_preset(ctx.user_id, preset_id, by=pp.BY_SELF)
        elif action == "template":
            found = pp.find_library_by_name(str(args.get("name") or ""))
            if found is None:
                raise ToolError("人设库里没有这个人设", code=ERROR_NOT_FOUND, hint="先用 options 看人设库里有哪些")
            result = pp.use_template(ctx.user_id, found[0], by=pp.BY_SELF)
        elif action == "reset":
            result = pp.reset(ctx.user_id, by=pp.BY_SELF)
        else:
            result = pp.undo(ctx.user_id, by=pp.BY_SELF)
    except pp.PersonaEditError as exc:
        code = {
            "permission": ERROR_PERMISSION,
            "quota": ERROR_QUOTA,
            "not_found": ERROR_NOT_FOUND,
        }.get(exc.code, ERROR_INVALID_ARGS)
        raise ToolError(str(exc), code=code) from exc
    ctx.persona_changed = True
    return {
        "now": result.get("label"),
        "changed": result.get("changed") or [],
        "summary": result.get("summary"),
        "note": "从下一句开始按新人设说话；只影响你们的私聊",
    }


_MANAGE_CODES = {
    "permission_denied": ERROR_PERMISSION,
    "quota_exceeded": ERROR_QUOTA,
    "not_found": ERROR_NOT_FOUND,
    "invalid_args": ERROR_INVALID_ARGS,
    "ambiguous": ERROR_INVALID_ARGS,
    "needs_confirm": ERROR_PERMISSION,
    "unsupported": "unsupported",
    "unavailable": "upstream",
    "tool_failed": "upstream",
}


# 一轮里最多调几次 group_manage（查群 + 找人 + 发起 + 确认，正常 4 次以内）
MANAGE_CALLS_PER_TURN = 6


async def _manage_group(ctx: PrivateToolContext, raw: str | None) -> tuple[int, str]:
    from . import group_manage as gm

    candidates = await gm.candidate_groups(ctx.session, ctx.user_id)
    if not candidates:
        raise ToolError("他不是任何一个你所在群的群主或管理员", code=ERROR_PERMISSION, hint="照实告诉他；超级管理员不受这条限制")
    if not raw:
        if len(candidates) == 1:
            return candidates[0][0], candidates[0][1]
        names = "、".join(name for _gid, name, _role in candidates[:6])
        raise ToolError(f"他管着好几个群：{names}", code=ERROR_INVALID_ARGS, hint="问他是哪个群，再用 group 指定")
    needle = raw.strip().casefold()
    if needle.isdigit():
        hits = [row for row in candidates if str(row[0]) == needle]
    else:
        hits = [row for row in candidates if row[1].casefold() == needle] or [
            row for row in candidates if needle in row[1].casefold()
        ]
    if not hits:
        raise ToolError(f"他管理的群里没有「{raw}」", code=ERROR_NOT_FOUND, hint="用 groups 看他能管哪些群")
    if len(hits) > 1:
        names = "、".join(row[1] for row in hits[:5])
        raise ToolError(f"有多个群匹配「{raw}」：{names}", code=ERROR_INVALID_ARGS, hint="问他具体是哪个群")
    return hits[0][0], hits[0][1]


async def _group_manage_tool(args: dict[str, Any], ctx: PrivateToolContext) -> dict[str, Any]:  # noqa: PLR0915
    """群管代办。权限、确认、额度、审计全在 group_manage，这里只做群匹配和错误翻译。"""

    from . import group_manage as gm

    action = str(args.get("action") or "groups")
    group_id: int | None = None
    ctx.manage_calls += 1
    if ctx.manage_calls > MANAGE_CALLS_PER_TURN:
        # 一轮里反复调（发起→自己确认→被拒→再发起……）：直接收尾，别让他一直等。
        ctx.stop_tools = True
        raise ToolError(
            "这一轮已经调了很多次群管",
            code=ERROR_QUOTA,
            hint="别再调 group_manage，用现在的结果回他；需要他确认的就问他",
        )
    try:
        if action == "cancel":
            count = gm.cancel_pending(ctx.user_id, str(args.get("confirm_id") or "") or None)
            return {"cancelled": count, "note": "已作废，没有执行" if count else "没有待确认的操作"}
        if action == "groups":
            rows = []
            candidates = await gm.candidate_groups(ctx.session, ctx.user_id)
            for gid, name, _role in candidates[:8]:
                try:
                    access = await gm.verify(ctx.bot, gid, ctx.user_id, source="private")
                    rows.append({"group": name, **access.as_dict()})
                except gm.ManageError as exc:
                    rows.append({"group": name, "group_id": str(gid), "unavailable": str(exc)})
            waiting = [
                {"confirm_id": item.id, "summary": item.summary} for item in gm.pending_for(ctx.user_id)
            ]
            out: dict[str, Any] = {"groups": rows, "waiting_confirm": waiting, "used_today": gm.used_today(ctx.user_id)}
            if ctx.is_superuser and superuser_full_access(ctx.user_id):
                # 超级管理员不必当群管：Bot 在的群都列出来（只展开前 8 个），其余用 group 指定
                out["you_are_superuser"] = True
                out["total_groups"] = len(candidates)
                if len(candidates) > len(rows):
                    out["note"] = "只展开了最近活跃的 8 个群，其余的用 group 写群名或群号直接操作"
            return out
        if action == "confirm":
            raw_id = str(args.get("confirm_id") or "").strip()
            try:
                pending = gm.take_pending(ctx.user_id, raw_id)
            except gm.ManageError as exc:
                waiting = gm.pending_view(ctx.user_id)
                # 编号对不上、但在等的都还没等到他回话：和同一轮自己确认是一回事。
                premature = exc.code == "needs_confirm" or (
                    exc.code == "not_found"
                    and bool(waiting)
                    and not any(row["he_replied_since"] for row in waiting)
                )
                if not premature:
                    if waiting and exc.code == "not_found":
                        raise ToolError(
                            str(exc),
                            code=ERROR_NOT_FOUND,
                            hint="还在等他确认的是："
                            + "；".join(f"{row['confirm_id']} {row['summary']}" for row in waiting)
                            + "。用对应的 confirm_id；都不是就重新发起",
                        ) from exc
                    raise
                # 同一轮自己确认：不算错，告诉模型去问他并收尾（以前报错会让它一轮里反复重试）。
                ctx.stop_tools = True
                row = next((item for item in waiting if item["confirm_id"] == raw_id), None) or (
                    waiting[0] if waiting else {}
                )
                return {
                    "done": False,
                    "waiting_for_reply": True,
                    "confirm_id": row.get("confirm_id"),
                    "summary": row.get("summary"),
                    "note": "还没执行：他还没回复，你不能替他确认。本轮别再调 group_manage，"
                    "用你自己的话把 summary 问他，等他下一条回复",
                }
            group_id = pending.group_id
            access = await gm.verify(ctx.bot, pending.group_id, ctx.user_id, source="private")
            result = await gm.execute(
                access, pending.action, pending.args, source="private", actor_name=ctx.nickname
            )
            return {**result, "summary": pending.summary, "group": pending.group_name}
        group_id, group_name = await _manage_group(ctx, args.get("group"))
        access = await gm.verify(ctx.bot, group_id, ctx.user_id, source="private")
        if action == "members":
            members = await gm.member_list(ctx.bot, group_id)
            return {"group": group_name, "members": gm.search_members(members, args.get("query"), limit=15), "total": len(members)}
        if action == "muted":
            return {"group": group_name, "muted": (await gm.list_muted(access))[:30]}
        if action == "notices":
            return {"group": group_name, "notices": (await gm.list_notices(access))[:10]}
        if action == "audit":
            return {"group": group_name, "recent": gm.audit_rows(group_id, limit=10)}
        summary, normalized = await gm.describe(access, action, args, group_name)
        if action in access.policy["confirm_actions"]:
            existing = gm.find_pending(ctx.user_id, group_id, action, normalized)
            pending = gm.create_pending(ctx.user_id, group_id, group_name, action, normalized, summary)
            if existing is not None and gm.replied_since(existing):
                # 上一轮问过他、他也回话了，模型却重新发起了同一件事（它看不到上一轮的 confirm_id）。
                return {
                    "needs_confirm": True,
                    "confirm_id": pending.id,
                    "summary": summary,
                    "already_asked": True,
                    "he_replied_since": True,
                    "note": "这件事之前已经问过他，他也回话了：他那句是同意的话，直接用 confirm 执行这个 confirm_id，"
                    "不用再问；不同意就 cancel；看不出来再问他一次",
                }
            if existing is not None:
                # 同一轮里重复发起：已经在等他了。
                ctx.stop_tools = True
                return {
                    "needs_confirm": True,
                    "confirm_id": pending.id,
                    "summary": summary,
                    "already_asked": True,
                    "note": "这件事已经在等他确认了。本轮别再调 group_manage，把 summary 问他，等他回复",
                }
            return {
                "needs_confirm": True,
                "confirm_id": pending.id,
                "summary": summary,
                "expires_in_seconds": gm.confirm_seconds(),
                "note": "还没执行。把 summary 原样问他要不要做；等他下一条回复同意后再用 confirm 执行，不同意就 cancel",
            }
        result = await gm.execute(access, action, normalized, source="private", actor_name=ctx.nickname)
        return {**result, "summary": summary, "group": group_name}
    except gm.ManageError as exc:
        if group_id is not None and exc.code == "permission_denied":
            gm.deny(group_id, ctx.user_id, action, "private", str(exc))
        raise ToolError(str(exc), code=_MANAGE_CODES.get(exc.code, "upstream"), hint=exc.hint) from exc


async def _execute_extra(
    name: str, args: dict[str, Any], ctx: PrivateToolContext
) -> dict[str, Any]:
    from . import private_organizer as organizer
    from . import private_services as services

    simple = {
        "notes": organizer.notes,
        "todos": organizer.todos,
        "reminders": organizer.reminders,
        "countdowns": organizer.countdowns,
        "mood": organizer.mood,
        "chat_prefs": organizer.chat_prefs,
    }
    if name in simple:
        return simple[name](ctx.user_id, args, ctx.now)
    if name == "toolbox":
        return services.toolbox(ctx, args)
    if name == "bot_status" and not ctx.is_superuser:
        raise ToolError("只有超级管理员能用", code=ERROR_PERMISSION)
    handlers = {
        "my_data": services.my_data,
        "feedback": services.feedback,
        "web_search": services.web_search,
        "web_fetch": services.web_fetch,
        "bot_status": services.bot_status,
    }
    return await handlers[name](ctx, args)


__all__ = [
    "ALL_TOOLS",
    "BRIDGE_NAMES",
    "BRIDGE_TOOLS",
    "COMPUTER_NAMES",
    "COMPUTER_TOOLS",
    "EXTRA_TOOLS",
    "MANAGE_NAMES",
    "MANAGE_TOOLS",
    "OFFER_NAMES",
    "PERSONA_TOOLS",
    "PRIVATE_TOOLS",
    "SCHEDULE_NAMES",
    "SCHEDULE_TOOLS",
    "SUPERUSER_TOOLS",
    "TASK_NAMES",
    "TASK_TOOLS",
    "TOOLS_BY_NAME",
    "WRITE_TOOLS",
    "PrivateTool",
    "PrivateToolContext",
    "execute_private_tool",
    "tool_schemas",
]
