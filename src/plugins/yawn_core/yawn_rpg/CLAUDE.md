# yawn_rpg — 跑团子插件

群聊 CoC 7版跑团：AI 主持人（KP）主持，按 YAML 模组推进剧情。
由父插件 `yawn_core/__init__.py` 的 `_load_sub_plugins()` 动态加载。

## 模块地图

| 模块 | 职责 |
|---|---|
| `engine.py` | 游戏引擎：每局一个 asyncio 任务（`run_game`），独占状态变更与群播报；内联 KP 智能体循环与工具执行器 |
| `ai_kp.py` | **无状态**：KP 提示词构造、局面上下文、工具 schema（动态枚举约束）。不建任务、不改状态、不发消息——刻意不是"AI 驱动任务"，勿改 |
| `commands.py` | 命令入口：群命令 / 私聊建卡监听 / 群自由文本 SAY 监听，只做校验 + 投入行动 |
| `state.py` | 内存状态：`Game`/`PlayerState`/`Action`、注册表与身份守卫式清理 |
| `module_schema.py` | 模组 pydantic 模型、条件表达式求值（`evaluate_condition`）、YAML 加载器 |
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
  `monster_hp` / 玩家 HP/SAN 只由引擎写入。阶段切换走 `_enter_phase()`（带日志）。
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
| `speak_as_npc` | NPC 须在当前场景；台词截断 150 字、去 `/` 前缀，以「【NPC名】…」播报 |
| `monster_attack` | 怪物须在场且存活；引擎用模组数值做对抗检定（玩家闪避对抗） |
| `end_session` | 结局条件须已满足（引擎复核），否则拒绝 |
| `get_situation` | 返回无剧透局面摘要 |

工具 schema 由 `ai_kp.build_tools(game)` **按当前局面动态生成**（出口 /
线索 / NPC / 怪物的枚举约束）。非法调用返回中文错误描述供 KP 自我纠正，
绝不抛异常；每次工具执行记一行 `info` 日志。

## 防剧透分界

KP 提示词（`ai_kp.build_situation`）只含：当前场景 narration、在场 NPC
的 `public_desc`/`persona`/`knows`、已发现线索**名称**、出口通行性
（布尔，不含解法）、调查员**定性**状态（无恙/轻伤/重伤/倒地）、近期
群聊记录。检定的成功/失败文案、线索 `text`、NPC `secrets`、出口条件、
结局条件、怪物数值**永不进提示词**。模组加载时校验 secret 不是
persona/公开信息的子串。

## 降级链（任何失败不卡局）

1. `RPG_AI_ENABLED=false` → 全程确定性模式：关键词触发自动检定、
   固定文案切景、NPC 只回 `fallback_line`。
2. 工具调用失败（含端点不支持 tools）→ 本局置 `tools_broken=True`，
   退化为纯叙述 `complete()`。
3. 纯叙述仍失败 → 确定性兜底：命中检定点 `triggers` 的发言自动执行
   检定；否则播报 `scene.idle_narration` → 通用兜底文案。
4. 结局安全网：每轮循环引擎按 `endings` 条件确定性扫描，不依赖
   KP 调用 `end_session`。

## 确定性路径（不经 AI）

- `/检定 技能 [困难|极难]`：玩家显式检定
- `/前往 地点`：出口条件校验 + 固定文案切景（与 `transition_scene` 工具共用 `_transition_exit`）
- `/攻击 怪物`：玩家主动攻击的对抗结算
- 模组 `auto: true` 出口：条件满足自动切景
- 关键词触发提示：`match_trigger` 命中时仅作为**建议**写入 KP 提示词
  （AI 模式下是否检定由 KP 决定；降级模式下自动执行）

## 阶段与交互

`SIGNUP`（报名 + `/选择模组 N`）→ `CHAR_CREATE`（系统掷卡私聊下发，
私聊 DSL 微调：重掷/加减点/重置/查看/确认，超时自动确认，DM 失败
自动确认）→ `PLAY`（场景循环：结局安全网 → 自动出口 → 行动分发）→
`ENDED`。PLAY 期群自由文本由 priority-0 非阻塞监听器投递 SAY，
`rpg_say_settle_window` 内合批为一次 KP 调用；私聊监听器**仅建卡期**
拦截（其余私聊放行给 ai_chat）。

## 配置键（`.env` 可覆盖，见 `config.py`）

`RPG_MIN_PLAYERS` / `RPG_MAX_PLAYERS`、`RPG_SIGNUP_TIMEOUT`、
`RPG_CHAR_CREATE_TIMEOUT` / `RPG_CHAR_REROLL_MAX` / `RPG_CHAR_SKILL_POOL`
（None→INT×2）/ `RPG_CHAR_SKILL_CAP`、`RPG_IDLE_TIMEOUT`、
`RPG_AI_ENABLED` / `RPG_AI_MAX_TOOL_ROUNDS` / `RPG_AI_TURN_TIMEOUT`、
`RPG_KP_TIMEOUT` / `RPG_KP_MAX_TOKENS`（推理模型须给足余量，否则
finish_reason=length 截断返空）/ `RPG_KP_TEMPERATURE` /
`RPG_KP_MAX_OUTPUT_CHARS` / `RPG_KP_MIN_INTERVAL`、
`RPG_AI_MAX_DAMAGE_PER_CALL` / `RPG_AI_MAX_SAN_LOSS`、
`RPG_SAY_SETTLE_WINDOW` / `RPG_SPEECH_TRUNCATE` / `RPG_MAX_CONTEXT_LINES`。

## 日志约定

沿用 `from nonebot import logger` + f-string，统一前缀 `跑团群 {group_id}`。
对局事件（阶段切换、场景进入、检定、工具执行、线索发现、结局、AI 降级）
记 `info`；完整 KP 提示词记 `debug`。
