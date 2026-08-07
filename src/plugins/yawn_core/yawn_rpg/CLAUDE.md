# yawn_rpg — 跑团子插件

群聊 CoC 7版跑团：AI 主持人（KP）主持，按 YAML 模组推进剧情。
由父插件 `yawn_core/__init__.py` 的 `_load_sub_plugins()` 动态加载。

## 模块地图

| 模块 | 职责 |
|---|---|
| `engine.py` | 游戏引擎：每局一个 asyncio 任务（`run_game`），独占状态变更与群播报；内联 KP 智能体循环与工具执行器；回合中吸纳 / 游戏内时钟 / NPC 进出 / 违规扫描 / 通用结局安全网 |
| `ai_kp.py` | **无状态**：KP 提示词构造（整局一次的剧本概览 + 每回合动态状态区）、工具 schema（**全静态，整局缓存**）。不建任务、不改状态、不发消息——刻意不是"AI 驱动任务"，勿改 |
| `ai_npc.py` | **无状态**：NPC 对白智能体——专用 `complete()` 按 NPC 自己的 persona/knows/secrets 生成台词（secrets 附不主动透露指令，KP 提示词永不可见）。不建任务、不改状态、不发消息；台词播报与兜底全在引擎 |
| `commands.py` | 命令入口：群命令 / 私聊建卡监听 / 群自由文本 SAY 监听，只做校验 + 投入行动 |
| `state.py` | 内存状态：`Game`/`PlayerState`/`Action`、注册表与身份守卫式清理；游戏内时钟属性与 NPC 在场包装层（死亡过滤 + HP 幂等初始化） |
| `module_schema.py` | 模组 pydantic 模型、条件表达式求值（`evaluate_condition`）、NPC 在场解析器（时间 + 行程）、YAML 加载器 |
| `modules/*.yaml` | 剧本模组（自带「雨夜旧宅」）；坏模组加载时 warning 跳过。写作规范见 `modules/README.md` |
| `charsheet.py` | CoC 7版人物卡：掷属性、派生值、技能表、加点校验（系统建卡，玩家微调） |
| `dice.py` | d100 检定分级、骰表达式求值——一切数值的唯一来源 |
| `dsl.py` | 建卡私聊文本 → `Action` 解析 |
| `api.py` | OneBot V11 安全封装（群消息/私聊/管理员查询），无禁言系列 |
| `config.py` | 玩法与 AI 配置（`RPG_*` 环境变量） |
| `models.py` | ORM 模型（`bind_key=yawn_rpg`）：仅 `RPGGame`/`RPGPlayer` 两张汇总表 |
| `migrations/` | **副本，勿改**。正本在 `data/nonebot_plugin_orm/migrations/yawn_core/yawn_rpg/` |

## 核心契约

- **命令层只校验、只投递**：命令处理器把 `Action` 投入 `game.action_queue`，
  从不直接改 `Game` 状态。唯一例外是报名阶段的增删与空房解散
  （`phase=ENDED` + worker 取消，同 werewolf）。
- **引擎独占写入**：`phase` / `current_scene` / `discovered_clues` /
  `monster_hp` / `npc_hp` / `flags` / `elapsed_minutes` / 玩家 HP/SAN
  只由引擎写入。阶段切换走 `_enter_phase()`（带日志）。
- **AI ↔ 系统的唯一通道是 tool_call**：KP 智能体循环在引擎内联运行
  （`run_kp_turn`）：`complete_with_tools` → 引擎逐个验证执行 tool_calls
  （`execute_tool`）→ 工具结果回填对话 → 循环至最终旁白或轮数上限。
  AI 从不直接接触状态，工具参数从不被信任。
- **KP 只给 NPC 意图、不写台词**：`speak_as_npc` 传 intent、`/对话` 传
  调查员原话，实际台词由独立的 NPC 智能体调用（`ai_npc.generate_npc_line`）
  按 NPC 自己的 persona/knows/secrets 生成——KP 与 NPC 视角分离。
- **回合中吸纳（步调同步）**：KP 回合内联 await 期间引擎不停消费输入——
  每个工具轮次顶部 `_pump_mid_turn` 非阻塞抽干队列：SAY 走与 `_handle_say`
  相同的 KP 前处理（入群聊、记 flag、tick）并以【插话】注入下一轮对话；
  其他行动按序存入 `mid_turn_buffer`，`_run_play` 于旁白后执行；回合内
  记到的违规在旁白后触发 `_world_reaction`。切景 `stow_actions` 不再丢弃
  队列，而是保序收存（过期命令由引擎现有校验给中文回执）。
