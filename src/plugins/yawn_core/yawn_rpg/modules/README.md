# 跑团模组写作规范

本目录存放 YAML 剧本模组（`*.yaml`）。模组是 AI 主持人（KP）的行动边界：
KP 只能在模组定义的场景、NPC、线索、结局范围内推进剧情，一切数值
（骰子、HP、SAN、时钟）由引擎掌控，KP 无权裁决。

- **参照范例**：同目录 `yuzhai_old_house.yaml`（自带模组「雨夜旧宅」），
  本文中出现的所有约定都能在里面找到实例。
- **引擎机制细节**（工具验证规则、降级链、时钟实现等）见上级目录
  `../CLAUDE.md`——那是面向引擎开发者的文档，本文面向模组作者。

## 加载行为

- 插件启动时扫描本目录 `*.yaml`（按文件名排序）：任一文件 pydantic
  校验失败只记一条 warning 日志并跳过该文件，**不会拖垮插件加载**；
  写坏模组时可查 bot 日志定位。
- 模组 `id` 全局唯一：后加载的同 id 文件被跳过（warning）。
- 玩家未 `/选择模组` 时，默认使用第一个成功加载的模组。

## 顶层字段（ModuleDef）

| 字段 | 必填 | 默认 | 说明 |
|---|---|---|---|
| `id` | ✔ | — | ASCII snake_case，注册与 `/选择模组` 的键，跨文件唯一 |
| `name` | ✔ | — | 中文显示名（列表面板、KP 概览标题） |
| `description` | | `""` | 一句话简介，列表面板展示 |
| `min_players` | | `1` | 生效下限 = `max(RPG_MIN_PLAYERS, min_players)`；须 ≤ `max_players` |
| `max_players` | | `6` | 报名上限 |
| `difficulty` | | `"入门"` | 自由文本，仅列表面板展示，无校验 |
| `opening` | ✔ | — | 开局播报文案（多行用 `\|` 块）；KP 概览中只取前 150 字作「前提」 |
| `start_scene` | ✔ | — | 起始场景 id，必须存在于 `scenes` |
| `scenes` | ✔ | — | 场景列表 |
| `npcs` | | `[]` | NPC 列表 |
| `monsters` | | `[]` | 怪物列表 |
| `clues` | | `[]` | 线索列表 |
| `endings` | | `[]` | 结局列表 |
| `events` | | `[]` | 具名事件列表（纯 KP 上下文） |
| `time` | | 见下文 | 游戏内时钟配置 |
| `generic_endings` | | `true` | 是否启用引擎内置通用结局安全网 |

## scenes（场景）

| 字段 | 必填 | 默认 | 说明 |
|---|---|---|---|
| `id` | ✔ | — | ASCII snake_case，模组内唯一 |
| `name` | ✔ | — | 中文名（进场景标题、KP 提示词、`/前往` 匹配） |
| `narration` | ✔ | — | 每次进入时播报；同样进 KP 场景块。**不要出现数字**（KP 硬规则：一切数值由系统播报） |
| `npcs` | | `[]` | 静态在场成员（NPC id 列表，须已定义）。**仅对 schedule 为空的 NPC 生效**——NPC 一旦有行程，这里写了也不算 |
| `monsters` | | `[]` | 怪物 id 列表（须已定义）；进入场景时按怪物 HP 初始化 |
| `checks` | | `[]` | 检定点列表，见下节 |
| `exits` | | `[]` | 出口列表，见下节 |
| `idle_narration` | | 无 | AI 关闭/失败且无关键词触发时的确定性兜底旁白；不写则用通用罐头文案 |

## checks（检定点，CheckPoint）

检定点是「玩家说到某关键词 → 系统自动提示/执行检定」的确定性机制。
AI 模式下它只是给 KP 的**建议**（是否检定由 KP 决定）；AI 关闭或
失败时自动执行。

