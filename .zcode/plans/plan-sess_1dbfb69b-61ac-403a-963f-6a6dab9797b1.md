# Agent 记忆系统进一步优化方案

方向（已确认）：记忆质量与生命周期 + 成本效率 + 检索注入改进；**不引入向量嵌入，保持纯词法**。全部改动不涉及表结构变更（`memory_type` 是自由字符串列，无数据库级枚举约束），**无需迁移**。

## A. 核心记忆持久层（memory_type="core"）

解决"重要事实 90 天过期 + 21 天半衰期被埋没"的问题。

**写入侧（memory.py）**
- 新增常量：`_CORE_PROMOTE_CONFIDENCE = 0.85`、`_CORE_PROMOTE_EVIDENCE = 3`。
- 新增 `_maybe_promote_core(row)`：`source_kind == "auto"` 且 `confidence >= 0.85` 且 `len(evidence_message_ids) >= 3` 时把画像行升级为 `memory_type="core"`、`expires_at=None`（永不自动过期）。在 `_store_model_facts`（memory.py:523-541 更新分支）与 `_extract_structured_memories`（memory.py:1238-1255 更新分支）的置信度棘轮之后调用。确定性 display_name 提取起始置信度 0.9、每次再确认 +0.05，两三次复现即可晋升——符合其高精度定位。
- `_prefetch_profiles`（memory.py:435）改为 `memory_type.in_(("profile", "core"))`：**必须改**，否则晋升行从预取图消失，同 key 再提取会 INSERT 新行触发 IntegrityError 整批回滚。

**读取侧（ranking + 注入）**
- `rank_memories`（memory.py:224）按类型半衰期：`{"core": 不衰减, "summary": 45d, "profile": 60d, "manual": 90d}`，默认 21d（见 F 一起重构）。
- dialogue.py:432 发言者查询 `in_(("core","profile","manual"))`；dialogue.py:435 排序 case 改为 core=0/profile=1/else=2（核心事实优先注入）；dialogue.py:466 `direct` 过滤集合加 "core"。
- tools.py:489（`get_person_profile`）、commands.py:241（`/Agent画像`）查询加 "core"。
- webui/app.py:203 `MemoryCreateBody.type` Literal 加 "core"（管理员可手工钉住）；service.py 的序列化与 group_by 自动兼容。前端 types.ts 如有联合类型一并补充。

**治理路径已验证无需改动**：purge 只删 `expires_at < now`（core 为 NULL 不删）；rebuild 删 auto 行（core 晋升自 auto，可从原文重推导）；成员隐私删除按 subject/related_user_ids 命中 core 行。

## B. 多值事实（爱好/偏好不再互相覆盖）

- 新增 `_LIST_FACT_KEYS = {"hobby","preference","skill","recurring_topic"}`、`_LIST_FACT_MAX = 5`。
- 新增 `merge_list_profile_update(old, old_conf, new, new_conf)`：值以"、"连接存储；新值已存在→确认路径（不动内容）；新旧互为子串→保留更长（信息更全）的一条；否则追加，超 5 条时淘汰最旧（FIFO）；置信度取 max。
- `_store_model_facts`（memory.py:527）对 list key 走新合并；`display_name/preferred_address` 保持原 `merge_profile_update` 单值覆盖语义。确认棘轮条件相应改为"新值已在值列表中"。`_extract_structured_memories` 只写 display_name，无需改。
- 加入 `__all__` 以便测试。

## C. 关系边治理

- `_store_model_relations`（memory.py:609-610）note 刷新：非空 note 且（原 note 为空 或 新置信度 ≥ 原置信度 + 0.15）时替换，替代"只在空时写入"。
- 新增 `decay_stale_relations(session, now)`：每日一次批量衰减 `last_seen_at < now-90d` 且 `source_kind=="auto"` 的边 `confidence = max(0.3, confidence*0.85)`（下限防僵尸边），只在 proactive.py `_compact_tick(cleanup=True)`（每日 03:30）分支调用，避免整理路径写放大。

## D. 低信号批次跳过 LLM

- 新增 `_LOW_SIGNAL_BATCH_CHARS = 600`。
- `_compact_group_memory_locked`（memory.py:861 前）：`fresh_rows` 总文本字符数 < 600 时跳过 `_model_summary` 与摘要写入，仍执行 `_prefetch_profiles/_prefetch_relations/valid_member_ids` + `_extract_structured_memories`（昵称/@提及提取零成本照跑），照常推进游标、purge、commit，dbg 记录"低信号批次跳过模型"。表情包/贴图刷屏不再消耗模型调用；当天可能无摘要，属可接受取舍。

## E. 触发阈值调优

- proactive.py:580-581：`_MEMORY_TRIGGER_COUNT` 12→16、`_MEMORY_MAX_PENDING_AGE` 5min→8min，更新 :684 注释。配合 D 降低活跃群整理频率、提高单次批次利用率。

## F. 检索与注入改进（纯词法）

- **IDF 加权重排**：`rank_memories` 重构——对候选行 key + content[:400] 统计 token 文档频率，查询 token 权重 `log(1 + N/(1+df))`，relevance = 命中权重和 / 查询总权重，压制"的了/回复"类 ubiquitous bigram 噪声；新增 keyword-only `topic_hint: str = ""` 参数（active_topic 令牌并入查询集、不挤占近 10 条消息窗口），dialogue.py `_load_context` 传入 `config.active_topic`。
- **候选池扩容**：dialogue.py:398 通用池 120→160（salience 序），另加 60 行 updated_at 序的"复现池"（捞回 salience 榜外但近期被再确认的记忆），并入现有去重循环。
- **发言者动态预算**：`speaker_take = min(11, 8 + max(0, 3 - topic数))`——话题记忆不足 3 条时把名额让给发言者画像；core 行因 A 中排序调整始终最先注入。

## 测试与收尾

- tests/test_agent_memory.py 新增：core 晋升条件与不匹配不晋升、core 行在 `_purge_expired` 90 天后存活、`_prefetch_profiles` 含 core（同 key 再提取不 IntegrityError）、多值合并（追加/封顶 FIFO/子串归并/重复确认不重复）、低信号批次跳过模型（monkeypatch `_model_summary` 断言未调用）且游标推进 + 确定性提取仍生效、`_memory_due` 新阈值边界（15/16 条、7/8 分钟）、IDF（常见 token 不稀释、稀有 token 命中优先）、按类型半衰期（老 profile 不再输给新低质行）、note 刷新、`decay_stale_relations` 下限。
- tests/test_agent_prompt_and_persona.py：现有分层/字节稳定测试回归；补一条 core 行进 speaker 易变层、稳定层不受影响的断言。
- tests/test_webui.py：MemoryCreateBody 接受 "core"。
- docs/development-roadmap.md 按项目惯例追加一节演进记录。
- 验证：`pytest tests/test_agent_memory.py tests/test_agent_prompt_and_persona.py tests/test_webui.py`，并对改动文件跑 Ruff 与 Pyright。