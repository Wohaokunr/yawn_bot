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
- 历史图片是否进入本轮视觉输入**不按字面判断**：只看结构（本人在 Bot 上次发言后 `MEDIA_BINDING_MAX_AGE_MINUTES` 内发的图）和工具路由模型的 `image` 标记；识图取字节统一走 `media_fetch.py`，失败要落原因码，模型看到的说明不含 URL/路径。
- 入站媒体来源必须区分 `current / reply / forward`。用户回复图片或合并转发里的图片可以进入本轮视觉输入，但仍受统一媒体数量、大小、URL 白名单和缓存隐私约束。
- 文件、语音、视频、位置、分享、音乐等不被当前模型原生理解的消息段只提供有界语义摘要；JSON/XML 卡片只保留“存在该卡片”的事实，原始 payload 不得进入 Prompt 或长期存储。
- 合并转发正文只允许生成有节点数/字符数上限的当前回合摘要，禁止把整棵转发树无限展开进 Prompt。
- 执行追踪统一放在 `execution_trace.py`。Trace 只允许作为当前进程内、数量和事件数都有硬上限的诊断数据，禁止写成新的长期聊天/媒体副本。
- Trace 必须同时做 key 级和字符串级脱敏：原始媒体 URL、签名地址、本机路径、`file`、裸 OneBot/CQ payload、密钥等不得进入 WebUI 执行轨迹。追踪本身失败也不得改变 Agent 正常控制流。
- 完整提示词抓取放在 `prompt_capture.py`，通过 `llm.add_request_observer` 记录每次真实请求（messages、tools、参数、回复）。只挂在当前 Trace 上（每条 `MAX_SNAPSHOTS_PER_TRACE` 次、单次 `MAX_SNAPSHOT_CHARS` 字符、全局 `MAX_TRACES_WITH_PROMPTS` 条），链接/本机路径/内嵌媒体统一脱敏，只给管理员看；观察者异常不得影响模型调用。`AGENT_TRACE_PROMPT_CAPTURE=off` 关闭。
- 调试执行与真实执行必须明确区分。dry-run 中模型提出的 tool call 只能标记为 `planned`，实际未执行的工具、发送和数据库写入必须显示为 `skipped`，禁止在 UI 上伪装成成功副作用。

## 提示词分段

- 发给模型的每一段文字都必须登记在 `prompt_sections.py` 的 `SECTIONS`，并通过 `apply_section` / `section_text` / `system_messages` 或 `ContentBuilder` 输出。新加提示词文字不登记就等于对 WebUI 黑箱，禁止直接拼进 messages。
- 没有覆盖时输出必须与默认逐字一致（有测试守着）；覆盖解析顺序是本群草稿 → 本群 → 全局草稿 → 全局。整层删空时不发空 system 消息。
- 安全分段（`safety=True`）可以被删除或替换，但写入必须带 `confirm_safety`。删除提示词文字不得关掉代码里的权限分级、特权配额、critical 门槛、出站校验和隐私退出过滤。
- 上下文数据分段（`KIND_DATA`）只能整块保留/删除，不得记录进程级样例，避免群消息越过 Trace 的隐私边界。
- 覆盖存 JSON（`AGENT_PROMPT_OVERRIDES_PATH`），带 `revision` 乐观锁和每段 12 条历史，不建表、不需要迁移。文件损坏时按默认运行。

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

## Bot 与成员的关系（bot_bond）

