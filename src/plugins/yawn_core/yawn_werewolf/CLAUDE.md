# yawn_werewolf — 狼人杀子插件

群聊狼人杀，四块板子：预女猎白（9–12 人）/ 预女猎白混（混血儿，12 人）/
禁言骑士（12 人）/ 禁票骑士（12 人），支持 LLM 驱动的 AI 玩家。由父插件
`yawn_core/__init__.py` 的 `_load_sub_plugins()` 动态加载。

## 模块地图

| 模块 | 职责 |
|---|---|
| `engine.py` | 游戏引擎：每局一个 asyncio 任务（`run_game`），独占状态变更与群播报 |
| `ai_player.py` | AI 驱动：每局一个驱动任务 + 若干后台决策任务，调用 LLM 合成 `Action` |
| `commands.py` | 命令入口：群命令 / 私聊命令 / 私聊自由文本监听，只做校验 + 投入行动 |
| `state.py` | 内存状态：`Game`/`PlayerState`/`Action`、注册表与身份守卫式清理 |
| `dsl.py` | 自由文本 → `Action` 解析（`parse_dm_action`），人类私聊与 AI 共用 |
| `roles.py` | 角色/阵营/死因枚举、板子注册表（`BOARDS`/`BoardSpec`，默认板子 `预女猎白`）、身份卡文本 |
| `api.py` | OneBot V11 安全封装：所有 API 异常降级为 warning，不打断游戏 |
| `config.py` | 玩法与超时配置（`WW_*` 环境变量） |
| `models.py` | ORM 模型（`bind_key=yawn_werewolf`）：仅 `WerewolfGame`/`WerewolfPlayer` 两张汇总表 |
| `migrations/` | **副本，勿改**。正本在 `data/nonebot_plugin_orm/migrations/yawn_core/yawn_werewolf/` |

## 核心契约

- **命令层只校验、只投递**：命令处理器把 `Action` 投入 `game.action_queue`，
  从不直接改 `Game` 状态。唯一例外是报名阶段的增删（`state.py` 的注册表函数）
  与空房解散时的 `phase=ENDED` + worker 取消。
- **引擎独占写入**：`current_speaker` / `vote_targets` / `vote_exclude` /
  `phase` 等信号只由引擎写入。阶段切换统一走 `_enter_phase()`（带日志）。
- **AI 驱动对 Game 只读**，副作用仅限三个通道：投入 `action_queue`、
  维护自己的 transcript（`public_log`/`private_log`）、代发 AI 群发言。
- **引擎 → 驱动的钩子必须同步**：`on_dm` / `on_announce` / `record_speech`
  只记录 + 置 `wake` 事件，绝不 await（引擎主循环正在等它们返回）。
- **AI 行动对引擎透明**：引擎不区分行动来源，照常二次裁决；被驳回的
  行动（私聊文本含"无效/无法使用/请重新"）给驱动一次重新决策机会。
- **骑士决斗中断发言轮换**：`DUEL_PHASES`（DAY_SPEECH/PK_SPEECH）内到达的
  `DUEL` 行动在 `_speech_rotation` 行动循环里交 `_resolve_knight_duel` 裁决——
  决斗到狼抛 `_DuelNightError`（`run_game` 与自爆并列捕获→判胜→入夜），
  决斗到好人骑士身亡、若狼人随即屠尽抛 `_ConcludedError`，否则白天继续。
  失败路径若发生过警徽移交，须恢复"除发言者外全员禁言"的轮换不变式并
  `_enter_phase` 回到本阶段（移交会把阶段切去 BADGE_TRANSFER）。
- **禁言/禁票执行点**：长老夜存 `game.silenced_seat`（进入长老阶段先清空，
  放弃/超时清空 `elder_last_target` 以打断连续链）；死讯同播禁言情况；
  禁言在 `_speech_rotation` 跳过发言人（全程不解禁），禁票在 `_vote_phase`
  经 `exclude_seats` 排除放逐环节投票资格（含 PK 投票，不含警长投票）。
  注意 `_collect_votes` 的逐条行动校验也查 `exclude_seats`，否则人类可绕过。
