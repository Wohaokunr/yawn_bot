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

## 签到与好感

- 签到只调 `checkin_service.perform_checkin`，好感只调 `affinity.grant`；Agent 里不要另写一份签到或直接改 `BotUser.affinity` / `UserGroup.group_affinity`。
- `checkin_for_me` 只签**调用者本人、当前群**，没有 user_id/group_id 参数；没有真实 actor（主动发言、定时唤醒）一律拒绝；功能开关走 `check_feature_permission(..., "checkin", ...)`。
- 群聊互动好感（`member_standing.reward_group_interaction`）只看结构：真实调用者、Bot 确实发出了回复、不是定时唤醒、成员没有隐私退出；不看说了什么。失败只记日志，不能影响回复后的状态提交。
- 发言人层 `bot_bonds` 里的 `overall`（好感等级词）和 `checkin_streak`（连签天数）由 `load_members` 同一次查询顺带取回，不加往返；只给词语和天数，不给好感数值。私聊原文和私聊记忆永远不进群 Prompt。
- 声称“帮你签好了”必须有本轮成功的 `checkin_for_me`（或查过 `get_my_checkin` 如实转述），见 `speech_claims.py` 的 `checkin` 族。

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
- 提示词要求模型**每次**先写 `【节奏:…】` 自己选节奏，并带可照抄的示例；节奏只决定“怎么发”，不是多说话的理由，提示里必须说明分条总字数和一条时一致。情绪倾向只读 `emotion_state`（强度 ≥0.3 才提示），选哪种仍由模型定。
- 模型没选节奏时只按它自己写的空行/换行兜底，**不再把一整段按句号切开**（那会把长话伪装成分条、助长变长）；只有模型声明了“分条/连发”却没写 `|||` 时才按句子/短句拆。声明连发但某条超过 `BURST_LONG_PART_CHARS` 字时按分条速度发。
- 分条跟着模型的语气走（2026-11 用户明确）：模型自己写的 `|||`（一字一条、话尾补一句“略略略～”）和【节奏:蹦字】一律照写的发，**不得按字数合并**。系统只在“声明分条/连发却没写 `|||`、正文 ≤ `SHORT_REPLY_MAX_CHARS` 字”时不替它硬拆。声明整段或没声明、但正文 ≥ `LONG_REPLY_MIN_CHARS` 且模型自己用空行/换行分了段的（非代码/列表）按段落均匀并成至多 `max_parts` 条；段落比条数上限多时合并而不是退回一整条。
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
- 代码只在独立的 runner 容器里执行（`sandbox/`，只接通外网的 `sandbox-egress`，内网由 runner 的 iptables 挡住）。每次运行都带 `network`：群聊看 `SandboxSettings.group_network`（默认关），私聊看 `private_network`（默认开）；隔离自检的 `network_guarded` 不过（容器有网却管不住防火墙）时整族隐藏。Bot 进程**绝不** `exec`/`eval` 模型代码；runner 地址只认 `AGENT_SANDBOX_URL`，Token 与任何 Bot 密钥都不得传入沙箱环境。
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

## 表情包

