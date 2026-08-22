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
  从不直接改 `Game` 状态。唯一例外是报名阶段的增删（`state.py` 的注册表函数）、
  报名阶段内存写入（`signup_names` / `role_requests`）与空房解散时的
  `phase=ENDED` + worker 取消。
- **选身份（报名阶段私聊 /选身份）**：请求记入 `game.role_requests`
  （user_id → 期望角色，`signup_names` 同款内存先例，退报名即清理），
  发牌时由引擎 `_resolve_role_requests` 消费——按份数满足（请求人数 ≤
  牌堆份数全部如愿，超出在请求者中 `random.sample`），板子中途切换导致
  的过期请求自然失效。**分配结果不做任何群播/私聊播报**：落选者可从
  自己的身份卡反推竞争赢家，播报只会放大泄漏；身份卡是唯一揭晓渠道。
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
- **夜间播报必须角色盲**：全群禁言期间由 `_night_heartbeat` 每隔
  `ww_night_warn_remain` 秒播一条通用氛围文案（`_NIGHT_AMBIENT_LINES`），
  文案不含角色/座位/阶段信息——被跳过的子阶段（如女巫双药已用）
  若造成"播报缺席"即可反推角色状态。旧的按子阶段点名倒计时
  （"女巫行动还剩 30 秒"）已废弃。心跳任务在 `_run_night` 的
  `try/finally` 里创建/取消，只调 `_announce`，不写任何引擎信号。
- **投票必播计票汇总**：`_collect_votes` 结尾一律经
  `_announce_vote_tally` 群播汇总块（得票排序 + 警长 1.5 票标注
  + 弃票/未投票名单），覆盖警长投票/重投/放逐/PK。1.5 权重逻辑
  唯一出处是 `_vote_counts`（`_tally_votes` 亦复用），改权重两处同步。
- **夜间超时只私聊告知**：女巫/预言家/长老/狼队超时的提示只 `_dm`
  当事人，绝不群播（群播即泄漏角色）；文案必须避开
  `无效`/`无法使用`/`请重新` 三个子串——它们触发 `ai_player.on_dm`
  的 AI 重新决策（`_allow_retry`），误含会给 AI 制造虚假重试。
- **身份卡首行不变式**：`build_role_card` 的首行必须保持
  `"═══ 狼人杀 · 身份卡 ═══"`——`ai_player._ROLE_CARD_HEADER`
  按此头跳过卡片，避免身份卡在 AI 上下文中双份传入。
- **显示名与名单**：人类报名者的群名片/昵称由命令层经
  `note_signup_name` 记入 `Game.signup_names`（报名阶段注册表写入
  例外，仅内存）；`display_name_of` 按 AI 伪装名 > 报名昵称 > QQ 号
  解析，用于报名名单与身份卡座位表。身份卡的 `roster` 全体玩家
  收到相同一份，不标注 AI（保持伪装）。
- **阶段报错与私聊提示防泄漏**：`commands._PHASE_CN` 把五个夜间
  子阶段一律折叠为"夜晚"；阶段闸门错误经 `_not_now` 附当前阶段
  与一行指引；私聊解析失败经 `_dm_hint_for` 按角色+阶段给裁剪提示，
  `dsl._DM_HINT` 仅作兜底。`dsl.py` 的匹配行为与 `allow_votes`
  语义是人类与 AI 共用契约，不得擅改。

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
- 终辩（`SHERIFF_REVOTE`）引擎窗口 60s（`config.SHERIFF_FINAL_SPEECH_SECONDS`，
  `engine._FINAL_SPEECH_TIMEOUT` 与 `ai_player._SHERIFF_FINAL_SPEECH_WINDOW`
  共用该常量），AI 发言超时在 `_llm_speech` 内夹取到 54s
  （60s 窗口 × `_PHASE_WINDOW_RATIO=0.9`）。
- **降级链**：LLM 失败 → 格式纠正重试一次 → 仍失败按阶段安全默认
  （弃票 / 过 / 不竞选 / 撕警徽 / 空刀托管）。任何失败都不卡局。

## 配置键（`.env` 可覆盖，见 `config.py`）

`WW_MIN_PLAYERS` / `WW_MAX_PLAYERS`（引擎取其与当前板子支持人数的交集：
无交集→报名即流局；开局人数不在板子 `counts` 内→发牌前流局。12 人专属
板子下 AI 自动补位目标随之变为 12）、`WW_SIGNUP_TIMEOUT`（默认 180；
提醒点两段：`WW_SIGNUP_WARN_REMAIN` 60 + `WW_SIGNUP_WARN_REMAIN_FINAL` 20）、
`WW_ROLE_REQUEST`（默认 true；报名阶段私聊 /选身份 请求期望角色）、
`WW_NIGHT_TIMEOUT`（女巫/预言家）、`WW_WOLF_TIMEOUT`（仅狼人阶段）、
`WW_NIGHT_WARN_REMAIN`（语义为夜间心跳播报间隔，旧"按子阶段剩余秒数
点名提醒"已废弃）、`WW_SPEECH_TIMEOUT`、
`WW_VOTE_TIMEOUT`、`WW_HUNTER_TIMEOUT`（猎人开枪决策）、
`WW_LAST_WORDS_TIMEOUT`（遗言）、`WW_SHERIFF_REGISTER_TIMEOUT`（默认 45，
有 AI 时再 +`WW_AI_REGISTER_BUFFER`）、`WW_BADGE_TIMEOUT`（默认 45；同时
复用为白天发言排序决策窗口）、`WW_API_TIMEOUT`（默认 10.0 秒；api.py 全部
OneBot 调用的 wait_for 上限）、`WW_AI_ENABLED` /
`WW_AI_AUTOFILL` / `WW_AI_MAX`、`WW_AI_DECISION_TIMEOUT`、
`WW_AI_SPEECH_TIMEOUT`、`WW_AI_MAX_TOKENS`、`WW_AI_SPEECH_MAX_TOKENS`、
`WW_AI_WOLF_DISCUSS`、`WW_AI_DISCUSS_TIMEOUT`、`WW_AI_REGISTER_BUFFER`
（有 AI 时竞选报名窗口的延长量）。

## 日志约定

沿用 `from nonebot import logger` + f-string，统一前缀 `狼人杀群 {group_id}`。
对局事件（阶段切换、发牌、死亡、夜间结算、投票、AI 回复与降级）记 `info`；
完整提示词记 `debug`。修改引擎流程时保持"每个关键裁决都有一行 info"。
