# YawnBot

[![CI](https://github.com/Wohaokunr/yawn_bot/actions/workflows/ci.yml/badge.svg)](https://github.com/Wohaokunr/yawn_bot/actions/workflows/ci.yml)
[![License](https://img.shields.io/badge/license-Apache--2.0-blue.svg)](LICENSE)

YawnBot 是一个基于 **NoneBot2 + OneBot V11** 的 QQ 机器人项目，包含群聊 Agent、RPG 跑团、狼人杀、番茄小说下载、签到/提醒、权限与管理 WebUI 等能力。项目使用 SQLite 持久化业务数据，并保持与具体 QQ 协议实现解耦：NapCat、Lagrange 等只需要按 OneBot V11 接入即可。

> **项目状态：0.x / 快速开发中。** 在 1.0 之前，配置、数据库 migration 和部分插件内部 API 仍可能变化。生产升级前请备份 `data/` 并阅读 Release Notes。

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

OneBot 端的 access token 必须与 YawnBot 使用的 `ONEBOT_V11_ACCESS_TOKEN` 一致。
手工/开发部署仍可直接写在 `.env`；生产 bootstrap 会把它迁移或自动生成到
`/opt/yawnbot/onebot.env`，YawnBot 与 NapCat 共用这一份专用凭据文件。

### 首次启动 NapCat Docker

NapCat 使用独立的 `deploy/napcat/compose.yaml`，不会进入 YawnBot Release 的
构建、拉取和重启生命周期。生产 bootstrap 会自动安装/接管 NapCat；手工部署时
先创建两个 Compose 共用的内部网络：

```bash
docker network inspect yawnbot-internal >/dev/null 2>&1 || docker network create yawnbot-internal
cd deploy/napcat
cp .env.example .env
python render-yawnbot-config.py --env-file ../../.env --output yawnbot-onebot.json
docker compose up -d
```

生产环境默认固定 `mlikiowa/napcat-docker:v4.18.19` 并使用
`pull_policy: missing`。因此普通 YawnBot 发布不会重新下载 NapCat 镜像，也不会
重新下载镜像内的 NTQQ；只有显式修改 `NAPCAT_IMAGE` 才升级 NapCat/NTQQ。

独立 Compose 会在 `deploy/napcat/data/` 下持久化：

```text
data/QQ       -> /app/.config/QQ
data/config   -> /app/napcat/config
data/plugins  -> /app/napcat/plugins
```

WebUI 只绑定宿主机回环地址。先建立 SSH 隧道，再打开
`http://127.0.0.1:6099/webui` 扫码登录：

```bash
ssh -L 6099:127.0.0.1:6099 <管理账号>@服务器地址
```

生产环境的 `deploy` 用户只授权给 GitHub Actions forced command，不用于人工
Shell/端口转发；请使用单独的管理账号建立隧道。

生产 bootstrap 会使用 NapCat Docker 官方 `MODE` 模板机制自动生成 Reverse
WebSocket 配置；无需再在 WebUI 手工填写。目标固定为：

```text
ws://yawnbot:8080/onebot/v11/ws
```

Access Token 自动来自 `/opt/yawnbot/onebot.env`。NapCat 与 YawnBot 通过
`yawnbot-internal` 网络和服务名通信，无需互相配置公网地址。若已有 NapCat
Compose，bootstrap 会优先复用其 QQ/config/plugins bind mount，避免丢失登录态。

### 为什么常规发布不会重新下载重依赖

- GitHub Release 的 BuildKit cache 持久化在 GHCR `:buildcache`；clean runner 会复用
  未变化的 npm、Python 和镜像构建层。
- Chromium 位于独立 `browser-runtime` 层，只由 `deploy/docker/playwright-version.txt`
  控制；普通源码或无关 Python 依赖变化不会重装 Chromium。
- 生产服务器按不可变 digest 拉取 YawnBot；Docker 自动复用已有 layer，并且同一
  digest 已存在时部署脚本直接跳过 `docker pull`。
- NapCat/NTQQ 是独立、版本固定的 Compose，不参与 YawnBot Release。

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

**启动后 QQ 没上线？** `/healthz` 只证明 YawnBot HTTP 进程正常。继续检查 OneBot 实现是否在线、反向 WS 地址和 token 是否一致。

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
- [开源前安全与发布验收](docs/open-source-readiness.md)
- [运行指标](docs/metrics.md)
- [RPG 新手与联合推理](docs/rpg-gameplay-guide.md)
- [番茄来源与使用边界](docs/fanqie-notice.md)

## Release 与可部署产物

正式发布由 `.github/workflows/release.yml` 完成，不从开发机上传 `dist` 或本地构建目录。

- 推送 `vX.Y.Z` Tag，或在 GitHub Actions 手动触发 `Release` 并填写版本号；
- Release 会先重新执行完整 CI，包括 fresh-checkout clean-install smoke 和 Docker clean-deploy smoke；
- 通过后从新的 GitHub Actions checkout 重新构建 WebUI 和 Docker 镜像；
- 镜像发布到 `ghcr.io/wohaokunr/yawn_bot:<version>`，同时保留 `sha-<commit>` 标签；
- GitHub Release 附带 `yawnbot-<version>-deploy.tar.gz`、镜像 digest 文件和 `SHA256SUMS.txt`；
- `deploy-production` 通过受保护的 `production` Environment，把不可变的
  `image@sha256:digest` 交给维护者服务器；业务运行时密钥始终只保存在服务器。

公开仓库的普通用户和 fork **不需要、也不应获得**维护者的 `production` Environment、SSH key 或业务运行时 secret。

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
deploy/                                容器与维护者部署脚本
docs/configuration/                    分主题配置参考
docs/deployment.md                     生产升级、备份、迁移、回滚与安全
tools/                                 开发/维护工具
tests/                                 回归测试
```

`webui/dist/`、数据库、浏览器 profile、媒体缓存、虚拟环境和工具私有状态都属于可再生或运行时数据，不应提交到 Git。

## 贡献

欢迎 Bug 报告、部署文档改进、测试和功能 PR。开始前请阅读 [CONTRIBUTING.md](CONTRIBUTING.md)。仓库提供 Bug、Feature 和 Deployment Help 三类 Issue Forms；较大的架构改动建议先建立 Issue 讨论边界和兼容性。

任何贡献都不应包含真实 `.env`、Token、QQ/NapCat 登录态、数据库或用户隐私数据。

## 安全

不要用公开 Issue 披露可利用的安全漏洞。WebUI 鉴权、权限绕过、OneBot 未授权控制、RCE/SSRF、secret 泄露或访客跨群/隐私泄露等问题请按 [SECURITY.md](SECURITY.md) 私下报告。

维护者在把仓库切换为 Public 前还会执行 `tools/history_secret_audit.py` 和 [开源前安全与发布验收](docs/open-source-readiness.md)，因为仅保证当前 HEAD 干净不足以证明旧 Git 历史可以公开。

## License

YawnBot 自有代码与文档按 [Apache License 2.0](LICENSE) 发布。第三方依赖、外部服务和第三方素材仍受各自许可证、服务条款和版权规则约束；Apache-2.0 不会重新许可这些第三方内容。