- 模型在正文写 `[表情:名字]`（也认 `[表情包: 名字]`、`【表情：名字】`、`[sticker:xx]`），`sticker_markup.expand_sticker_segments` 把它拆成内部 `sticker` 伪段，前后各插一个带 `around_sticker` 标记的 `break`，让表情包在节奏序列里自成一条；不增加工具回合。伪段只在 SpeechPlan 内部存在，发送前必须经 `sticker_send.resolve_plan_stickers` 换成真正的 `reaction` 段或去掉（去掉时用 `drop_sticker_at` 收回自动 break、把两侧文字接回一条），绝不进 OneBot 校验。
- 改分条节奏（`speech_rhythm`/`speech_delivery`）时保持 `break` 伪段语义不变；表情包的位置、冷却与选择不属于节奏层。
- **不按聊天内容的关键词决定发不发表情包**：时机由模型自己判断；系统只做两层门控——提示词层（`sticker_turn_instruction`，开关/本群开放/库存/冷却/每日上限/人设 `allow_spontaneous_reaction`）和发送层（`resolve_plan_stickers` 再查一遍并按 `max_per_message` 截断）。菜单挑选不看聊天内容：优先最近没发过的，按小时稳定洗牌；按情绪标签挑时随机且偏向少用的。
- 库存储在 `AGENT_FILE_ROOT/reactions/index.json`（`sticker_library.py`，兼容旧 `reactions` 索引）；策略与用量是 JSON（`data/agent_stickers/`，`sticker_policy.py`），不需要迁移。新增提示词文本登记在 `prompt_sections.py`（`speech.sticker`、`proactive.sticker`）。
- 发送为 `image` 段 + `sub_type=1` + `summary="[名字]"`；历史转述统一写成 `[表情包: 名字]`，与成员发来的表情包同一写法。
- 收藏走 `save_sticker`（族 `reaction`，state_write）：只认当前消息/引用里的图片或近期附件编号，按 `collect_mode` 决定直接可用还是待审核；成员已退出记忆的不收。没有本轮成功的 `save_sticker` 不得声称“收藏了”（`speech_claims.py` 的 `sticker_save` 族）。
- `delete_member_memories` 必须同时调 `sticker_library.forget_member`。新增 reaction 族工具时同步 `tests/test_agent_hot_path.py` 的 `EXPECTED_FAMILY_TOOL_COUNT` 与 `tests/test_agent_sticker.py`。

## 定时任务

- 工具族 `schedule`：`schedule_task`（可 `dry_run` 只预览）、`list_scheduled_tasks`、`update_scheduled_task`（edit/pause/resume；改时间必须带 `kind`）、`cancel_scheduled_task`（`task_id` 或 `all_mine`）。成员只能改/取消自己设的任务，群主/管理员可以动本群任意任务；“仅管理员可创建”在 handler 里拒绝，不在发现阶段隐藏（成员仍要能查看、取消自己的）。
- 规则引擎是纯函数 `schedule_spec.py`：once/interval/daily/weekly/monthly/cron，附加 window、start_at/end_at、months、skip_dates。**APScheduler 3 的星期 0=周一**，标准 cron 的星期字段一律先转成 sun…sat 名字再交给 CronTrigger；新增规则种类时同步 `describe`、`min_gap_minutes` 与前端 `webui/src/schedules/schedule-meta.ts`。
- 存储走 `AgentScheduledTask`（迁移 `d7f9b1c3e5a7`），表不存在时 `schedule_store` 抛 `ScheduleUnavailableError`，工具如实说“暂不可用”，WebUI 只读接口降级。策略与当日用量是 JSON（`data/agent_schedule/`），不需要迁移和重启。
- 执行器 `schedule_runner.py` 每 20 秒一次：`claim_due` 用 `revision` 条件更新领租约，**先推进 `next_run_at` 并提交再执行**，进程崩溃最多漏一次、不会重复执行。同一个群串行（共用群锁），跨群并发受 `concurrency` 限制。错过宽限按 `missed_policy`（补跑最多一次、只补 `catch_up_max_hours` 以内）；免打扰按 `quiet_policy` 推迟或跳过；失败按 2/5/15 分钟退避重试，连续失败到上限自动暂停；创建者退群自动暂停。
- `agent` 模式的唤醒回合：设置 contextvar `ScheduledWake`，构造 `trigger_source="scheduled_task"`、`message_id=None` 的合成事件交给 `dialogue.process_group_message`，创建者作为 actor（`mention_creator` 时进 `mentions` 以便 @）。`context_loader` 把 `wake.prompt_payload()` 放进 `context["scheduled_wake"]`，`prompt.py` 据此追加 `core.scheduled_wake` 段。**唤醒回合里隐藏并拦截**特权/高危工具和 `schedule_task`（`tools._wake_blocks`），防止定时任务自我繁殖或无人值守地管群。`notify` 模式不调模型，直接 `send_outbound_message` 并落库。
- 不得在没有本轮成功的 `schedule_task`/`update_scheduled_task` 时声称“已经定好了”，没有 `cancel_scheduled_task` 时声称“取消了”（`speech_claims.py` 的 `schedule_set`/`schedule_cancel` 族）。
- 指令文本创建时过 `persona.text_overrides_policy`，拒绝 CQ 码与 @全体；指令只在到点时作为“创建者当时的请求”交给模型，不提升权限。
- 不改 `dialogue.py`（已接近 1500 行上限）：唤醒语义全部通过 contextvar + `context_loader` + `prompt.py` + `tools.py` 接入。新增 schedule 工具时同步 `tests/test_agent_hot_path.py` 的 `EXPECTED_FAMILY_TOOL_COUNT` 与 `tests/test_agent_schedule.py`。

