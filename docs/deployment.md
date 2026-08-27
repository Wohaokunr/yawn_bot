# 生产部署、升级与回滚

本文只处理生产运维：Docker/原生运行边界、SQLite 备份、ORM migration、升级、回滚、反向代理与安全。第一次运行请先看根目录 [README](../README.md)，配置项看 [`docs/configuration/`](configuration/README.md)。

## 1. 当前运行边界

YawnBot 当前仍按**单实例**设计：

- SQLite 保存平台数据、Agent 数据、提醒、任务和局末记录；
- RPG/狼人杀局内状态仍主要驻留单进程内存；
- 不支持多个 YawnBot 实例同时消费同一群的游戏动作；
- 不保证进程重启后恢复未结束的游戏；
- `data/` 必须持久化并纳入备份。

仅把 SQLite 换成远程数据库不能解决双引擎问题。多实例前必须先完成房间所有权、租约/fencing token、跨实例动作路由和幂等恢复。

## 2. Docker Compose 生产基线

仓库根目录的 `compose.yaml` 用于本地构建和 clean-deploy smoke。生产环境使用
`deploy/production/compose.yaml`，只接受 GHCR 的不可变 digest 引用：

```bash
ghcr.io/wohaokunr/yawn_bot@sha256:<release-digest>
```

服务器固定目录为：

```text
/opt/yawnbot/.env                  运行时配置与密钥，权限 600
/opt/yawnbot/compose.yaml          生产 Compose
/opt/yawnbot/bin/deploy-release    部署入口
/opt/yawnbot/data/                 持久数据
/opt/yawnbot/data/backups/         部署前 SQLite online backup
/opt/yawnbot/deployments/          每次成功部署的 manifest
```

生产 Compose：

- 只运行一个 `yawnbot` 服务；
- 把 `/opt/yawnbot/data` 挂载到 `/app/data`；
- 只把 `8080` 绑定到宿主机回环地址；
- 使用 `/healthz` 做 liveness；
- 容器以非 root 用户运行；
- 连接共享的 `yawnbot-internal` 网络；
- 禁止容器启动时自行迁移，migration 只由部署脚本显式执行。

可在 `.env` 增加：

```dotenv
YAWNBOT_AUTO_MIGRATE=false
```

`.env` 不由 GitHub Actions 下发。OneBot Token、AI API key、WebUI token 等密钥只在
服务器长期保存。

## 3. 为什么容器启动前要同步 migration

仓库的 canonical migration 位于：

```text
data/nonebot_plugin_orm/migrations/
```

但 Compose 又把整个 `/app/data` 挂成持久 volume。如果直接依赖镜像中的 `/app/data`，第一次创建 volume 后，后续新镜像里的 migration 会被旧 volume 内容遮住。

因此 Dockerfile 把版本化 migration 额外复制到：

```text
/opt/yawnbot/migrations
```

`deploy/docker-entrypoint.sh` 每次启动先同步到：

```text
/app/data/nonebot_plugin_orm/migrations
```

再执行 ORM upgrade。不要把这个同步步骤删掉，也不要把运行时数据库反向复制回仓库 migration 目录。

## 4. SQLite 备份

### 4.1 生产部署：SQLite online backup

`deploy-release` 在停止旧容器前通过 Python `sqlite3.Connection.backup()` 创建一致性
快照，并执行 `PRAGMA integrity_check`。文件名为：

```text
/opt/yawnbot/data/backups/pre-deploy-<version>-<UTC timestamp>.sqlite3
```

默认保留最近 10 份。不要把在线 WAL 数据库直接用 `cp` 复制成备份。

### 4.2 手工维护：停机冷备份

升级前先停止 YawnBot，确保没有继续写 SQLite：

```bash
docker compose stop yawnbot
```

然后备份整个 data volume，而不是只备份一个 `db.sqlite3`。这样可以同时保留数据库、WAL/SHM、Agent 媒体索引和其他需要恢复的运行时数据。

具体 volume 名可查看：

```bash
docker compose config --volumes
```

或：

```bash
docker volume ls
```

把 volume 内容复制/打包到独立备份目录后，再继续升级。备份必须存放在 volume 之外，否则删除 volume 时会一起丢失。

### 4.3 原生部署

停止 Bot 后备份整个 `data/`，至少包括 SQLite 主文件以及同目录可能存在的：

```text
*.sqlite3
*-wal
*-shm
```

不要在进程仍写入 WAL 时只复制主数据库文件。需要在线备份时使用 SQLite 官方 backup API/命令，而不是普通文件复制。

## 5. ORM migration

仓库可能存在多个 migration head，因此统一使用 `heads`：

```bash
uv run nb orm heads
uv run nb orm current
uv run nb orm upgrade heads
uv run nb orm current
```

Docker 自动迁移执行的也是：

```text
nb orm upgrade heads
```

模型发生变化后生成 migration：

```bash
uv run nb orm revision -m "short description"
```

自动生成脚本必须人工审查，尤其是 SQLite table rebuild、外键、nullable/default 变化。migration 属于源码，必须与触发它的模型变更一起提交。

## 6. 标准升级流程