- Bot 视角的关系只存在 `AgentBotBond`（群 + Bot + 成员唯一）和 `AgentMemory(type="bond")`（共同经历），**不得**写进成员关系图 `AgentRelation`；整理时模型给出的含 Bot 的 relations 一律剔除，`@Bot` 也不记 mentions 边。
- 互动统计只看结构：消息段 `at`、`reply_chain[0].user_id`、Bot 说话后 3 分钟内的接话、5 分钟一呼一应。不要按正文关键词判断"在找 Bot"或"关系变好"，情绪判断交给记忆模型。
- 熟悉度由计数确定性推导（消息量开平方 + 活跃天数），模型只能有界地调好感/信任（每批 ±0.12 / ±0.08，单证据减半），不能直接给熟悉度或阶段。
- 模型观察必须同时有该成员本人的证据和本批真实来往；已有文本字段只接受置信 ≥0.6 的新观察；被锁定的字段（`locked_fields`）任何自动路径都不改。
- 关系文本全部走 `bot_bond.clean_bond_text`（memory_hygiene + 人设覆盖检查，打码即整条丢弃）；不要在别处另写校验。
- 注入只放在发言人层（`_SPEAKER_CONTEXT_KEYS` 的 `bot_bonds`），只给词语不给数值，900 字符硬预算；关系使用规则 `_BOT_BOND_GUIDANCE` 只在有关系时追加，静态前缀不变。
- `note_our_bond` 只写调用者本人并锁字段；`forget_about_me` 删关系时保留管理员手工内容并如实告知。隐私退出成员读写都拒绝、WebUI 不展示。
- 表未迁移时 `prefetch_bonds` 返回 None，整理、上下文、工具全部降级跳过，不能让原有记忆整理失败。

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
- 普通对话保留小型 core tool bundle，再由工具路由（`tool_planner.py` + `tool_preload.py`）按当前消息预装最多两个工具包；不能为了省 token 把模型退化成大多数回合没有行动能力，也不能每轮无条件注入全量 OneBot schema。
- **禁止按关键词/字面猜用户意图**来决定开放哪些工具或消息段。需要判断时只能用 `agent_router` 任务的轻量模型，且只给它当前这一条消息和结构布尔信号（reply/media/@别人），不给群上下文、成员资料或记忆。路由失败一律退回渐进披露，不退回关键词。唯一例外是 critical 工具的显式动作词门槛，那是安全闸，不是意图猜测。
- 预装必须逐包走 `_discover_tools_for_actor` 的同一套权限检查；路由说要、权限不给的工具不能出现。消息段只由结构信号、路由结论和已装载工具族（`discovery_segment_types`）决定。
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
- `discover_tools` 至少要有 `query` 或 `family` 之一；只给 `family` 是合法调用（族名先过 `tool_router.normalize_tool_family`，中文别名如“公告”“群管”也要认），未知族名并入关键词搜索而不是报错。当前不能用的工具放进 `unavailable` 列表并给出原因（`actor_not_admin`、`bot_not_admin`、`not_allowlisted` 等），让模型能如实告诉成员“为什么不能做”；critical 工具无论如何都不出现在任何列表里。
- 审计 `result` 的成功/跳过/失败/待确认分类只认 `audit_outcome.classify_audit_result`：主动发言的 `wait`/`close` 是跳过，不是失败；`delivery_unknown` 是待确认。WebUI 统计、筛选和前端 `agent-meta.auditOutcome` 必须与它同口径，新增 result 值时三处一起改。

## 触发、进度与回合预算

- 触发来源：`mention`、`reply`、`wake_word`，以及隐式的 `name_call`（不带 @ 叫了人设名/Bot 昵称）和 `conversation_intent`（3 分钟内明确找过 Bot 的成员接着提需求）。隐式触发放在 `implicit_trigger.py`，必须同时满足：`explicit_wakeup_enabled`、没有 @ 别人/回复别人、路由模型判定 `request=true`；`conversation_intent` 还要路由给出工具包。`AGENT_IMPLICIT_TRIGGER=off` 或 `AGENT_ROUTER_ENABLED=false` 时整段关闭。
- 慢回合反馈放在 `progress_notice.py`：先给触发消息贴 👀，只有慢工具（沙箱）超过长阈值仍在跑才补一句；文字优先用模型自己的开场白，否则按阶段轮换人设化说法。禁止恢复每轮固定的“稍等”文案。`AGENT_AI_WAIT_NOTICE_MODE` 可选 reaction/text/off。
- 所有 Agent 模型请求都不传 `max_tokens`（`llm.NO_OUTPUT_LIMIT`），输出长短和推理深浅由 `*_REASONING_EFFORT` 控制；不要再用参数截断模型输出。超时、轮次和回合时限按装载的工具放宽（`turn_budget.py`），中途发现沙箱时顺延轮次和时限。
- 工具参数 JSON 解析失败要区分“被截断”（`truncated=true` + 拆短提示）和“写错了”；最后一轮前注入 `FINAL_ROUND_NOTE`，让模型收尾而不是继续调工具。
- 队列 TTL 只算排队等待，处理耗时不计入，长回合不能因为超过 120 秒被丢弃回复。

