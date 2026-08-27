# YawnBot

YawnBot 是一个基于 **NoneBot2 + OneBot V11** 的 QQ 机器人项目，包含群聊 Agent、RPG 跑团、狼人杀、番茄小说下载、签到/提醒、权限与管理 WebUI 等能力。项目使用 SQLite 持久化业务数据，并保持与具体 QQ 协议实现解耦：NapCat、Lagrange 等只需要按 OneBot V11 接入即可。

> 新用户推荐直接使用 Docker Compose。Docker 构建会自动完成 WebUI 前端构建、Python 锁定依赖安装和 Playwright Chromium 安装，不要求宿主机提前准备 Node/npm 或浏览器运行时。

## 主要能力

| 模块 | 能力 |
| --- | --- |
| Core | 权限、帮助、签到、好友审批、定时提醒、用户/群活跃记录、运行指标 |
| 群聊 Agent | 多轮群聊、主动发言、短会话、记忆/画像/关系、OneBot 复合消息、表情包与媒体能力 |
| RPG | YAML 模组、角色创建、探索/战斗、AI KP/NPC、联合推理、事件日志与回放 |
| 狼人杀 | 多板子、昼夜流程、AI 玩家、安全超时与托管 |
| 番茄小说 | 公开免费内容搜索、榜单、章节任务、TXT 生成与发送 |
| WebUI | 运行概览、环境/Agent 管理、调试、游戏管理、访客只读群视图 |

## 架构

```text
NapCat / Lagrange / 其他 OneBot V11 实现
                │
                │ WebSocket / HTTP
                ▼
        ┌──────────────────┐
        │     YawnBot      │
        │ NoneBot2/FastAPI │
        ├──────────────────┤
        │ Core / Agent     │
        │ RPG / Werewolf   │
        │ Fanqie / WebUI   │
        └────────┬─────────┘
                 │
                 ▼
          SQLite + data/
```

YawnBot 不把 QQ 客户端实现打进自身镜像，也不要求某个固定 OneBot 实现。

---

## Docker 快速启动（推荐）

### 1. 前置条件

只需要：

- Git
- Docker Engine / Docker Desktop
- Docker Compose v2
- 一个可用的 OneBot V11 实现

### 2. Clone 与最小配置

```bash
git clone https://github.com/Wohaokunr/yawn_bot.git
cd yawn_bot
cp .env.example .env
```

Windows PowerShell：

```powershell
git clone https://github.com/Wohaokunr/yawn_bot.git
cd yawn_bot
Copy-Item .env.example .env
```

打开 `.env`，至少修改：

```dotenv
SUPERUSERS=["你的QQ号"]
ONEBOT_V11_ACCESS_TOKEN=换成随机且独立的Token
```

首次部署不要求 AI Key；最小模板已默认关闭 RPG/狼人杀 AI。

### 3. 一条命令构建

```bash
docker compose build
```

构建阶段会自动：

- `npm ci && npm run build` 生成 WebUI；
- 使用 `uv.lock` 安装 Python 依赖；
- 安装 Playwright Chromium 与所需系统库；
- 只把运行期文件放进最终镜像。

### 4. 一条命令启动

```bash
docker compose up -d
```

检查状态：

```bash
docker compose ps
```

健康检查：

```bash
curl http://127.0.0.1:8080/healthz
```

应返回类似：

```json
{"status":"ok","service":"yawnbot","subplugins":{"loaded":4,"missing":0,"failed":0}}
```

Compose 使用 named volume 持久化 `/app/data`。容器启动时会先同步镜像内 canonical ORM migration，再自动执行 `nb orm upgrade heads`，因此新镜像不会被旧数据卷中的 migration 文件遮住。

停止：

```bash
docker compose down
```

不要在需要保留数据时使用 `docker compose down -v`。

---

## OneBot V11 怎么接

默认是 **反向 WebSocket**：让 NapCat、Lagrange 或其他 OneBot V11 实现主动连接 YawnBot。

原生部署：

```text
ws://127.0.0.1:8080/onebot/v11/ws
```

Docker 部署、OneBot 实现在宿主机时：

```text
ws://<Docker宿主机地址>:8080/onebot/v11/ws
```

如果 OneBot 实现和 YawnBot 在同一个 Docker network，可使用服务名：

```text
ws://yawnbot:8080/onebot/v11/ws
```

OneBot 端的 access token 必须与 `.env` 中 `ONEBOT_V11_ACCESS_TOKEN` 一致。

也支持正向 WebSocket / HTTP API，配置见 [Core、OneBot 与存储配置](docs/configuration/core.md)。

---

## WebUI

WebUI 默认关闭。Docker 镜像已经包含构建好的前端，只需要在 `.env` 增加：

```dotenv
WEBUI_ENABLED=true
WEBUI_ADMIN_TOKEN=至少32字符的高熵随机Token
```

本机访问：

```text
http://127.0.0.1:8080/webui
```

公网部署必须通过 HTTPS 反向代理，并设置：

```dotenv
WEBUI_COOKIE_SECURE=true
```

详细说明见 [WebUI 配置](docs/configuration/webui.md)。

---

## 原生 Windows / Linux 部署

Docker 是推荐路径；需要直接在宿主机运行时，至少安装：

