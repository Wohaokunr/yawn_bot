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
| `/groups`、`/groups/:id`、`/users` | `access-pages.tsx`、`group-workspace.tsx` | `groups.py`、`users.py` |
| `/agent/:groupId` | `agent.tsx` + `agent-workspace/`（cockpit、状态栏、配置导入导出）+ `agent-panels/`（配置、人设、记忆、关系、隐私、消息、审计）+ `agent-debug/`（执行轨迹、Prompt/上下文/工具检查、模拟工作台）；`agent-meta.ts`、`relation-meta.ts`、`relation-graph.tsx`（d3-force）、`relations/`（关系面板 2.0：列表/成员/矩阵/洞察视图、编辑与详情抽屉、`relation-analysis.ts` 纯图算法；状态在 URL `relations.*`） | `agent*.py`、`audits.py` |
| `/games` | `games.tsx` | `games.py` |
| `/modules` | `modules.tsx` | `rpg_modules.py` |
| `/fanqie` | `fanqie.tsx` | `fanqie.py` |
| `/environment` | `environment.tsx`、`environment-components.tsx`、`environment-config.ts` | `environment*.py` |
| `/guest-access` | `guest-access.tsx` | `guest_access_routes.py` |
| `/audits` | `audits.tsx` | `audits.py` |
| `/guest`、`/guest/:groupId`（访客） | `guest-home.tsx` | 访客只读接口（`deps.py` 过滤） |
| 登录 | `login.tsx` | `auth_routes.py` |

测试与源码同目录（`*.test.ts(x)`）。

## 关联位置（跨目录）

- 业务语义来自 Python 侧：Agent 见 [yawn_agent/CLAUDE.md](../src/plugins/yawn_core/yawn_agent/CLAUDE.md)（调试页的 dry-run 语义受 [AGENTS.md](../src/plugins/yawn_core/yawn_agent/AGENTS.md) 约束：未执行的副作用必须显示为 `skipped`）；对局见 [yawn_werewolf](../src/plugins/yawn_core/yawn_werewolf/CLAUDE.md)、[yawn_rpg](../src/plugins/yawn_core/yawn_rpg/CLAUDE.md)；番茄见 [yawn_fanqie](../src/plugins/yawn_core/yawn_fanqie/CLAUDE.md)。
- 配置页条目来自 `src/plugins/yawn_core/webui/environment_catalog.dotenv`，说明在 [docs/configuration/webui.md](../docs/configuration/webui.md)。
- Docker 构建会在镜像里执行前端构建，见 [deploy/CLAUDE.md](../deploy/CLAUDE.md)。