| 字段 | 必填 | 默认 | 说明 |
|---|---|---|---|
| `id` | ✔ | — | **全局唯一（跨所有场景）**。引擎用单一集合记录已触发检定，id 撞车会使后写检定点的 `once` 被前者永久占用 |
| `skill` | ✔ | — | `san` 或技能 key（见下表） |
| `difficulty` | | `regular` | `regular` / `hard`（技能值 ×½）/ `extreme`（×⅕） |
| `mode` | | `individual` | `individual` 保持单人检定；`team` 令全部在场调查员共同检定 |
| `required_successes` | | 多数通过 | 仅 `mode: team` 可用；达到该成功人数即团队成功 |
| `triggers` | | `[]` | 触发关键词：大小写不敏感的子串匹配。每句发言至多触发一个检定点 |
| `priority` | | `0` | 多检定点同时命中时高者优先，同分按声明序。SAN 检点建议给 `1` |
| `once` | | `false` | 整局至多触发一次（线索/伤害/SAN 检点务必开启） |
| `success_text` | ✔ | — | 成功时引擎逐字播报 |
| `failure_text` | | `""` | 失败时引擎逐字播报（也请写全） |
| `clue` | | 无 | 成功时奖励的线索 id（须已定义）；同时让该线索可被 KP 用 `grant_clue` 主动授予（once 检点触发且失败后不可授予——系统已裁决，KP 不得覆盖） |
| `san_loss` | | 无 | `"成功侧/失败侧"` 骰表达式，如 `"1/1d6"`。**`skill: san` 时必填** |
| `damage_on_fail` | | 无 | 失败时对检定者结算的伤害骰表达式 |
| `time_cost` | | 无 | 本次检定消耗的分钟数（覆写默认 `check: 10`） |

**合法 skill 键**（`san` + `charsheet.SKILLS`）：
`library` 图书馆、`listen` 聆听、`spot_hidden` 侦查、`psychology` 心理学、
`persuade` 说服、`fast_talk` 话术、`intimidate` 恐吓、`stealth` 潜行、
`lockpicking` 锁匠、`mech_repair` 机械维修、`elec_repair` 电气维修、
`first_aid` 急救、`medicine` 医学、`climb` 攀爬、`swim` 游泳、`jump` 跳跃、
`brawl` 斗殴、`firearms` 射击、`dodge` 闪避、`drive` 驾驶。
（`cthulhu_mythos` 克苏鲁神话可通过加载校验，但 KP 工具不允许主动检定它，
写进检定点无意义。）

**骰表达式**（`san_loss` / `damage_on_fail` / 怪物 `damage` 通用）：
`NdM`、`NdM±K` 或纯整数；约束 1 ≤ N ≤ 100，1 ≤ M ≤ 1000。

## exits（出口，Exit）

| 字段 | 必填 | 默认 | 说明 |
|---|---|---|---|
| `to_scene` | ✔ | — | 目标场景 id（须已定义） |
| `condition` | | 无 | 通行条件（条件表达式，见下文）。引擎对 KP 工具与 `/前往` 一视同仁地强制执行；**KP 只见「可通行/暂不可通行」布尔**，看不到条件本身——被拒后 KP 应叙述阻碍（如门锁着），不要反复重试 |
| `keywords` | | `[]` | `/前往` 的同义词匹配词（目标场景名永远可匹配，keywords 只补充「地下室/地窖」这类别名）。KP 工具按 scene_id 传参，不走 keywords |
| `auto` | | `false` | 条件满足时自动切景（无需 KP/玩家介入）。只用于无条件序幕走廊之类；连续自动切景超过「场景数 +1」会被判为恒真条件成环并兜底收尾 |
| `narration` | | `""` | 通行时播报的固定过渡文案（上锁出口建议写开锁瞬间的描述） |
| `time_cost` | | 无 | 通行消耗分钟数（覆写默认 `move: 10`） |

## npcs（NPC）