- **数值由系统掌控**：骰子只在 `dice.py` 里掷；检定/伤害/SAN 由引擎
  播报固定文案。KP 旁白禁止出现数字（提示词硬规则 + 引擎不做依赖）。

## 工具目录与验证规则（execute_tool）

| 工具 | 引擎验证 |
|---|---|
| `request_check` | 技能存在（SAN 检定改用 `san_check`）；目标玩家存活；**系统掷 d100** 并播报 |
| `san_check` | 损失骰表达式合法；损失按 `RPG_AI_MAX_SAN_LOSS` 钳制；以当前 SAN 为技能值掷骰 |
| `deal_damage` / `heal` | 数值按 `RPG_AI_MAX_DAMAGE_PER_CALL` 钳制；目标须存在 |
| `transition_scene` | 目标须为**当前场景出口**；出口 `condition` 由引擎强制执行，不满足返回错误（KP 据此叙述"门锁着"） |
| `grant_clue` | 线索须挂在当前场景（检定点奖励 / 怪物死亡奖励）且未被发现；once 检定点已触发且失败时线索不可授予（系统已裁决，KP 不得覆盖） |
| `speak_as_npc` | NPC 须经**时间/行程解析器**在场于当前场景且存活；死亡与不在场返回不同回执；**KP 只传 intent**，台词由 NPC 智能体（`ai_npc`）按其人格生成，失败落 `fallback_line`；截断 150 字，以「【NPC名】…」播报 |
| `monster_attack` | 怪物须在场且存活；引擎用模组数值做对抗检定（玩家闪避对抗） |
| `end_session` | 结局条件须已满足（引擎复核），否则拒绝 |
| `query_story` | 按名称或 id 精确匹配查结局 / 具名事件的来龙去脉；**KP-only 回执**，绝不群播；结局返 名称+summary+倾向（不返 condition），事件返 名称+summary |
| `get_situation` | 返回无剧透局面摘要（用于状态变化后的工具调用之间刷新）；场景块同回合未变则回执「无变化」，变化时只返回新场景块（不含易变尾） |

工具 schema **全静态**：`ai_kp.build_tools(module, player_names)` 整局只
构建一次（`Game.tools_cache` 惰性缓存）。随场景变化的枚举（出口 / 线索 /
NPC / 怪物）不再进 schema——合法取值范围由局面区块的括号 id 列出，越界
调用由 `execute_tool` 穷尽校验并以中文回执纠正，绝不抛异常。tools + 系统
提示词的 wire 前缀逐字节稳定（前缀缓存的前提，同时纯省 token），user
消息的半稳定局面块（场景块 + 已发生事件）在其后接续参与回合间缓存。
每次工具执行记一行 `info` 日志。

## 防剧透分界

KP 提示词分两层。**整局一次的剧本概览**（拼在系统消息后，
`ai_kp.build_module_overview`，`Game.kp_overview` 惰性缓存，整局
逐字节稳定 → 落在前缀缓存内）：模组前提、NPC 名册（名字(id) +
public_desc + 人格 + 可透露 knows）、全部结局名字(id)（含通用结局）
与具名事件名字(id)、`query_story` 指引。**每回合动态状态区**（user
消息，**按稳定度降序**）：场景块（当前场景名(id) + narration、
在场 NPC（**只有**名字(id) + public_desc + 当前活动——persona/
knows 已在概览）、在场存活怪物名(id)、已发现线索名称(id)、
出口通行性（布尔 + id，不含解法）、调查员定性状态（无恙/轻伤/
重伤/倒地））+ [已发生事件] 为半稳定前缀（场景状态/事件未变时
逐字节稳定）→ 【当前任务】指令 → [时间]、近期群聊（易变尾，
**永远最后**）。前缀缓存：tools + 系统提示词（含概览）整局稳定；
user 消息前部的场景块与已发生事件在状态未变的回合间接续命中；
每回合必变的任务指令与时钟/群聊追加最后，失效范围自任务处收缩。
回合内 `get_situation` 按 `Game.kp_situation_scene_block` 去重
（回合内时钟不 tick、群聊不变，状态区在工具回执里纯冗余）。检定的
成功/失败文案、线索 `text`、NPC `secrets`、出口条件、结局条件
（`query_story` 也不返 condition，只返作者写的 summary）、怪物数值
**永不进提示词**。**secrets 的唯一入口是该 NPC 自己的智能体提示词**
（`ai_npc`，附不主动透露指令）。模组加载时校验 secret 不是 persona/
公开信息/行程活动的子串（正是概览会用的字段）。

