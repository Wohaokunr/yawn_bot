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
        "file": {"type": "string", "minLength": 1},
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
        "读取当前群某成员的昵称、角色和头衔",
        {"user_id": {"type": "integer"}},
        required=("user_id",),
        actions=("get_group_member_info",),
        family="member",
        keywords=("群成员", "管理员", "群主", "头衔", "这人是谁", "他是谁", "她是谁"),
    ),
    ToolDefinition(
        "list_group_members",
        "读取当前群成员列表（默认30人，最多50人）",
        {"limit": {"type": "integer", "minimum": 1, "maximum": 50}},
        actions=("get_group_member_list",),
        family="member",
        keywords=("成员列表", "都有谁", "群里有谁", "所有成员"),
    ),
    ToolDefinition(
        "get_message",
        "读取当前群上下文中已知 message_id 对应的消息详情",
        {"message_id": {"type": "integer"}},
        required=("message_id",),
        actions=("get_msg",),
        family="history",
        keywords=("这条消息", "那条消息", "原消息", "刚才那条"),
    ),
    ToolDefinition(
        "get_recent_group_messages",
        "读取当前群最近消息（默认10条，最多30条）",
        {"count": {"type": "integer", "minimum": 1, "maximum": 30}},
        actions=("get_group_msg_history",),
        family="history",
        keywords=(
            "最近消息",
            "聊天记录",
            "消息记录",
            "历史消息",
            "最近聊",
            "刚才聊",
            "群里在聊",
            "大家在聊",
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
                "description": "描述要完成的任务；传‘全部工具’可先查看工具包目录",
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
        "搜索当前群已沉淀的记忆（默认6条，最多10条）",
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
        "给当前群上下文中已知消息添加 QQ 表情回应；emoji_id 必须使用真实已知 ID",
        {
            "message_id": {"type": "integer"},
            "emoji_id": {
                "type": "string",
                "minLength": 1,
                "maxLength": 16,
                "pattern": "^[0-9]+$",
            },
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
        "读取当前群公告列表",
        {},
        actions=("_get_group_notice",),
        family="notice",
        keywords=("群公告", "公告内容", "公告列表", "之前公告", "当前公告"),
    ),
    ToolDefinition(
        "list_essence_messages",
        "读取当前群精华消息列表",
        {},
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
        "get_group_file_link",
        "获取当前群已知群文件的下载链接",
        {
            "file_id": {"type": "string", "minLength": 1, "maxLength": 160},
            "busid": {"type": "integer", "minimum": 0},
        },
        required=("file_id", "busid"),
        actions=("get_group_file_url",),
        family="file",
        keywords=("文件链接", "下载链接", "下载地址", "群文件链接"),
    ),
    ToolDefinition(
        "mute_member",
        "禁言群成员",
        {
            "user_id": {"type": "integer"},
            "duration": {"type": "integer", "minimum": 1, "maximum": 2592000},
        },
        required=("user_id", "duration"),
        actions=("set_group_ban",),
        admin=True,
        permission_level=TOOL_PERMISSION_PRIVILEGED,
        family="moderation",
        keywords=("禁言",),
    ),
    ToolDefinition(
        "create_group_announcement",
        "创建群公告",
        {"content": {"type": "string", "maxLength": 1000}},
        required=("content",),
        actions=("send_group_notice", "_send_group_notice"),
        admin=True,
        permission_level=TOOL_PERMISSION_PRIVILEGED,
        family="notice",
        keywords=("发布公告", "发公告", "创建公告", "写公告"),
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
        keywords=("删除公告", "删公告", "移除公告", "撤掉公告"),
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
    ),
    ToolDefinition(
        "send_file",
        "发送经过安全校验的群文件或文档",
        {
            "file": {"type": "string"},
            "name": {"type": "string", "maxLength": 128},
        },
        required=("file", "name"),
        actions=("upload_group_file",),
        permission_level=TOOL_PERMISSION_PRIVILEGED,
        family="file",
        keywords=("发文件", "发送文件", "上传文件"),
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
MESSAGE_SEND_TOOLS = frozenset({"send_message", "send_forward"})
CORE_DIALOGUE_TOOL_NAMES = frozenset(
    item.name for item in TOOL_DEFINITIONS if item.core
)

__all__ = [
    "ADMIN_TOOLS",
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
]
