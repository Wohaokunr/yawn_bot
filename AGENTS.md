# 项目维护注意事项

- `tools/rpg_module_editor` 的时间线必须按控件实际宽度渲染，不能假设终端固定为 96 列。
- 列表排序应通过返回新索引的共享移动逻辑完成；边界移动失败时不得修改数据、选中索引或脏状态。
- 重建主列表或子列表选项时，应尽量保留当前实体/条目的选择；删除后选择相邻的有效条目。
- 修改 RPG 模组编辑器后，应运行其 pytest、Ruff 和 Pyright 检查。
- `yawn_rpg` 当前只保证单进程内的一群一局；若要多实例部署，必须先把房间所有权、用户跨群唯一约束和动作路由迁到共享存储，并使用租约或 fencing token 防止双引擎消费。
- `yawn_rpg` 的动作入口应保持“只校验并入队、引擎单写”；报名、退报名、房主移交等房间状态也应纳入同一串行状态机，避免阶段切换竞态。
- RPG 动作队列需要容量、单用户配额、去重与过期策略；AI 调用需要全局并发上限、按局公平调度和可观测的降级路径。
- RPG 游戏性优化应优先补齐多人协作与节奏闭环（队伍检定、协助/对抗、轮次或行动点、个人秘密与线索共享），再扩充 AI 叙事能力。
- RPG 若支持长局或生产级运行，应持久化可恢复快照和动作日志，并为重复投递、进程重启与结局写入设计幂等语义。
- `yawn_rpg` 的命令和监听器必须通过 `submit_action()` 入队；不可直接写报名名单、房主或局内状态。引擎消费后必须 `release_action()`，否则会泄漏玩家配额。
- RPG 探索轮中主要行动每人每轮一次；切场景会使旧场景动作失效并清空未使用协助。个人线索默认仅发现者可见，只有显式分享后才可群播。
- `/局面` 的群内渲染只能包含公共信息；HP/SAN、个人线索全文、未公开 NPC 情报和个人关系必须单独私聊，私聊失败只能提示加好友，不得回退正文到群内。
- RPG 探索期限只在缓冲区、pending 和队列均无行动时起算；AI/KP 处理期间暂停。行动过期判断使用阶段、场景、探索轮、战斗轮和当前行动者快照，不得恢复真实排队 TTL。
- 自然语言先在固定合批窗口收集原始消息，再并行分类并保持原顺序；首个 NPC 路由是 KP 合批边界，等待提示是一次性临时群消息，不得写入 `group_log`。

## 任务完成记录

- 2026-08-13：完成 P1-2 场景与结局静态可达性诊断。编辑器 lint 新增基于条件词条、检定/死亡/社交奖励和引擎 flag 写入来源的保守固定点分析，报告条件阻断的场景与结局、无写入来源的条件、全部结局不可达和悬空出口；不修改在线 RPG 引擎或 P1-1 试玩器语义。新增可达性回归覆盖自锁场景、多线索、怪物死亡、NPC 死亡 flag、不可达结局、现有模组零误报及 schema 悬空引用。

- 2026-08-14：新增可选 `yawn_fanqie` 番茄小说子插件。仅处理番茄公开书籍/阅读页和页面字体资源，不登录、不绕过验证码或付费控制；新增 provider、PUA 字体解码、章节范围交互、持久化任务/断点、单 worker 有界队列、用户/群/全局权限链、私聊 TXT 投递和 canonical `yawn_fanqie` 迁移。迁移需由维护者手动执行，正文只短期保存在 localstore 数据目录；完成后应运行插件定向测试、Ruff、Pyright、compileall、正式 discovery 和 `git diff --check`。
- 2026-08-14：修复番茄查询回归：搜索改用公开 `/search/<关键词>` 路径，书籍链接查询把 `_set_book_and_ask_range()` 的 NoneBot `RejectedException` 交还状态机，不再误报“查询失败：RejectedException(0)”。provider、交互命令、任务 worker、权限拦截、断点复用、文件合并和私聊投递均补齐不含正文的 debug/exception 日志；新增搜索路径与状态机异常传播回归测试。保留未跟踪的 RPG 迁移文件，不要混入番茄修复提交。