## 私聊 Agent

- 入口只有一个：`ai_chat._process_chat` → `_try_private_agent` → `private_chat_bridge.get_private_handler()` → `private_agent.handle_private_turn`。处理函数返回 `None` 或抛异常时，`ai_chat` 回滚会话并退回原来的流式纯对话；私聊 Agent 不得自己另开发送通道或绕过 `ChatMessage` 落库。
- 核心插件不导入 `yawn_agent`：处理函数由 `yawn_agent/__init__.py` 在加载时 `register_private_handler` 注册。子插件没加载时私聊照旧可用。
- 自动接管（`PRIVATE_AGENT_AUTO_REPLY`）只对好友私聊生效（`PRIVATE_AGENT_FRIENDS_ONLY`）；成员发 `/退出` 后写入暂停状态，不再自动回复他，`/对话` 清除暂停。暂停状态是 JSON 文件，不需要迁移。
- 每轮模型请求走 `llm.complete_with_tools_result(task="agent_dialogue", max_tokens=NO_OUTPUT_LIMIT)`；最后一轮不带工具并追加 `private.final_round`。所有提示词文本都在 `private_prompt.py`，并在 `prompt_sections.py` 登记为 `private.*`（`private.safety` 是安全段）。新增私聊提示词时同步登记，否则 WebUI 看不到也改不了。
- 私聊工具不收 `user_id`，一律作用于发消息的本人；签到走 `checkin_service.perform_checkin`，好感只经 `affinity.grant`（来源 `private_chat`，有日上限）。写工具成功后先提交再回复，模型后续失败不丢副作用。没有本轮成功的 `checkin` 不得声称“签好了”（`verify_claims`）。
- 读群数据的边界：只读用户**当前活跃**的群，且该群 `GroupAgentConfig.enabled` 为真、本人未隐私退出（`AgentPrivacy.opted_out`）。群里的记忆和关系是不可信数据，进上下文前过 `memory_hygiene`，只给词语不给数值。`group_recent` 只允许可读的群，并对号码打码。
- 私聊记忆存在 `AgentMemory`（`scope="private"`、`group_id` 为空、`memory_type="private_fact"`），**永远不进群聊提示词**。成员要求“也在群里记住/忘掉”时，才通过 `memory_self` 写入/删除对应群的画像，且同样只限可读的群。自动整理（每 6 条私聊一次，后台）不覆盖 Agent 或手动写入的同键记忆。
- 网页账号新人引导 `webui_onboarding`（guide/ask_password/cancel）**没有密码参数**：`ask_password` 只在 `webui_account_commands` 里开一个 5 分钟的收密码窗口并由 Bot 直接私发提示；他下一条私聊由 `capture_listener`（优先级 -3）直接收走设密码，不进日志、历史和模型。不要给任何私聊工具加“密码”参数，也不要让模型复述密码。
- 新增私聊工具时同步 `private_tools.tool_schemas()`、`execute_private_tool` 与 `tests/test_private_agent.py`；参数校验沿用 `tool_args.validate_tool_arguments` 与 `tool_error_payload`。
- 个人助理数据（笔记、待办、提醒、倒数日、心情、聊天偏好、调度标记）存在 `private_store`：`data/private_agent/users/<QQ>.json`（`PRIVATE_AGENT_DATA_DIR` 可改），**不建表、不需要迁移**；读改写一律走 `private_store.update` 持锁完成。用户文本入库前过 `private_organizer.clean_text`（去控制字符、限长、拒绝像指令的内容），因为它们之后会作为工具结果回到模型。
- 带 `action` 的工具成功后，`ctx.succeeded` 同时记 `工具名` 和 `工具名.动作`；声明核验靠 `private_agent._CLAIM_ALIASES` 把它们映射到群聊声明族（如 `reminders.add` → `schedule_task`），只查过列表不能说“设好了”。
- 私聊联网搜索复用群聊的 DeepSeek 客户端、后台总开关、屏蔽域名和全站日预算（`record_admin_call` 记账），但不占任何群/成员配额；私聊另有每人每日次数（`PRIVATE_AGENT_WEB_DAILY`，记在个人数据 `flags`），没搜到结果或失败时退还。
- `private_scheduler` 只发两类私聊：用户自己设的提醒（不受免打扰/`proactive` 偏好影响，错过 12 小时以上的一次性提醒丢弃）和主动消息（早安、签到提醒、生日、倒数日当天；用户关了 `proactive`、暂停了自动回复、在免打扰时段都不发，每天每类最多一次，标记先写后发）。`PRIVATE_AGENT_PROACTIVE=false` 时全部停发。发出的消息记进最近的私聊会话。
- 对话 worker 有连发合并（`PRIVATE_AGENT_MERGE_WINDOW_MS`）：等一个短窗口，把队列里紧跟着的普通消息并成一轮，用最后一条事件回复；带 `override_text` 的命令消息不参与合并。
- `ai_chat._process_chat` 在 commit 前缓存 `chat_session.id`；生产会话 `expire_on_commit=True`，测试里涉及完整回合的要用 `prod_maker`（`tests/test_private_agent.py`）这种生产同款会话。
- 私聊回复统一经 `private_delivery.deliver` 发出：先按 `speech_rhythm` 的 【节奏:…】/`|||` 分条，再把 `[表情:名字]`、`[图片:sandbox:路径]`、`[文件:sandbox:路径]` 拆成单独的消息。媒体发不出去时换成一句文字说明，不得静默吞掉；某条发送失败就停，只把已送达的部分落库。私聊表情包只用通用表情（scope 0），额度按人记在个人数据 `flags`（`sticker_*`），本人 `emoji` 偏好为 `none` 时不给菜单。
- 私聊识图在 `private_vision.py`：图片段复用群聊的 `media.prepare_media_inputs`（伪群号 0、不落库）和 `turn_media.vision_route`；`direct` 把 image 块附在最后一条 user 消息上（5 分钟内没发新图时，最近的图只附回**当初发图的那条历史消息**，本条注明“没有新图片”；不得附到本条上，否则模型会把一张旧表情包当成每轮新发的），`caption` 用识图模型转述，`none` 附一句看不到。转述和附件入箱结果（`sandbox:` 引用或失败原因）由 `private_agent._annotate_turn_message` 追加写回他这条消息的入库内容，之后几轮在历史里原位可见；不要再把旧图转述放进资料（模型分不清新旧）。对话模型拒收图片（`LLMMultimodalUnsupportedError`）时同一轮改走转述重试。图片仍会照常放进虚拟电脑，但看图不能靠 run_code 读文件信息。
- 已知只收文字的对话模型（`turn_media.known_text_only`：deepseek-chat/reasoner、mimo-v2/2.5-pro/flash）在 `AI_DEFAULT_MULTIMODAL=auto` 下当作不支持图片，群聊和私聊都改走识图模型转述；没配 `AI_VISION_MODEL` 时由 `llm.implied_vision_model` 推断同家识图模型（mimo-v2/2.5-pro/flash → mimo-v2.5，走默认服务商）；推断不出时识图路线是 none，WebUI 识图卡会给出改法。
- 私聊虚拟电脑（`private_computer`）每人一个工作区 `p<QQ>`，只能读写/发送本人工作区的文件；策略看 `SandboxSettings.private_*`，次数按人计（`reserve_private_run`），不占群/成员配额。私聊发来的媒体经 `import_media` 放进 `inbox/`（`PRIVATE_AGENT_ACCEPT_MEDIA` 和 `private_inbox` 都开着才放）；只发图片/文件的好友消息也会交给 Agent（`event_media` 里 `type` 必须放在 `**data` 之后，图片段 data 自带 `type` 字段）。
- 大文件入箱（1GB 级）：`sandbox_inbox.fetch_file` 把字节落到 Bot 端临时文件（`_Fetch`，用完 `close()`），`SandboxClient.write_file(Path)` 流式 PUT，runner `write_file_stream` 边收边写；声明大小超过 `BASE64_SAFE_BYTES`（8MB）的附件**不走**会回 base64 的 OneBot 接口（NapCat 的 WS 帧会超 16MB 断线），私聊文件用 `get_private_file_url`、群文件用 `get_group_file_url` 拿链接下载。私聊上限看 `private_inbox_max_file_mb`（默认 1024），再夹到 runner 的 `max_upload_mb`；超限提示不得建议压缩分卷或网盘。
- 私聊附件入箱（`private_computer.import_media`）只看 `private_inbox`，**不套**群聊的 `inbox_enabled` / `inbox_types`，所有格式原样放入（可执行后缀不补 .txt）。协议端接口（`get_private_file_url`、`get_file`）返回的下载链接按 `trusted_host_allowed` 放行任意公网地址（QQ 私聊文件常是 IP 直连链接），仍挡内网/回环/单段主机名；消息段里的链接仍只认 `AGENT_SANDBOX_INBOX_HOSTS`。协议端报“接口不存在/不支持”时换下一种取法，不得当成文件过期。文件没进虚拟电脑时 `private_file_text.read_files` 在 Bot 端抽 txt/代码/docx/pptx/xlsx 文字兜底，节选写回历史。
- 能力包：`computer_kits`（list/open，只读）和 `computer_do` 是私聊虚拟电脑的渐进式披露工具；目录与参数 DSL 在 `computer_kits.py`，能力本体在 `sandbox/yawnkit/caps/`。`computer_do` 先按目录 schema 校验参数，再生成 `json.loads(<JSON 字面量>)` 传参的一行代码（不把模型字符串拼进源码），解析 `<<YAWNKIT_RESULT>>` 标记里的结果；没解析到结果时如实报失败。`net.*` 在 `private_network` 关闭时直接拒绝。新增/改能力时两边一起改，`tests/test_private_computer_kits.py` 对照签名检查；工具总数变化时同步 `tests/test_private_agent_v2.py`。
- 发言判断（`private_turn_taking`）：最终回复只写 `【不回】` 时不发不落库（返回 `""`），只写 `【等他说完】` 时挂 `PRIVATE_AGENT_WAIT_SECONDS` 计时器，期间没新消息由 `resume_after_wait` 接话；想好的回复还没发、他又发来新消息（`chat_state` 的排队数或到达计数变了）时让给下一轮，连续让 `MAX_YIELDS` 次或超过 90 秒就照常回。本轮已有写操作成功时不沉默、不让。判断“说没说完/要不要回”只交给模型，系统只看标记、队列、计数和时间，**不做关键词匹配**。
- 团队通信（`private_team`）：每人一个留言板（个人数据 `mail`，最近若干条），地址是 `main`、任务编号、项目成员名或 `all`（同项目内广播）。小助手用 `team` 工具收发，主智能体每轮把未读放进资料 `helper_messages`，用 `tasks` 的 `message` 动作回话；小助手 `need_reply` 发给 main 时去抖 3 秒触发一次协调回合（`private_agent.coordinate`，只带 `tasks` 工具，可以 `【不回】`）。
- **对外只有一个“我”**（`private_voice`）：用户看不到主/子智能体的分工。所有发给用户的文字（主回合、汇报轮、协调轮、进度消息）都经 `private_agent.one_voice` → `private_voice.scrub_internal` 清掉“小助手/子智能体/主助手/任务编号/工具名”（人设名里本身带这些词时不替换名字）；新增发送通道也必须过它。提示词里不得再让模型说“交给小助手了”。子智能体的任务单自动带 `chat_so_far`（最近聊天节选，只含他和 Bot 的原话）、`asked`、`voice`（人设短卡）、`call_them`，`report_progress`/`ask_user` 的文字是“我”直接对他说的话（`private.worker_voice`）。
- 进度消息默认 `PRIVATE_AGENT_PROGRESS_STYLE=natural`：不带「标题」前缀，他同时有多件事在做（一个项目算一件）才带“X那边：”，计数放句尾；和上一条相同的不发，距上一条（派活那句回复也算）不足 `PRIVATE_AGENT_PROGRESS_MIN_GAP_SECONDS` 的只记录不发；实际发出的记进任务 `told`，在资料 `tasks.told_him` 和汇报里给模型看，避免重复。
- 派活意愿：`delegate_task` 只有 `goal` 必填（标题从 goal 截、tools 按电脑可用/附件推断），本轮附件引用自动并进 brief，`follow_up_of` 把上一件做完的结论与文件带过去；开着任务时派活工具排在工具表最前。主回合自己调重活工具（`private_voice.HEAVY_TOOLS`）达到 `PRIVATE_AGENT_HANDOFF_AFTER` 次时插一次 `private.handoff_nudge`；工具轮次用完还在调工具且调过重活时（`PRIVATE_AGENT_AUTO_HANDOFF`），`_auto_handoff` 把这件事连同已查到的结果放到后台，最后一轮改用 `private.auto_handoff`；放不进去（额度/关闭）照旧 `private.final_round`。判断只看工具名、次数和附件结构，不做关键词。
- 汇报：前后脚做完的几件事等 `PRIVATE_AGENT_REPORT_BATCH_SECONDS` 合成一次（主任务带 `batch`），每人一把汇报锁；`report_task` 拿到回合锁后用 `_already_told` 复查，已经说过的不重复。他正好在说话时，没汇报、没被免打扰压着的结果放进资料 `just_finished` 顺着本轮说，**送达后**才 `mark_reported(via="chat")`，本轮沉默/让出则留给汇报轮。等回答（blocked）的汇报轮带 `tasks` 工具，聊天里已有答案就直接 answer 并【不回】。兜底汇报用第一人称、不带「标题」。
- 多成员项目（`delegate_project`）：2~6 个成员，`depends_on` 成 DAG（有环拒绝），没有依赖的先开工；成员结束后 `_advance_project` 把依赖成员的成果（或“没做成”）并进下游任务单再开工，全部结束才整体汇报一次（成员不单独汇报）。项目记在个人数据 `projects`。
- 子智能体循环：`plan`（步骤清单）/`memo`（跨续跑保留的笔记）工具，上下文超 `CONTEXT_CHAR_BUDGET` 时 `compact_history` 只截早先的工具结果（保留 `sandbox:` 引用、不动最近 12 条）；轮次用完未交付按 `PRIVATE_AGENT_TASK_CONTINUATIONS` 续跑；`finish` 时计划还有未完成步骤会被要求自查一次。
- 后台小助手（主从多智能体）：主智能体用 `delegate_task` 派活，`private_tasks` 管理队列与并发（每人 `PRIVATE_AGENT_TASK_PER_USER`、全站 `…_TASK_SLOTS`、每日 `…_TASK_DAILY`、单个 `…_TASK_TIMEOUT_SECONDS`），`private_worker` 跑子智能体。子智能体**只看任务单**（不带聊天记录、记忆或个人数据），工具只有 web/computer/toolbox 三族 + `finish`/`ask_user`/`report_progress`，不能写个人数据、不能发消息；`finish.files` 只保留本任务工具结果里出现过的 `sandbox:` 引用。
- 子任务每完成一步调 `report_progress`，`private_tasks` 直接发模板进度（不过模型、不进会话历史，免打扰不发，每个任务最多 20 条）。结束后的汇报轮（`private_agent.report_task`）与普通回合共用 `user_turn_lock`，不带工具，由主智能体按人设转述；没做成或在等回答时过 `verify_claims` 不许说“弄好了”。免打扰时段完成的汇报压着，由 `private_scheduler` 的 `flush_held` 补发。
- 任务记录存在个人数据 `tasks`（最近 20 个），不建表；`ask_user` 的子智能体上下文只在内存（24 小时），丢了就把回答并进任务单重做。进程启动时 `queued`/`running` 一律记失败（重启中断），不自动重跑；`my_data` 清空数据前先 `cancel_all`。WebUI 用户抽屉能看任务、取消任务（`DELETE /users/{id}/private-agent/tasks/{task_id}`）。
- **每用户私聊人设**（`private_persona`）：存在个人数据的 `persona` 键（`private_store.empty_record` 里必须有这个键，`_normalize` 会丢未知键），不建表、无迁移。生效顺序：本人专属卡（`custom`）→ 本人选的人设库模板（`library`，按模板 id 引用，模板删了标 `missing_template` 回退）→ 策略 `default_template_id` → 群聊全局 → 内置。策略总开关关掉时本人设置保留但暂不生效（`suspended`）。只有前两种算“专属”（`PERSONAL_SOURCES`），这时才加 `private.persona_scope` 段（化解 `private.role` 里“同一份人设”的说法）、才让定时问候带口吻、才按表现力收窄表情包额度（只收窄，不放宽）；默认用户的提示词与改动前逐字一致。
- 人设写入一律经 `private_persona._apply`：校验锁定/每日次数/修订号（409），记历史（最多 10 版，含改了哪些字段），`undo` 弹回上一版。本人改（`my_persona` 工具，`by=self`）额外过 `looks_like_instruction` 与 `contains_pii`，并受策略的可改字段/预设/模板开关约束；管理员改不计次数、不受锁定限制。所有卡片都过 `persona_override_reason`。人设库快照由 `persona_library.refresh_private_library` 刷新：人设库任何增删改（WebUI `_library_changed`）都要调它，否则私聊侧看不到新模板。
- **私聊提示词作用域**：`prompt_sections` 的 `user:<QQ>` 作用域只收私聊分段（`private.*`、`ptool.*`），不能和群号同时给；解析顺序 本人草稿 → 本人 → 全局草稿 → 全局。不显式传 `user_id` 时取 `use_private_user` 绑定的当前用户——主回合、汇报、协调、小助手、记忆整理五个入口都要包一层（带 `pipeline`），新增私聊链路时同样要包，否则本人覆盖不生效、抓取也进不了他的缓冲。群聊工作台的目录不再列出私聊分段，私聊分段只在「私聊 Agent」页管理；全局作用域和群聊共用，导出时只留私聊分段，`replace_scope` 不允许用在全局。
- 私聊请求抓取：有私聊用户绑定、没有 Trace 时，`prompt_capture` 把快照放进这位用户自己的环形缓冲（不再混进 `_background`），附 `userId`、`pipeline`、`persona`；WebUI 可暂停/清空。注册表的链路清单（`private_prompt_registry.PIPELINES`）要和真实拼装顺序一致，`tests/test_private_prompt_registry.py` 检查每个私聊分段都至少出现在一条链路里——新增私聊分段时同步加进链路。
- WebUI 用户抽屉能看私聊 Agent 状态、暂停/恢复自动回复、删除私聊记忆。核心侧查询（`turns_today`、`private_memory_query`、`delete_private_memories`、类别名 `PRIVATE_MEMORY_LABELS`）放在 `private_chat_bridge.py`，Agent 与 WebUI 共用；改私聊记忆的存储标记时两边一起生效，不要在 `yawn_agent` 里另写一份。