| 字段 | 必填 | 默认 | 说明 |
|---|---|---|---|
| `id` / `name` | ✔ | — | id 唯一；name 用于播报、自然语言路由与 `/攻击` 的名称子串查找 |
| `public_desc` | ✔ | — | 公开形象。进 KP 概览与每回合场景块——只写安全信息 |
| `persona` | ✔ | — | 人格。进 KP 概览（压缩一行）+ NPC 对白智能体（全文） |
| `knows` | | `[]` | 可在对话中自然说出的信息。进 KP 概览 + NPC 智能体 |
| `secrets` | | `[]` | 机密。**唯一入口是该 NPC 自己的对白智能体提示词**（附「绝不主动透露」指令），KP 概览永不可见。加载期校验：任何 secret 不得是 persona / public_desc / 任一 knows / 任一行程 activity 的子串（正是会进概览的字段），违者拒载 |
| `fallback_line` | | `""` | AI 关闭/失败时自然语言 NPC 对话与 `speak_as_npc` 的罐头回复 |
| `initial_rapport` | | `0` | 对单个调查员的初始好感，范围 `-100~100` |
| `initial_attitude` | | `0` | 对全队共享的初始公共态度，范围 `-100~100` |
| `facts` | | `[]` | 只能由社交节点解锁的个人情报；正文只私聊发现者 |
| `social_nodes` | | `[]` | 可由自然语言路由触发的社交诉求与确定性检定规则 |
| `schedule` | | `[]` | 行程表，见下 |
| `hp` | | `10` | 生命值（可被 `/攻击`，镜像怪物战斗） |
| `attack_skill` | | `40` | 反击命中率（d100 百分制） |
| `attack_name` | | `"攻击"` | 攻击描述名（如「油灯」） |
| `damage` | | `"1d3"` | 伤害骰表达式 |
| `dodge` | | `30` | 被攻击时的闪避对抗值 |
| `on_death_clue` | | 无 | 死亡时自动授予的线索 id（须已定义） |
| `on_death_text` | | `""` | 死亡播报文案 |

**击杀 NPC 的硬性后果**（作者只能知情、无法配置）：记 `murder` +
`npc_dead:<id>` flag → 触发通用逮捕结局；在场存活 NPC 立即反击。

### facts 与 social_nodes（NPC 社交）

群内自然语言是 NPC 对话的唯一入口。系统先用轻量路由器判断这是普通
`kp_say`、`npc_talk` 还是 `social_action`；路由失败、置信度不足或 AI
关闭时，只按消息中的 NPC 名称/id，或该玩家最近交互的 NPC 做确定性兜底。
普通 NPC 对话不掷骰；命中社交节点后，才由引擎按节点声明调用确定性技能检定。

NPC 的关系有两层：`rapport` 是「NPC 对当前玩家」的个人好感，
`attitude` 是「NPC 对全队」的公共态度。两者都限制在 `-100~100`，玩家
只看到「敌对 / 警惕 / 中立 / 友善 / 信任」分段，不显示裸数值。每个 NPC
的公开上下文独立保存最近六轮，其他 NPC 的对话不会串入；`secrets` 和
未公开 `facts.text` 也不会进入群聊、KP 提示词或其他 NPC 上下文。

`NPCFact` 的正文是私人信息，只会通过私聊发给解锁者；玩家可用
`/分享情报 NPC名 情报名` 经行动队列公开。`private_clues` 复用个人线索
归属机制，`public_clues` 直接全队播报。节点文案中的成功/失败目标必须是
安全的公开反应，不能把私人情报正文写进 `goal`、`success_text` 或
`failure_text`。

最小示例：

```yaml
facts:
  - id: hidden_fact
    name: 夜间守秘
    text: NPC 私下告诉当前玩家的完整情报。
social_nodes:
  - id: ask_secret
    name: 追问夜间动静
    goal: 让 NPC 说明自己为何在夜里守着旧宅
    requires_facts: []
    strategies:
      - skill: persuade
        name: 温和劝说
        difficulty: regular
      - skill: fast_talk
        name: 顺势套话
        difficulty: hard
      - skill: intimidate
        name: 直接施压
        difficulty: extreme
    min_rapport: -20
    min_attitude: -20
    max_attempts: 3
    retry_rapport_penalty: 2
    retry_attitude_penalty: 1
    success_rapport_delta: 15
    success_attitude_delta: 4
    failure_rapport_delta: -5
    failure_attitude_delta: -2
    success_text: NPC 在众人面前松口了。
    failure_text: NPC 变得更加警惕。
    unlock_facts: [hidden_fact]
    private_clues: []
    public_clues: []
    success_flags: [npc_opened_up]
    failure_flags: []
```

`SocialStrategy` 可以覆盖节点的成功/失败关系变化和文案；省略时继承
节点默认值。失败重试会额外扣除
`retry_rapport_penalty * (attempt - 1)` 与
`retry_attitude_penalty * (attempt - 1)`，超过 `max_attempts` 后不再检定。
普通礼貌、共情、道歉等情绪只产生很小的关系微调，并按每 NPC 每探索轮
的上限累计；社交节点的重大变化不受该微调上限影响。

