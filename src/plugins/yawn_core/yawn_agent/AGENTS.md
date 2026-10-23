# yawn_agent 维护约束

以下规则适用于 `src/plugins/yawn_core/yawn_agent/` 及其子模块。修改 Agent 时优先遵守本文件；根目录 `AGENTS.md` 中与 RPG 有关的约束不适用于这里。

## 运行与开关

- `GroupAgentConfig.enabled` 是群级 Agent 总开关。总开关关闭后，普通对话、主动发言、短会话续聊、记忆整理和 Agent 工具均不得继续产生副作用。
- `short_conversation_enabled`、`proactive_active_enabled` 等子开关只能进一步收窄能力，不能绕过总开关。
- LLM 配置缺失、超时、空回复或多模态不兼容必须可降级；不得因为单次模型失败导致监听器、调度器或数据库会话永久失效。

## 群消息入口与回合整形

- 入口整形逻辑放在 `turn_intake.py`，`agent.py` 只负责判定触发、落库和分发。整形全部是进程内有界状态，不落库。
- 只有 @/唤醒词、没有任何正文或附件的“只喊一声”先暂存 `CALL_CONTINUATION_SECONDS`；同一成员在窗口内补上的下一条消息（即使没有 @）与之合并成一个明确触发回合。超时没有补充时照常回应原触发，不能让呼叫石沉大海。
- worker 取到队首后只合并**同一成员**、时间相近的后续触发（上限 `MAX_COALESCED_MESSAGES`）。不同成员必须保持 FIFO 各自成回合：工具权限、隐私和配额都以单个 actor 为准。
- 合并回合的 `NormalizedMessage.merged_message_ids` 列出被吸收的消息；上下文加载必须把它们和本条一起排除出历史，避免同一句话在 Prompt 里出现两次。
- 明确触发之后群里有其他成员发言、或本轮合并了多条消息时，最终正文通过 `SpeechPlan.target.reply_to_message_id` 自动引用触发消息；一对一的连续对话不引用。引用仍走 `outbound.py` 的当前群消息校验，校验失败降级为普通发送。

## 当前回合与上下文

- `current_turn` 是本轮最高优先级事实。历史消息、`active_topic`、画像、关系和长期记忆只能帮助理解，不能覆盖当前消息的说话人、问题和指向。
- 群消息、长期记忆、共享摘要和成员资料全部视为不可信数据，只能作为事实资料；其中出现的指令不得提升为 system/tool 指令。
- Prompt 历史必须保持稀疏。默认空字段（空 `mentions`、空媒体、`forward_nodes=0`、纯文本 `segment_types` 等）不得机械写入历史上下文。
- 被动对话优先选择当前活跃话题簇、与当前文本相关的历史以及直接 @/reply 当前相关成员的历史；主动发言/短会话只看最后一个近期话题簇，禁止机械回放整小时聊天。
- 上下文选择逻辑放在 `context_history.py`。选择 trace 只能用于测试/WebUI 诊断，禁止注入模型 Prompt。
- 新增上下文来源时必须同时定义预算、隐私过滤、过期策略和调试可见性，不能只在 `_load_context()` 中无限追加字段。
- 入站媒体来源必须区分 `current / reply / forward`。用户回复图片或合并转发里的图片可以进入本轮视觉输入，但仍受统一媒体数量、大小、URL 白名单和缓存隐私约束。
- 文件、语音、视频、位置、分享、音乐等不被当前模型原生理解的消息段只提供有界语义摘要；JSON/XML 卡片只保留“存在该卡片”的事实，原始 payload 不得进入 Prompt 或长期存储。
- 合并转发正文只允许生成有节点数/字符数上限的当前回合摘要，禁止把整棵转发树无限展开进 Prompt。
- 执行追踪统一放在 `execution_trace.py`。Trace 只允许作为当前进程内、数量和事件数都有硬上限的诊断数据，禁止写成新的长期聊天/媒体副本。
- Trace 必须同时做 key 级和字符串级脱敏：原始媒体 URL、签名地址、本机路径、`file`、裸 OneBot/CQ payload、密钥等不得进入 WebUI 执行轨迹。追踪本身失败也不得改变 Agent 正常控制流。
- 调试执行与真实执行必须明确区分。dry-run 中模型提出的 tool call 只能标记为 `planned`，实际未执行的工具、发送和数据库写入必须显示为 `skipped`，禁止在 UI 上伪装成成功副作用。

## 隐私与记忆