## 游戏内时钟与 NPC 行程

- **时钟**：`Game.clock_start_minutes`（进 PLAY 时按 `module.time.start`
  初始化）+ `elapsed_minutes`（每个**成功**行动 tick 推进；失败行动不
  耗时）。默认分钟数见 `_DEFAULT_TIME_COSTS`（say 5 / talk 10 / check 10 /
  move 10 / attack 5），模组 `time.costs` 按键覆写，`CheckPoint.time_cost` /
  `Exit.time_cost` 按点覆写。**KP 工具引发的检定不再 tick**（所在回合
  已付过 SAY/TALK 成本）。玩家命令：`/时间`（只读直答）、`/等待 N`
  （钳制 `[1, RPG_WAIT_MAX]`，缺省 `RPG_WAIT_DEFAULT`）。
- **行程**：`NPC.schedule` 条目 `{from, to, scene, activity, condition,
  away}`。语义：schedule **非空即权威**（不再看 `scene.npcs`）；按声明序
  取第一条"条件成立 + 时钟落在窗口"的条目；时段为 `[from, to)` 半开区间，
  **支持跨午夜**（from > to），`from == to` 为全天窗口；`away: true` 表示
  外出；**全部条目不匹配 = 不在场**（作者须自行用兜底条目覆盖）。空
  schedule 的 NPC 保持静态 `scene.npcs` 语义（旧模组零改动兼容）。
- **解析分层**：`ModuleDef.npc_presence` / `npcs_in_scene` / `npc_schedule_match`
  是纯解析器（不感知死亡）；`Game.npcs_in_scene` / `npc_present` 包装层
  过滤 `dead_npcs` 并幂等初始化 `npc_hp`（仿怪物 HP 初始化）。在场显示、
  speak_as_npc 校验、/对话 查找、局面提示词四处统一走包装层。
- **进出播报**：`_tick_time` 每次推进后对**当前场景**做在场 diff，合并
  一条消息播报（离场 flavor 取命中条目的 activity）；刚死亡的 NPC 不再
  重复播报离场。

## 条件表达式（evaluate_condition，确定性）

词条以 `&` 组合（须全部满足）：`always` / `clue:<id>` / `clues:<a>+<b>` /
`monster_dead:<id>` / `scene:<id>` / `all_players_incapped` /
`time_after:HH:MM` / `time_before:HH:MM` / `time_between:HH:MM-HH:MM`
（跨午夜窗口）/ `flag:<name>` / `flag:<name>>=N`。未知词条、空词条
（悬挂 `&`）与非法格式保守判 False。**时间词按剧本时间轴求值**：
目标时刻折算为偏移 offset = (目标 − 开局时刻) mod 1440，与 elapsed
比较——21:00 开局的 `time_after:06:00` 只在时钟推进到次日 06:00 后
成立，而非开局钟面已过 06:00 即成立；`time_between` 的偏移窗口按
elapsed 的日内位置匹配，保留每日重复语义。NPC 行程的 from/to 窗口
是独立机制，不受此语义影响。**结局条件恒真（空 / 仅 always）在加载
期拒绝**——否则开局第一次安全网扫描即触发、瞬间终局。

**镜像契约**：新增词条必须同时改四处——`evaluate_condition` +
`_validate_condition`（加载期校验）+ `ConditionContext`（快照字段）+
`Game.condition_context`（组装）。漏一处要么悬空引用过校验，要么
运行时永久软锁。

## flags 与系统级通用结局

- **flags**：`Game.flags: dict[str, int]`（名称 → 累计次数），唯一写入
  入口 `engine.raise_flag`。写入点：发言违规扫描（`arson` / `threat` /
  `destroy` 关键词）、攻击 NPC（`assault`）、击杀 NPC（`npc_dead:<id>` +
  `murder`）。