- 2026-08-14：修复番茄小说交互状态机：移除多个 `got` 参数之间使用 `reject_arg()` 的跨阶段跳转，统一使用 `fanqie_choice` 输入和 `fanqie_step`（input/book/range/confirm）路由；同行输入书名后，后续编号只读取目录，不会再次被当作搜索关键词。新增完整“书名→编号→章节范围→确认”回归，番茄定向测试 8 项通过。

- 2026-08-12：为 yawn_core 增加持久化定时提醒；调度必须使用 nonebot-plugin-apscheduler 的全局 scheduler，消息发送必须通过 OneBot V11 Bot API，重启时从 ORM 恢复启用任务。
- 2026-08-12：定时提醒只持久化可复用的 OneBot 消息段，拒绝 reply、forward、node 等临时段；文本支持 {{倒计时:YYYY-MM-DD}}，新增提醒的 ORM 迁移必须由维护者手动生成并应用。
- 2026-08-12：按维护者要求生成并应用定时提醒迁移 ea3af2a76220；因已有迁移存在多个 head，实际使用 uv run nb orm upgrade heads，数据库已创建 yawn_core_scheduledreminder。

- 2026-08-12：已创建并推送 GitHub 私有仓库 `Wohaokunr/yawn_bot`；本地 Claude 临时工作树不纳入主仓库。
- 2026-08-12：已隔离损坏的 SQLite 数据库并通过 `nb orm upgrade` 重建；SQLite 的 `-wal`、`-shm` 运行时文件已加入忽略规则，避免再次提交。
- 2026-08-12：`yawn_rpg` 从 Git 提交 `13c4a22` 恢复到当前工作区；当前 `main` 分支不含该目录，恢复文件会先显示为未跟踪，确认内容后再由维护者决定是否加入主分支。
- 2026-08-12：修复 `yawn_core` 子插件加载器，使狼人杀与 RPG 独立加载；恢复 RPG 依赖的共享 `llm.py`，并通过 NoneBot 按 `pyproject.toml` 的正式发现流程验证 RPG 已注册。
- 2026-08-12：修复 RPG 命令成功入队时把空字符串传给 `NoneBot.finish()` 的问题；成功入队改为静默结束，避免 OneBot 报 `message must contain at least one sendable segment`，并保留拥塞、重复和过期行动的提示。
- 2026-08-12：实现 NPC 自然语言社交系统：自然语言经 `ai_social` 路由到 KP、NPC 对话或社交节点；每个 NPC 隔离维护公开上下文、个人好感、公共态度、尝试次数与个人情报；社交节点使用确定性检定、关系门槛、递增重试惩罚、情报/线索/flag 奖励；移除 `/对话`、`/询问`，新增 `/分享情报`；补齐 NPC 生成失败、公开奖励和 KP 回合 SAY 配额释放兜底。RPG pytest 11 项、Ruff 与 Pyright 均通过，并同步允许项目已有的中文标点。
- 2026-08-12：新增 `docs/yawnbot-architecture.html` 项目架构图；按 Lieflat B2 Force Graph 模板覆盖 NoneBot/OneBot、`yawn_core`、RPG、狼人杀、ORM/SQLite、LLM 与迁移链路，并完成 HTML 脚本语法、浏览器渲染和筛选/节点详情交互检查。
- 2026-08-12：审查狼人杀子插件但未修改业务代码；确认死者在白天流程中会被重新解禁、狼人杀私聊监听与 Yawn 对话监听同优先级导致无斜杠行动可能被吞、引擎任务未首次运行或获取 Bot 失败时会残留幽灵房间，以及狼人杀与 RPG 可在同群并存并造成同名命令路由冲突。后续修复应优先补生命周期清理、私聊路由、死者禁言、跨玩法互斥和有界行动队列。
- 2026-08-12：狼人杀业务源码（排除项目规定不编辑的重复迁移副本）通过 Ruff、Pyright 和 compileall；现有 pytest 9 项通过，但仓库没有狼人杀专属测试。狼人杀 ORM 表及迁移已在本地数据库中存在，重复迁移副本单独触发 Ruff 56 项格式/导入告警。
- 2026-08-12：将架构图换为 Lieflat `porcelain` 青瓷蓝预设，统一更新暗卡背景、节点层次、连线、图例和 tooltip；已完成浏览器视觉与图层筛选检查。
- 2026-08-12：按用户选择将架构图改为 Lieflat `wire` 编辑部红：保留黑灰暗卡结构，仅用荧光橙强调共享 `yawn_core` 层，其余层级保持灰阶。
- 2026-08-12：新增个人技能 `C:\Users\ASUS\.codex\skills\rpg-module-author`，用于交互式编写覆盖 yawn_rpg 全部当前模组特性的 YAML；技能以 `module_schema.py`、`modules/README.md` 和 `tools/rpg_module_editor --check` 为校验依据，综合模板已通过结构校验与同源加载器注册验证。
- 2026-08-12：将 `13c4a22` 的 RPG 模组编辑器恢复到主工作树并同步当前 schema；新增团队检定模式/人数、NPC 初始关系、情报、社交节点与三类策略的完整表单，补齐嵌套 YAML 往返、未知键/引用/播报数字诊断、线索与情报改名级联及机密泄露反馈；补回编辑器工具依赖。编辑器 pytest 48 项、Ruff、Pyright 均通过。
- 2026-08-12：完善 RPG 模组编辑器打开文件体验；文件选择器只显示 `.yaml`/`.yml` 与目录，支持选中文件反馈、打开/取消按钮，并修复另存为目录高亮与 YAML 扩展名处理；主界面新增工具栏、Footer 快捷键提示及 Ctrl+Tab 分区切换。编辑器 pytest 50 项、Ruff、Pyright 均通过。
- 2026-08-12：修复 RPG 模组编辑器 Scenes 页在窄屏/短屏下检定点、出口、在场成员面板被压扁或移出视口的问题；Scenes 改为列表与可滚动表单的响应式布局，三个子面板保持最小可用高度。修复最大化及弹窗期间 resize 状态写入错误 Screen 导致主界面塌缩的问题，并补充五组终端尺寸回归测试；编辑器 pytest 66 项、Ruff、Pyright 均通过。
- 2026-08-12：完成 RPG 模组编辑器响应式 TUI 与引用字段智能下拉；布局按 60×24、80×30、96×36、140×44、240×80 自适应，时间线使用实际控件宽度；新增可搜索单值/多值实体引用、条件词条筛选、Ctrl+F 搜索定位和实体/嵌套条目深复制，未知引用保留为手动值。编辑器 pytest 60 项、Ruff、Pyright 均通过。
- 2026-08-12：新增青春轻小说 RPG 模组《潮声停靠之前》；以 NPC 社交节点成败均写入的剧情 flag 累积「自己的声音」，真结局只判断汐见未央是否自主选择，不把上车规定为标准答案；场景推进同时使用已获线索驱动的 NPC 行程与钟点兜底，避免快慢团错过关键 NPC。编辑器 lint 现会把社交节点声明的 success_flags/failure_flags 识别为合法写入来源。模组校验零错误零警告，编辑器 pytest 67 项、RPG pytest 11 项、Ruff 与 Pyright 均通过。
- 2026-08-12：诊断狼人杀 AI 玩家无法添加：当前 `main` 仅包含基础报名实现，缺少 `添加AI/移除AI` 命令、`PlayerState.is_ai` 与 AI 驱动器；相关功能位于未合并的 `feat/werewolf-ai` 分支提交链（29352b1 起）。
- 2026-08-12：将 `feat/werewolf-ai` 合并到 `main`；保留主线当前 RPG/编辑器版本，合入狼人杀 AI 命令、驱动器、板子与状态支持及 `is_ai`/板子字段迁移，排除嵌套 worktree、`.vs` 状态、重复 RPG 迁移和顶层迁移副本。AI 变更 Ruff、Pyright、compileall 与 NoneBot 插件加载通过；RPG 核心 11 项、编辑器校验/YAML 33 项、响应式 16 项、应用冒烟 14 项、提醒 5 项通过，编辑器社交表单 1 项保留主线既有失败。
- 2026-08-12：`yawn_bot` 私有仓库受当前 GitHub 计划限制无法直接启用 Pages；未公开整个项目，改将 `docs/yawnbot-architecture.html` 发布到现有公开 Pages 站点 `Wohaokunr/Wohaokunr.github.io`，页面地址为 `https://wohaokunr.github.io/yawnbot-architecture.html`，部署工作流验证成功。
- 2026-08-12：按最新工作区重绘 `docs/yawnbot-architecture.html`；保持 Lieflat `wire` 编辑部红配色，补入定时提醒、RPG《潮声停靠之前》与 Textual 编辑器、狼人杀四种板子 / AI 玩家及 board/is_ai 迁移，并增加手机安全区布局、横向筛选、单指拖动 / 双指缩放说明、缩放 / 重置 / 适应屏幕按钮。HTML 脚本、宽屏渲染和 390×844 窄屏交互检查通过，窄屏无横向溢出。
- 2026-08-12：将更新后的架构图发布到公开 Pages 仓库 `Wohaokunr/Wohaokunr.github.io` 的 `main` 分支，提交 `090110436135`；Pages Actions 部署成功，线上地址为 `https://wohaokunr.github.io/yawnbot-architecture.html`，并确认线上内容包含移动端控件、RPG 新模组和狼人杀 AI 节点。
- 2026-08-12：修复狼人杀 AI 合并后的迁移链：`cc9ace87b0ea` 接续狼人杀基础迁移 `155f2713d519`，`33d3f5e9af32` 改为接续 `cc9ace87b0ea`，并移除已由主线 RPG 迁移覆盖的误生成字段类型变更；`is_ai` 迁移通过临时服务端默认值兼容已有战绩。数据库仍按项目约定由维护者手动执行 `uv run nb orm upgrade heads`。
- 2026-08-12：完成项目代码审查：确认 RPG 动作配额释放遗漏、AI 切场景不推进时间、切场景不清理战斗状态；狼人杀白天会解禁死者、RPG/狼人杀命令与私聊监听存在路由冲突；狼人杀与 Yawn 对话队列缺少背压；定时提醒注册失败仍可能提示创建成功。RPG 核心 11 项测试、目标插件 Ruff/Pyright（Python 3.10）与 compileall 通过；编辑器回归 66 项通过，`test_scene_edit_and_rename_cascade` 存在偶发时序失败，后续修复前需保留该风险记录。
- 2026-08-12：修复审查清单全部已确认问题：新增 RPG/狼人杀共享群组与用户占用登记；RPG 局外行动配额释放、空房结束、AI 切场计时、战斗状态清理、通用结局工具枚举；狼人杀死者禁言恢复、私聊行动优先级与斜杠命令隔离、有界/去重/单用户配额行动队列及 AI 统一入队；AI 对话队列背压与流式调用共享并发上限；定时提醒调度失败回滚；编辑器异步字段回写等待；统一 Python 3.10 与迁移目录 lint 排除，并修正全局 Pyright/Ruff 阻断项。新增跨玩法、队列、提醒和对话回归，目标测试 87 项通过，Ruff、Pyright、compileall 与 git diff --check 均通过。
- 2026-08-13：完成代码审查并记录以下待修复 bug（本次只读审查，未改业务代码）：
  - **P0 提醒数据库结构不一致**：`src/plugins/yawn_core/data_models/scheduled_reminder.py:28-37` 新增 `schedule_type`、`run_at` 并将 `cron_expression` 改为可空，但规范迁移 `migrations/versions/ea3af2a76220_add_scheduled_reminders.py:26-40` 和当前 `data/nonebot_plugin_orm/db.sqlite3` 仍只有旧列。启动恢复 `src/plugins/yawn_core/reminder.py:1055-1058` 查询 ORM 时会触发 `OperationalError: no such column: schedule_type`，提醒功能可能阻断启动恢复或全部不可用。必须先补迁移并由维护者手动应用。
  - **P1 Cron 提醒执行一次后失效**：`src/plugins/yawn_core/reminder.py:1020-1036` 的 `_finalize_once()` 只有一次性类型判断包住停用逻辑，但 `_remove_reminder_job()` 在判断外；`_run_reminder_job()` 在 `:1039-1045` 对所有 `success/error` 调用它。循环提醒第一次执行后 APScheduler 任务被删除，数据库仍为 `enabled=True`，直到重启才恢复。
  - **P1 过期一次性提醒永久挂起**：`src/plugins/yawn_core/reminder.py:1063-1070` 恢复任务时发现 `run_at <= now` 只记录日志并 `continue`，没有标记停用、记录失败或补偿发送。该提醒保持启用但没有 job，之后重新启用又会因时间已过去而被拒绝。
  - **P1 立即发送错误消费未来的一次性提醒**：`src/plugins/yawn_core/reminder.py:1748-1751` 的“立即发送”无论是手动测试还是提前通知，成功/失败都会调用 `_finalize_once()`；未来 `run_at` 的一次性提醒因此被提前 `enabled=False` 并移除计划任务。
  - **P1 提醒运行时绕过用户级功能开关**：`src/plugins/yawn_core/reminder.py:987-992` 发送时只查询 `GroupFeature`，没有按 `permission.py` 约定检查 `UserFeature -> GroupFeature`，私聊目标也没有检查 `GlobalUserFeature`。已对单个用户禁用提醒后，已存在的 job 仍可能继续发送。
  - **P1 presence 锁重试丢失写入**：`src/plugins/yawn_core/presence.py:109-120` 提交遇到 SQLite `OperationalError` 后执行 `rollback()`，但没有重新查询/重建 `BotUser`、`BotGroup`、`UserGroup` 或字段更新；后续重试提交空事务，瞬时锁竞争会静默丢掉本次活跃记录。
  - **P1 presence 并发首次写入未幂等**：`src/plugins/yawn_core/presence.py:37-101` 使用先 `get` 后 `add`。同一用户/群的并发首条消息都看到不存在并插入唯一键，后提交者抛出 `IntegrityError`，当前重试只捕获 `OperationalError`，可能直接使事件预处理失败。
  - **P1 管理面板外键父记录缺失**：`src/plugins/yawn_core/panel.py:1112-1121,1222-1231,1380-1406` 的“管理用户/群功能”路径允许超管指定从未被 presence 记录的 QQ 或群，但只创建子表记录，未确保 `BotUser`、`BotGroup`、`UserGroup` 父行存在，提交时会因外键约束失败。
  - **P1 非模式 AI 对话并发写会话**：`src/plugins/yawn_core/ai_chat.py:524-535` 在用户未进入模式时直接调用 `_process_chat()`，没有复用 per-user worker。连续快速发送 `/对话 A`、`/对话 B` 可并发创建/更新会话并交叉提交，导致历史分叉、乱序或消息丢失。
  - **P1 流式 AI 截断内容污染历史**：`src/plugins/yawn_core/ai_chat.py:363-384` 在 OpenAI 流中断后仍返回已经累计的完整字符串，而该字符串可能有部分段落未成功发送；`_process_chat()` 在 `:463-472` 将其作为完整 assistant 回复持久化，用户看到的内容与后续上下文不一致。
  - **P1 RPG 入队后的房主权限竞态**：`src/plugins/yawn_core/yawn_rpg/commands.py:208-215,317-320` 只在命令入队前验证房主权限；`src/plugins/yawn_core/yawn_rpg/engine.py:528-556` 消费 `LEAVE_GAME`、`MODULE_SELECT`、`START_GAME` 时不复核当前房主。旧房主可先排队开始/选模组，再退报名并移交房主，过期动作仍会执行。
  - **P1 RPG 战斗行动权可绕过**：`src/plugins/yawn_core/yawn_rpg/engine.py:2716-2770` 只为 `ATTACK`、`PASS_TURN` 检查 `combat_order`；`CHECK`、`TALK_NPC`、`WAIT`、`ASSIST` 可由任意玩家执行且不推进战斗索引，`MOVE` 经 `enter_scene()`（`:957-980`）还会清空战斗状态。可导致玩家在他人回合重复行动、推进世界时钟或直接逃出战斗。
  - **P1 RPG 战斗当前玩家倒地会跳过下一位**：`src/plugins/yawn_core/yawn_rpg/engine.py:2677-2686` 先过滤失能玩家，再按旧 `combat_index + 1` 计算新索引。顺序 `[A,B,C]` 且 A 倒地时，新列表为 `[B,C]` 但索引变为 1，直接跳到 C。
  - **P1 RPG 尚未启动的任务取消会泄漏房间注册**：`src/plugins/yawn_core/yawn_rpg/state.py:573-582` 的 `stop_game()` 取消并等待 worker，但依赖 `run_game()` 的 `finally` 调 `discard_game()`。若任务尚未首次运行，`finally` 不执行，`_games`、`_user_index` 和跨玩法 registry 残留幽灵房间。该场景已用最小状态复现。
  - **P2 RPG SAY 合并丢消息**：`src/plugins/yawn_core/yawn_rpg/state.py:500-512` 在 SAY 配额满时可能修改已从队列取出、正在处理的动作；而 `src/plugins/yawn_core/yawn_rpg/engine.py:2203-2228` 已复制该动作文本并进入 await。新发言返回接受但不会进入当前 KP 批次，最终静默丢失。
  - **P1 狼人杀发牌期间仍可修改报名状态**：`src/plugins/yawn_core/yawn_werewolf/engine.py:1690-1753` 已按名单/板子生成玩家并逐人发身份，但阶段仍为 `SIGNUP`；此时 `/退报名`、`/报名`、`/板子`、`/开始游戏` 仍被 `yawn_werewolf/commands.py:283-300,319-422,536-569` 接受，造成 roster、board、host、用户索引与已发身份不一致。
  - **P1 狼人杀 AI 迟到行动污染后续阶段**：`src/plugins/yawn_core/yawn_werewolf/ai_player.py:514-528,652-665,765-770` 的后台投票任务不携带发起阶段/轮次令牌；任务跨越阶段完成后仍可入队。旧的 `VOTE` 可能在下一轮投票被计入，旧发言也可能在下一阶段播出（发言发送路径 `:790-831`）。
  - **P1 狼人杀非本局用户可灌满行动队列**：`src/plugins/yawn_core/yawn_werewolf/commands.py:75-94` 统一入口和 `state.py:381-398` 只检查容量、重复和单用户配额，不检查 actor 是否属于本局。多个群外用户可提交无效投票等动作占满队列，真实玩家会收到拥塞提示；该行为已通过最小 `Game` 状态复现。
  - **P1 狼人杀狼警长自爆后警徽残留**：`src/plugins/yawn_core/yawn_werewolf/engine.py:1528-1534` 的 `_handle_detonation()` 只调用 `_kill()` 并播报，没有移交或撕除警徽。死亡狼警长仍被 `game.sheriff()` 返回，后续警徽状态永久错误。
  - **P1 狼人杀遗言自爆跳过死亡技能结算**：首夜 `_run_day()` 在 `src/plugins/yawn_core/yawn_werewolf/engine.py:1472-1499` 先处理遗言，之后才处理猎人开枪和警徽；`SELF_DETONATE_PHASES` 包含遗言阶段，自爆抛出 `_DetonatedError` 后直接进入下一夜，首夜猎人死者可能永远无法开枪，警长也不会移交警徽。
  - **P1 狼人杀放逐猎人开枪不递归**：`src/plugins/yawn_core/yawn_werewolf/engine.py:1420-1431` 只处理被放逐猎人一次；若其开枪带走另一名猎人，目标不会再次触发开枪。夜死路径有递归 `pending` 处理，放逐路径行为不一致。

  验证记录：`uv run pytest -q` 为 84 passed/3 failed；失败来自提醒重构后已删除或改签的旧内部 API。`uv run pytest -q src/plugins/yawn_core/yawn_rpg/tests` 为 13 passed。`uv run pyright src tools/rpg_module_editor` 报 1 个 reminder 可空值错误；`uv run ruff check src tests tools/rpg_module_editor` 报 8 个错误，其中 6 个来自当前未提交的提醒改动。上述审查期间未修改业务代码。