## 发言清洗与声明核验

- QQ 纯文本规范化放在 `speech_format.py`（Markdown、重复标点/笑声/emoji、AI 免责声明、客服结尾、角色标签、泄漏的工具 JSON/内部字段/QQ 号、重复句子），由 `speech_quality._polish_text` 统一调用；代码块原样保留，清洗后为空时保留原文。
- “已完成”声明核验放在 `speech_claims.py`。对话路径通过 `SpeechPlan.verified_tools` 传入本轮真实成功的工具；为 None 时（主动发言、调试）不做核验。新增写操作工具时同步补它的声明族。

## 发言节奏（分条发送）

- 一次发言拆成几条、什么节奏由模型决定：正文用 `|||` 分条，开头可写 `【节奏:整段/分条/连发/蹦字】`。解析、兜底拆分、停顿计算全部在 `speech_rhythm.py`（纯函数）；依次发送、催促合并、失败停止在 `speech_delivery.py`。不要在别处另写分条逻辑。
- 兜底拆分只看结构（代码块、列表、空行段落、换行、句末标点），**不按关键词猜情绪或意图**；代码块里的 `|||` 不拆。复杂问题（complexity=complex）和短回应（ping_ack/acknowledge/react/close）不兜底拆，`AGENT_REPLY_SPLIT=marked` 时只认模型标记，`off` 时一律一条。
- 清洗按条做：开场类清洗只看第一条、结尾类只看最后一条（`_polish_text(position=...)`），长度修剪和撞重在整句层面做；近似重复的后续分条直接去掉。声明核验对整句做。
- 只有第一条带自动引用、只有第一条检查触发过期；模型自己写的 reply/at 段留在它所在的那一条。任何一条准备失败都整体退回单条发送。
- 发送中群队列里出现新的触发（`collector.queue_size > 0`）或停顿总和超过 `AGENT_REPLY_MAX_SPREAD_SECONDS`，剩下的合成一条立刻发完；某条确认失败或回执不确定就停止后续，没发的条目要清理临时文件。
- 每条单独落库（`persist_delivered`），`send_message` 的分条结果放在 `outbound_parts` 里由 `record_tool_send` 逐条写；去重指纹、近期发言、主动发言审计按整句记，主动/续聊的一次分条只记一条 `proactive_reply` 审计（`arguments.parts` 记条数），不计入多次 unanswered。
- `break` 伪消息段只在 SpeechPlan 内部存在，发送前一定被 `segment_groups` / `strip_break_segments` 消费，绝不进 OneBot 校验。进度提示、历史文本等单条场景用 `strip_rhythm_markup` 去掉标记。
- 节奏说明只放在易变的发言策略层（`rhythm_instruction`），不进 450 token 的静态前缀。

## 主动发言与短会话

- 主动发言策略、Prompt 和结构化决策解析放在 `proactive_policy.py`；`proactive.py` 负责调度、数据库和发送副作用。
- “要不要说”与“说什么”必须保持逻辑分离。`speak / wait / close` 是主动/续聊的合法决策，不得把沉默当异常强行补一句话。
- 被动回复后的短守卫与主动→主动冷却分开；被 @ 回复不能刷新完整主动冷却期。
- 短会话合批的静默窗口随参与人数变化：只有一个成员在接话时用 `CONVERSATION_DIRECT_QUIET_SECONDS`，多人讨论时用 `CONVERSATION_QUIET_SECONDS`，两者都受 `CONVERSATION_MAX_BATCH_SECONDS` 硬期限约束。
- 主动发言 tick 对候选群有界并发处理，并优先使用最近在该群收发消息的机器人取群锁，保证与该群对话 worker 互斥。
- 短会话是进程内临时状态，不是业务持久化状态。总时长、Bot 发言数、评估数、连续 wait 数和合批硬期限必须保持有界。
- 明确 @/reply Bot 的消息由普通对话 FIFO 路径处理，不得与短会话自动批次重复消费。
- 冷场暖场的门槛、退避、骰子和 Prompt 上下文放在 `idle_warmup.py`（纯策略），沉默事实由 `activity.warmup_state` 从消息与审计推导，不新增持久化字段、不用进程内状态，重启后不丢。
- 冷场以真人（含隐私退出成员）最后一次发言为锚点；Bot 自己的消息不能重置冷场计时。一段冷场里主动消息连续没人接达到 `max_unanswered_for(behavior)` 条、或模型在暖场场景回答 `close`，都必须暂停到真人开口。
- 暖场的 wait/失败按次数指数退避；同一段冷场的骰子点数固定（`episode_roll`），概率表示“每段冷场”而不是“每分钟”。
- 主动发言审计的 `arguments.topic` 用于避免同一段沉默重提同一话题；暖场审计附 `unanswered`、`idle_minutes` 便于诊断。新增主动场景时注意它们会计入 `unanswered`（`WARMUP_SENT_RESULTS`）。
- 主动/续聊发送前用 `idle_warmup.near_duplicate_of` 做近似撞重，不要退回只比较完全相同的字符串。