### Docker / GitHub Release

1. Release workflow 重跑 quality gates，并发布带 SBOM/provenance 的 GHCR 镜像。
2. `deploy-production` 把 `image@sha256:digest`、版本和 commit SHA 传给服务器。
3. 服务器在旧实例仍运行时做 SQLite online backup 并校验。
4. 拉取不可变镜像。
5. 停止旧 YawnBot；NapCat 不参与此生命周期。
6. 使用新镜像显式执行 `nb orm upgrade heads`。
7. 执行 `docker compose up -d --no-deps yawnbot`。
8. 等待 `/healthz`，然后记录 deployment manifest。
9. 确认 OneBot V11 重连，再验证 WebUI/关键命令。

每份 manifest 记录 previous/current image、commit SHA、DB backup、迁移前后状态、
目标 migration heads 和部署时间。

### 原生 Windows/Linux

1. 停 Bot、备份 `data/`。
2. `git pull --ff-only`。
3. `uv sync --locked`。
4. `uv run nb orm upgrade heads`。
5. 启用 WebUI 时重新 `npm ci && npm run build`。
6. Playwright 版本变化或浏览器缺失时执行 `uv run playwright install chromium`。
7. `uv run nb run`。
8. 检查启动报告、`/healthz` 和 OneBot 连接。

## 7. 回滚

代码回滚不等于数据库回滚。

发布前至少记录：

- Git commit；
- ORM migration heads；
- data/SQLite 备份；
- Docker 镜像/tag（如使用）；
- 关键 `.env` 配置版本（不要把密钥写进版本库）。

如果新版本只改代码、没有 migration，可停止新版本后切回旧 digest 再启动。

如果已执行 schema migration：

1. 先阅读对应 downgrade 是否真实可逆；
2. 在数据库副本验证 downgrade；
3. 无法确认安全时，不要强行 downgrade；
4. 恢复发布前的完整 data/SQLite 备份，再切回对应代码版本。

部署脚本在 migration 或 healthcheck 失败时会退出并保留诊断现场，不会自动 downgrade
schema、恢复旧数据库或偷偷切回旧镜像。

## 8. GitHub production Environment

`production` Environment 只保存连接服务器所需的四项：

```text
DEPLOY_HOST
DEPLOY_USER
DEPLOY_SSH_PRIVATE_KEY
DEPLOY_HOST_KEY
```

运行时业务密钥不进入 GitHub Actions。建议为 production Environment 开启 required
reviewers；同一时间只允许一个 production deploy。

## 9. 反向代理与 HTTPS

公开 WebUI 时，推荐让 Caddy/Nginx/Traefik 等在 YawnBot 前终止 TLS：

```text
Internet
   │ HTTPS
   ▼
Reverse Proxy
   │ HTTP / WebSocket
   ▼
YawnBot :8080
```

代理需要正确转发：

- `Host`；
- WebSocket `Upgrade` / `Connection`；
- 原始客户端地址相关 header（按你的代理策略）；
- `/webui`、`/webui/api/v1` 以及 OneBot WS 所需路径。

启用公网 WebUI 时：

```dotenv
WEBUI_ENABLED=true
WEBUI_ADMIN_TOKEN=<至少32字符高熵随机值>
WEBUI_COOKIE_SECURE=true
```

不要直接把管理 Token 写进镜像。优先用 Compose secrets、宿主机环境、受限权限的 `.env` 或其他密钥管理方式注入。

## 10. 网络与端口

反向 WebSocket 模式下，OneBot 实现需要访问：

```text
/onebot/v11/ws
```

WebUI 使用：

```text
/webui
/webui/api/v1
```

健康检查使用：

```text
/healthz
```

如果 WebUI 不需要公网访问，可以只让反向代理/内网访问 8080，不要直接暴露管理端。

## 11. 安全基线

- OneBot access token、WebUI admin token、AI API key 三者分离；
- `.env`、数据库、浏览器 profile、缓存和备份不提交 Git；
- WebUI 公网访问使用 HTTPS + Secure Cookie；
- 生产运行保持单 YawnBot 实例；
- 只给容器/服务用户必要文件权限；
- 定期检查 data volume 磁盘空间和备份可恢复性；
- Agent 外部媒体/文件域名使用 allowlist；
- 不把个人浏览器 profile 给番茄 Playwright 使用；
- 不在生产长期打开 `AGENT_DEBUG_LOG=true`。

## 12. 发布前质量门槛

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

插件发现冒烟：

```bash
uv run python -c "import nonebot; nonebot.init(); nonebot.load_from_toml('pyproject.toml'); required=('yawn_core','yawn_core:yawn_agent','yawn_core:yawn_rpg','yawn_core:yawn_werewolf','yawn_core:yawn_fanqie'); missing=[name for name in required if nonebot.get_plugin(name) is None]; assert not missing, missing"
```

容器发布还应检查：

```bash
docker compose config
docker compose build
docker compose up -d
docker compose ps
```

发布前必须在 CI 或目标主机执行一次完整 image build 与 `/healthz` 启动 smoke；仅做 Compose YAML 静态解析不足以证明 clean deploy 可用。