### schedule（行程条目）

```yaml
schedule:
  - { from: "21:00", to: "23:30", scene: living_room, activity: 守着油灯，欲言又止 }
  - { from: "23:30", to: "05:00", scene: basement, condition: monster_dead:ghoul, activity: 在空荡荡的地下室里发呆 }
  - { from: "23:30", to: "05:00", scene: living_room, activity: 守着灯打盹 }   # 兜底条目
```

| 字段 | 必填 | 说明 |
|---|---|---|
| `from` / `to` | ✔ | HH:MM。**务必加引号**——YAML 1.1 会把裸 `21:00` 按六十进制解析成整数 1260（schema 会尽力修复，但别依赖） |
| `scene` | 条件必填 | 所在场景 id；`away: true` 时免填 |
| `activity` | | 当前活动的公开描述（进 KP 场景块「正在：…」与进出播报）；不得含机密（同 secrets 校验） |
| `condition` | | 条目生效条件（完整条件表达式） |
| `away` | | `true` = 外出（不在任何场景） |

语义要点：

- **schedule 非空即权威**：完全覆盖 `scene.npcs` 静态成员；空 schedule
  的 NPC 保持旧式静态在场语义。
- 窗口为 `[from, to)` 半开区间，**支持跨午夜**（`from > to`，如 23:00→05:00）；
  `from == to` 表示全天窗口（常配合条件条目使用，如「一发生袭击就逃离」）。
- 按**声明序**取第一条「条件成立 + 时钟落在窗口」的条目。
- **全部条目不匹配 = NPC 不在场**。务必写兜底条目覆盖剩余时段
  （范例中以无条件条目收尾）。
- 编辑器 P1-3 检查会以 `time.start` 到最早 `time_after` 结局为可玩窗口；没有
  `time_after` 终局时检查一个完整日周期。它按行程边界、跨午夜窗口和条件时间边界
  报告可达场景覆盖空档、不可达目标场景及在窗口内永远不会命中的条目。显式
  `away: true` 是有意离场，不会被当作覆盖空档。

## monsters（怪物）

| 字段 | 必填 | 默认 | 说明 |
|---|---|---|---|
| `id` / `name` | ✔ | — | `/攻击` 按名称子串匹配，**怪物优先于 NPC** |
| `hp` | ✔ | — | 进入其所在场景时初始化 |
| `attack_skill` | ✔ | — | 命中率（d100 百分制） |
| `damage` | ✔ | — | 伤害骰表达式 |
| `attack_name` | | `"攻击"` | 攻击描述名 |
| `dodge` | | 无 | 闪避对抗值；不写/为 0 = 不会闪避 |
| `on_death_clue` | | 无 | 死亡线索（须已定义）：**死亡后才可经 `grant_clue` 授予**；同时满足 `monster_dead:<id>` 条件 |
| `on_death_text` | | `""` | 死亡播报文案 |

## clues（线索）

| 字段 | 必填 | 说明 |
|---|---|---|
| `id` / `name` | ✔ | 名称进 KP 场景块「[已发现线索]」（只有名字 + id） |
| `text` | ✔ | 发现时以「〔线索〕name」播报**一次**。永不进任何提示词——别在 text 里写「KP 应如何如何」 |

## endings（结局）

| 字段 | 必填 | 默认 | 说明 |
|---|---|---|---|
| `id` | ✔ | — | `end_session` 工具的合法取值 |
| `condition` | ✔ | — | 条件表达式。**不可恒真**（空或仅 `always` 词条会在加载期被拒——否则开局第一次安全网扫描就瞬间终局） |
| `text` | ✔ | — | 终局逐字播报（惯例 `═══ 结局 · X ═══` 标题行） |
| `outcome` | | `"neutral"` | `good` / `bad` / `neutral` |
| `name` | | `""` | 概览与 `query_story` 的显示名（空回退 id） |
| `summary` | | `""` | 来龙去脉，**仅 KP 经 `query_story` 查询可见**，绝不播报、绝不返 condition。按**导演指引**写（「可借 NPC 之口施压，但不得阻止」） |

扫描规则：引擎在每轮主循环顶部按**声明序**扫描模组结局，随后才是
内置通用结局——**模组结局永远优先**，声明序即优先级。约定：

