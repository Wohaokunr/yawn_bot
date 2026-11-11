# webui — 管理台前端

React 19 + Ant Design 6 + React Router 7 的单页应用，Vite 构建，`base: "/webui/"`。
构建产物 `webui/dist/`（已 gitignore）由后端 `src/plugins/yawn_core/webui/app.py` 托管；
后端 API 与认证见 [src/plugins/yawn_core/webui/CLAUDE.md](../src/plugins/yawn_core/webui/CLAUDE.md)。

## 命令

```bash
npm ci
npm run dev          # Vite dev server，/webui/api 代理到 127.0.0.1:8080（需本地起 bot）
npm test -- --run    # Vitest（jsdom，setup 在 src/testSetup.ts）
npm run typecheck    # tsc -b
npm run build        # tsc -b && vite build → dist/
```

CI（`.github/workflows/ci.yml`）对前端跑 test / typecheck / build；仓库根的 `tests/test_webui_spa.py` 依赖 `dist/`，本地没 build 会失败。

## 结构

| 路径 | 职责 |
|---|---|
| `index.html`、`src/main.tsx` | 入口；`dev-preview.html` + `src/dev-preview.tsx` 是无后端的本地预览 |
| `src/App.tsx` | 路由表：admin、guest、user（QQ 个人账号，只有 `/me/*`）三套路由，按会话角色切换 |
| `src/nav-config.tsx` | 侧栏与命令面板共用的导航表（新增页面要登记这里；`navForRole`/`homePathForRole` 按角色给导航） |
| `src/app-shell.tsx`、`src/layout.tsx`、`src/command-palette.tsx` | 外壳、页面框架、⌘K 命令面板 |
| `src/api.ts`、`src/auth-session.ts`、`src/types.ts` | 统一 `fetch('/webui/api/v1…')`（CSRF、错误信封）、`/stream` WebSocket、会话、共享类型 |
| `src/shared.tsx` | 通用组件与格式化 helper |
| 样式 | `design-system.css`（设计 token）、`layout.css`、`interaction.css`、`theme-anime.css`（浅色语义变量 `--bg/--surface/--ink/--brand-*`）、`theme-dark.css`（深色覆盖 + 状态色 `--success/--warning/--danger/--info/--purple` 及 `*-bg`）、`styles.css`、`glass.ts` |
| 视觉约定 | 全站系统 UI 字体（不再加载 Nunito / 快乐体）；唯一强调色樱粉只用于主按钮、选中态和链接，卡片标题不加装饰点，图标底、头像、标签不用粉色装饰；`--ease-spring`/`--ease-bounce` 沿用旧名但已改成无过冲的指数缓出，不要再加回弹曲线；`ConfigProvider` 关闭了按钮的两字中文自动空格（`button.autoInsertSpace=false`）；`colorBgLayout` 是透明的，所以 `Segmented` 的轨道色在 `theme.tsx` 里显式给出。页头一律用 `shared.tsx` 的 `PageHeader`（窄屏只有刷新时缩成图标按钮，与标题同一行；状态 chip 放 `status`），不要再做整块渐变横幅 |
| 外壳 | `app-shell.tsx`：侧栏 = 品牌 + 可滚动菜单 + 底部一行连接状态（`.sider-footer`）；`nav-config.tsx` 里 `group: ""` 的条目不加分组标题；QQ 个人账号在手机宽度下有底部标签栏（`USER_TABBAR_KEYS` 四项：主页/小本本/日历/签到 + “更多”打开完整导航）；QQ 个人账号的顶栏工具（提醒铃铛、随手记、快捷键）来自 `me/MeShellExtras.tsx` 的 `MeHeaderTools` |
| 主题 | `src/theme.tsx`：`ThemeProvider`（含 antd `ConfigProvider`，浅/深两套 token）、`useThemeMode()`、顶栏 `ThemeToggle`；偏好存 `localStorage` 的 `yawnbot.theme`（缺省跟随系统），生效值写 `<html data-theme>`，`index.html` 内联脚本在首帧前同步设置防闪白 |

