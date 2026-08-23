# Agent 记忆系统优化：上下文分层提升缓存命中 + 可靠性修复

## 已确认的不足

**缓存命中（用户重点）**
1. `prompt.py:build_messages` 把全部动态上下文序列化为**单条** system 消息，`canonical_json(sort_keys=True)` 按字母序输出：`active_topic → activity → emotion_state → group_id → group_name → members → memories → messages → relations`。每次请求都变的 `activity`/`emotion_state`/`active_topic` 排在最前，服务端前缀缓存到第一个变化 token 即失效——**约 6000 字符的记忆预算 + 关系 + 消息完全无法缓存**。
2. 注入记忆混合了「慢变」（群日摘要、跨群共享摘要——只在整理任务运行时变化）和「每请求变」（当前发言人画像、话题相关性重排结果），未区分。
3. 本地缓存指标 `prompt_cache_key` 只覆盖静态前缀，无法观测稳定层命中率。

**记忆系统可靠性**
4. `memory.py:_purge_expired` 无条件删除过期 `GroupAgentMessage`，不区分是否已整理（`id > last_compacted_message_id`）。整理连续失败超过 `raw_retention_days`（默认 7 天）时素材静默丢失（游标不动、消息先被删）。
5. `_delete_member_memories_locked` 删除一个成员时删掉**该群全部** `AgentRelation` 边，其他成员间的关系一并丢失。
6. `tools.py:search_group_memory` 只做 SQL LIKE 子串匹配取前 10，无相关性排序，同义/部分命中时召回差。

## 改动方案

### A. 上下文按变化频率分层（核心，无 schema 变更）

目标消息结构（前缀缓存逐层命中）：

```
[system] 静态前缀（版本+规则+人设+工具）           ← 已稳定
[system] 群背景资料：{稳定层 JSON}                 ← 只在整理/改名时变化（新增）
[system] 当前群聊状态：{易变层 JSON}               ← 每请求变化
[user]   用户消息
```

- **`dialogue.py:_load_context`**：注入记忆的 `source_scope` 标签从 `speaker/group/shared_public` 细化为 `speaker / group_summary / topic / shared_public`（`summaries[:5]` → `group_summary`，`ranked_local[:3]` → `topic`）。6000 字符预算流水分配逻辑不变。
- **`prompt.py:build_messages`**：按字段拆分 context——
  - 稳定层：`group_id`、`group_name`、`source_scope ∈ {group_summary, shared_public}` 的记忆条目（群日摘要+跨群共享，均按 `memory_key` 日期序排列保证跨请求字节稳定）；
  - 易变层：`active_topic`、`activity`、`emotion_state`、`members`、`messages`、`relations` 及其余记忆条目（发言人画像+话题相关）。
  - 两层各自 `canonical_json` 序列化为独立 system 消息；`PROMPT_VERSION` 升级为 `yawn-agent-v4`。
- **`context.py:build_context`**：签名与扁平 dict 返回值保持不变（调用方零改动），仅在排序逻辑上把稳定层记忆改为按 `memory_key` 排序（`salience` 每次整理都会变，不适合做稳定排序键）。
- **`proactive.py`**：复用 `build_messages`，自动受益，无需改动。
- **缓存指标**：`prompt.py` 新增 `stable_context_key()`（sha256(稳定层 JSON)），`dialogue.py` 现有 `record_agent_cache` 逻辑增加 `("context", hit/miss)` 上报，`_PROMPT_CACHE_KEYS` LRU 同时跟踪稳定层指纹，便于验证优化效果。

### B. `_purge_expired` 保护未整理消息

- 签名增加 `compacted_cursor: int`；消息删除条件追加 `GroupAgentMessage.id <= compacted_cursor`（游标之后的过期消息保留等待整理）。
- 硬上限兜底：`expires_at < now - timedelta(days=30)` 的消息无条件删除，防止整理长期失败时无限堆积。
- 调用方 `_compact_group_memory_locked` 传入 `config.last_compacted_message_id`。

### C. 成员隐私删除只删相关关系边

- `_delete_member_memories_locked` 中关系边删除条件从「该群全部」改为 `or_(subject_user_id == member, object_user_id == member)`，其他成员间的关系保留。

### D. `search_group_memory` 工具检索增强

- LIKE 命中候选放宽到 30 条（保留现有隐私过滤），再用 `rank_memories(rows, [query], speaker_id=None, now, limit=10)` 按查询词 bigram 相关性+显著度重排取 10，替代「LIKE 前 10 即返回」。

## 测试与验证

- 更新 `tests/test_agent_prompt_and_persona.py`：新消息结构（4 条消息）、稳定层/易变层字段归属断言。
- 更新 `tests/test_agent_memory.py`：`source_scope` 标签变化；新增：
  1. 同一整理窗口内两次不同请求（易变层不同）稳定层 JSON 字节一致；
  2. purge：过期但 `id > cursor` 的消息保留、`id <= cursor` 的删除、超 30 天硬上限删除；
  3. 删除成员只删其相关关系边，无关边保留；
  4. `search_group_memory` 重排返回相关条目。
- 运行：`uv run pytest tests/test_agent_memory.py tests/test_agent_prompt_and_persona.py tests/test_group_agent.py`、Ruff、Pyright；无 schema 变更，不需要新迁移。
- `docs/development-roadmap.md` 追加本次演进记录（沿用现有格式）。

## 明确不做（后续可选）

- **embedding 语义检索**：默认端点（MiMo OpenAI 兼容）无 embeddings API 保证，引入外部依赖风险大；现有 bigram + 重排已覆盖字面相关场景。
- **日摘要周/月级 roll-up** 与 **每群记忆容量上限**：TTL（30/90/180 天）+ 6000 字符注入预算已兜底，属独立后续项，避免本次范围膨胀。
- **工具集随发起人权限分片**：管理员触发的请求占比极小，普通成员前缀占主导，碎片化影响可忽略。