- 时间兜底结局（如 `time_after:06:00` 天亮）声明在**最后**，否则会遮蔽
  其后声明的结局。
- 不写 `all_players_incapped` 结局的模组由通用「全军覆没」兜底；想要
  自定义全灭演出就自写一个，它会抢占通用版。

`generic_endings: true`（默认）时的内置通用结局（阈值常量化，按此序）：
纵火 flag ≥4 的彩蛋（neutral）→ ≥2 火灾（bad）→ 谋杀逮捕（bad）→
袭击 flag ≥3 制服（bad）→ 全员倒地（bad）。设 `false` 可全部关闭
（不推荐：失去暴力/纵火升级的安全网）。

## events（具名事件，PlotEvent）

纯 KP 上下文，**不触发任何机制**：条件满足时记入「已发生事件」，进 KP
提示词与概览，KP 可经 `query_story` 查其 summary 来把握剧情节奏。

| 字段 | 必填 | 说明 |
|---|---|---|
| `id` / `name` | ✔ | 唯一 id；名称进概览与「[已发生事件]」 |
| `summary` | | 导演指引，仅 `query_story` 可见 |
| `condition` | | 可空——空条件 = 序幕事件，开局首轮即记。与结局不同，**事件恒真无害**，加载期不拒 |

## time（游戏内时钟）

```yaml
time:
  start: "21:00"          # 开局时刻（HH:MM，加引号）
  costs: { check: 15 }    # 按行动类型覆写分钟数（可选）
```

默认成本：`say: 5` / `talk: 10` / `check: 10` / `move: 10` / `attack: 5` /
`wait: 0`。只有**成功**的行动推进时钟。覆写优先级：检定点 / 出口的
`time_cost` > `time.costs` > 引擎默认。`start` 同时是条件表达式时间词
的基准轴（见下文）。

## 条件表达式

词条以 `&` 组合（须全部满足）；未知词条、空词条（悬挂 `&`）与非法
格式在运行时保守判 False，**加载期校验所有引用**（id 拼错会在加载时
报错，不会变成局内永久软锁）。

| 词条 | 含义 |
|---|---|
| `always` | 恒真占位 |
| `clue:<id>` | 线索已发现 |
| `clues:<a>+<b>` | 多条线索全部已发现（AND） |
| `monster_dead:<id>` | 怪物已被击杀 |
| `scene:<id>` | 当前场景 |
| `all_players_incapped` | 全员倒地 |
| `time_after:HH:MM` / `time_before:HH:MM` | 剧本时间轴比较 |
| `time_between:HH:MM-HH:MM` | 时间窗口（支持跨午夜，每日重复） |
| `flag:<name>` / `flag:<name>>=N` | 引擎 flag 计数 ≥1 / ≥N |

**时间词按剧本时间轴求值**：目标时刻折算为相对 `time.start` 的偏移
（mod 1440 分钟）与已流逝时间比较。21:00 开局时 `time_after:06:00`
要等时钟真正推进到次日 06:00 才成立——不会因为开局钟面「看起来已过
06:00」而成立。NPC 行程的 from/to 是独立的绝对时刻机制，不受影响。

**flag 由引擎写入，作者只能消费**：发言关键词扫描写 `arson`（放火/烧了/
点火/汽油/烧死/一把火）、`threat`（恐吓/威胁/杀了/弄死）、`destroy`
（砸了/砸烂/烧毁/拆了）；攻击 NPC 写 `assault`；击杀 NPC 写 `murder` +
`npc_dead:<id>`。

## 可见性边界（防剧透速查）

| 时机 | KP 能看到 |
|---|---|
| 整局一次（概览） | 模组前提（opening 前 150 字）、NPC 名册（public_desc + persona + knows）、全部结局与具名事件的**名字** |
| 每回合（场景块） | 场景名 + narration、在场 NPC 的名字 + public_desc + 当前 activity、存活怪物名、已发现线索名、出口通行性布尔、调查员定性状态（无恙/轻伤/重伤/倒地）、游戏内时钟、近期群聊、已发生事件名 |
| 经 `query_story` 查询 | 结局/事件的 name + summary + 倾向（**不返 condition**） |
| **永不可见** | 检定的成功/失败文案（结算前）、线索 `text`（发现前不播报、且永不进提示词）、出口条件、结局与事件条件、怪物/NPC 战斗数值、NPC `secrets`（仅该 NPC 自己的对白智能体可见） |

