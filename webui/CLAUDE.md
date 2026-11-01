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
| `src/App.tsx` | 路由表：admin 与 guest 两套路由，按会话角色切换 |
| `src/nav-config.tsx` | 侧栏与命令面板共用的导航表（新增页面要登记这里） |
| `src/app-shell.tsx`、`src/layout.tsx`、`src/command-palette.tsx` | 外壳、页面框架、⌘K 命令面板 |
| `src/api.ts`、`src/auth-session.ts`、`src/types.ts` | 统一 `fetch('/webui/api/v1…')`（CSRF、错误信封）、`/stream` WebSocket、会话、共享类型 |
| `src/shared.tsx` | 通用组件与格式化 helper |
| 样式 | `design-system.css`（设计 token）、`layout.css`、`interaction.css`、`theme-anime.css`、`styles.css`、`glass.ts` |

**页面**（路由 → 文件 → 后端模块）

| 路由 | 文件 | 后端 |
|---|---|---|
| `/overview` | `overview.tsx` | `overview_routes.py` |
| `/groups`、`/groups/:id`、`/users` | `directory/`（群组与用户 2.0；`access-pages.tsx` 只是转出口）：`GroupsPage.tsx` 状态条 + 即时搜索/筛选/排序 + 多选批量；`GroupDetailPage.tsx` 概览/群功能/成员/覆盖一览四标签；`UsersPage.tsx`；`UserProfileDrawer.tsx` 两页共用的用户档案（本群/所在群/私聊功能 + `user-insights.tsx` 的好感与签到、私聊 Agent）；`components.tsx` 里 `FeatureTable`（继承/开/关分段，写明继承后的值）、`BatchBar`、状态条；`directory-meta.ts` 纯逻辑；状态在 URL `groups.*`、`members.*`、`users.*`、`group.tab`、`user`、`user.tab`。窄屏覆盖了 theme-anime.css 的表格 640px 最小宽度（列已按断点隐藏） | `groups.py`、`users.py`、`directory.py` |
| `/private-agent` | `private-agent/`：`PrivateAgentPage.tsx`（提示词 / 人设 / 最近请求三个标签，状态在 URL `pa.tab`、`pa.user`、`pa.section`、`pa.group`、`pa.filter`、`pa.q`、`pa.pipe`）；`PromptsTab.tsx` 私聊提示词注册表（全局/某位用户、五条链路图、体检、分组、编辑抽屉与按草稿预览、单独改过的用户与提升为全局）；`PersonaTab.tsx` 人设策略、用户列表与批量、用户人设抽屉（`PersonaCardForm` 表单、模板/预设/复制群人设、锁定、撤销、导入导出、存进人设库、历史、编译预览）；`CapturesTab.tsx` 按用户的请求快照（复用 `PromptSnapshotView`）；`private-meta.ts` 纯逻辑；用户抽屉 `user-insights.tsx` 的私聊卡片链接到这里；群聊 `PromptStudio` 不再列私聊分段，只给跳转提示 | `private_agent_routes.py`、`private_agent_models.py` |
| `/agent/:groupId` | `agent.tsx` + `agent-workspace/`（cockpit、状态栏、配置导入导出）+ `agent-panels/`（配置、人设、记忆、关系、隐私、消息、审计）+ `agent-debug/`（执行轨迹、Prompt/上下文/工具检查、模拟工作台）+ `agent-prompts/`（提示词工作台：每段默认/样例/覆盖、编辑抽屉、一键删除安全上下文、导入导出；`PromptSnapshotView` 完整请求视图供 Trace 和调试结果复用；状态在 URL `prompts.*`）；`agent-meta.ts`、`relation-meta.ts`、`relation-graph.tsx`（d3-force）、`relations/`（关系面板 2.0：列表/成员/矩阵/洞察/Bot 视角视图、编辑与详情抽屉、`relation-analysis.ts` 纯图算法；状态在 URL `relations.*`） | `agent*.py`、`audits.py` |
| `/games` | `games.tsx`（对局中心外壳 + 跑团）；狼人杀在 `werewolf/`：`WerewolfWorkspace.tsx` 状态条 + 实时对局/战绩/数据洞察/规则与配置四视图，`GameRoom.tsx` 观战室（圆桌、座位详情、时间线/投票/玩家/AI 标签，事件按 `after_seq` 增量拉取），`ww-meta.ts` 纯逻辑（阶段/角色/死因字典、筛选、计票、局势、告警），状态在 URL `ww.*`，上帝视角开关存 `localStorage` 的 `yawnbot.ww.reveal`；未开上帝视角时夜间子阶段折叠为“夜晚”、AI 思考不显示 | `games.py`、`werewolf_admin.py` |
| `/modules` | `modules.tsx` | `rpg_modules.py` |
| `/fanqie` | `fanqie.tsx` | `fanqie.py` |
| `/environment` | `environment.tsx`（页面：分类导航 + 单分类字段列表 / 跨分类搜索结果，状态在 URL `env.*`）、`environment-editors.tsx`（按类型的字段编辑器、Token 编辑器）、`environment-components.tsx`（提供商/档位卡片、任务路由表、体检、待重启横幅、保存栏与预览）、`environment-fields.ts`（纯逻辑：分类、筛选、校验——镜像后端 `environment_schema.py`）、`environment-config.ts`（模型档位/任务路由表）、`environment.css` | `environment*.py` |
| `/sandbox` | `sandbox/`（`SandboxPage.tsx` 状态条 + 概览/运行记录/工作区文件/控制台/策略五个标签；`sandbox-meta.ts` 纯逻辑与校验，`sandbox-api.ts` 接口；状态在 URL `sandbox.*`，控制台草稿存 `localStorage`） | `sandbox.py` |
| `/web-search` | `web-search/`（`WebSearchPage.tsx` 状态条 + 概览/最近调用/试搜/策略与限额四个标签，复用沙箱页的 `sbx-*` 样式；`web-search-meta.ts` 纯逻辑与校验，`web-search-api.ts` 接口；标签在 URL `web.tab`） | `web_search.py` |
| `/stickers` | `stickers/`（`StickersPage.tsx` 状态条 + 表情包库/发送与收藏策略/提示词预览三个标签；`stickers-meta.ts` 纯逻辑与校验，`stickers-api.ts` 接口，上传走原始字节；标签在 URL `stk.tab`） | `stickers.py` |
| `/schedules` | `schedules/`（`SchedulesPage.tsx` 状态条 + 任务/运行记录/策略三个标签；`TasksTab.tsx` 列表、批量与详情抽屉，`TaskEditor.tsx` 新建/编辑抽屉与后端实时预览，`schedule-meta.ts` 纯逻辑：规则表单 ⇄ 规则对象、预校验、策略范围；规则对象保持 snake_case 与 Agent 工具一致；状态在 URL `schedules.*`） | `schedules.py` |
| `/guest-access` | `guest-access.tsx` | `guest_access_routes.py` |
| `/audits` | `audits.tsx` | `audits.py` |
| `/guest`、`/guest/:groupId`（访客） | `guest-home.tsx` | 访客只读接口（`deps.py` 过滤） |
| 登录 | `login.tsx` | `auth_routes.py` |

测试与源码同目录（`*.test.ts(x)`）。

## 关联位置（跨目录）

- 业务语义来自 Python 侧：Agent 见 [yawn_agent/CLAUDE.md](../src/plugins/yawn_core/yawn_agent/CLAUDE.md)（调试页的 dry-run 语义受 [AGENTS.md](../src/plugins/yawn_core/yawn_agent/AGENTS.md) 约束：未执行的副作用必须显示为 `skipped`）；对局见 [yawn_werewolf](../src/plugins/yawn_core/yawn_werewolf/CLAUDE.md)、[yawn_rpg](../src/plugins/yawn_core/yawn_rpg/CLAUDE.md)；番茄见 [yawn_fanqie](../src/plugins/yawn_core/yawn_fanqie/CLAUDE.md)。
- 配置页条目来自 `src/plugins/yawn_core/webui/environment_catalog.dotenv`，说明在 [docs/configuration/webui.md](../docs/configuration/webui.md)。
- Docker 构建会在镜像里执行前端构建，见 [deploy/CLAUDE.md](../deploy/CLAUDE.md)。