- **违规扫描**：`_handle_say` 每批逐行扫描多字词组（紧词组降误报），
  命中先记 flag（**先于 tick**，flag 驱动的行程变化当轮即体现），再走
  `_world_reaction`：在场首个 NPC 按人格反应一句（AI 启用 → `ai_npc`
  生成；AI 禁用 / 生成失败 → 固定惊呼罐头）。回合中吸纳到的违规
  在 KP 旁白之后反应。NPC 反应、反击、结局在零 AI 下全部成立。
- **通用结局**：`engine._GENERIC_ENDINGS`（导入期合成的 `Ending` 对象，
  不走模组加载校验，带 name/summary——概览列出、`query_story` 可查）
  由 `check_endings` 在模组结局**之后**扫描
  （`module.generic_endings` 可关）。模组结局永远优先。阈值常量化：
  `flag:arson>=4` 彩蛋（neutral）→ `flag:arson>=2` 火灾（bad）→
  `flag:murder` 逮捕（bad）→ `flag:assault>=3` 制服（bad）→
  `all_players_incapped` 全军覆没（bad，兜底未自写 TPK 结局的模组；
  模组结局与更具体的通用结局永远优先），声明序即优先级。结局安全网
  在 `_run_play` 循环顶每轮复检，时间 / 标记触发的结局自然在下一轮
  点火。

## 具名事件与结局说明（query_story）

- **开局全貌**：KP 系统消息含整局一次的【剧本概览】——模组前提、
  NPC 名册、全部结局（含通用结局）与具名事件的名字，KP 开局即可
  规划剧情走向。
- **PlotEvent**（模组顶层可选 `events:`）：`id`/`name`/`summary`/
  `condition`（空 = 序幕事件，开局首轮即记）。纯 KP 上下文，不触发
  任何机制；条件复用现有语法（无新词条，镜像契约不动），加载期
  校验引用但**不拒恒真**（事件恒真无害）。
- **check_events**：与 `check_endings` 同节奏（`_run_play` 循环顶每轮
  复检），新满足者记入 `Game.occurred_events` + info 日志；提示词
  易变尾按模组声明序列出 `[已发生事件]` 名称。回合内触发的事件
  下一回合才进提示词（同结局节奏）。
- **Ending.name/summary**：name 进概览（空回退 id）；summary 是 KP
  向来龙去脉（导演指引），仅 `query_story` 回执，绝不播报。
- **query_story**：名称或 id 精确匹配；结局返 名称+summary+倾向
  （**不返 condition**——守防剧透边界，触发语境由作者写进 summary），
  事件返 名称+summary；未命中回执指向概览。回执只进 KP 对话，
  群里零播报。

## NPC 战斗

NPC 战斗数值镜像 `Monster`（`hp` / `attack_skill` / `attack_name` /
`damage` / `dodge` / `on_death_clue` / `on_death_text`，全部带默认值，
旧模组 NPC 以默认弱小数值变得可攻击）。`/攻击` 经 `_find_attack_target`
联合查找（**怪物优先**，名称子串匹配兼容旧行为）。玩家打 NPC：斗殴检定
→ `_opposed_dodge` 闪避对抗（平手防方胜，与怪物战斗共享语义）→ 扣
`npc_hp` → 归零走 `_kill_npc`（死亡文案 + 线索 + murder 标记）；
**NPC 存活则立即确定性反击**（`do_npc_attack`，镜像 `do_monster_attack`）。

## 降级链（任何失败不卡局）

1. `RPG_AI_ENABLED=false` → 全程确定性模式：关键词触发自动检定、
   固定文案切景、NPC 只回 `fallback_line`；违规发言由罐头 NPC 台词
   回应；违规升级照样触发通用结局。
2. 工具调用失败（含端点不支持 tools）→ 本局置 `tools_broken=True`，
   退化为纯叙述 `complete()`。NPC 对白走独立的 `complete()` 调用，
   不受 `tools_broken` 影响。
3. 纯叙述仍失败 → 确定性兜底：命中检定点 `triggers` 的发言自动执行
   检定（时间照常 tick）；否则播报 `scene.idle_narration` → 通用兜底文案。
4. NPC 台词生成失败（或 AI 关）→ `/对话` 与 `speak_as_npc` 落
   `fallback_line`；违规世界反应落固定惊呼罐头（**非** `fallback_line`——
   那是聊天搪塞语，作纵火反应是退化）。