编辑器 P1-3 私密性检查把 `NPC.secrets` 与 `NPCFact.name/text` 视为私密源，
并对群播文案、场景/出口/检定/线索/结局文案、NPC 死亡与 fallback 文案、行程
`activity`、KP 概览/场景块以及 NPC/路由公共上下文做规范化后的精确子串检查。
命中时报告私密源路径和公共汇路径；检查只做静态文本匹配，不尝试推断改写语义。

## 写作规范清单

1. **id 一律 ASCII snake_case**（模组/场景/检定点/NPC/怪物/线索/结局/
   事件），中文只进 `name` 等展示字段。
2. **时间加引号** `"21:00"`（防 YAML 六十进制陷阱）。
3. SAN 检点给 `priority: 1` + 宽触发词（范例连「看」都写）；搜索类默认 0。
4. 线索/伤害/SAN 检点一律 `once: true`；`success_text` 与 `failure_text` 都写全。
5. 上锁出口 = `condition` + 一段开锁瞬间的 `narration`。
6. NPC schedule 以**无条件兜底条目**收尾；`from == to` + `condition` 表达
   「满足条件则全天如此」（如 `{ from: "00:00", to: "00:00", away: true, condition: flag:assault }`）。
7. 结局声明序 = 优先级：具体结局在前，时间兜底结局最后；`name` +
   `summary` 都要写（summary 按导演指引写）。
8. 多行文本用 YAML `|` 块；narration 类播报文本**不出现数字**。
9. `check id` 跨场景全局唯一；线索 id 被引用处（检定点奖励、出口条件、
   死亡线索、结局条件）都要在 `clues` 里定义。

## 最小骨架模板

复制后按需扩展（已通过加载校验的结构）：

```yaml
id: my_module                    # ASCII snake_case，跨文件唯一
name: 示例模组
description: 一份最小模组骨架
min_players: 1
max_players: 6
difficulty: 入门
time:
  start: "21:00"
generic_endings: true

opening: |
  开篇播报文案……

start_scene: entrance

scenes:
  - id: entrance
    name: 入口
    narration: |
      场景播报文案……
    idle_narration: 四周一片沉寂。
    checks:
      - id: entrance_search       # 跨场景全局唯一
        skill: spot_hidden
        once: true
        triggers: [搜索, 查看, 翻找]
        success_text: 你在门垫下摸到一把生锈的钥匙。
        failure_text: 门垫下只有积年的灰尘。
        clue: rusty_key
    exits:
      - to_scene: inner_room
        keywords: [内室, 里屋]
        condition: clue:rusty_key  # 上锁出口
        narration: 锈钥匙在锁孔里艰涩地转了一圈，门开了。

  - id: inner_room
    name: 内室
    narration: |
      内室昏暗……
    npcs: [guard]                 # guard 有 schedule → 以此行为准

npcs:
  - id: guard
    name: 守卫
    public_desc: 一个睡眼惺忪的守卫。
    persona: |
      你是这座宅子的守卫，胆小怕事……回复简短。
    knows: [这宅子很久没人来了]
    secrets: [你偷偷在墙缝里藏了违禁品]   # 不得是 persona/knows/activity 的子串
    fallback_line: 嗯？什么？我什么都不知道。
    schedule:                      # 声明序=优先级；无匹配=不在场 → 必须兜底
      - { from: "21:00", to: "23:00", scene: inner_room, activity: 来回踱步 }
      - { from: "23:00", to: "21:00", scene: inner_room, activity: 靠着墙打盹 }  # 跨午夜兜底

clues:
  - id: rusty_key
    name: 生锈钥匙
    text: 一把锈迹斑斑的钥匙，像是开内室门的。

endings:                           # 声明序=优先级；条件不可恒真
  - id: truth_revealed
    condition: clue:rusty_key
    name: 真相大白
    outcome: good
    summary: 导演指引：调查员取得钥匙后达成……
    text: |
      ═══ 结局 · 真相大白 ═══
      结局播报文案……

events:                            # 纯 KP 上下文；空条件=序幕事件
  - id: prologue
    name: 序幕
    summary: 导演指引：开局铺垫……
```