**页面**（路由 → 文件 → 后端模块）

| 路由 | 文件 | 后端 |
|---|---|---|
| `/overview` | `overview.tsx` | `overview_routes.py` |
| `/groups`、`/groups/:id`、`/users` | `directory/`（群组与用户 2.0；`access-pages.tsx` 只是转出口）：`GroupsPage.tsx` 状态条 + 即时搜索/筛选/排序 + 多选批量；`GroupDetailPage.tsx` 概览/群功能/成员/覆盖一览四标签；`UsersPage.tsx`；`UserProfileDrawer.tsx` 两页共用的用户档案（本群/所在群/私聊功能 + `user-insights.tsx` 的好感与签到、私聊 Agent）；`components.tsx` 里 `FeatureTable`（继承/开/关分段，写明继承后的值）、`BatchBar`、状态条；`directory-meta.ts` 纯逻辑；状态在 URL `groups.*`、`members.*`、`users.*`、`group.tab`、`user`、`user.tab`。窄屏覆盖了 theme-anime.css 的表格 640px 最小宽度（列已按断点隐藏） | `groups.py`、`users.py`、`directory.py` |
| `/affinity` | `affinity/`：`AffinityAdminPage.tsx` 好感度（概览统计与立刻结算衰减 / 等级权益逐级编辑 / 礼物与商店 / 任务·衰减·问候，标签在 URL `aff.tab`，整份编辑整份保存、撤销、恢复默认）；`AffinityPlayPanel.tsx` 的 `MyAffinityPlay` 给 `/me/affinity` 的「等级权益」「任务·送礼·商店」两个标签；`affinity-api.ts` 接口、`affinity-meta.ts` 纯逻辑（策略校验/比较）、`types.ts` 与后端一一对应（策略保持 snake_case）；测试夹具 `fixtures/*.json` 由后端 `perks_payload`/`policy_meta` 导出，后端改形状时重新导出 | `affinity_routes.py` |
| `/private-agent` | `private-agent/`：`PrivateAgentPage.tsx`（提示词 / 人设 / 最近请求三个标签，状态在 URL `pa.tab`、`pa.user`、`pa.section`、`pa.group`、`pa.filter`、`pa.q`、`pa.pipe`）；`PromptsTab.tsx` 私聊提示词注册表（全局/某位用户、五条链路图、体检、分组、编辑抽屉与按草稿预览、单独改过的用户与提升为全局）；`PersonaTab.tsx` 人设策略、用户列表与批量、用户人设抽屉（`PersonaCardForm` 表单、模板/预设/复制群人设、锁定、撤销、导入导出、存进人设库、历史、编译预览）；`CapturesTab.tsx` 按用户的请求快照（复用 `PromptSnapshotView`）；`private-meta.ts` 纯逻辑；用户抽屉 `user-insights.tsx` 的私聊卡片链接到这里；群聊 `PromptStudio` 不再列私聊分段，只给跳转提示 | `private_agent_routes.py`、`private_agent_models.py` |
| `/agent/:groupId` | `agent.tsx` + `agent-workspace/`（cockpit、状态栏、配置导入导出）+ `agent-panels/`（配置、人设、记忆、关系、隐私、消息、审计）+ `agent-debug/`（执行轨迹、Prompt/上下文/工具检查、模拟工作台）+ `agent-prompts/`（提示词工作台：每段默认/样例/覆盖、编辑抽屉、一键删除安全上下文、导入导出；`PromptSnapshotView` 完整请求视图供 Trace 和调试结果复用；状态在 URL `prompts.*`）；`agent-meta.ts`、`relation-meta.ts`、`relation-graph.tsx`（d3-force）、`relations/`（关系面板 2.0：列表/成员/矩阵/洞察/Bot 视角视图、编辑与详情抽屉、`relation-analysis.ts` 纯图算法；状态在 URL `relations.*`） | `agent*.py`、`audits.py` |
| `/games` | `games.tsx`（对局中心外壳 + 跑团）；狼人杀在 `werewolf/`：`WerewolfWorkspace.tsx` 状态条 + 实时对局/战绩/数据洞察/规则与配置四视图，`GameRoom.tsx` 观战室（圆桌、座位详情、时间线/投票/玩家/AI 标签，事件按 `after_seq` 增量拉取），`ww-meta.ts` 纯逻辑（阶段/角色/死因字典、筛选、计票、局势、告警），状态在 URL `ww.*`，上帝视角开关存 `localStorage` 的 `yawnbot.ww.reveal`；未开上帝视角时夜间子阶段折叠为“夜晚”、AI 思考不显示 | `games.py`、`werewolf_admin.py` |
| `/modules` | `modules.tsx` | `rpg_modules.py` |
| `/fanqie` | `fanqie.tsx` | `fanqie.py` |
| `/environment` | `environment.tsx`（页面：分类导航 + 单分类字段列表 / 跨分类搜索结果，状态在 URL `env.*`）、`environment-editors.tsx`（按类型的字段编辑器、Token 编辑器）、`environment-components.tsx`（提供商/档位卡片、任务路由表、体检、待重启横幅、保存栏与预览）、`environment-fields.ts`（纯逻辑：分类、筛选、校验——镜像后端 `environment_schema.py`）、`environment-config.ts`（模型档位/任务路由表）、`environment.css` | `environment*.py` |
| `/sandbox` | `sandbox/`（`SandboxPage.tsx` 状态条 + 概览/运行记录/工作区文件/控制台/策略五个标签；`sandbox-meta.ts` 纯逻辑与校验，`sandbox-api.ts` 接口；状态在 URL `sandbox.*`，控制台草稿存 `localStorage`） | `sandbox.py` |
| `/web-search` | `web-search/`（`WebSearchPage.tsx` 状态条 + 概览/最近调用/试搜/策略与限额四个标签，复用沙箱页的 `sbx-*` 样式；`web-search-meta.ts` 纯逻辑与校验，`web-search-api.ts` 接口；标签在 URL `web.tab`） | `web_search.py` |
| `/stickers` | `stickers/`：`StickersPage.tsx` 只是外壳（状态条 + 八个标签：表情包库/AI 标注与偷图/策略/菜单预览/统计/体检/合集/回收站，标签在 URL `stk.tab` = `library`/`ai`/`policy`/`preview`/`insights`/`health`/`packs`/`trash`）；`StickerLibraryTab.tsx` 库（筛选全在 URL：`stk.f`/`q`/`tag`/`src`/`ai`/`pack`/`mood`/`flag`/`sort`，编辑框 `stk.edit`；密度存 `localStorage` 的 `yawnbot.stk.density`；全选/反选、批量加减标签/开放群/合集/置顶/权重、标签管理、导出导入；编辑框含别名、置顶、权重、合集、备注、单张每日上限、换图、上一张/下一张，导出 `errorText`）；`StickerUpload.tsx` 上传框（选图后自动 AI 预览，并发 2，可直接放进合集）；`StickerAiTab.tsx` 识图模型状态、批量重标任务（范围含文件名占位/没把握/重试失败的，带预计剩余时间）、试一张、偷来的表情包；`StickerPolicyTab.tsx` 策略（含发送节奏、菜单怎么挑、回收站、按群单独设置 `OverridesEditor`）；`StickerPreviewTab.tsx` 菜单预览（群在 `stk.group`，可选发言场景，列出菜单每项为什么在、名字对照测试）；`StickerInsightsTab.tsx` 统计；`StickerManageTabs.tsx` 的 `HealthTab`/`PacksTab`/`TrashTab`；`StickerTransfer.tsx` 导入弹窗与 `saveBlob`；`stickers-meta.ts` 纯逻辑与校验，`stickers-api.ts` 接口（上传、预览、换图、导入走原始字节），测试夹具 `sticker-fixtures.ts` | `stickers.py`、`sticker_admin_routes.py` |
| `/schedules` | `schedules/`（`SchedulesPage.tsx` 状态条 + 任务/私聊提醒/运行记录/策略四个标签；`PrivateTab.tsx` 所有人的私聊提醒（来源标签、筛选在 URL `schedules.p*`、详情抽屉 `schedules.private=QQ:编号`）；`TasksTab.tsx` 列表、批量与详情抽屉，`TaskEditor.tsx` 新建/编辑抽屉与后端实时预览，`schedule-meta.ts` 纯逻辑：规则表单 ⇄ 规则对象、预校验、策略范围；规则对象保持 snake_case 与 Agent 工具一致；状态在 URL `schedules.*`） | `schedules.py` |
| `/guest-access` | `guest-access.tsx` | `guest_access_routes.py` |
| `/audits` | `audits.tsx` | `audits.py` |
| `/guest`、`/guest/:groupId`（访客） | `guest-home.tsx` | 访客只读接口（`deps.py` 过滤） |
| `/me/*`（QQ 个人账号：主页、search、calendar、insights、achievements、assistant、schedules、tasks、chat、persona、memory、chats、groups、groups/:groupId、affinity、games、account） | `me/`：`MeHomePage.tsx` 主页与我管理的群；`MySchedulesPage.tsx` 我的定时任务（私聊提醒 + 群任务一处管，新建抽屉复用 `schedules/TaskEditor` 的 `RuleEditor`，接口 `me-schedule-api.ts`）；`MyGroupPage.tsx` 群管理（`GroupManagePanel.tsx`，子视图在 URL `manage.view`，写操作先 preview 再弹窗确认）/Agent 设置（复用 `AgentConfigPanel manager`）/群人设（复用 `PersonaPanel manager`：人设库只读、无试演）/群关系洞悉（复用 `RelationsPanel readOnly`，默认洞察视图）/群功能设置（复用 `FeatureTable`），标签在 URL `tab`，`groupManage.webui=false` 时只剩后两个；`MyPersonaPage.tsx` 私聊人设（复用 `PersonaCardForm`）；`MyAccountPage.tsx` 账号与数据（改密码/退出所有设备/我的数据统计与导出；`MyDevicesCards.tsx` 登录设备与最近登录、登录提醒）；「我的空间」2.0 新页（共用 `me-space-api.ts` 接口、`me-space-meta.ts` 纯逻辑、`space-ui.tsx` 的 `useResource`/`useAction`/`MeSection`/`StatTile`/`UrlTabs`、样式 `me-space.css` 末尾是深色覆盖）：`MyAssistantPage.tsx` 小本本 `/me/assistant`（待办/笔记/倒数日/心情，标签在 URL `tab`）、`MyChatSettingsPage.tsx` 私聊设置 `/me/chat`（暂停自动回复、回复偏好只提交改动字段、主动提议）、`MyMemoryPage.tsx` Bot 记得我 `/me/memory`（私聊记忆/群里的我/群私互通/别再记回来）、`MyChatsPage.tsx` 聊天记录 `/me/chats`（会话在 URL `chat`，搜索、改名、导出、删除）、`MyTasksPage.tsx` 后台小助手 `/me/tasks`、`MyGroupsPage.tsx` 我在的群 `/me/groups`（只有管理的群能点进 `/me/groups/:id`）、`MyAffinityPage.tsx` 好感与签到 `/me/affinity`、`MyGamesPage.tsx` 狼人杀战绩 `/me/games`；`MeHomePage.tsx` 是今日概览仪表盘（左“今天”2×2：待办/提醒/倒数日/签到，右“我和 Bot”：好感 + 心情；下面是我管理的群和“更多”入口，已在上面露出的入口不重复）。侧栏分组与面包屑都由 `nav-config.tsx` 的 `USER_NAV` 派生；`me-meta.ts` 纯逻辑（密码规则镜像后端 `webui_accounts.password_problem`）；`me-api.ts` 接口。「我的空间」3.0（接口 `me-hub-api.ts`、纯逻辑 `me-hub-meta.ts`、共用件 `hub-ui.tsx`、样式 `me-v3.css` 末尾是深色覆盖）：新页 `MySearchPage.tsx` 搜一搜 `/me/search`（关键词在 URL `q`）、`MyCalendarPage.tsx` 日历 `/me/calendar`（月份 `month`、选中日 `day`）、`MyAchievementsPage.tsx` 成就 `/me/achievements`、`MyInsightsPage.tsx` 数据洞察 `/me/insights`（范围 `days`）；`MeShellExtras.tsx` 是顶栏的提醒铃铛、随手记（别处用 `openCapture()` 打开）和快捷键表（`/` 搜索、`N` 随手记、`?` 帮助、`G`+字母跳页，表在 `me-hub-meta.ts` 的 `GO_KEYS`）；`hub-ui.tsx` 里 `useUiPrefs`（跟账号走的界面偏好：主页模块顺序与隐藏、星标入口、置顶会话、已读提醒、登录后先去哪一页）、`useInbox`、`useLocalPrefs`（只在本机的显示偏好，写到 `<html data-me-density/font/calm>`）和无依赖小图表 `Heatmap`/`Spark`/`Columns`；写完数据发 `ME_CHANGED_EVENT` 让铃铛和主页自己刷新。`MyChatsPage.tsx` 的 `at` 参数跳到某条消息，`MyAssistantPage.tsx` 的 `find` 参数定位并闪一下某条待办/笔记。本机偏好的 `localStorage` 键：`yawnbot.me.display`、`yawnbot.me.todos.view`、`yawnbot.me.groups.sort`、`yawnbot.me.search.recent`、`yawnbot.me.visited.memory` | `self_service_routes.py`、`me_schedule_routes.py`、`me_space_routes.py`、`me_insight_routes.py`、`me_hub_routes.py`、关系只读接口 |
| 登录 | `login.tsx`（运维/QQ 账号/访客三种；从私聊链接 `?login=qq`/`?login=guest` 进来时切换条只有 QQ 账号和访客，运维收成页脚链接；`?login=qq` 或上次用过的方式存在 `localStorage` 的 `yawnbot.login.mode`；QQ 账号含设置码设密码流程；「自动登录」勾选存 `yawnbot.login.remember`（默认勾），勾着时记住 QQ 号 `yawnbot.login.qq`；`notice` 显示掉线原因）。`App.tsx` 登录后每 10 分钟/切回标签页读一次 `/auth/session` 续期，QQ 账号刚登录时用 `LoginNotice` 提示上次登录或新地点 | `auth_routes.py` |