## 人设（v3）

- 人设只有一种形状：`persona.PersonaCard`。WebUI、QQ 命令、人设库、调试草稿都读写它；超长或越界的输入在模型校验里裁剪/夹到 0–4，而不是让整次保存失败。
- 生效顺序固定为“本群自定义（`persona_enabled=True` 且卡可解析）→ 人设库里的全局人设 → 内置默认”，统一用 `resolve_persona_source` / `effective_persona_card` 取；跟随全局时 `persona_profile` 只是暂存草稿，运行时（含情绪表达强度）不得读它。
- 全局人设存在 `AgentPersonaTemplate`（`is_global` 最多一行），运行时只读 `persona.global_persona()` 的进程内缓存；人设库每次写入后必须调用 `persona_library.refresh_global_persona`。表还没迁移时一律降级为内置默认，不能影响对话。
- 存量 v2 `persona_profile` 在读取时由 `parse_persona_card` 惰性转换，不写迁移脚本批量改数据；新写入一律是 `schema_version=3`。
- 人设文本不得覆盖系统策略：所有写入口（WebUI、人设库、QQ 命令）都要过 `persona_override_reason`。“自称本喵、叫成员杂鱼”这类角色指令是允许的；“忽略以上规则”、伪造 system/CQ 码、`@全体成员` 一律拒绝。
- 编译后的人设 JSON 有 `PERSONA_PROMPT_TOKEN_LIMIT` 预算，超出时按示例 → 喜好 → 避开话题 → 口癖 → 长文本的顺序裁剪；默认人设下静态前缀必须保持在 450 tokens 以内（有测试守护）。
- 行为影响只能收窄运行配置：活跃度决定主动候选系数和自动续聊轮数，接梗决定主动 reaction；开关、概率、冷却和每日上限仍以 `GroupAgentConfig` 为硬边界。

## 模块边界

- `dialogue.py` 只负责一轮对话编排（`_prepare_turn` → `_run_turn`/`_run_tool_round` → `_finalize_turn_reply`），不再继续吸收历史选择算法；历史筛选放 `context_history.py`，工具发送后的状态推进放 `speech_finalize.py`。
- `proactive.py` 只负责主动调度与副作用，不再继续吸收策略/Prompt/决策解析；纯策略放 `proactive_policy.py`。
- `outbound.py` 负责消息准备、验证、实际发送和 segment fallback；`capabilities.py` 负责 action/segment 能力视图。
- `memory.py` 负责记忆沉淀、检索排序和治理；不要把通用 Prompt 历史选择逻辑塞回记忆模块。
- 为兼容已有测试/内部调用，结构拆分时可以在旧模块保留薄别名；新增代码应直接依赖新模块的公开边界。

## 质量门槛

- 修改上下文选择后至少运行 `tests/test_agent_prompt_and_persona.py`、`tests/test_agent_memory.py`。
- 修改人设后至少运行 `tests/test_agent_prompt_and_persona.py`、`tests/test_agent_persona_library.py`、`tests/test_agent_command_sessions.py`、`tests/test_agent_speech.py`，以及前端 `PersonaPanel.test.tsx`。
- 修改消息发送/能力矩阵后至少运行 `tests/test_agent_outbound.py`、`tests/test_agent_security.py`。
- 修改 Agent WebUI 后运行相关 Python WebUI 测试、前端 Vitest、TypeScript typecheck 和 build。
- 合并前 Agent 核心回归必须全绿；新增行为应优先补场景回归，而不是只补单个 helper 的实现细节测试。

## 代码沙箱