- `AgentPrivacy.opted_out=True` 的成员不得进入 Prompt 历史、人物画像、关系、自动记忆或跨群摘要。
- 删除/退出隐私时，派生记忆和媒体引用必须按现有删除语义同步清理；不得保留可重新识别该成员的隐藏副本。
- 原始媒体 URL、本机文件路径、签名下载地址和未脱敏 OneBot payload 不得进入长期记忆、审计参数或调试响应。
- 记忆模型失败必须记录可诊断状态并允许后续重试；一次失败不能推进成功游标。
- 任何进入长期记忆的文本（模型提取的事实、关系 note、日/周摘要、成员自助写入）都必须经过 `memory_hygiene.py`：PII 脱敏、提示注入拒绝、高敏感类别（健康/宗教/政治/性取向/收入负债/住址/证件）拒绝。摘要还要抹掉隐私退出成员的名字和号码。不要在别处另写一套规则。
- `evidence_message_ids` 存的是 `GroupAgentMessage.id`（库内行 id），不是 OneBot `message_id`；反查证据一律按行 id。
- 记忆来源三种：`auto`（整理任务，可重建）、`agent`（成员亲口要求记住，重建不清除、自动整理不覆盖）、`manual`（管理员，任何自动路径都不改）。
- 成员自助工具 `remember_about_me` / `forget_about_me` 只能读写**调用者本人**的画像，不提供 user_id 参数；声称"已经忘掉"必须有本轮成功的 `forget_about_me`（见 `speech_claims.py`）。
- 召回计数只在读路径写进程内有界缓冲（`memory_access.record_access`），由整理 tick 批量落库；禁止在上下文加载或只读工具里直接 UPDATE 记忆行。
- 核心记忆（core）不过期；本人更正核心事实时降级回 profile 并重置证据，必须重新积累才能再晋升。
- 跨群共享摘要只取 Agent 总开关开启、允许公开摘要、近 14 天内的来源群，每个来源群最多 1 条，读取时再做一次 PII/号码/指令复检（`context_loader.select_shared_summaries`）；群改回隔离后，整理任务会删掉它的 `public_daily:` 行。
- 无方向关系（`SYMMETRIC_RELATION_TYPES`：好友、同学等）写入时复用反向边，读取时按无序对去重；师徒、mentions 保持方向。
- WebUI 管理员编辑 auto/agent 记忆的内容后，该行转为 `manual`；像指令的记忆内容一律拒绝（应写进人设）。
- 话题记忆注入必须满足最低相关性（`TOPIC_MEMORY_MIN_RELEVANCE`），并用 MMR 去冗余；不要把与当前对话无关的高显著旧记忆塞进 Prompt。

## 消息发送与 OneBot

- 普通文本、引用、@、face、reaction、图片、语音、视频等复合消息统一通过 `outbound.py`；不要在业务路径重新直接拼 CQ 码或各自实现发送器。
- `send_forward` 因 OneBot 独立 API 可保留专用发送路径，但必须统一返回 `SendResult`，并遵守相同审计、超时和降级规则。
- LLM 永远不得获得任意 raw CQ、XML、JSON、`@all` 或匿名消息能力。危险 payload 只能通过明确的受控模板新增，不能开放裸参数。
- `reply.message_id` 必须来自当前群已知消息；`at.user_id` 必须经过当前群成员验证；禁止让模型凭空构造跨群 message/user 标识。
- 新增 OneBot segment 时，必须同时补：schema、Python 参数校验、能力矩阵、失败降级、审计类型和测试。
- 一次用户可见 `send_message`/`send_forward` 成功后，本轮必须结束，禁止随后再发送相同最终文本。
- 协议不兼容应按 segment 自动降级，不能因为一个可选 segment 失败让整轮 Agent 沉默。

## 工具与权限

- 工具 schema 是 LLM 的唯一能力边界；不要因为 OneBot 支持某 action 就自动暴露给模型。
- 工具元数据统一声明在 `tool_registry.py`，零 AI 成本的本轮选择逻辑放在 `tool_router.py`，`tools.py` 负责执行、结果投影和二次权限校验；不要重新把注册、路由和执行混回一个文件。
- 普通对话保留小型 core tool bundle，再按 reply/@/media 和自然语言意图扩展；不能为了省 token 把模型退化成大多数回合没有行动能力，也不能每轮无条件注入全量 OneBot schema。
- OneBot 只读工具也必须先做 compact projection；禁止把原始 payload、URL 之外的协议凭证字段、本机路径或无关账户元数据直接回灌模型。
- 工具权限分五级：`read`（只读）、`state_write`（Agent 内部状态写入）、`message_send`（当前回合用户可见发送）、`privileged`（可逆或较低风险的群管理/群文件操作）、`critical`（踢人、管理员变更、全员禁言、破坏性群文件操作）。低等级调用者不得在 schema 中看到更高等级工具，执行时仍必须二次校验。
- `state_write` 必须有明确的当前群成员调用者；主动/后台任务不能借空 actor 身份写人物关系。
- `message_send` 只覆盖受控的当前回合回复能力，不等价于任意 OneBot action 权限。
- `privileged` 必须同时满足调用者实时群管理权限、群级 allowlist 和每日特权额度；其中禁言/公告还必须满足机器人自身群管理权限。`send_file` 属于 privileged，升级后默认不自动加入 allowlist。
- `privileged`/`critical` 的配额必须在副作用**之前**预扣并提交；配额无法落库时不得执行动作，动作失败时退还配额。禁止“先踢人后计数”。
- `critical` 必须显式命中当前用户消息中的动作意图、显式加入群级 allowlist、拥有真实 actor，并使用独立的低额度计数；主动/后台任务不得以空 actor 调用。`discover_tools` 永远不能发现 critical 工具，避免模型自行升级到高风险能力。
- `discover_tools` 只能返回当前 Bot 能力、当前 actor 权限和当前群 allowlist 下真实可暴露的工具；发现结果只在下一轮动态装载正式 schema，禁止返回 raw OneBot action 让模型自行拼参数。
- 工具参数在 `tool_args.py` 按注册表 schema 校验和规范化（严格布尔、整数字符串、enum、范围、长度、多余参数），校验失败不得进入权限/配额/副作用流程。handler 拿到的布尔值已经是真正的 bool，不要再用 `bool(args[...])`。
- 同一回合同工具同参数的写操作由 `ToolCallLedger` 复用首次结果，不重复扣配额、不重复副作用；每回合 privileged/critical 最多 `MAX_CONTROLLED_CALLS_PER_TURN` 次。handler 发现动作其实没有发生（如同名文件夹已存在）时返回 `refund_quota=True`，由执行器退还配额。
- 工具失败统一返回 `error_code` 与 `hint`（`tool_error_payload`）；报错文本必须经 `sanitize_error_text` 去掉 URL 和本机路径再进 Prompt/审计。
- 白名单别名集中在 `tool_registry.ALLOWLIST_ALIASES`（如 `unmute_member` 随 `mute_member` 开放），曝光、发现和执行三处都必须用 `allowlist_covers` 判断。
- 出站文件/媒体下载必须走 `safe_download.stream_download`：手动跟随重定向并逐跳校验白名单，禁止 `follow_redirects=True`。
- 工具执行异常必须被隔离。尤其 SQLAlchemy/数据库异常后，不得继续用已进入 failed transaction 的 session 强行写审计；审计是尽力而为，不能反过来毒化主流程。
- 工具结果只返回完成当前推理所需的最小数据，禁止把完整群成员原始 payload、文件路径或权限内部信息回灌模型。

