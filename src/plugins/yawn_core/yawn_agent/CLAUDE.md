# yawn_agent — 群聊 Agent 子插件

QQ 群里的 LLM 群友：被 @/唤醒词触发的对话、主动插话、短会话续聊、长期记忆、多模态、
受权限分级约束的 OneBot 工具。由父插件的 `_load_sub_plugins()` 动态加载。

**本目录的硬约束写在 [AGENTS.md](AGENTS.md)，下方已导入；改代码前必须遵守。**
根目录 [AGENTS.md](../../../../AGENTS.md) 中关于 RPG 的约束不适用于这里。

@AGENTS.md

## 一轮对话的数据流

1. `agent.py` 监听群消息 → `message_parser.py` 归一化（含嵌套引用、转发摘要）→ 落库 `GroupAgentMessage` → 判定是否触发（不 @ 的隐式触发见 `implicit_trigger.py`）。
2. `turn_intake.py` 回合整形（只喊一声等待补充、同成员连发合批）→ `collector.py` 有界队列。
3. `dialogue.py` 编排：`context_loader.py` / `context_repository.py` 取数据 → `context_history.py` 稀疏化与相关性筛选 → `context_budget.py` 装箱 → `prompt.py` 拼提示词（`persona.py` 编译人设）。
4. `tool_planner.py`（无上下文轻量模型）判断要哪些工具包，`tool_preload.py` 按发现权限预装 → `tool_router.py` 定核心包/轮次/消息段 → 调 `../llm.py`（`turn_budget.py` 定超时与轮次）→ `tools.py` + `tool_handlers/` 执行（`tool_registry.py` 声明 schema 与权限级别）；慢回合反馈在 `progress_notice.py`。
5. `speech*.py` 生成并质检 `SpeechPlan` → `speech_rhythm.py` 规划分条与停顿 → `outbound.py` 逐条校验 → `speech_delivery.py` 按节奏发送 → `speech_finalize.py` 推进状态。
6. 全程 `execution_trace.py` 记录有界、脱敏的诊断轨迹，`prompt_capture.py` 附上每次真实发给模型的完整请求，供 WebUI 调试页使用。

主动发言走 `proactive.py`（调度/副作用）+ `proactive_policy.py`（决策）；冷场暖场门槛在 `idle_warmup.py`；短会话在 `conversation.py`。

## 模块分组

