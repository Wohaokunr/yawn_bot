# Agent Speech P9-P11

## P9: Speech Act

`SpeechPlan` 继续描述“要说什么”，P9 新增 `speech_act.py` 描述“这句话在完成什么对话任务”。

当前确定性话语动作包括：

- `answer`：明确 @/reply 时优先直接解决问题，不能用反问代替回答。
- `acknowledge`：对“嗯/懂了/收到”等短确认只做自然确认，不扩写成新话题。
- `react`：轻量 reaction 场景不再补同义长句。
- `tool_report`：工具结果只报告真实结果和必要下一步。
- `close`：识别“解决了/先这样/不用了”等自然收束信号，不追加 CTA 或新问题。
- `continue`：普通承接只贡献一个相关新信息点，不机械续聊。

该层不决定权限、不执行工具，也不决定主动发言是否应该发生。

## P10: Group Turn Taking

新增 `turn_taking.py`，只读取 `current_turn`、`topic_state` 和最近 Prompt 消息，计算低/中/高三档话轮压力。

- 明确 @/reply 永远获得完整回答优先级，不因 Bot 最近说过话而故意缺答案。
- 非明确互动中，如果最近 6 条里 Bot 已发言至少 2 次且当前话题有多人参与，则进入 `high` 压力：只回应一个最相关点，优先短句/轻反应，不逐人作答、不追加占话轮的反问。
- `medium` 压力只要求克制，不改变事实完整性。
- P10 只约束已经被现有控制面选中的发言，不绕过 `proactive_policy.py` 的 `speak/wait/close` 决策。

P9/P10 均编译进易变 Prompt 尾部，不修改静态前缀、缓存键或数据库结构。

## P11: Speech Scorecard

新增 `speech_scorecard.py`，提供完全离线、零额外模型调用的场景评测：

- 复用运行时 `speech_quality.py` 检查 boilerplate、用户复述、通用 CTA、短场景过长和近期重复。
- 增加话语动作检查，例如 `close` 后重新提问、`acknowledge` 过度展开、`react` 过长。
- 增加高话轮压力下的超长回复与追问惩罚。
- `SpeechScenario` / `run_speech_scorecard()` 可直接放进 pytest/CI，返回稳定分数、扣分原因和失败场景名。

P11 的目的不是用另一个 LLM 给主模型打分，而是把“群聊自然度不能回退”的关键行为变成可重复的工程门槛。
