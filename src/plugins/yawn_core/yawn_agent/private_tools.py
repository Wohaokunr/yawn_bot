# ruff: noqa: E501, C901, PLR0911, PLR0912, TID252, TC003, TRY003, RUF001
"""私聊 Agent 的工具：全部只作用于**当前私聊的这位用户本人**。

没有任何工具接受 user_id；群相关的工具只能选用户本人活跃所在的群，读 Agent 数据
（摘要、画像、关系）还要求该群开着 Agent、用户没有隐私退出。参数一律先过
``tool_args.validate_tool_arguments``，失败统一返回 ``tool_error_payload``。

签到、好感、记忆都调用已有服务（``checkin_service`` / ``affinity`` /
``memory_self`` / ``private_memory``），这里不另写业务逻辑。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
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
from . import private_memory
from .memory_hygiene import contains_pii, looks_like_instruction, scrub_pii
from .memory_self import SELF_FACT_KEYS, forget_member_facts, remember_member_fact
from .private_computer import COMPUTER_TOOL_SPECS
from .private_context import SharedGroup, group_bonds, group_profile_facts
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


PRIVATE_TOOLS: tuple[PrivateTool, ...] = (
    PrivateTool(
        "my_overview",
        "查这位用户的概况：和你的关系等级、在几个群、各群签到/连签/积分合计、你什么时候认识他",
    ),
    PrivateTool(
        "checkin",
        "替这位用户签到（等同他在群里发 /签到）。不指定群时在他所在的所有群里签；今天签过的返回 already",
        {"group": _GROUP_SCHEMA},
        writes=True,
    ),
    PrivateTool(
        "checkin_status",
        "查这位用户在各群的签到情况：今天签没签、连签、累计天数、积分",
        {"group": _GROUP_SCHEMA},
    ),
    PrivateTool(
        "list_my_groups",
        "列出你和这位用户共同在的群，以及每个群里你的 Agent 有没有开",
    ),
    PrivateTool(
        "what_you_remember",
        "查你记得关于这位用户的事：私聊里他告诉你的、你在各群里了解到的画像",
    ),
    PrivateTool(
        "remember_about_me",
        "按这位用户的明确要求记住关于他自己的一件事。also_in_groups 只有他明确说“群里也这样”时才设为 true，会同步到他所在且开了 Agent 的群",
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
        "按这位用户的要求忘掉关于他的记忆；可按类别或关键词只删一部分。include_groups=true 时连他在各群的画像也一起删",
        {
            "key": _FACT_KEY_SCHEMA,
            "keyword": {"type": "string", "minLength": 1, "maxLength": 40},
            "include_groups": {"type": "boolean"},
        },
        writes=True,
    ),
    PrivateTool(
        "our_relationship",
        "查你和这位用户的关系：整体好感等级、各群里的熟悉程度、他怎么叫你、你们的梗",
    ),
    PrivateTool(
        "group_recent",
        "看这位用户所在的某个群最近在聊什么（读你整理的群聊日摘要）",
        {"group": {**_GROUP_SCHEMA, "description": "群名（可以只写一部分）或群号"}},
        ("group",),
    ),
    PrivateTool(
        "list_commands",
        "查现在私聊/群里有哪些命令可用、各自做什么；query 可按关键词筛选",
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
        "这位用户的私人笔记本：add 记一条、list 列出（置顶在前）、search 按关键词找、delete 删、pin/unpin 置顶。他说“帮我记一下/存一下”且不是关于他本人的长期特征时用这个",
        {
            "action": {"type": "string", "enum": ["add", "list", "search", "delete", "pin", "unpin"]},
            "content": {"type": "string", "minLength": 1, "maxLength": 300},
            "tag": {"type": "string", "minLength": 1, "maxLength": 12, "description": "可选标签，如 书单/密码提示/灵感"},
            "keyword": {"type": "string", "minLength": 1, "maxLength": 30},
            "id": _ID,
            "limit": _LIMIT,
        },
        ("action",),
        writes=True,
    ),
    PrivateTool(
        "todos",
        "这位用户的待办清单：add 加一项（可带截止日期 due 和优先级）、list 列出、done 完成、undo 撤销完成、delete 删除、clear_done 清掉已完成的",
        {
            "action": {"type": "string", "enum": ["add", "list", "done", "undo", "delete", "clear_done"]},
            "content": {"type": "string", "minLength": 1, "maxLength": 120},
            "due": {"type": "string", "maxLength": 10, "description": "截止日期 YYYY-MM-DD"},
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
        "到点在私聊里提醒这位用户：add 设提醒（at 绝对时间或 in_minutes 相对分钟二选一，repeat 可每天/工作日/每周）、list 列出、cancel 取消、snooze 推迟。时间按资料里的 now 推算",
        {
            "action": {"type": "string", "enum": ["add", "list", "cancel", "snooze"]},
            "content": {"type": "string", "minLength": 1, "maxLength": 120, "description": "到时候提醒他的内容"},
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
        "心情记录：log 记下他此刻的心情（score 1很差~5很好，可带一句 note），history 看最近几天的心情和趋势。只在他愿意时记",
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
        "这位用户和你私聊的偏好：get 查看；set 修改他明确提出的项——回复长短、表情多少、希望你怎么称呼他、免打扰时段、每天早安问候时间、签到提醒时间、生日（月-日）、是否接受你主动发消息",
        {
            "action": {"type": "string", "enum": ["get", "set"]},
            "reply_length": {"type": "string", "enum": ["short", "normal", "long"]},
            "emoji": {"type": "string", "enum": ["none", "some", "lots"]},
            "call_me": {"type": "string", "maxLength": 16, "description": "传 off 清除"},
            "quiet_start": _CLOCK,
            "quiet_end": _CLOCK,
            "morning_greeting": _CLOCK,
            "checkin_nudge": {**_CLOCK, "description": "每天几点还没签到就私聊提醒他；off 关闭"},
            "birthday": {"type": "string", "maxLength": 5, "description": "MM-DD，传 off 清除"},
            "proactive": {"type": "boolean", "description": "false 表示除了他自己设的提醒，你都不要主动发消息"},
        },
        ("action",),
        writes=True,
    ),
    PrivateTool(
        "affinity_detail",
        "你和他关系的细节：progress 离下一级还有多远（只有词语）、sources 最近 30 天好感主要来自哪里、history 最近的变化记录",
        {"view": {"type": "string", "enum": ["progress", "sources", "history"]}, "limit": _LIMIT},
    ),
    PrivateTool(
        "checkin_board",
        "某个群今天的签到顺序（前 10 名）、他今天排第几、连签和下一个里程碑",
        {"group": {**_GROUP_SCHEMA, "description": "群名（可以只写一部分）或群号"}},
        ("group",),
    ),
    PrivateTool(
        "toolbox",
        "小工具：calc 算式计算、convert 单位换算（长度/重量/体积/面积/时间/数据/速度/温度）、date_diff 两个日期差几天、date_add 某天加减几天是哪天星期几、dice 掷骰子、coin 抛硬币、pick 帮他从选项里随机选一个、fortune 今日运势（娱乐）、world_time 某城市现在几点。需要精确数字时用它，别心算",
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
        "chat_history",
        "你们的私聊记录：search 按关键词找以前聊过的内容（他问“我之前跟你说过的那个…”时用）、stats 聊了多久多少条",
        {
            "action": {"type": "string", "enum": ["search", "stats"]},
            "keyword": {"type": "string", "minLength": 1, "maxLength": 30},
            "limit": {"type": "integer", "minimum": 1, "maximum": 10},
        },
    ),
    PrivateTool(
        "my_data",
        "他在你这里存了什么：summary 各类数据条数、quota 今天还能和你聊几轮/搜几次、wipe 清空他的全部个人数据和私聊记忆（必须他明确确认后才带 confirm=true）",
        {"action": {"type": "string", "enum": ["summary", "quota", "wipe"]}, "confirm": {"type": "boolean"}},
        writes=True,
    ),
    PrivateTool(
        "feedback",
        "把他对机器人的建议、bug 或投诉转给管理员（每天最多 3 条）。只有他明确想反馈时才用",
        {
            "content": {"type": "string", "minLength": 1, "maxLength": 300},
            "kind": {"type": "string", "enum": ["bug", "suggestion", "complaint", "other"]},
        },
        ("content",),
        writes=True,
    ),
    PrivateTool(
        "web_search",
        "联网搜索（新闻、实时信息、你不确定的事实）。返回标题、摘要和编号 w1、w2…；需要细节再用 web_fetch",
        {"query": {"type": "string", "minLength": 1, "maxLength": 100, "description": "几个关键词"}},
        ("query",),
    ),
    PrivateTool(
        "web_fetch",
        "打开 web_search 结果（编号如 w1）或网址读正文",
        {
            "target": {"type": "string", "minLength": 1, "maxLength": 500},
            "offset": {"type": "integer", "minimum": 0, "maximum": 200000},
        },
        ("target",),
    ),
    PrivateTool(
        "bot_status",
        "（仅超级管理员）机器人运行状态：运行时长、今天私聊人数和回复数、反馈条数",
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
        "把费时间的活放到后台做，你这边马上回他一句：联网查好几处再整理对比、写代码算数据画图、处理他发来的文件、做表格/PPT/文档、写长文，"
        "估计要调三次以上工具或半分钟以上的都适合。只要写 goal；最近的聊天、本轮附件、他的称呼会自动带过去，brief 只补聊天里没有的要求。"
        "做的过程中会用你的口吻给他报进度，做完你收到结果再用自己的话告诉他。对他来说都是你在做，别提后台和小助手",
        {
            "title": {
                "type": "string",
                "minLength": 1,
                "maxLength": 20,
                "description": "短标题，如 查周末天气；不写就从 goal 里截",
            },
            "goal": {
                "type": "string",
                "minLength": 1,
                "maxLength": 600,
                "description": "要交付什么、做到什么程度",
            },
            "brief": {
                "type": "string",
                "maxLength": 1500,
                "description": "小助手需要的背景：他的要求、已知信息、格式偏好、相关文件的 sandbox: 引用",
            },
            "tools": {
                "type": "array",
                "minItems": 1,
                "maxItems": 3,
                "items": {"type": "string", "enum": ["web", "computer", "toolbox"]},
                "description": "web 联网搜索、computer 他的虚拟电脑（算、画图、处理文件）、toolbox 小工具；不写就按情况自动选",
            },
            "save_to_notes": {
                "type": "boolean",
                "description": "他说要存下来时为 true：做完自动存进他的笔记",
            },
            "follow_up_of": {
                "type": "string",
                "maxLength": 12,
                "description": "接着之前做完的哪件事做（tasks 里的编号，如 t3）：他说“展开第二点”“换成表格”时用，上次的结论和文件会带过去",
            },
        },
        ("goal",),
        writes=True,
    ),
    PrivateTool(
        "delegate_project",
        "大活组队做：拆成 2~6 个成员分头干，成员之间能互相发消息、共用他的虚拟电脑，依赖别人结果的成员用 depends_on 等对方做完再开工"
        "（比如 调研A、调研B 并行，汇总 depends_on 两者）。全部做完后你会收到合并的结果再转述。一个人能做完的用 delegate_task",
        {
            "title": {"type": "string", "minLength": 1, "maxLength": 20, "description": "给他看的项目名"},
            "goal": {"type": "string", "minLength": 1, "maxLength": 600, "description": "整个项目要交付什么"},
            "brief": {"type": "string", "maxLength": 1500, "description": "所有成员都需要的背景"},
            "members": {
                "type": "array",
                "minItems": 2,
                "maxItems": 6,
                "description": "成员列表",
                "items": {
                    "type": "object",
                    "properties": {
                        "name": {"type": "string", "maxLength": 12, "description": "成员名，如 调研员/写手/程序员"},
                        "goal": {"type": "string", "maxLength": 600, "description": "这个成员负责交付什么"},
                        "tools": {
                            "type": "array",
                            "items": {"type": "string", "enum": ["web", "computer", "toolbox"]},
                        },
                        "depends_on": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "要等哪些成员（写 name）做完才开工",
                        },
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
        "你在后台做的事：list 看最近在做/做完的和进度；result 看某件事的完整结果（他追问细节时用）；cancel 取消（他说不要了）；"
        "answer 把他的回答转给正在等回答的那件事；message 给正在做的那件补充要求、改方向、回答 helper_messages 里问你的事"
        "（id 写编号/成员名，all 发给所有在做的）",
        {
            "action": {"type": "string", "enum": ["list", "result", "cancel", "answer", "message"]},
            "id": {
                "type": "string",
                "maxLength": 12,
                "description": "任务编号（如 t12）、项目成员名，message 时也可以写 all",
            },
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
        "你在你们私聊里的人设（只在和他的私聊里生效，群里不变）：view 看现在的人设、来源和今天还能改几次；options 看能套的模板；"
        "set 按他的要求改（fields 只写要改的）；preset 套内置模板（name 写模板名）；template 用人设库里的人设（name 写名字）；"
        "reset 回到默认人设；undo 撤销上一次修改；history 看改过什么。只有他明确要你换自称、称呼、说话方式、性格时才改，别自作主张；"
        "改完下一句就按新人设说话",
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
# 只给超级管理员的工具：普通用户的 schema 里看不到。
SUPERUSER_TOOLS = frozenset({"bot_status"})
ALL_TOOLS: tuple[PrivateTool, ...] = (
    PRIVATE_TOOLS + EXTRA_TOOLS + PERSONA_TOOLS + COMPUTER_TOOLS + TASK_TOOLS
)
COMPUTER_NAMES = frozenset(tool.name for tool in COMPUTER_TOOLS)
TASK_NAMES = frozenset(tool.name for tool in TASK_TOOLS)
TOOLS_BY_NAME: dict[str, PrivateTool] = {tool.name: tool for tool in ALL_TOOLS}
WRITE_TOOLS = frozenset(tool.name for tool in ALL_TOOLS if tool.writes)


def tool_schemas(
    *, is_superuser: bool = False, computer: bool = False, tasks: bool = False
) -> list[dict[str, Any]]:
    """本轮给模型的工具；虚拟电脑和小助手任务只在可用时出现。"""

    # 能放后台时把派活工具排在最前面：模型更容易想到“交出去”，而不是自己一路调工具。
    ordered = (
        [*TASK_TOOLS, *(tool for tool in ALL_TOOLS if tool.name not in TASK_NAMES)]
        if tasks
        else list(ALL_TOOLS)
    )
    return [
        tool.schema()
        for tool in ordered
        if (is_superuser or tool.name not in SUPERUSER_TOOLS)
        and (computer or tool.name not in COMPUTER_NAMES)
        and (tasks or tool.name not in TASK_NAMES)
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
    hits = _match_groups(ctx, raw)
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


async def _remembered(ctx: PrivateToolContext) -> dict[str, Any]:
    private = [
        private_memory.fact_view(row)
        for row in await private_memory.list_facts(
            ctx.session, ctx.user_id, now=ctx.now
        )
    ]
    readable = [g for g in ctx.groups if g.agent_readable]
    names = {g.group_id: g.name for g in readable}
    facts = await group_profile_facts(
        ctx.session, ctx.user_id, list(names), now=ctx.now
    )
    return {
        "told_you_in_private": private,
        "learned_in_groups": [
            {
                "key": f["key"],
                "content": f["content"],
                "group": names.get(f["group_id"], ""),
            }
            for f in facts
        ],
    }


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
    if result["changed"] or groups_changed:
        ctx.mutated = True
    payload: dict[str, Any] = {
        "private_removed": result["removed"],
        "private_trimmed": result["trimmed"],
        "changed": bool(result["changed"] or groups_changed),
    }
    if result["protected"]:
        payload["kept_admin_entries"] = result["protected"]
    if args.get("include_groups"):
        payload["groups_changed"] = groups_changed
    return payload


async def _relationship(ctx: PrivateToolContext) -> dict[str, Any]:
    snap = await affinity.snapshot(ctx.session, ctx.user_id, now=ctx.now)
    bonds = await group_bonds(
        ctx.session, ctx.user_id, ctx.bot_id, ctx.groups, now=ctx.now
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
    return {"group": group.name, "summaries": summaries, "empty": not summaries}


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


async def execute_private_tool(
    name: str, raw_args: Any, ctx: PrivateToolContext
) -> dict[str, Any]:
    """执行一个私聊工具，返回给模型的 payload（``ok`` + ``result`` 或错误）。"""

    tool = TOOLS_BY_NAME.get(name)
    if tool is None:
        return {"ok": False, "error": f"没有工具 {name}", "error_code": "invalid_args"}
    from .private_voice import HEAVY_TOOLS

    if name in HEAVY_TOOLS:
        ctx.heavy_calls += 1
    try:
        args = validate_tool_arguments(
            name, tool.properties, tool.required, raw_args or {}
        )
        if name == "my_overview":
            result = await _overview(ctx)
        elif name == "checkin":
            result = await _checkin(ctx, args.get("group"))
        elif name == "checkin_status":
            result = await _checkin_status(ctx, args.get("group"))
        elif name == "list_my_groups":
            result = _list_groups(ctx)
        elif name == "what_you_remember":
            result = await _remembered(ctx)
        elif name == "remember_about_me":
            result = await _remember(ctx, args)
        elif name == "forget_about_me":
            result = await _forget(ctx, args)
        elif name == "our_relationship":
            result = await _relationship(ctx)
        elif name == "group_recent":
            result = await _group_recent(ctx, str(args["group"]))
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
        else:
            result = await _execute_extra(name, args, ctx)
    except Exception as exc:  # noqa: BLE001
        return tool_error_payload(exc)
    ctx.succeeded.add(name)
    if name in _ORGANIZER_ACTIONS and str(args.get("action") or "") in _ORGANIZER_ACTIONS[name]:
        # 声明核验按“工具.动作”区分：查了待办不能说“已经帮你加上了”
        ctx.succeeded.add(f"{name}.{args['action']}")
    return {"ok": True, "result": result}


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
    "reminders": frozenset({"add", "cancel", "snooze"}),
    "countdowns": frozenset({"add", "delete"}),
    "mood": frozenset({"log"}),
    "chat_prefs": frozenset({"set"}),
    "my_data": frozenset({"wipe"}),
    "tasks": frozenset({"cancel", "answer", "message"}),
    "my_persona": frozenset({"set", "preset", "template", "reset", "undo"}),
    "computer_files": frozenset({"delete"}),
}


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
        "affinity_detail": services.affinity_detail,
        "checkin_board": services.checkin_board,
        "chat_history": services.chat_history,
        "my_data": services.my_data,
        "feedback": services.feedback,
        "web_search": services.web_search,
        "web_fetch": services.web_fetch,
        "bot_status": services.bot_status,
    }
    return await handlers[name](ctx, args)


__all__ = [
    "ALL_TOOLS",
    "COMPUTER_NAMES",
    "COMPUTER_TOOLS",
    "EXTRA_TOOLS",
    "PERSONA_TOOLS",
    "PRIVATE_TOOLS",
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