测试与源码同目录（`*.test.ts(x)`）。

**深色模式约定**：新页面颜色只用 CSS 变量或 antd token，不写死 `#fff`/浅色；需要深色专属写法时在自己的 css 里用 `:root[data-theme="dark"] .xxx {…}`；TSX 里按主题分支用 `useThemeMode().resolved`。导出的图片（如关系图谱 SVG/PNG）保持浅色底。

## 关联位置（跨目录）

- 业务语义来自 Python 侧：Agent 见 [yawn_agent/CLAUDE.md](../src/plugins/yawn_core/yawn_agent/CLAUDE.md)（调试页的 dry-run 语义受 [AGENTS.md](../src/plugins/yawn_core/yawn_agent/AGENTS.md) 约束：未执行的副作用必须显示为 `skipped`）；对局见 [yawn_werewolf](../src/plugins/yawn_core/yawn_werewolf/CLAUDE.md)、[yawn_rpg](../src/plugins/yawn_core/yawn_rpg/CLAUDE.md)；番茄见 [yawn_fanqie](../src/plugins/yawn_core/yawn_fanqie/CLAUDE.md)。
- 配置页条目来自 `src/plugins/yawn_core/webui/environment_catalog.dotenv`，说明在 [docs/configuration/webui.md](../docs/configuration/webui.md)。
- Docker 构建会在镜像里执行前端构建，见 [deploy/CLAUDE.md](../deploy/CLAUDE.md)。