| 分组 | 文件 |
|---|---|
| 入口与配置 | `__init__.py`、`agent.py`、`config_store.py`、`command_definitions.py`、`commands.py`（群内配置/记忆命令）、`log.py` |
| 入站与回合 | `message_parser.py`、`implicit_trigger.py`（叫名字/接着提需求）、`turn_intake.py`、`collector.py`、`turn_taking.py`、`turn_fallbacks.py`、`turn_budget.py`（超时/轮次/时限）、`progress_notice.py`（贴表情与进度提示）、`activity.py` |
| 对话编排 | `dialogue.py`、`dialogue_support.py`、`dialogue_turn_support.py`、`dialogue_runtime_support.py` |
| 上下文 | `context.py`、`context_loader.py`、`context_repository.py`、`context_history.py`、`context_budget.py`、`topic_state.py`、`emotion.py` |
| 提示词与人设 | `prompt.py`、`prompt_sections.py`（全部提示词分段登记、WebUI 覆盖存储与解析、位置清单）、`persona.py`（PersonaCard、模板、解析/编译/行为推导）、`persona_library.py`（人设库与全局人设缓存）、`reactions.py` |
| 审计 | `audit_outcome.py`（审计 result → 成功/跳过/失败/待确认） |
| 工具 | `tool_registry.py`（schema/权限/白名单别名）、`tool_router.py`（核心包/发现打分/消息段/轮次）、`tool_planner.py`（工具路由模型）、`tool_preload.py`（按路由预装）、`tools.py`（执行与二次校验）、`tool_args.py`（参数校验、错误分类、回合账本）、`tool_execution.py`、`tool_support.py`、`tool_result_speech.py`、`tool_handlers/`（按族：`admin`、`checkin`、`files`、`history`、`inbox`、`member`、`memory`、`message`、`notice`、`sandbox`、`web`、`schedule`） |
| 代码沙箱 | `sandbox_client.py`（runner HTTP-over-Unix-socket 客户端、15 秒健康缓存）、`sandbox_policy.py`（开放范围/仅管理员/冷却/每日次数，JSON 落在 `data/agent_sandbox/`）、`sandbox_refs.py`（`sandbox:<路径>` 引用取回与清理）、`sandbox_output.py`（报错摘要、traceback 精简、按行截断）、`sandbox_inbox.py`（群聊附件入箱：收集本条/引用/历史附件、取字节链路、内容哈希命名、每轮自动放入）、`sandbox_inbox_store.py`（入箱事件 JSONL、隐私抹除）、`tool_handlers/inbox.py`（`import_attachments` 等三个工具）；runner 本体在仓库根 [sandbox/](../../../../sandbox/README.md) |
| 网页搜索 | `web_search_client.py`（DeepSeek Search/Fetch API 客户端：密钥来源、重试、错误分类、网址安全校验）、`web_search_policy.py`（开放范围/限额/每日用量/最近调用，JSON 落在 `data/agent_web_search/`）、`web_search_output.py`（查询运算符拼装、TTL 缓存、结果与正文投影）、`tool_handlers/web.py`（`web_search`/`web_fetch`/`web_search_info`）；WebUI 后端 `../webui/web_search.py`、前端 `webui/src/web-search/` |
| 表情包 | `sticker_library.py`（库与 `index.json`，sha256 去重、按群开放、待审核）、`sticker_policy.py`（开关/频率/冷却/每日上限/收藏策略/用量，JSON 落在 `data/agent_stickers/`）、`sticker_markup.py`（`[表情:名字]` 标记 ↔ `sticker` 伪段、菜单提示词，纯函数）、`sticker_send.py`（发送前解析与门控、本轮菜单）、`reactions.py`（旧 reaction_id 接口）、`tool_handlers/sticker.py`（`save_sticker`）；WebUI 后端 `../webui/stickers.py`、前端 `webui/src/stickers/` |
| 定时任务 | `schedule_spec.py`（纯规则引擎：单次/间隔/每天/每周/每月/cron、窗口、起止、跳过日期、描述与预览）、`schedule_policy.py`（开放范围/谁能建/数量与频率/免打扰/补跑/重试策略，JSON 落在 `data/agent_schedule/`）、`schedule_store.py`（校验、限额、去重、租约领取、暂停/恢复/取消）、`schedule_runner.py`（20 秒调度、到点唤醒 Agent 或直接提醒、重试与自动暂停）、`schedule_wake.py`（唤醒回合的 contextvar 与提示词载荷）、`tool_handlers/schedule.py`（四个工具） |
| 发言 | `speech_format.py`（QQ 纯文本清洗）、`speech_claims.py`（完成声明核验）、`speech.py`、`speech_act.py`、`speech_policy.py`、`speech_native.py`、`speech_quality.py`、`speech_scorecard.py`、`speech_runtime.py`、`speech_finalize.py`、`speech_rhythm.py`（发言节奏：三竖线分条、整段/分条/连发/蹦字、打字停顿）、`speech_delivery.py`（按节奏依次发送、催促合并、失败停止、逐条落库） |
| 发送与能力 | `outbound.py`（唯一发送通道）、`capabilities.py`（OneBot 能力矩阵与机器人权限）、`safe_download.py`（出站下载白名单与重定向校验） |
| 主动与短会话 | `proactive.py`、`proactive_policy.py`、`idle_warmup.py`（冷场暖场门槛/退避/骰子）、`conversation.py` |
| 签到与好感 | `member_standing.py`（发言人层的好感等级/连签投影、群聊互动加好感 `reward_group_interaction`）、`tool_handlers/checkin.py`（`checkin_for_me`/`get_my_checkin`）；业务逻辑在父插件 `../checkin_service.py`、`../affinity.py` |
| 私聊 Agent | `private_agent.py`（一轮编排：日上限、上下文、工具循环、完成声明核验、分条发送、私聊好感与记忆整理）、`private_context.py`（只读上下文：共同群、好感等级、签到、群里关系与画像，字符预算）、`private_tools.py`（29 个私聊工具的表与分发，都只作用于本人；`bot_status` 只给超级管理员）、`private_organizer.py`（笔记/待办/提醒/倒数日/心情/聊天偏好 + 提示词摘要）、`private_services.py`（好感明细、签到榜、小工具箱分发、私聊记录检索、我的数据、反馈、私聊联网搜索、管理员状态）、`private_toolbox.py`（计算/换算/日期/骰子/运势/世界时间，纯函数）、`private_store.py`（个人数据 JSON，每人一个文件，无迁移）、`private_scheduler.py`（20 秒一次：到点提醒、早安、签到提醒、生日、倒数日）、`private_memory.py`（私聊记忆 `scope="private"`）、`private_prompt.py`（提示词文本，登记在 `prompt_sections.py` 的 `private.*`）、`private_delivery.py`（回复分条 + 表情包/图片/文件发送、私聊表情包额度）、`private_computer.py`（每人一个虚拟电脑工作区 `p<QQ>`：run_code/computer_files、发送前取文件、私聊附件入箱）、`private_tasks.py`（后台小助手任务管理：并发槽、日上限、进度、汇报、追问、取消、重启恢复）、`private_worker.py`（子智能体循环：只看任务单，web/computer/toolbox + finish/ask_user/report_progress）；入口在父插件 `../private_chat_bridge.py` |
| 记忆 | `memory.py`（整理、排序、治理）、`memory_hygiene.py`（脱敏/注入/敏感类别/近似去重）、`memory_retrieval.py`（工具检索）、`memory_access.py`（召回计数缓冲）、`memory_consolidation.py`（周摘要）、`memory_self.py`（成员自助记住/忘掉）、`bot_bond.py`（Bot 自己和成员的关系：互动统计、有界推导、注入视图、工具与治理） |
| 媒体 | `media.py`、`media_fetch.py`（识图取字节链路、失败原因码、取图统计）、`media_context.py`、`media_provider.py`、`media_store.py`、`media_caption.py`、`media_memory.py`（看过的图的短期转述，给历史用）、`turn_media.py`（一轮的图片装载、多模态能力记忆、收图即转述） |
| 诊断 | `execution_trace.py`、`prompt_capture.py`（每次真实模型请求的完整快照，挂在 Trace 上，脱敏有界） |
| 本地测试 | `tests/test_context_and_parser.py`（大部分 Agent 测试在仓库根 `tests/test_agent_*.py`） |

