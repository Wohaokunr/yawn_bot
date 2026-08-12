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

## 任务完成记录

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
- 2026-08-13：代码审查发现待修复问题：提醒模型新增 `schedule_type`/`run_at` 但规范迁移和当前 SQLite 表仍缺列；提醒调度完成路径会无条件移除 Cron 任务，循环提醒首发后失效；过期一次性提醒重启时保持启用但永不调度；立即发送一次性提醒会提前停用未来任务。`presence.track_user` 的 SQLite 锁重试 rollback 后未重建变更，并发首次写入只捕获 `OperationalError` 而未处理唯一键冲突。非模式 `/对话 内容` 绕过用户 worker 造成会话并发写入，流式 LLM 中断时会把未完整发送的全文写入历史。RPG 存在入队后房主权限失效仍可开始/选模组、战斗中 MOVE/WAIT/CHECK/TALK/ASSIST 绕过战斗行动权、SAY 合并到已消费动作导致发言丢失。狼人杀发牌和发身份期间仍允许退报名/改板子，AI 迟到投票/发言可污染后续阶段，群外用户可灌满行动队列，狼警长自爆后警徽状态残留。验证：`uv run pytest -q` 为 84 passed/3 failed（提醒重构删除或改签旧内部 API），`uv run pyright src tools/rpg_module_editor` 有 1 个 reminder 可空值错误，`uv run ruff check src tests tools/rpg_module_editor` 有 8 个错误（其中 6 个来自未提交提醒改动）。