## 发言清洗与声明核验

- QQ 纯文本规范化放在 `speech_format.py`（Markdown、重复标点/笑声/emoji、AI 免责声明、客服结尾、角色标签、泄漏的工具 JSON/内部字段/QQ 号、重复句子），由 `speech_quality._polish_text` 统一调用；代码块原样保留，清洗后为空时保留原文。
- “已完成”声明核验放在 `speech_claims.py`。对话路径通过 `SpeechPlan.verified_tools` 传入本轮真实成功的工具；为 None 时（主动发言、调试）不做核验。新增写操作工具时同步补它的声明族。

## 主动发言与短会话

- 主动发言策略、Prompt 和结构化决策解析放在 `proactive_policy.py`；`proactive.py` 负责调度、数据库和发送副作用。
- “要不要说”与“说什么”必须保持逻辑分离。`speak / wait / close` 是主动/续聊的合法决策，不得把沉默当异常强行补一句话。
- 被动回复后的短守卫与主动→主动冷却分开；被 @ 回复不能刷新完整主动冷却期。
- 短会话合批的静默窗口随参与人数变化：只有一个成员在接话时用 `CONVERSATION_DIRECT_QUIET_SECONDS`，多人讨论时用 `CONVERSATION_QUIET_SECONDS`，两者都受 `CONVERSATION_MAX_BATCH_SECONDS` 硬期限约束。
- 主动发言 tick 对候选群有界并发处理，并优先使用最近在该群收发消息的机器人取群锁，保证与该群对话 worker 互斥。
- 短会话是进程内临时状态，不是业务持久化状态。总时长、Bot 发言数、评估数、连续 wait 数和合批硬期限必须保持有界。
- 明确 @/reply Bot 的消息由普通对话 FIFO 路径处理，不得与短会话自动批次重复消费。

## 模块边界

- `dialogue.py` 只负责一轮对话编排（`_prepare_turn` → `_run_turn`/`_run_tool_round` → `_finalize_turn_reply`），不再继续吸收历史选择算法；历史筛选放 `context_history.py`，工具发送后的状态推进放 `speech_finalize.py`。
- `proactive.py` 只负责主动调度与副作用，不再继续吸收策略/Prompt/决策解析；纯策略放 `proactive_policy.py`。
- `outbound.py` 负责消息准备、验证、实际发送和 segment fallback；`capabilities.py` 负责 action/segment 能力视图。
- `memory.py` 负责记忆沉淀、检索排序和治理；不要把通用 Prompt 历史选择逻辑塞回记忆模块。
- 为兼容已有测试/内部调用，结构拆分时可以在旧模块保留薄别名；新增代码应直接依赖新模块的公开边界。

## 质量门槛

- 修改上下文选择后至少运行 `tests/test_agent_prompt_and_persona.py`、`tests/test_agent_memory.py`。
- 修改消息发送/能力矩阵后至少运行 `tests/test_agent_outbound.py`、`tests/test_agent_security.py`。
- 修改 Agent WebUI 后运行相关 Python WebUI 测试、前端 Vitest、TypeScript typecheck 和 build。
- 合并前 Agent 核心回归必须全绿；新增行为应优先补场景回归，而不是只补单个 helper 的实现细节测试。