- Python 3.10+
- [uv](https://docs.astral.sh/uv/)
- OneBot V11 实现
- 可选：Node/npm（只在启用 WebUI 时需要）
- 可选：Playwright Chromium（只在使用番茄搜索时需要）

安装锁定依赖：

```bash
uv sync --locked
```

复制并修改最小配置：

```bash
cp .env.example .env
```

Windows PowerShell：

```powershell
Copy-Item .env.example .env
```

首次部署/升级数据库：

```bash
uv run nb orm upgrade heads
```

启用 WebUI 时：

```bash
cd webui
npm ci
npm run build
cd ..
```

需要番茄关键词搜索时：

```bash
uv run playwright install chromium
```

启动：

```bash
uv run nb run
```

启动阶段会直接报告子插件加载状态。WebUI 已开启但前端缺失会快速失败；番茄搜索缺少 Chromium 会在启动日志给出安装命令。

---

## AI

AI 是可选能力。需要 Agent 高级对话、AI KP/NPC 或狼人杀 AI 时，再配置 OpenAI-compatible 服务：

```dotenv
AI_API_KEY=your-api-key
AI_BASE_URL=https://your-openai-compatible-endpoint/v1
AI_MODEL=your-model
RPG_AI_ENABLED=true
WW_AI_ENABLED=true
```

多 Provider、default/light/vision 模型档位、推理策略与 Agent 路由见 [AI 与 Agent 配置](docs/configuration/ai-agent.md)。

---

## 常见问题

**启动后 QQ 没上线？**  `/healthz` 只证明 YawnBot HTTP 进程正常。继续检查 OneBot 实现是否在线、反向 WS 地址和 token 是否一致。

**WebUI 404 / 启动报前端缺失？** Docker 路径会自动构建前端。原生部署需在 `webui/` 执行 `npm ci && npm run build`。

**番茄搜索提示浏览器不存在？** 原生部署执行 `uv run playwright install chromium`；Docker 镜像已自动包含 Chromium。

**没有 AI Key 能不能跑？** 可以。最小模板默认 `RPG_AI_ENABLED=false`、`WW_AI_ENABLED=false`，Core 与大部分非 AI 功能仍可启动。

**为什么不能开多个 YawnBot 实例共同处理同一个群？** 当前 RPG/狼人杀局内状态仍以单进程为主，多实例房间所有权、租约和 fencing token 尚未完成。生产环境保持单实例。

---

## 配置与运维文档

- [配置总览](docs/configuration/README.md)
- [Core、OneBot 与存储](docs/configuration/core.md)
- [AI 与 Agent](docs/configuration/ai-agent.md)
- [WebUI](docs/configuration/webui.md)
- [番茄、RPG 与狼人杀](docs/configuration/fanqie-games.md)
- [生产部署、升级、备份与回滚](docs/deployment.md)
- [Repository hygiene](docs/repository-hygiene.md)
- [运行指标](docs/metrics.md)
- [RPG 新手与联合推理](docs/rpg-gameplay-guide.md)
- [番茄来源与使用边界](docs/fanqie-notice.md)

## Release 与可部署产物

正式发布由 `.github/workflows/release.yml` 完成，不从开发机上传 `dist` 或本地构建目录。

- 推送 `vX.Y.Z` Tag，或在 GitHub Actions 手动触发 `Release` 并填写版本号；
- Release 会先重新执行完整 CI，包括 fresh-checkout clean-install smoke 和 Docker clean-deploy smoke；
- 通过后从新的 GitHub Actions checkout 重新构建 WebUI 和 Docker 镜像；
- 镜像发布到 `ghcr.io/wohaokunr/yawn_bot:<version>`，同时保留 `sha-<commit>` 标签；
- GitHub Release 附带 `yawnbot-<version>-deploy.tar.gz`、镜像 digest 文件和 `SHA256SUMS.txt`。

当前做到 **Continuous Delivery**：自动产出可部署镜像/包，但不会自动 SSH 到任何生产机。以后确定生产环境后，应追加受 GitHub Environment 审批与保护规则约束的 Deploy job。

## 开发

常用质量门槛：

```bash
python tools/repo_guard.py
uv run pytest -q
uv run ruff check src tests tools
uv run pyright src tools
uv run python -m compileall -q src tools
git diff --check

cd webui
npm ci
npm test -- --run
npm run typecheck
npm run build
```

插件发现冒烟检查：

```bash
uv run python -c "import nonebot; nonebot.init(); nonebot.load_from_toml('pyproject.toml'); required=('yawn_core','yawn_core:yawn_agent','yawn_core:yawn_rpg','yawn_core:yawn_werewolf','yawn_core:yawn_fanqie'); missing=[name for name in required if nonebot.get_plugin(name) is None]; assert not missing, missing"
```

## 代码结构

```text
src/plugins/yawn_core/                 Core 与业务子插件
data/nonebot_plugin_orm/migrations/    受版本控制的 canonical ORM migration
webui/                                 React / Vite 管理台源码
deploy/                                容器启动脚本
docs/configuration/                    分主题配置参考
docs/deployment.md                     生产升级、备份、迁移、回滚与安全
tools/                                 开发/维护工具
tests/                                 回归测试
```

`webui/dist/`、数据库、浏览器 profile、媒体缓存、虚拟环境和工具私有状态都属于可再生或运行时数据，不应提交到 Git。
