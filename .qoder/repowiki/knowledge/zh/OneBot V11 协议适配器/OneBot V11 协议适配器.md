---
kind: external_dependency
name: OneBot V11 协议适配器
slug: onebot-v11
category: external_dependency
category_hints:
    - auth_protocol
scope:
    - '**'
source_files:
    - src/plugins/yawn_core/friend_approve.py
---

通过 nonebot-adapter-onebot 实现 OneBot V11 协议，用于与 QQ 机器人 API 通信。关键接口包括：`send_private_msg` 发送私聊消息、`set_friend_add_request` 处理好友申请（需要 flag 参数）、`get_group_info` 获取群信息。事件类型区分私聊和群聊，群消息事件包含 `group_id`、`sender.card`/`sender.nickname` 等字段。