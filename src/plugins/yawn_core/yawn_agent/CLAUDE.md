# yawn_agent — 群聊 Agent 子插件

QQ 群里的 LLM 群友：被 @/唤醒词触发的对话、主动插话、短会话续聊、长期记忆、多模态、
受权限分级约束的 OneBot 工具。由父插件的 `_load_sub_plugins()` 动态加载。

**本目录的硬约束写在 [AGENTS.md](AGENTS.md)，下方已导入；改代码前必须遵守。**
根目录 [AGENTS.md](../../../../AGENTS.md) 中关于 RPG 的约束不适用于这里。

@AGENTS.md

## 一轮对话的数据流

1. `agent.py` 监听群消息 → `message_parser.py` 归一化（含嵌套引用、转发摘要）→ 落库 `GroupAgentMessage` → 判定是否触发。
2. `turn_intake.py` 回合整形（只喊一声等待补充、同成员连发合批）→ `collector.py` 有界队列。
3. `dialogue.py` 编排：`context_loader.py` / `context_repository.py` 取数据 → `context_history.py` 稀疏化与相关性筛选 → `context_budget.py` 装箱 → `prompt.py` 拼提示词（`persona.py` 编译人设）。
4. `tool_router.py` 零成本选本轮工具 → 调 `../llm.py` → `tools.py` + `tool_handlers/` 执行（`tool_registry.py` 声明 schema 与权限级别）。
5. `speech*.py` 生成并质检 `SpeechPlan` → `outbound.py` 校验并发送 → `speech_finalize.py` 推进状态。
6. 全程 `execution_trace.py` 记录有界、脱敏的诊断轨迹，供 WebUI 调试页使用。

主动发言走 `proactive.py`（调度/副作用）+ `proactive_policy.py`（决策）；短会话在 `conversation.py`。

## 模块分组

| 分组 | 文件 |
|---|---|
| 入口与配置 | `__init__.py`、`agent.py`、`config_store.py`、`command_definitions.py`、`commands.py`（群内配置/记忆命令）、`log.py` |
| 入站与回合 | `message_parser.py`、`turn_intake.py`、`collector.py`、`turn_taking.py`、`turn_fallbacks.py`、`activity.py` |
| 对话编排 | `dialogue.py`、`dialogue_support.py`、`dialogue_turn_support.py`、`dialogue_runtime_support.py` |
| 上下文 | `context.py`、`context_loader.py`、`context_repository.py`、`context_history.py`、`context_budget.py`、`topic_state.py`、`emotion.py` |
| 提示词与人设 | `prompt.py`、`persona.py`、`reactions.py` |
| 工具 | `tool_registry.py`（schema/权限/白名单别名）、`tool_router.py`（选择/发现打分/意图提示）、`tools.py`（执行与二次校验）、`tool_args.py`（参数校验、错误分类、回合账本）、`tool_execution.py`、`tool_support.py`、`tool_result_speech.py`、`tool_handlers/`（按族：`admin`、`files`、`history`、`member`、`memory`、`message`、`notice`） |
| 发言 | `speech_format.py`（QQ 纯文本清洗）、`speech_claims.py`（完成声明核验）、`speech.py`、`speech_act.py`、`speech_policy.py`、`speech_native.py`、`speech_quality.py`、`speech_scorecard.py`、`speech_runtime.py`、`speech_finalize.py` |
| 发送与能力 | `outbound.py`（唯一发送通道）、`capabilities.py`（OneBot 能力矩阵与机器人权限）、`safe_download.py`（出站下载白名单与重定向校验） |
| 主动与短会话 | `proactive.py`、`proactive_policy.py`、`conversation.py` |
| 记忆 | `memory.py`（整理、排序、治理）、`memory_hygiene.py`（脱敏/注入/敏感类别/近似去重）、`memory_retrieval.py`（工具检索）、`memory_access.py`（召回计数缓冲）、`memory_consolidation.py`（周摘要）、`memory_self.py`（成员自助记住/忘掉） |
| 媒体 | `media.py`、`media_context.py`、`media_provider.py`、`media_store.py`、`media_caption.py` |
| 诊断 | `execution_trace.py` |
| 本地测试 | `tests/test_context_and_parser.py`（大部分 Agent 测试在仓库根 `tests/test_agent_*.py`） |

## 关联位置（跨目录）

- **数据表**：Agent 的模型不在本目录，而在 [../data_models/](../data_models/CLAUDE.md)（`group_agent_config`、`group_agent_message`、`agent_memory`、`agent_audit`、`agent_media_*`）。改表要走迁移，见 [data/CLAUDE.md](../../../../data/CLAUDE.md)。
- **LLM 客户端**：[../llm.py](../CLAUDE.md)（模型路由、并发上限）。
- **WebUI**：后端 [../webui/agent*.py](../webui/CLAUDE.md)；前端 [webui/src/agent-*](../../../../webui/CLAUDE.md)（cockpit、配置面板、调试轨迹）。
- **测试**：[tests/test_agent_*.py](../../../../tests/CLAUDE.md)、`tests/test_group_agent.py`；各改动该跑哪些见 AGENTS.md「质量门槛」。
- **设计文档**：[docs/agent-speech-*.md](../../../../docs/CLAUDE.md)；配置项见 [docs/configuration/ai-agent.md](../../../../docs/configuration/ai-agent.md)。