- `run_python` / `run_shell` 等 sandbox 工具族只能经 `discover_tools` 或工具路由预装（同一套检查）装载，发现阶段用 `sandbox_policy.exposure_reason` 门控：沙箱关闭、群未开放、仅管理员、离线、隔离自检未通过时整族隐藏并给出原因，模型据此如实说明。
- 代码只在独立的无网络 runner 容器里执行（`sandbox/`），Bot 进程**绝不** `exec`/`eval` 模型代码；runner 地址只认 `AGENT_SANDBOX_URL`，Token 与任何 Bot 密钥都不得传入沙箱环境。
- 每个群一个工作区 `g<群号>`，工具只能访问当前群的工作区；产物以 `sandbox:<相对路径>` 引用交给 `send_message`（image/record）/`send_file`，由 `sandbox_refs.materialize` 取回到 `AGENT_FILE_ROOT/.sandbox_exports/` 并以 base64 发送，发送后删除；直接引用导出目录一律拒绝。
- 次数先预扣后执行；runner 繁忙/拒绝/离线（代码根本没跑）必须退还。审计参数不记代码正文，只记 `code_chars`。
- 没有本轮成功的 `run_python`/`run_shell` 不得声称“代码跑完了/图画好了/曲子生成了”（`speech_claims.py` 的 `sandbox_run` 族）。
- `run_shell` 是同一工作区里的 bash（`--noprofile --norc -o pipefail`），runner 的 `/health.languages` 不含 `shell` 时（旧 runner）不暴露；Bot 只在 shell 请求里带 `language` 字段，保持对旧 runner 兼容。
- 可靠性约定：同一回合第二次起的运行跳过冷却（仍计每日次数）；剩余冷却不超过 `MAX_COOLDOWN_WAIT` 时等待而不是报错；runner 忙/离线各自动重试一次；代码没跑成时退还次数**和冷却**。运行失败（`status != ok`）在审计里记为 error，`ToolCallLedger` 不缓存失败结果，`run_python`/`run_shell` 永不按参数去重。
- 结果投影走 `sandbox_output.py`：失败给 `error` 摘要（异常、用户代码行号、那行代码、对症修法），traceback 只留用户帧，成功时滤掉字体/弃用警告，按行截断并说明怎么看全量。
- 新增 sandbox 工具时同步 `tests/test_agent_hot_path.py` 的 `EXPECTED_FAMILY_TOOL_COUNT` 与 `tests/test_agent_sandbox.py`。
- 群聊附件入箱（`sandbox_inbox.py`）：是否自动放入只看结构化事实（消息有无附件、沙箱工具是否已暴露、`exposure_reason`、成员是否退出记忆），**不做关键词匹配**；给模型的说明写进 `turn.context["sandbox_inbox"]`（实时易变层），不进稳定前缀。
- 入箱取字节只走：消息段 base64 → OneBot 接口 → `AGENT_SANDBOX_INBOX_HOSTS` 白名单下载（`safe_download.stream_download(host_check=...)`，逐跳校验）→ `AGENT_SANDBOX_INBOX_LOCAL_ROOTS` 里的本机路径；不得新增绕过白名单的取法，模型永远看不到 URL、OneBot `file` 标识或本机路径。
- 入箱文件写到 `inbox/`，名字用内容哈希、可执行后缀改成 `.txt`；事件记录 `sandbox_inbox_store` 不记 URL/路径以外的原始标识，`delete_member_memories` 必须同时调 `forget_member`。
- 没有本轮成功的 `import_attachments`/`import_group_file`/`run_python`/`run_shell` 不得声称“已经放进电脑了”（`speech_claims.py` 的 `sandbox_import` 族）。

## 网页搜索