## 关联位置（跨目录）

- **数据表**：Agent 的模型不在本目录，而在 [../data_models/](../data_models/CLAUDE.md)（`group_agent_config`、`group_agent_message`、`agent_memory`、`agent_audit`、`agent_media_*`、`agent_persona_template`）。改表要走迁移，见 [data/CLAUDE.md](../../../../data/CLAUDE.md)。
- **LLM 客户端**：[../llm.py](../CLAUDE.md)（模型路由、并发上限）。
- **WebUI**：后端 [../webui/agent*.py](../webui/CLAUDE.md)；前端 [webui/src/agent-*](../../../../webui/CLAUDE.md)（cockpit、配置面板、调试轨迹）。
- **测试**：[tests/test_agent_*.py](../../../../tests/CLAUDE.md)、`tests/test_group_agent.py`；各改动该跑哪些见 AGENTS.md「质量门槛」。
- **代码沙箱**：runner 与镜像依赖在 [sandbox/](../../../../sandbox/README.md)，WebUI 管理页后端 `../webui/sandbox.py`、前端 `webui/src/sandbox/`。
- **定时任务**：数据表 `../data_models/agent_scheduled_task.py`，WebUI 管理页后端 `../webui/schedules.py`、前端 `webui/src/schedules/`，测试 `tests/test_agent_schedule.py`、`tests/test_webui_schedules.py`。
- **设计文档**：[docs/agent-speech-*.md](../../../../docs/CLAUDE.md)；配置项见 [docs/configuration/ai-agent.md](../../../../docs/configuration/ai-agent.md)。