- **混血儿胜负不对称**：阵营恒为 GOOD、屠边算民边（`VILLAGER_SIDE_ROLES`），
  但个人胜负随主人阵营——仅 `_persist_end` 特判 `is_winner`、`_finish` 附注主人。

## AI 驱动时序要点

- 驱动主循环一次只处理一帧 `_process_phase`。**批量决策（上警 / 投票）
  必须用 `_spawn()` 派后台任务**，不能 `await asyncio.gather(...)`——
  否则 LLM 调用会阻塞循环，错过随后打开的发言窗口（历史上警长竞选
  AI 失声的根因）。
- 发言窗口优先级最高：`_process_phase` 顶部先检查 `current_speaker`。
- **骑士决斗决策**：与上警/投票一样走 `_spawn` 后台任务（`_maybe_spawn_knight`
  于 DAY_SPEECH/PK_SPEECH 派发），绝不阻塞发言窗口；LLM 超时夹取到
  `ww_ai_discuss_timeout`，迟到的决斗被引擎阶段门安全丢弃，等价不决斗。
- **狼队夜间两段式**：阶段一并行讨论（各 AI 狼发 `说XXX` 提议，引擎
  转发给队友狼；夜晚无发言窗口竞争，可直接 `gather`），阶段二串行
  出刀（后手狼读到队友提议 + 引擎刀型统计，天然共识）。
  `WW_AI_WOLF_DISCUSS=false` 退回单段直接出刀。
- LLM 经 `../llm.py` 的 `complete()`（非流式 + 总超时 + 并发上限 6）。
  推理模型首字节慢且推理耗 token，决策/发言调用须给足预算
  （`WW_AI_MAX_TOKENS` / `WW_AI_SPEECH_MAX_TOKENS`）并与阶段窗口对齐
  超时（`WW_AI_DECISION_TIMEOUT` / `WW_AI_SPEECH_TIMEOUT` 默认 90s；
  狼人阶段 `WW_WOLF_TIMEOUT` 180s，投票 90s，发言 120s），否则会
  `finish_reason=length` 截断返空或全程超时托管。
- 终辩（`SHERIFF_REVOTE`）引擎窗口 60s（`engine._FINAL_SPEECH_TIMEOUT`），
  AI 发言超时在 `_llm_speech` 内夹取到 50s。
- **降级链**：LLM 失败 → 格式纠正重试一次 → 仍失败按阶段安全默认
  （弃票 / 过 / 不竞选 / 撕警徽 / 空刀托管）。任何失败都不卡局。

## 配置键（`.env` 可覆盖，见 `config.py`）

`WW_MIN_PLAYERS` / `WW_MAX_PLAYERS`（引擎取其与当前板子支持人数的交集：
无交集→报名即流局；开局人数不在板子 `counts` 内→发牌前流局。12 人专属
板子下 AI 自动补位目标随之变为 12）、`WW_SIGNUP_TIMEOUT`、`WW_NIGHT_TIMEOUT`（女巫/
预言家）、`WW_WOLF_TIMEOUT`（仅狼人阶段）、`WW_SPEECH_TIMEOUT`、
`WW_VOTE_TIMEOUT`、`WW_SHERIFF_REGISTER_TIMEOUT`、`WW_AI_ENABLED` /
`WW_AI_AUTOFILL` / `WW_AI_MAX`、`WW_AI_DECISION_TIMEOUT`、
`WW_AI_SPEECH_TIMEOUT`、`WW_AI_MAX_TOKENS`、`WW_AI_SPEECH_MAX_TOKENS`、
`WW_AI_WOLF_DISCUSS`、`WW_AI_DISCUSS_TIMEOUT`、`WW_AI_REGISTER_BUFFER`
（有 AI 时竞选报名窗口的延长量）。

## 日志约定

沿用 `from nonebot import logger` + f-string，统一前缀 `狼人杀群 {group_id}`。
对局事件（阶段切换、发牌、死亡、夜间结算、投票、AI 回复与降级）记 `info`；
完整提示词记 `debug`。修改引擎流程时保持"每个关键裁决都有一行 info"。
