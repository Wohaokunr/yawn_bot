# yawn_agent 记忆系统 bug 修复与优化

## 背景

记忆系统链路：`agent.py` 落库原始消息（7 天保留）→ `proactive.py` 定时任务（每日 03:30 全量 + 每 4 小时增量）调 `memory.py: compact_group_memory` 提取摘要/画像/关系 → `dialogue.py: _load_context` 按相关性重排注入提示词；命令层（`commands.py`）与 WebUI（`webui/app.py`）提供治理入口。

## 修复项

### 1. 游标停摆（高）— `memory.py: compact_group_memory`

问题：游标推进（L508）在 `if fresh_rows and config is not None:` 块内。当一批未整理消息全部来自隐私退出用户时 `fresh_rows` 为空，游标永不推进，每 4 小时/每日重复拉取最多 500 行直到消息 7 天过期；WebUI `pendingMessages` 永远显示高位。混合场景下游标只推进到 fresh 最大 id，隐私用户的队尾消息也被反复重读。

修复：提取逻辑全部成功执行后，把游标推进移出 `if fresh_rows` 块：只要阈值达标（`rows` 非空）且 config 存在，就推进到 `max(row.id for row in rows)`（全部取回的行，含被隐私过滤掉的）。保持两点语义不变：阈值未达标时仍不推进（攒批等待）；LLM 摘要抛异常时异常传播、游标不推进（下轮重试）。

### 2. "我是X"正则污染画像（中）— `memory.py: _extract_structured_memories`

问题：L697 正则把谓语陈述（"我是真的服了"）当昵称提取，固定 confidence=0.85；`merge_profile_update` 规则是"新置信度≥旧值即覆盖"，一条垃圾匹配可覆盖真名。

修复：抽小助手 `_match_display_name(text) -> tuple[str, float] | None`（便于纯函数测试）：
- 强模式 `我叫/称我为/叫我` → confidence 0.85（行为不变）；
- 弱模式 `我是` → confidence 0.6，且捕获长度 ≤ 12、不得含功能词（的/了/不/很/在/都/也/就/还/吗/呢/吧/啊 等单字集合）——含功能词直接丢弃，连弱记录都不建。
- 合并更新处把硬编码 0.85 改为按模式置信度传入，弱匹配（0.6）无法覆盖强记录（0.85），强可覆盖弱。

### 3. LLM 提取不校验 user_id（中）— `memory.py: _store_model_facts / _store_model_relations`

问题：接受幻觉 QQ 号或 `user_id=0`（LLM 省略时），本批真实发言者集合明明可得。

修复：`compact_group_memory` 里计算 `valid_user_ids = {row.user_id for row in fresh_rows}`，作为新参数传入两个函数；facts 的 `user_id`、relations 的 `subject/target` 不在集合内即跳过。

### 4. 提交冲突回滚后返回值失真（低）— `memory.py: compact_group_memory`

`except IntegrityError` 回滚分支返回 0，不再返回 `deleted.rowcount`（回滚后删除并未生效，WebUI 手动整理的调用方会误当成功）。

### 5. 活跃度统计低估（低，优化）— `dialogue.py` + `proactive.py`

问题：`_load_context` 用最新 40 条、`_collect_candidates` 用最新 60 条消息在 Python 侧算 60 分钟窗口统计，活跃群覆盖不全，`messages_60m`/`participants_60m`/`member_messages_60m` 低估，影响 coldness 评分与插话门槛。另外 `_collect_candidates` 的统计不过滤隐私退出用户，与 `_load_context` 语义不一致。

修复：在 `dialogue.py` 新增聚合查询助手（一条 SQL，CASE 计数 + distinct + max）：`messages_5m/20m/60m`、`participants_60m`、`member_messages_60m`（排除 role="bot"）、`last_message_at`、`last_member_message_at`、`mentions_60m`（contains "@"）；`replies_60m` 用 SQLite `json_array_length` 判 JSON 非空。统一排除隐私退出用户。`_load_context` 与 `_collect_candidates` 都改用该助手；`_collect_candidates` 保留"保留期内无任何消息则跳过"的判断（等价于 `last_message_at is None`）。

### 已知限制（记录不修复，用户已确认另开任务）

- 隐私退出的深度清理随原始消息 7 天过期而断链（evidence 关联失效，跨主体画像与群摘要残留）。彻底修复需给 `AgentMemory` 加 `related_user_ids` 冗余列（含迁移），本轮不做，在 `delete_member_memories` 注释中写明。
- `updated_at`/`created_at`（SQLite CURRENT_TIMESTAMP=UTC）与 `now_beijing()`（UTC+8）存在 8 小时偏差，仅影响 21 天半衰期约 1% 权重和 TTL 早删 8 小时，不动。

## 测试与验证

- `tests/test_agent_memory.py` 新增纯函数测试：`_match_display_name` 强/弱/垃圾用例；`_extract_structured_memories`（假 session 收集 add）弱匹配不覆盖强记录、垃圾不建记录；`_store_model_facts` 幻觉 user_id 被跳过。沿用现有"构造 ORM 实例不落库"模式。
- 运行 `uv run pytest tests/test_agent_memory.py tests/test_group_agent.py -q`、`uv run ruff check`（触及文件）、`uv run pyright`（触及文件）。保持 LF 行尾（Windows 编辑易引入 CRLF 破坏 ruff）。
- 不涉及 ORM 模型与迁移改动，无需 `nb orm` 检查。

## 完成记录（2026-08-22）

- [x] 游标推进已移到成功提取之后，按整批 `rows` 推进，隐私过滤全批和混合批次均不会停摆；阈值不足或模型异常仍保留原有不推进语义。
- [x] 自称画像提取已拆分强/弱模式：弱模式降置信度并过滤功能词，强模式可覆盖弱模式，补充 `叫我` 形式。
- [x] LLM 画像与关系主体已限制在本批真实发言者集合内；提交冲突回滚返回值改为 `0`。
- [x] 对话与主动发言共用一条聚合 SQL 计算活跃度，精确覆盖保留期内消息并统一排除隐私退出用户。
- [x] 已补充回归测试；`pytest` 20 项通过，触及文件 Ruff 与 Pyright 均通过，`git diff --check` 通过。