- 联网只走 DeepSeek 官方 Search / Fetch API（`web_search_client.py`），不得另接爬虫或让 Bot 进程直接抓取任意网址；读网页前用 `validate_fetch_url` 拒绝内网/本机/带凭证/非 http(s) 地址。
- `web` 工具族只经工具路由或 `discover_tools` 装载，发现阶段用 `web_search_policy.exposure_reason` 门控（`web_closed`/`web_no_key`/`actor_not_admin`/`web_paused`），被挡住时进 `unavailable` 并给原因。
- 次数先预扣后请求，只有成功且有结果（`billed`）才保留；失败、无结果、命中缓存都退还，命中缓存也要过 `check_access`。每回合次数记在 `ToolCallLedger.web_calls`，结果编号记在 `ToolCallLedger.web_refs`。
- 结果投影走 `web_search_output.py`：去重、过滤后台屏蔽域名、摘要/正文按字符预算截断，标注 `results_are_data` / `content_is_data`。搜索结果是不可信数据，不得进入长期记忆。
- 最近调用记录只放进程内有界缓冲（含搜索词），`delete_member_memories` 必须同时调 `forget_member_recent`；WebUI 只回显密钥来源和掩码。
- 没有本轮成功的 `web_search`/`web_fetch` 不得声称“搜了一下/搜索结果显示”（`speech_claims.py` 的 `web_search` 族）。
- 新增 web 工具时同步 `tests/test_agent_hot_path.py` 的 `EXPECTED_FAMILY_TOOL_COUNT` 与 `tests/test_agent_web_search.py`。

## 定时任务

- 工具族 `schedule`：`schedule_task`（可 `dry_run` 只预览）、`list_scheduled_tasks`、`update_scheduled_task`（edit/pause/resume；改时间必须带 `kind`）、`cancel_scheduled_task`（`task_id` 或 `all_mine`）。成员只能改/取消自己设的任务，群主/管理员可以动本群任意任务；“仅管理员可创建”在 handler 里拒绝，不在发现阶段隐藏（成员仍要能查看、取消自己的）。
- 规则引擎是纯函数 `schedule_spec.py`：once/interval/daily/weekly/monthly/cron，附加 window、start_at/end_at、months、skip_dates。**APScheduler 3 的星期 0=周一**，标准 cron 的星期字段一律先转成 sun…sat 名字再交给 CronTrigger；新增规则种类时同步 `describe`、`min_gap_minutes` 与前端 `webui/src/schedules/schedule-meta.ts`。
- 存储走 `AgentScheduledTask`（迁移 `d7f9b1c3e5a7`），表不存在时 `schedule_store` 抛 `ScheduleUnavailableError`，工具如实说“暂不可用”，WebUI 只读接口降级。策略与当日用量是 JSON（`data/agent_schedule/`），不需要迁移和重启。
- 执行器 `schedule_runner.py` 每 20 秒一次：`claim_due` 用 `revision` 条件更新领租约，**先推进 `next_run_at` 并提交再执行**，进程崩溃最多漏一次、不会重复执行。同一个群串行（共用群锁），跨群并发受 `concurrency` 限制。错过宽限按 `missed_policy`（补跑最多一次、只补 `catch_up_max_hours` 以内）；免打扰按 `quiet_policy` 推迟或跳过；失败按 2/5/15 分钟退避重试，连续失败到上限自动暂停；创建者退群自动暂停。
- `agent` 模式的唤醒回合：设置 contextvar `ScheduledWake`，构造 `trigger_source="scheduled_task"`、`message_id=None` 的合成事件交给 `dialogue.process_group_message`，创建者作为 actor（`mention_creator` 时进 `mentions` 以便 @）。`context_loader` 把 `wake.prompt_payload()` 放进 `context["scheduled_wake"]`，`prompt.py` 据此追加 `core.scheduled_wake` 段。**唤醒回合里隐藏并拦截**特权/高危工具和 `schedule_task`（`tools._wake_blocks`），防止定时任务自我繁殖或无人值守地管群。`notify` 模式不调模型，直接 `send_outbound_message` 并落库。
- 不得在没有本轮成功的 `schedule_task`/`update_scheduled_task` 时声称“已经定好了”，没有 `cancel_scheduled_task` 时声称“取消了”（`speech_claims.py` 的 `schedule_set`/`schedule_cancel` 族）。
- 指令文本创建时过 `persona.text_overrides_policy`，拒绝 CQ 码与 @全体；指令只在到点时作为“创建者当时的请求”交给模型，不提升权限。
- 不改 `dialogue.py`（已接近 1500 行上限）：唤醒语义全部通过 contextvar + `context_loader` + `prompt.py` + `tools.py` 接入。新增 schedule 工具时同步 `tests/test_agent_hot_path.py` 的 `EXPECTED_FAMILY_TOOL_COUNT` 与 `tests/test_agent_schedule.py`。
