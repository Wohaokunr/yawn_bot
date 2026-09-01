# Agent Speech P6-P8

## P6: QQ 原生表达选择

- 新增 `speech_native.py`，只从 `current_turn` 已知事实生成 reply/@/reaction 候选，不发明 message_id/user_id。
- reply 场景自动开放 `reply + reaction` segment，并加载 `search_reactions`，让模型可以在真实 reaction_id 和文本之间选择。
- 明确呼叫优先信息完整；reaction 不能替代事实、步骤或风险说明。
- 所有结构化消息仍由 `outbound.py` 做成员、message_id、媒体与 OneBot 能力校验。

## P7: Topic State

`active_topic` 暂时保留为兼容存储标签，Prompt 新增实时计算的 `topic_state`：

- `status`: empty / unknown_age / fresh / cooling / stale
- `continuity`: none / new / continuing / active_cluster
- 最近话题簇消息数、真人参与人数、消息年龄和最近 3 个锚点 message_id

`topic_state` 是 Prompt 中的话题权威表示，不再让一个旧的 `active_topic` 字符串覆盖当前回合。

## P8: 工具结果口语化

- 新增统一 Tool Result Speech Policy。
- role=tool 返回只作为事实，禁止向群友照抄 JSON、字段名、权限/Trace/协议状态。
- 成功只说用户关心的结果；失败只说可公开原因和必要下一步。
- 写操作只有明确成功后才能声称完成；unknown delivery 不能重复发送，也不能断言一定失败。
- 列表结果默认先概括数量，再挑与问题相关的条目。
