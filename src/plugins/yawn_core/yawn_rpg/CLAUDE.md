# yawn_rpg — 跑团子插件

群聊 CoC 7版跑团：AI 主持人（KP）主持，按 YAML 模组推进剧情。
由父插件 `yawn_core/__init__.py` 的 `_load_sub_plugins()` 动态加载。

## 模块地图

| 模块 | 职责 |
|---|---|
| `engine.py` | 游戏引擎：每局一个 asyncio 任务（`run_game`），独占状态变更与群播报；内联 KP 智能体循环与工具执行器；游戏内时钟 / NPC 进出 / 违规扫描 / 通用结局安全网 |
| `ai_kp.py` | **无状态**：KP 提示词构造、局面上下文、工具 schema（**全静态，整局缓存**）。不建任务、不改状态、不发消息——刻意不是"AI 驱动任务"，勿改 |
| `commands.py` | 命令入口：群命令 / 私聊建卡监听 / 群自由文本 SAY 监听，只做校验 + 投入行动 |
| `state.py` | 内存状态：`Game`/`PlayerState`/`Action`、注册表与身份守卫式清理；游戏内时钟属性与 NPC 在场包装层（死亡过滤 + HP 幂等初始化） |
| `module_schema.py` | 模组 pydantic 模型、条件表达式求值（`evaluate_condition`）、NPC 在场解析器（时间 + 行程）、YAML 加载器 |
| `modules/*.yaml` | 剧本模组（自带「雨夜旧宅」）；坏模组加载时 warning 跳过 |
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
- **数值由系统掌控**：骰子只在 `dice.py` 里掷；检定/伤害/SAN 由引擎
  播报固定文案。KP 旁白禁止出现数字（提示词硬规则 + 引擎不做依赖）。

## 工具目录与验证规则（execute_tool）

| 工具 | 引擎验证 |
|---|---|
| `request_check` | 技能存在（SAN 检定改用 `san_check`）；目标玩家存活；**系统掷 d100** 并播报 |
| `san_check` | 损失骰表达式合法；损失按 `RPG_AI_MAX_SAN_LOSS` 钳制；以当前 SAN 为技能值掷骰 |
| `deal_damage` / `heal` | 数值按 `RPG_AI_MAX_DAMAGE_PER_CALL` 钳制；目标须存在 |
| `transition_scene` | 目标须为**当前场景出口**；出口 `condition` 由引擎强制执行，不满足返回错误（KP 据此叙述"门锁着"） |
| `grant_clue` | 线索须挂在当前场景（检定点奖励 / 怪物死亡奖励）且未被发现 |
| `speak_as_npc` | NPC 须经**时间/行程解析器**在场于当前场景且存活；死亡与不在场返回不同回执；台词截断 150 字、去 `/` 前缀，以「【NPC名】…」播报 |
| `monster_attack` | 怪物须在场且存活；引擎用模组数值做对抗检定（玩家闪避对抗） |
| `end_session` | 结局条件须已满足（引擎复核），否则拒绝 |
| `get_situation` | 返回无剧透局面摘要（用于状态变化后的工具调用之间刷新） |

工具 schema **全静态**：`ai_kp.build_tools(module, player_names)` 整局只
构建一次（`Game.tools_cache` 惰性缓存）。随场景变化的枚举（出口 / 线索 /
NPC / 怪物）不再进 schema——合法取值范围由局面区块的括号 id 列出，越界
调用由 `execute_tool` 穷尽校验并以中文回执纠正，绝不抛异常。tools + 系统
提示词的 wire 前缀逐字节稳定（前缀缓存的前提，同时纯省 token）。每次工具
执行记一行 `info` 日志。

## 防剧透分界

KP 提示词（`ai_kp.build_situation`）只含：当前场景名(id) + narration、
在场 NPC（经解析器，含括号 id 与行程活动）的 `public_desc`/`persona`/
`knows`、在场存活怪物名(id)、已发现线索**名称**(id)、出口通行性
（布尔 + id，不含解法）、调查员**定性**状态（无恙/轻伤/重伤/倒地）、
`[时间]`（游戏内时钟）、近期群聊记录。区块顺序稳定 → 易变（群聊与
【当前任务】指令最后）。检定的成功/失败文案、线索 `text`、NPC `secrets`、
出口条件、结局条件、怪物数值**永不进提示词**。模组加载时校验 secret
不是 persona/公开信息/行程活动的子串。

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
（跨午夜窗口）/ `flag:<name>` / `flag:<name>>=N`。未知词条与非法格式
保守判 False。

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
  命中先记 flag（**先于 tick**，flag 驱动的行程变化当轮即体现），再：
  AI 启用 → 向 KP 任务追加【世界反应】强指令；AI 禁用 → 在场首个 NPC
  罐头台词阻止。NPC 反应、反击、结局在零 AI 下全部成立。
- **通用结局**：`engine._GENERIC_ENDINGS`（导入期合成的 `Ending` 对象，
  不走模组加载校验）由 `check_endings` 在模组结局**之后**扫描
  （`module.generic_endings` 可关）。模组结局永远优先。阈值常量化：
  `flag:arson>=4` 彩蛋（neutral）→ `flag:arson>=2` 火灾（bad）→
  `flag:murder` 逮捕（bad）→ `flag:assault>=3` 制服（bad），声明序即
  优先级。结局安全网在 `_run_play` 循环顶每轮复检，时间 / 标记触发的
  结局自然在下一轮点火。

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
   退化为纯叙述 `complete()`。
3. 纯叙述仍失败 → 确定性兜底：命中检定点 `triggers` 的发言自动执行
   检定（时间照常 tick）；否则播报 `scene.idle_narration` → 通用兜底文案。
4. 结局安全网：每轮循环引擎按模组结局 + 通用结局条件确定性扫描，
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
自动确认）→ `PLAY`（时钟按 `time.start` 初始化；场景循环：结局安全网
→ 自动出口 → 行动分发）→ `ENDED`。PLAY 期群自由文本由 priority-0
非阻塞监听器投递 SAY，`rpg_say_settle_window` 内合批为一次 KP 调用；
私聊监听器**仅建卡期**拦截（其余私聊放行给 ai_chat）。

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
`RPG_SAY_SETTLE_WINDOW` / `RPG_SPEECH_TRUNCATE` / `RPG_MAX_CONTEXT_LINES`。

## 日志约定

沿用 `from nonebot import logger` + f-string，统一前缀 `跑团群 {group_id}`。
对局事件（阶段切换、场景进入、检定、工具执行、线索发现、结局、AI 降级、
时间推进、NPC 死亡、flag 记录）记 `info`；完整 KP 提示词记 `debug`。
