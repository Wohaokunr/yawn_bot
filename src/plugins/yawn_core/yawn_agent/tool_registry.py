# ruff: noqa: E501
"""Agent tool registry: schema, permissions and OneBot capability metadata."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .outbound import MAX_FORWARD_NODES, MAX_OUTBOUND_SEGMENTS

TOOL_PERMISSION_READ = "read"
TOOL_PERMISSION_STATE_WRITE = "state_write"
TOOL_PERMISSION_MESSAGE_SEND = "message_send"
TOOL_PERMISSION_PRIVILEGED = "privileged"
TOOL_PERMISSION_CRITICAL = "critical"
TOOL_PERMISSION_RANK = {
    TOOL_PERMISSION_READ: 0,
    TOOL_PERMISSION_STATE_WRITE: 1,
    TOOL_PERMISSION_MESSAGE_SEND: 2,
    TOOL_PERMISSION_PRIVILEGED: 3,
    TOOL_PERMISSION_CRITICAL: 4,
}

MESSAGE_SEGMENT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "type": {
            "type": "string",
            "enum": [
                "text",
                "reply",
                "at",
                "face",
                "reaction",
                "image",
                "record",
                "video",
                "rps",
                "dice",
                "poke",
                "share",
                "contact",
                "location",
                "music",
            ],
        },
        "text": {"type": "string", "maxLength": 4000},
        "message_id": {"type": "integer"},
        "user_id": {"type": "integer", "minimum": 1},
        "id": {"type": "integer", "minimum": 0, "maximum": 65535},
        "reaction_id": {"type": "string", "minLength": 1, "maxLength": 64},
        "file": {
            "type": "string",
            "minLength": 1,
            "description": "image/record/video 段的文件：URL、机器人文件库相对名，或代码沙箱产物 sandbox:<路径>",
        },
        "poke_type": {"type": "string", "minLength": 1, "maxLength": 32},
        "poke_id": {"type": "string", "minLength": 1, "maxLength": 32},
        "url": {"type": "string", "minLength": 1, "maxLength": 2048},
        "title": {"type": "string", "minLength": 1, "maxLength": 120},
        "content": {"type": "string", "maxLength": 300},
        "contact_type": {"type": "string", "enum": ["qq", "group"]},
        "latitude": {"type": "number", "minimum": -90, "maximum": 90},
        "longitude": {"type": "number", "minimum": -180, "maximum": 180},
        "provider": {"type": "string", "enum": ["qq", "163", "xm"]},
    },
    "required": ["type"],
    "additionalProperties": False,
}

MESSAGE_SEGMENT_FIELDS: dict[str, frozenset[str]] = {
    "text": frozenset({"text"}),
    "reply": frozenset({"message_id"}),
    "at": frozenset({"user_id"}),
    "face": frozenset({"id"}),
    "reaction": frozenset({"reaction_id"}),
    "image": frozenset({"file"}),
    "record": frozenset({"file"}),
    "video": frozenset({"file"}),
    "rps": frozenset(),
    "dice": frozenset(),
    "poke": frozenset({"poke_type", "poke_id"}),
    "share": frozenset({"url", "title", "content"}),
    "contact": frozenset({"contact_type", "id"}),
    "location": frozenset({"latitude", "longitude", "title", "content"}),
    "music": frozenset({"provider", "id"}),
}


@dataclass(frozen=True, slots=True)
class ToolDefinition:
    name: str
    description: str
    properties: dict[str, Any]
    required: tuple[str, ...] = ()
    actions: tuple[str, ...] = ()
    admin: bool = False
    owner_only: bool = False
    permission_level: str = TOOL_PERMISSION_READ
    family: str = "general"
    keywords: tuple[str, ...] = ()
    core: bool = False
    discoverable: bool = True
    # critical 工具专用：当前触发消息（调用者本人正文）必须包含其中之一，
    # 才能装载 schema 并执行；历史、引用和模型推断均不算显式意图。
    intent_keywords: tuple[str, ...] = ()
    # 给模型的前置依赖提示，随 discover_tools 结果返回；例如“先查 user_id”。
    prerequisites: str = ""


TOOL_DEFINITIONS = (
    ToolDefinition(
        "get_group_info",
        "读取当前群的名称和成员数量等基础信息",
        {},
        actions=("get_group_info",),
        family="group",
        keywords=("群信息", "群资料", "多少人", "群人数"),
    ),
    ToolDefinition(
        "get_group_member",
        "读取当前群某成员的昵称、角色、头衔、等级、入群时间和最后发言时间",
        {"user_id": {"type": "integer", "minimum": 1}},
        required=("user_id",),
        actions=("get_group_member_info",),
        family="member",
        keywords=("群成员", "管理员", "群主", "头衔", "这人是谁", "他是谁", "她是谁", "入群时间", "多久没说话", "最后发言", "等级"),
        prerequisites="只知道昵称时先用 find_group_member 拿 user_id",
    ),
    ToolDefinition(
        "find_group_member",
        "按昵称或群名片模糊查找当前群成员，返回最匹配的 user_id（最多10人）",
        {
            "query": {"type": "string", "minLength": 1, "maxLength": 64, "description": "昵称、群名片或其中一部分"},
            "limit": {"type": "integer", "minimum": 1, "maximum": 10},
        },
        required=("query",),
        actions=("get_group_member_list",),
        family="member",
        keywords=("找人", "找一下", "是哪个", "qq号", "QQ号", "谁叫", "叫什么", "user_id", "是谁", "哪位"),
    ),
    ToolDefinition(
        "list_group_admins",
        "列出当前群的群主和管理员",
        {},
        actions=("get_group_member_list",),
        family="member",
        keywords=("管理员有谁", "谁是管理", "群主是谁", "管理名单", "有哪些管理", "找管理"),
    ),
    ToolDefinition(
        "list_group_members",
        "读取当前群成员列表（默认30人，最多50人），可按角色过滤",
        {
            "limit": {"type": "integer", "minimum": 1, "maximum": 50},
            "role": {"type": "string", "enum": ["owner", "admin", "member"]},
        },
        actions=("get_group_member_list",),
        family="member",
        keywords=("成员列表", "都有谁", "群里有谁", "所有成员"),
    ),
    ToolDefinition(
        "get_message",
        "读取当前群上下文中已知 message_id 对应的消息详情；消息带图片时会把图片一起交给你看（历史里 media_types 含 image、需要看原图时用它）",
        {"message_id": {"type": "integer"}},
        required=("message_id",),
        actions=("get_msg",),
        family="history",
        keywords=("这条消息", "那条消息", "原消息", "刚才那条", "查看消息", "消息内容", "非文本消息", "查看非文本消息内容"),
    ),
    ToolDefinition(
        "get_recent_group_messages",
        "读取当前群最近消息（默认10条，最多30条），可按发言人或关键词过滤",
        {
            "count": {"type": "integer", "minimum": 1, "maximum": 30},
            "user_id": {"type": "integer", "minimum": 1, "description": "只看这位成员的发言"},
            "keyword": {"type": "string", "minLength": 1, "maxLength": 40, "description": "只看包含该词的消息"},
        },
        actions=("get_group_msg_history",),
        family="history",
        keywords=(
            "最近消息",
            "刚才说了",
            "说了什么",
            "聊天记录",
            "消息记录",
            "历史消息",
            "最近聊",
            "刚才聊",
            "群里在聊",
            "大家在聊",
            "查看消息内容",
            "刚才消息内容",
            "非文本消息",
        ),
    ),
    ToolDefinition(
        "discover_tools",
        "渐进披露入口：按任务发现可用工具；也可用 family 加载一个工具包。query/family 至少提供一个；发现只加载 schema，不代表动作已执行",
        {
            "query": {
                "type": "string",
                "minLength": 1,
                "maxLength": 120,
                "description": "描述要完成的任务；传`全部工具`可先查看工具包目录",
            },
            "family": {
                "type": "string",
                "minLength": 1,
                "maxLength": 32,
                "description": "工具包标识；通常来自上一次 discover_tools 返回的 toolpacks.name",
            },
            "limit": {"type": "integer", "minimum": 1, "maximum": 12},
        },
        family="discovery",
        keywords=("工具", "能力", "能不能", "怎么操作"),
        core=True,
        discoverable=False,
    ),
    ToolDefinition(
        "search_group_memory",
        "按关键词或自然语言问题搜索当前群已沉淀的记忆，结果按相关性排序并去重（默认6条，最多10条）",
        {
            "query": {"type": "string", "minLength": 1, "maxLength": 120},
            "limit": {"type": "integer", "minimum": 1, "maximum": 10},
        },
        required=("query",),
        family="memory",
        keywords=("记得", "记忆", "以前", "上次", "之前说", "还记不记得"),
    ),
    ToolDefinition(
        "get_person_profile",
        "读取群内人物画像（默认6条，最多10条）",
        {
            "user_id": {"type": "integer"},
            "limit": {"type": "integer", "minimum": 1, "maximum": 10},
        },
        required=("user_id",),
        family="profile",
        keywords=("画像", "人物资料", "个人资料", "认识他", "认识她", "了解他", "了解她"),
    ),
    ToolDefinition(
        "list_user_relations",
        "查询群内某成员的已知关系（默认12条，最多20条）",
        {
            "user_id": {"type": "integer"},
            "limit": {"type": "integer", "minimum": 1, "maximum": 20},
        },
        required=("user_id",),
        family="profile",
        keywords=("关系", "和谁熟", "朋友是谁", "对象是谁", "同事", "同学"),
    ),
    ToolDefinition(
        "record_user_relation",
        "记录对话中明确观察到的两位成员之间的关系",
        {
            "subject_user_id": {"type": "integer"},
            "object_user_id": {"type": "integer"},
            "type": {
                "type": "string",
                "minLength": 1,
                "maxLength": 32,
                "description": "优先使用：好友/死党/情侣/伴侣/亲属/师徒/同事/同学/搭子/对立",
            },
            "note": {
                "type": "string",
                "maxLength": 200,
                "description": "一句话关系背景，没有可省略",
            },
        },
        required=("subject_user_id", "object_user_id", "type"),
        permission_level=TOOL_PERMISSION_STATE_WRITE,
        family="profile",
        keywords=("记录关系", "记住关系", "我们是", "是我朋友", "是我对象", "是我同事", "是我同学", "是我搭子", "是我死党", "是我伴侣"),
    ),
    ToolDefinition(
        "get_my_relationship",
        "查看你（Bot）和某位成员之间的关系：熟悉程度、他怎么叫你、你们的梗、他的边界和共同经历；不传 user_id 时查当前发言人",
        {
            "user_id": {"type": "integer"},
        },
        family="profile",
        keywords=("我们熟吗", "你认识我吗", "你记得我吗", "我们的关系", "你和他熟吗", "我们之间"),
    ),
    ToolDefinition(
        "note_our_bond",
        "按调用者本人的明确要求，记住他和你（Bot）之间的一件事：他怎么称呼你、希望你怎么称呼他、你们的梗、他不希望你做的事、一段共同经历（只能写调用者自己与你的关系）",
        {
            "kind": {
                "type": "string",
                "enum": [
                    "member_calls_bot",
                    "bot_calls_member",
                    "inside_joke",
                    "boundary",
                    "moment",
                ],
                "description": "member_calls_bot他对你的称呼/bot_calls_member他希望你怎么叫他/inside_joke你们的梗/boundary他不希望你做的事/moment值得记住的共同经历",
            },
            "content": {"type": "string", "minLength": 1, "maxLength": 100},
        },
        required=("kind", "content"),
        permission_level=TOOL_PERMISSION_STATE_WRITE,
        family="memory",
        keywords=("记住我们", "我们的梗", "以后叫你", "以后你叫我", "别再叫我", "别对我"),
    ),
    ToolDefinition(
        "remember_about_me",
        "按调用者本人的明确要求记住关于他自己的一条事实（只能写调用者自己的资料）；敏感信息和联系方式会被拒绝",
        {
            "key": {
                "type": "string",
                "enum": [
                    "display_name",
                    "preferred_address",
                    "hobby",
                    "preference",
                    "dislike",
                    "skill",
                    "current_status",
                ],
                "description": "display_name昵称/preferred_address希望的称呼/hobby爱好/preference喜好/dislike讨厌的/skill技能/current_status近期状态（两周后遗忘）",
            },
            "content": {"type": "string", "minLength": 1, "maxLength": 100},
        },
        required=("key", "content"),
        permission_level=TOOL_PERMISSION_STATE_WRITE,
        family="memory",
        keywords=("记住我", "记一下我", "帮我记", "别忘了我", "以后叫我", "以后喊我", "我的爱好是", "我最近在"),
    ),
    ToolDefinition(
        "forget_about_me",
        "按调用者本人的要求忘掉关于他自己的记忆；可按类别或关键词只删一部分，不提供则清除全部自身画像",
        {
            "key": {
                "type": "string",
                "enum": [
                    "display_name",
                    "preferred_address",
                    "hobby",
                    "preference",
                    "dislike",
                    "skill",
                    "recurring_topic",
                    "current_status",
                    "bot_bond",
                ],
                "description": "bot_bond 表示忘掉他和你（Bot）之间的关系与共同经历",
            },
            "keyword": {"type": "string", "minLength": 1, "maxLength": 40},
        },
        permission_level=TOOL_PERMISSION_STATE_WRITE,
        family="memory",
        keywords=("忘掉我", "忘了我", "别记", "不要记", "删掉我的", "删除我的记忆"),
    ),
    ToolDefinition(
        "search_reactions",
        "按情绪或场景标签搜索本地表情包库，返回 reaction_id",
        {
            "query": {"type": "string", "minLength": 1, "maxLength": 80},
            "limit": {"type": "integer", "minimum": 1, "maximum": 8},
        },
        required=("query",),
        family="reaction",
        keywords=("表情包", "reaction", "无语", "吃瓜", "震惊"),
    ),
    ToolDefinition(
        "send_message",
        "发送当前群的一条结构化消息；成功后不要重复发送同样最终文本",
        {
            "segments": {
                "type": "array",
                "minItems": 1,
                "maxItems": MAX_OUTBOUND_SEGMENTS,
                "items": MESSAGE_SEGMENT_SCHEMA,
            }
        },
        required=("segments",),
        actions=("send_group_msg",),
        permission_level=TOOL_PERMISSION_MESSAGE_SEND,
        family="message",
        keywords=("回复", "引用", "艾特", "表情", "图片", "发图", "语音", "视频", "骰子", "猜拳", "戳一戳", "poke", "分享", "链接", "名片", "位置", "定位", "音乐", "歌曲"),
        core=True,
    ),
    ToolDefinition(
        "react_to_message",
        "给当前群上下文中已知消息添加或取消 QQ 表情回应；emoji_id 必须使用真实已知 ID",
        {
            "message_id": {"type": "integer"},
            "emoji_id": {
                "type": "string",
                "minLength": 1,
                "maxLength": 16,
                "pattern": "^[0-9]+$",
            },
            "remove": {"type": "boolean", "description": "true 表示取消之前的表情回应"},
        },
        required=("message_id", "emoji_id"),
        actions=("set_msg_emoji_like",),
        permission_level=TOOL_PERMISSION_MESSAGE_SEND,
        family="reaction",
        keywords=("回应表情", "贴表情", "点个表情", "表情回应", "reaction"),
    ),
    ToolDefinition(
        "send_forward",
        "发送受控合并转发；message 节点只允许引用当前群近期已知 message_id",
        {
            "nodes": {
                "type": "array",
                "minItems": 1,
                "maxItems": MAX_FORWARD_NODES,
                "items": {
                    "type": "object",
                    "properties": {
                        "type": {"type": "string", "enum": ["message", "custom"]},
                        "message_id": {"type": "integer"},
                        "user_id": {"type": "integer", "minimum": 1},
                        "content": {"type": "string", "minLength": 1, "maxLength": 4000},
                    },
                    "required": ["type"],
                    "additionalProperties": False,
                },
            }
        },
        required=("nodes",),
        actions=("send_group_forward_msg",),
        permission_level=TOOL_PERMISSION_MESSAGE_SEND,
        family="message",
        keywords=("合并转发", "转发"),
    ),
    ToolDefinition(
        "list_group_notices",
        "读取当前群公告列表（按发布时间倒序，默认10条），可按关键词过滤",
        {
            "limit": {"type": "integer", "minimum": 1, "maximum": 20},
            "keyword": {"type": "string", "minLength": 1, "maxLength": 40},
        },
        actions=("_get_group_notice",),
        family="notice",
        keywords=("公告内容", "公告列表", "之前公告", "当前公告", "看公告", "查公告"),
    ),
    ToolDefinition(
        "list_essence_messages",
        "读取当前群精华消息列表（默认20条，最多30条）",
        {"limit": {"type": "integer", "minimum": 1, "maximum": 30}},
        actions=("get_essence_msg_list",),
        family="essence",
        keywords=("精华消息", "群精华", "精华列表", "哪些精华"),
    ),
    ToolDefinition(
        "list_muted_members",
        "读取当前群正在禁言的成员（最多50人）",
        {"limit": {"type": "integer", "minimum": 1, "maximum": 50}},
        actions=("get_group_shut_list",),
        family="moderation",
        keywords=("禁言列表", "谁被禁言", "还在禁言", "正在禁言"),
    ),
    ToolDefinition(
        "get_group_honor",
        "读取当前群荣誉信息，例如龙王、群聊之火等",
        {
            "type": {
                "type": "string",
                "enum": ["all", "talkative", "performer", "legend", "strong_newbie", "emotion"],
            }
        },
        actions=("get_group_honor_info",),
        family="group",
        keywords=("群荣誉", "龙王", "群聊之火", "群聊炽焰", "冒尖小春笋", "快乐源泉"),
    ),
    ToolDefinition(
        "list_group_files",
        "列出当前群根目录或指定文件夹中的群文件（最多30项）",
        {
            "folder_id": {"type": "string", "minLength": 1, "maxLength": 160},
            "limit": {"type": "integer", "minimum": 1, "maximum": 30},
        },
        actions=("get_group_root_files", "get_group_files_by_folder"),
        family="file",
        keywords=("群文件", "文件列表", "文件夹", "找文件", "有什么文件"),
    ),
    ToolDefinition(
        "search_group_files",
        "按文件名关键词搜索当前群文件（根目录和一级文件夹，最多20项）",
        {
            "keyword": {"type": "string", "minLength": 1, "maxLength": 80},
            "limit": {"type": "integer", "minimum": 1, "maximum": 20},
        },
        required=("keyword",),
        actions=("get_group_root_files", "get_group_files_by_folder"),
        family="file",
        keywords=("搜文件", "搜索文件", "找文件", "有没有那个文件", "文件在哪", "哪个文件"),
    ),
    ToolDefinition(
        "get_group_file_space",
        "读取当前群文件的容量、已用空间和文件数",
        {},
        actions=("get_group_file_system_info",),
        family="file",
        keywords=("群文件空间", "文件容量", "还剩多少空间", "空间满", "文件数量"),
    ),
    ToolDefinition(
        "list_agent_files",
        "列出机器人文件库里可以用 send_file 发送的文件（只返回相对文件名）",
        {
            "keyword": {"type": "string", "minLength": 1, "maxLength": 80},
            "limit": {"type": "integer", "minimum": 1, "maximum": 50},
        },
        family="file",
        keywords=("文件库", "能发什么文件", "可以发的文件", "有哪些文件可以发", "发文件", "发个文件", "资料包"),
    ),
    ToolDefinition(
        "get_group_file_link",
        "获取当前群已知群文件的下载链接",
        {
            "file_id": {"type": "string", "minLength": 1, "maxLength": 160},
            "busid": {"type": "integer", "minimum": 0, "description": "来自文件列表；省略时用 102"},
        },
        required=("file_id",),
        actions=("get_group_file_url",),
        family="file",
        keywords=("文件链接", "下载链接", "下载地址", "群文件链接"),
    ),
    ToolDefinition(
        "mute_member",
        "禁言群成员；duration 单位是秒（10分钟=600，1小时=3600，1天=86400）",
        {
            "user_id": {"type": "integer", "minimum": 1},
            "duration": {"type": "integer", "minimum": 1, "maximum": 2592000},
        },
        required=("user_id", "duration"),
        actions=("set_group_ban",),
        admin=True,
        permission_level=TOOL_PERMISSION_PRIVILEGED,
        family="moderation",
        keywords=("禁言", "闭嘴", "关小黑屋"),
        prerequisites="只知道昵称时先用 find_group_member 拿 user_id",
    ),
    ToolDefinition(
        "unmute_member",
        "解除群成员禁言；白名单开启禁言时自动可用",
        {"user_id": {"type": "integer", "minimum": 1}},
        required=("user_id",),
        actions=("set_group_ban",),
        admin=True,
        permission_level=TOOL_PERMISSION_PRIVILEGED,
        family="moderation",
        keywords=("解除禁言", "解禁", "取消禁言", "放出来", "解开禁言"),
        prerequisites="不确定谁在禁言时先用 list_muted_members",
    ),
    ToolDefinition(
        "recall_message",
        "撤回当前群一条近期消息；撤回他人消息需要机器人是管理员",
        {"message_id": {"type": "integer"}},
        required=("message_id",),
        actions=("delete_msg",),
        admin=True,
        permission_level=TOOL_PERMISSION_PRIVILEGED,
        family="moderation",
        keywords=("撤回", "撤回消息", "撤掉这条", "删掉这条消息", "把这条撤了"),
        prerequisites="message_id 必须来自当前上下文、引用消息或 get_recent_group_messages",
    ),
    ToolDefinition(
        "recall_bot_message",
        "撤回机器人自己 2 分钟内在当前群发的消息；不传 message_id 时撤回最近一条",
        {"message_id": {"type": "integer"}},
        actions=("delete_msg",),
        permission_level=TOOL_PERMISSION_PRIVILEGED,
        family="moderation",
        keywords=("撤回你刚才", "撤回刚才", "你撤回", "把你那条撤了", "撤回你的消息"),
    ),
    ToolDefinition(
        "create_group_announcement",
        "创建群公告；content 写成书面、完整的公告正文，不带角色口癖和 Markdown",
        {
            "content": {"type": "string", "minLength": 1, "maxLength": 1000},
            "pinned": {"type": "boolean", "description": "是否置顶"},
            "confirm_required": {"type": "boolean", "description": "是否需要群成员确认收到"},
            "show_edit_card": {"type": "boolean", "description": "是否引导成员修改群名片"},
        },
        required=("content",),
        actions=("send_group_notice", "_send_group_notice"),
        admin=True,
        permission_level=TOOL_PERMISSION_PRIVILEGED,
        family="notice",
        keywords=(
            "发布公告", "发公告", "创建公告", "写公告", "置顶公告", "贴公告", "出个公告",
            "发群公告", "发个通知", "群通知", "新公告", "notice", "announcement",
        ),
        prerequisites="发布前确认公告内容；重复内容会被拒绝",
    ),
    ToolDefinition(
        "set_essence_message",
        "把当前群消息设为精华消息",
        {"message_id": {"type": "integer"}},
        required=("message_id",),
        actions=("set_essence_msg",),
        admin=True,
        permission_level=TOOL_PERMISSION_PRIVILEGED,
        family="essence",
        keywords=("设为精华", "加精", "设置精华", "加入精华"),
    ),
    ToolDefinition(
        "remove_essence_message",
        "把当前群消息移出精华消息",
        {"message_id": {"type": "integer"}},
        required=("message_id",),
        actions=("delete_essence_msg",),
        admin=True,
        permission_level=TOOL_PERMISSION_PRIVILEGED,
        family="essence",
        keywords=("取消精华", "移出精华", "删除精华", "撤销精华"),
    ),
    ToolDefinition(
        "delete_group_notice",
        "删除当前群的一条群公告",
        {"notice_id": {"type": "string", "minLength": 1, "maxLength": 128}},
        required=("notice_id",),
        actions=("_del_group_notice",),
        admin=True,
        permission_level=TOOL_PERMISSION_PRIVILEGED,
        family="notice",
        keywords=("删除公告", "删公告", "移除公告", "撤掉公告", "删群公告", "撤公告"),
        prerequisites="先用 list_group_notices 找到 notice_id",
    ),
    ToolDefinition(
        "set_group_card",
        "修改当前群某成员的群名片",
        {
            "user_id": {"type": "integer", "minimum": 1},
            "card": {"type": "string", "maxLength": 80},
        },
        required=("user_id", "card"),
        actions=("set_group_card",),
        admin=True,
        permission_level=TOOL_PERMISSION_PRIVILEGED,
        family="member",
        keywords=("改群名片", "修改群名片", "设置群名片", "改名片"),
    ),
    ToolDefinition(
        "set_special_title",
        "设置当前群成员的专属头衔；机器人需要群主权限",
        {
            "user_id": {"type": "integer", "minimum": 1},
            "special_title": {"type": "string", "maxLength": 80},
        },
        required=("user_id", "special_title"),
        actions=("set_group_special_title",),
        admin=True,
        owner_only=True,
        permission_level=TOOL_PERMISSION_PRIVILEGED,
        family="member",
        keywords=("专属头衔", "设置头衔", "改头衔"),
    ),
    ToolDefinition(
        "set_group_name",
        "修改当前群名称",
        {"group_name": {"type": "string", "minLength": 1, "maxLength": 100}},
        required=("group_name",),
        actions=("set_group_name",),
        admin=True,
        permission_level=TOOL_PERMISSION_PRIVILEGED,
        family="group",
        keywords=("改群名", "修改群名", "设置群名", "群名称改成"),
    ),
    ToolDefinition(
        "create_group_folder",
        "在当前群文件中创建文件夹",
        {"name": {"type": "string", "minLength": 1, "maxLength": 120}},
        required=("name",),
        actions=("create_group_file_folder",),
        admin=True,
        permission_level=TOOL_PERMISSION_PRIVILEGED,
        family="file",
        keywords=("创建群文件夹", "新建群文件夹", "新建文件夹"),
    ),
    ToolDefinition(
        "kick_member",
        "把当前群成员移出群聊；高风险操作",
        {
            "user_id": {"type": "integer", "minimum": 1},
            "reject_add_request": {"type": "boolean"},
        },
        required=("user_id",),
        actions=("set_group_kick",),
        admin=True,
        permission_level=TOOL_PERMISSION_CRITICAL,
        family="moderation",
        keywords=("踢出群", "踢出去", "移出群", "踢掉", "踢人"),
        discoverable=False,
        intent_keywords=("踢", "移出"),
    ),
    ToolDefinition(
        "set_whole_group_mute",
        "开启或关闭当前群全员禁言；高风险操作",
        {"enable": {"type": "boolean"}},
        required=("enable",),
        actions=("set_group_whole_ban",),
        admin=True,
        permission_level=TOOL_PERMISSION_CRITICAL,
        family="moderation",
        keywords=("全员禁言", "解除全员禁言", "关闭全员禁言"),
        discoverable=False,
        intent_keywords=("全员禁言",),
    ),
    ToolDefinition(
        "set_group_admin",
        "设置或取消当前群管理员；仅机器人为群主时可执行，高风险操作",
        {
            "user_id": {"type": "integer", "minimum": 1},
            "enable": {"type": "boolean"},
        },
        required=("user_id", "enable"),
        actions=("set_group_admin",),
        admin=True,
        owner_only=True,
        permission_level=TOOL_PERMISSION_CRITICAL,
        family="moderation",
        keywords=("设为管理员", "设置管理员", "取消管理员", "撤销管理员"),
        discoverable=False,
        intent_keywords=("管理员",),
    ),
    ToolDefinition(
        "delete_group_file",
        "删除当前群文件；高风险操作",
        {
            "file_id": {"type": "string", "minLength": 1, "maxLength": 160},
            "busid": {"type": "integer", "minimum": 0},
        },
        required=("file_id", "busid"),
        actions=("delete_group_file",),
        admin=True,
        permission_level=TOOL_PERMISSION_CRITICAL,
        family="file",
        keywords=("删除群文件", "删群文件", "删除这个文件"),
        discoverable=False,
        intent_keywords=("删",),
    ),
    ToolDefinition(
        "move_group_file",
        "移动当前群文件到指定目录；高风险操作",
        {
            "file_id": {"type": "string", "minLength": 1, "maxLength": 160},
            "target_dir": {"type": "string", "minLength": 1, "maxLength": 160},
        },
        required=("file_id", "target_dir"),
        actions=("move_group_file",),
        admin=True,
        permission_level=TOOL_PERMISSION_CRITICAL,
        family="file",
        keywords=("移动群文件", "把文件移到", "移动这个文件"),
        discoverable=False,
        intent_keywords=("移动", "移到", "挪到"),
    ),
    ToolDefinition(
        "rename_group_file",
        "重命名当前群文件；高风险操作",
        {
            "file_id": {"type": "string", "minLength": 1, "maxLength": 160},
            "current_parent_directory": {"type": "string", "maxLength": 160},
            "new_name": {"type": "string", "minLength": 1, "maxLength": 160},
        },
        required=("file_id", "current_parent_directory", "new_name"),
        actions=("rename_group_file",),
        admin=True,
        permission_level=TOOL_PERMISSION_CRITICAL,
        family="file",
        keywords=("重命名群文件", "群文件改名", "重命名这个文件"),
        discoverable=False,
        intent_keywords=("重命名", "改名"),
    ),
    ToolDefinition(
        "delete_group_folder",
        "删除当前群文件夹；高风险操作",
        {"folder_id": {"type": "string", "minLength": 1, "maxLength": 160}},
        required=("folder_id",),
        actions=("delete_group_folder",),
        admin=True,
        permission_level=TOOL_PERMISSION_CRITICAL,
        family="file",
        keywords=("删除群文件夹", "删群文件夹", "删除这个文件夹"),
        discoverable=False,
        intent_keywords=("删",),
    ),
    ToolDefinition(
        "run_python",
        "在本群的代码沙箱里运行一段 Python 3 代码（无网络，限时限内存）；可做计算、数据分析、画图（matplotlib 图自动存成 PNG）、生成 Excel/CSV/音乐。返回输出和新产物的 sandbox: 引用",
        {
            "code": {
                "type": "string",
                "minLength": 1,
                "maxLength": 20000,
                "description": "完整可运行的 Python 代码；最后一行表达式的值会自动显示。文件写到当前目录，工作区在同群的多次运行之间保留",
            },
            "purpose": {"type": "string", "maxLength": 80, "description": "一句话说明这段代码要做什么，会记进运行历史"},
            "timeout_seconds": {"type": "integer", "minimum": 1, "maximum": 90, "description": "省略用管理员设置的默认时限"},
        },
        required=("code",),
        permission_level=TOOL_PERMISSION_STATE_WRITE,
        family="sandbox",
        keywords=("运行代码", "跑代码", "跑一下", "执行代码", "python", "Python", "算一下", "计算", "画图", "绘图", "图表", "统计", "数据分析", "表格", "excel", "Excel", "生成文件", "作曲", "写首歌", "生成音乐", "旋律", "midi", "MIDI", "二维码", "词云", "沙箱", "虚拟机"),
        prerequisites="预装 numpy/pandas/scipy/matplotlib/seaborn/sympy/networkx/pillow/openpyxl/mido/jieba/wordcloud/qrcode 和 yawnkit（music/charts/files）；不能联网、不能 pip install",
    ),
    ToolDefinition(
        "run_shell",
        "在本群的代码沙箱里用 bash 运行终端命令（与 run_python 共用同一个工作区，无网络）；适合 ls/cat/head/tail/grep/find/wc/sort/uniq/jq/unzip 查看和整理文件，也能 python3 xxx.py 跑脚本。返回输出和新产物的 sandbox: 引用",
        {
            "command": {
                "type": "string",
                "minLength": 1,
                "maxLength": 20000,
                "description": "bash 命令或多行脚本；每次都从工作区根目录开始（cd 只在本次有效），开了 pipefail",
            },
            "purpose": {"type": "string", "maxLength": 80, "description": "一句话说明这条命令要做什么，会记进运行历史"},
            "timeout_seconds": {"type": "integer", "minimum": 1, "maximum": 90, "description": "省略用管理员设置的默认时限"},
        },
        required=("command",),
        permission_level=TOOL_PERMISSION_STATE_WRITE,
        family="sandbox",
        keywords=("终端", "命令行", "shell", "bash", "grep", "cat", "ls", "查找文件", "解压", "看看文件", "统计行数", "jq"),
        prerequisites="和 run_python 同一个群工作区；不能联网、不能 pip/apt 安装；没有 sudo",
    ),
    ToolDefinition(
        "list_sandbox_files",
        "列出本群代码沙箱工作区里的文件（含之前运行生成的产物）",
        {
            "prefix": {"type": "string", "maxLength": 200, "description": "只列出这个目录/前缀下的文件"},
            "limit": {"type": "integer", "minimum": 1, "maximum": 50},
        },
        family="sandbox",
        keywords=("沙箱文件", "生成了什么", "上次的图", "之前的文件", "工作区"),
    ),
    ToolDefinition(
        "read_sandbox_file",
        "读取本群代码沙箱里文本/表格类文件的内容（图片音频只返回元信息）",
        {
            "path": {"type": "string", "minLength": 1, "maxLength": 512, "description": "工作区相对路径或 sandbox: 引用"},
            "max_chars": {"type": "integer", "minimum": 200, "maximum": 6000},
        },
        required=("path",),
        family="sandbox",
        keywords=("看看文件内容", "读一下结果", "打开文件"),
    ),
    ToolDefinition(
        "delete_sandbox_file",
        "删除本群代码沙箱里的一个文件或空目录",
        {"path": {"type": "string", "minLength": 1, "maxLength": 512, "description": "工作区相对路径或 sandbox: 引用"}},
        required=("path",),
        permission_level=TOOL_PERMISSION_STATE_WRITE,
        family="sandbox",
        keywords=("删掉沙箱文件", "清理沙箱", "删除生成的文件"),
    ),
    ToolDefinition(
        "sandbox_info",
        "查看代码沙箱的状态、预装库、时限与本群/本人今日剩余次数",
        {},
        family="sandbox",
        keywords=("沙箱状态", "能跑代码吗", "装了什么库", "还能跑几次"),
    ),
    ToolDefinition(
        "import_attachments",
        "把群成员发的图片/文件/语音/视频放进本群内置电脑（沙箱工作区 inbox/），返回每个附件的路径，之后用 run_python/run_shell 处理",
        {
            "ids": {
                "type": "array",
                "items": {"type": "string", "minLength": 1, "maxLength": 32},
                "maxItems": 20,
                "description": '附件 id：本条消息的媒体序号（"1"、"2"…）或 list_chat_attachments 返回的 id；省略=本条消息的全部附件，本条没有时取发起人最近发的那条',
            },
        },
        permission_level=TOOL_PERMISSION_STATE_WRITE,
        family="sandbox",
        keywords=("附件", "这张图", "这个文件", "这段语音", "处理图片", "处理文件", "分析表格", "放进电脑", "传进沙箱", "导入文件", "inbox"),
        prerequisites="附件可能已经自动入箱：先看 sandbox_inbox 提示或 list_chat_attachments 的 in_workspace，已经在的不用再导入",
    ),
    ToolDefinition(
        "list_chat_attachments",
        "列出本条消息和群里最近一段时间成员发过的图片/文件/语音/视频（id、类型、文件名、谁发的、是否已在内置电脑里）",
        {
            "mine_only": {"type": "boolean", "description": "只看发起人自己发的附件"},
            "limit": {"type": "integer", "minimum": 1, "maximum": 30},
        },
        family="sandbox",
        keywords=("刚才的文件", "刚发的图", "之前发的", "最近的附件", "群里发的文件", "哪个文件"),
    ),
    ToolDefinition(
        "import_group_file",
        "把当前群「群文件」里的一个文件下载进本群内置电脑（沙箱工作区 inbox/），用于分析群文件里的表格、文本、压缩包等",
        {
            "file_id": {"type": "string", "minLength": 1, "maxLength": 160, "description": "search_group_files 返回的 file_id"},
            "name": {"type": "string", "minLength": 1, "maxLength": 160, "description": "文件名（带后缀），决定工作区里的文件名"},
            "busid": {"type": "integer", "minimum": 0},
        },
        required=("file_id",),
        actions=("get_group_file_url",),
        permission_level=TOOL_PERMISSION_STATE_WRITE,
        family="sandbox",
        keywords=("群文件", "分析群文件", "下载群文件", "群文件里的表格"),
        prerequisites="先用 search_group_files 拿到 file_id 和文件名",
    ),
    ToolDefinition(
        "send_file",
        "把机器人文件库里的文件、代码沙箱产物（sandbox: 引用）或白名单域名的链接上传到当前群文件",
        {
            "file": {
                "type": "string",
                "minLength": 1,
                "maxLength": 2048,
                "description": "list_agent_files 返回的相对文件名、run_python 返回的 sandbox:<路径>，或白名单域名 URL",
            },
            "name": {"type": "string", "minLength": 1, "maxLength": 128, "description": "群文件里显示的文件名"},
            "folder_id": {"type": "string", "minLength": 1, "maxLength": 160, "description": "上传到哪个群文件夹；省略为根目录"},
        },
        required=("file", "name"),
        actions=("upload_group_file",),
        permission_level=TOOL_PERMISSION_PRIVILEGED,
        family="file",
        keywords=("发文件", "发送文件", "上传文件", "传文件", "发个文件", "传到群文件"),
        prerequisites="先用 list_agent_files 拿到真实文件名，不要猜路径",
    ),
)

TOOL_BY_NAME = {item.name: item for item in TOOL_DEFINITIONS}
ADMIN_TOOLS = frozenset(item.name for item in TOOL_DEFINITIONS if item.admin)
PRIVILEGED_TOOLS = frozenset(
    item.name
    for item in TOOL_DEFINITIONS
    if item.permission_level == TOOL_PERMISSION_PRIVILEGED
)
CRITICAL_TOOLS = frozenset(
    item.name
    for item in TOOL_DEFINITIONS
    if item.permission_level == TOOL_PERMISSION_CRITICAL
)
CONTROLLED_TOOLS = PRIVILEGED_TOOLS | CRITICAL_TOOLS
# 白名单别名：开启左侧任一工具即视为允许该工具（解除禁言随禁言开放）。
ALLOWLIST_ALIASES: dict[str, tuple[str, ...]] = {
    "unmute_member": ("mute_member",),
    "recall_bot_message": ("recall_message",),
}


def allowlist_covers(name: str, allowlist: set[str] | frozenset[str] | None) -> bool:
    """群级白名单是否允许 ``name``；None 表示调用方不做白名单过滤。"""

    if allowlist is None:
        return True
    if name in allowlist:
        return True
    return any(alias in allowlist for alias in ALLOWLIST_ALIASES.get(name, ()))


def tool_argument_summary(definition: ToolDefinition) -> dict[str, list[str]]:
    """紧凑参数说明：必填/可选参数名带类型，随 discover_tools 返回给模型。"""

    def _render(key: str) -> str:
        schema = definition.properties.get(key) or {}
        kind = str(schema.get("type") or "any")
        enum = schema.get("enum")
        if enum:
            kind = "|".join(str(item) for item in enum)
        return f"{key}:{kind}"

    required = [_render(key) for key in definition.required]
    optional = [
        _render(key) for key in definition.properties if key not in definition.required
    ]
    summary: dict[str, list[str]] = {}
    if required:
        summary["required"] = required
    if optional:
        summary["optional"] = optional
    return summary
MESSAGE_SEND_TOOLS = frozenset({"send_message", "send_forward"})
CORE_DIALOGUE_TOOL_NAMES = frozenset(
    item.name for item in TOOL_DEFINITIONS if item.core
)

__all__ = [
    "ADMIN_TOOLS",
    "ALLOWLIST_ALIASES",
    "CONTROLLED_TOOLS",
    "CORE_DIALOGUE_TOOL_NAMES",
    "CRITICAL_TOOLS",
    "MESSAGE_SEGMENT_FIELDS",
    "MESSAGE_SEGMENT_SCHEMA",
    "MESSAGE_SEND_TOOLS",
    "PRIVILEGED_TOOLS",
    "TOOL_BY_NAME",
    "TOOL_DEFINITIONS",
    "TOOL_PERMISSION_CRITICAL",
    "TOOL_PERMISSION_MESSAGE_SEND",
    "TOOL_PERMISSION_PRIVILEGED",
    "TOOL_PERMISSION_RANK",
    "TOOL_PERMISSION_READ",
    "TOOL_PERMISSION_STATE_WRITE",
    "ToolDefinition",
    "allowlist_covers",
    "tool_argument_summary",
]