- 2026-08-13：完成定时提醒交互向导：新建、列表、详情、编辑、复制、启用/停用、立即发送和二次确认删除均使用单一会话状态机；移除旧版单行添加与英文动作入口。时间支持每天、工作日、每周、每月及一次性事件，消息段和倒计时占位符继续按可持久化规则校验。修复循环提醒首轮误删调度、一次性提醒立即发送提前终止、运行时用户/群功能开关绕过、启动恢复过期事件悬挂和操作后详情状态过期问题。新增迁移 `f4b6e8d2a901` 扩展 `schedule_type`、`run_at` 并允许循环 Cron 为空；数据库迁移仍需维护者手动执行 `uv run nb orm upgrade heads`。目标提醒测试 7 项、全量 pytest 88 项、Ruff、Pyright 与 `git diff --check` 通过。
- 2026-08-13：完成审查报告全部 bug 修复：Core 补齐 presence 并发重试、管理面板外键父行、好友审批空超管、AI 对话用户级串行与已送达历史；提醒在一次性触发被功能开关拦截时会明确停用；RPG 重验开局权限、封闭战斗行动权与自然语言工具绕过、修正倒地轮转、动作冻结和旧 worker 身份清理；狼人杀锁定发牌阶段、拒绝非成员灌队列、丢弃迟到 AI 行动/发言并统一死亡技能、警徽与任务生命周期。已执行 `uv run nb orm upgrade heads`，数据库达到 `f4b6e8d2a901`、`c9d1a7163db4`、`33d3f5e9af32` 三个 head，并确认提醒表新列及可空性正确。全量 pytest 105 项、Ruff、Pyright、compileall 与 `git diff --check` 均通过。
- 2026-08-13：完成 RPG 玩家流程与体验优化：新增 `/局面`（报名/建卡/游戏三阶段公开摘要与请求者私聊摘要）、`/线索` 公共/个人可见性分流和完整调查手记；建卡显示技能上限、阶段期限、确认进度与超时待确认提醒；开局人数/模组上限预检不再因人数不足直接流局。
- 2026-08-13：RPG 行动改为阶段/场景/逻辑轮次快照，移除真实时间行动 TTL；探索倒计时只计算真正无人输入的空闲时间，队列优先，切景创建新轮；无效目标/格式/协助不消耗主要行动，实际检定和 NPC 拒绝算有效行动；战斗开场播报行动顺序，结局追加公开系统回顾。
- 2026-08-13：自然语言 SAY 采用固定窗口原始合批、并行分类和首个 NPC 路由边界；新增一次性临时 AI 等待提示，不进入剧情上下文；补充 RPG 玩家流程、信息隔离、行动节奏、批处理和结局回顾回归测试。未修改 YAML 模组、ORM 模型或迁移。
- 2026-08-13：优化狼人杀游戏流程与玩家体验：拆分警长终辩和重投阶段，修复长老夜间阶段门、切换板子人数门槛、重复投票静默丢弃及狼队明确空刀等待；AI 决策按阶段总预算并取消迟到任务；新增公开 `/狼人状态`、私聊 `/身份` 重发及 `/认主`、`/禁言` 入口，移除与跑团冲突的 `/跳过` 别名。新增狼人杀流程与命令回归测试，目标 Ruff、Pyright、compileall、全量 pytest 和正式插件加载均通过。
- 2026-08-13：完成项目结构与开发方向分析：仓库由 `yawn_core` 基础能力、`yawn_rpg` AI CoC 引擎与 YAML/Textual 模组工具链、`yawn_werewolf` 状态机与 AI 驱动组成；当前优先级应是先收敛 RPG 玩家体验/成长工作区改动并拆分提交，再补 CI、README/部署示例、启动自检与运行指标，随后围绕模组内容生产和狼人杀回放/统计扩展。当前两种玩法仍是单进程内存房间，只有局末 ORM 汇总；多实例、快照恢复和分布式队列应在明确生产目标后再投入。分析基线为 123 项 pytest 全部通过，Ruff、Pyright、compileall 与 `git diff --check` 通过；分析期间保留工作区未提交的 RPG 改动，未将其混入本次文档提交。
- 2026-08-13：落地 P0 发布维护基线与启动可靠性：补齐 README、`.env.example`、单进程部署/迁移/备份说明、P0-P2 可执行路线和 GitHub Actions CI，并为陈旧 `.qoder/repowiki` 增加现行入口说明。共享 LLM 改为首次调用时惰性创建客户端，`AI_API_KEY` 缺失或空白不得阻断 Bot/插件加载，普通与工具补全应返回 `None` 走现有降级，私聊 AI 明确提示未配置；调用方不得自行新建客户端绕过 `get_client()`。可选游戏子插件继续逐个故障隔离，同时必须记录 `loaded/missing/failed` 启动报告，新增子插件时同步维护报告规格和测试。本轮全量 pytest 129 项、Ruff、Pyright、compileall、无密钥正式插件发现、CI YAML、迁移 heads 与 `git diff --check` 均通过；下一步优先实现固定种子模组试玩器与场景/结局可达性检查。
- 2026-08-13：完成 P1-1 固定种子模组试玩器。新增 `tools.rpg_playtest` CLI：`uv run python -m tools.rpg_playtest MODULE.yaml --seed SEED --ending ENDING_ID [--players N] [--max-depth N] [--max-states N] [--json]`；直接以 YAML + `ModuleDef.model_validate` 加载，不调用全局模组注册、NoneBot、ORM 或 LLM。模拟器使用独立 `random.Random(seed)`，按 CoC 规则生成标准队伍，BFS 状态键包含完整剧情状态、战斗/HP/SAN、NPC 关系和 RNG 快照；固定动作顺序覆盖移动、自动出口、个人/团队/SAN 检定、社交、等待、战斗、闪避、伤害、反击和结局扫描。默认上限为深度 40、生成状态 50,000；成功只在命中指定结局时返回 0，未知结局、非法人数、不可达、深度/状态超限均返回非零并带原因与探索统计。JSON 字段保持稳定，包含模组、seed、目标/最终结局、初始/最终玩家、步骤骰点、场景、线索、flag、事件、时间和搜索统计。工具内部的相关性剪枝、移动回环抑制和时间边界等待仅用于控制离线搜索规模，不改变在线 RPG 引擎语义。
- 2026-08-13：P1-1 验证通过：试玩器专测 10 项；旧宅 `seed=0` 命中 `truth_revealed`，潮声 `seed=20260813`、2 人命中 `lanterns_tonight`，团队检定固定骰序与等待边界均有回归；两份现有 YAML schema 校验均为 0 错误。全量 pytest 使用隔离临时目录为 139 passed；目标 Ruff/Pyright、Ruff 全仓、compileall、`git diff --check` 通过。全量 Pyright 仍受当前依赖安装的既有类型桩/权限问题影响时，以目标目录检查结果为准；本轮未实现 P1-2 静态可达性诊断。
- 2026-08-13：当前 Codex 沙箱将 `.git` 的 index、objects、refs 设为只读，无法在本环境内更新独立提交；实现与验证已完成，需在可写工作树执行本目标的 `git add`/`git commit`。
- 2026-08-13：将 P1-1 固定种子试玩器整合进模组编辑器 TUI；新增“试玩”页、F6/工具栏入口、草稿/已保存文件数据源切换、seed/结局/人数/搜索上限设置、后台线程运行、轨迹与 JSON 报告及 Textual 剪贴板复制。TUI 复用 `tools.rpg_playtest` 的 schema 校验、确定性搜索和稳定渲染，不修改在线引擎语义；新增试玩页挂载、草稿与磁盘数据源、失败诊断、复制 JSON 和 80/100/140 列响应式回归测试。
- 2026-08-13：P1-1 TUI 集成验证通过：编辑器测试 74 项、试玩器测试 10 项、全量 pytest 146 项（1 个既有 NoneBot adapter warning）；`uv run ruff check src tests tools`、`uv run pyright src tools`、`compileall` 和 `git diff --check` 均通过。CLI JSON 默认格式保持兼容，TUI JSON 使用同一字段并仅增加可读缩进。
- 2026-08-13：优化 P1-1 试玩轨迹的人读渲染：步骤改为动作标题、场景/时间、骰点与状态变化分层展示，动作和检定结果增加中文标签，结尾汇总最终位置、线索、flag、事件及搜索统计；JSON 字段与搜索语义不变。新增渲染回归 1 项，并通过实际 TUI 试玩/复制 JSON 检查。
- 2026-08-13：修复 P1-1 TUI“运行试玩”按钮被设置行默认 `1fr` 高度推到视口外的问题；设置行和字段容器改为内容高度，并新增 179×47 宽屏可视性回归，确保运行/复制按钮及轨迹结果区可见。试玩页专测 8 项通过。
- 2026-08-13：快速核对路线图当前进度：P0 发布维护基线已完成；P1-1 固定种子模组试玩器及编辑器 TUI、轨迹可读性和控件可见性已完成（`6838701`，当前 `main` 比 `origin/main` 超前 2 个提交）。本次复核试玩器 11 项、编辑器试玩页 8 项、目标 Ruff 与 `git diff --check` 均通过；P1-2 场景/结局静态可达性诊断仍待实现，暂不进入回放统计或多实例部署。
- 2026-08-14：修复番茄任务创建在异步 ORM 提交后读取 job.id 导致 MissingGreenlet 的问题；提交前缓存 job_id 与章节数，补充提交后属性失效回归测试；番茄定向 pytest 9 项、Ruff、Pyright 均通过。
- 2026-08-14：修复番茄 worker 在异步 ORM 提交后继续读取书籍/章节对象导致的 MissingGreenlet；新增不可变章节快照并缓存任务书籍字段、章节状态日志字段，确保下载、写入和合并只使用普通值或新会话数据；番茄定向 pytest 11 项、Ruff、Pyright、compileall 和 diff check 均通过。
- 2026-08-14：降低番茄章节默认请求间隔至 0.5 秒，保留 0.2 秒下限与 `FANQIE_REQUEST_DELAY` 配置覆盖；同步更新部署说明和环境示例，番茄定向 pytest 12 项、Ruff、Pyright、compileall 和 diff check 均通过。