5. 结局安全网：每轮循环引擎按模组结局 + 通用结局条件确定性扫描，
   不依赖 KP 调用 `end_session`。

## 确定性路径（不经 AI）

- `/检定 技能 [困难|极难]`：玩家显式检定（消耗 check 时间）
- `/前往 地点`：出口条件校验 + 固定文案切景（与 `transition_scene` 工具
  共用 `_transition_exit`；成功后消耗 move 时间）
- `/攻击 怪物|NPC`：对抗结算（怪物优先；NPC 会反击、可死亡）
- `/时间`：查询游戏内时钟（只读，不耗时）
- `/等待 [N]`：原地等待 N 分钟（缺省 `RPG_WAIT_DEFAULT`，钳制 `RPG_WAIT_MAX`）
- 模组 `auto: true` 出口：条件满足自动切景
- 关键词触发提示：`match_trigger` 命中时仅作为**建议**写入 KP 提示词
  （AI 模式下是否检定由 KP 决定；降级模式下自动执行）
- 违规关键词扫描：记 flag → NPC 反应指令 / 罐头台词 → 通用结局升级

## 阶段与交互

`SIGNUP`（报名 + `/选择模组 N`）→ `CHAR_CREATE`（系统掷卡私聊下发，
私聊 DSL 微调：重掷/加减点/重置/查看/确认，超时自动确认，DM 失败
自动确认）→ `PLAY`（时钟按 `time.start` 初始化，**群聊记录清空**——
报名/建卡播报不进 KP 提示词；场景循环：结局安全网 → 事件扫描
→ 自动出口 → 行动分发；连续自动切景超过场景数 +1 判为恒真条件成环、
兜底收尾）→ `ENDED`。PLAY 期群自由文本由 priority-0 非阻塞监听器
投递 SAY，`rpg_say_settle_window` 内合批为一次 KP 调用；KP 回合期间
到达的消息经回合中吸纳处理（见核心契约）；私聊监听器
**仅建卡期**拦截（其余私聊放行给 ai_chat）。两个监听规则的特性开关
判定按 (用户, 群) 随对局缓存（TTL 300 秒，`commands._FEATURE_CACHE_TTL`），
避免逐条消息查库；开关变更局内最多 5 分钟后生效。

## 配置键（`.env` 可覆盖，见 `config.py`）

`RPG_MIN_PLAYERS` / `RPG_MAX_PLAYERS`、`RPG_SIGNUP_TIMEOUT`、
`RPG_CHAR_CREATE_TIMEOUT` / `RPG_CHAR_REROLL_MAX` / `RPG_CHAR_SKILL_POOL`
（None→INT×2）/ `RPG_CHAR_SKILL_CAP`、`RPG_IDLE_TIMEOUT`、
`RPG_WAIT_DEFAULT` / `RPG_WAIT_MAX`、
`RPG_AI_ENABLED` / `RPG_AI_MAX_TOOL_ROUNDS` / `RPG_AI_TURN_TIMEOUT`、
`RPG_KP_TIMEOUT` / `RPG_KP_MAX_TOKENS`（推理模型须给足余量，否则
finish_reason=length 截断返空）/ `RPG_KP_TEMPERATURE` /
`RPG_KP_MAX_OUTPUT_CHARS` / `RPG_KP_MIN_INTERVAL`、
`RPG_AI_MAX_DAMAGE_PER_CALL` / `RPG_AI_MAX_SAN_LOSS`、
`RPG_SAY_SETTLE_WINDOW` / `RPG_SPEECH_TRUNCATE` / `RPG_MAX_CONTEXT_LINES`、
`RPG_NPC_TIMEOUT` / `RPG_NPC_MAX_TOKENS`（推理模型须给足余量，
否则截断返空 → NPC 全程罐头）/ `RPG_NPC_TEMPERATURE` /
`RPG_NPC_CONTEXT_LINES`。

## 日志约定

沿用 `from nonebot import logger` + f-string，统一前缀 `跑团群 {group_id}`。
对局事件（阶段切换、场景进入、检定、工具执行、线索发现、结局、AI 降级、
时间推进、NPC 死亡、flag 记录）记 `info`；完整 KP 提示词记 `debug`。
