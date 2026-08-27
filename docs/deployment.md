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

推荐从仓库根目录执行：

```bash
git pull --ff-only
docker compose build
docker compose up -d
```

Compose 默认：

- 只运行一个 `yawnbot` 服务；
- 把 `/app/data` 持久化到 named volume；
- 暴露容器 `8080`；
- 使用 `/healthz` 做 liveness；
- 容器以非 root 用户运行；
- 启动时自动同步 canonical migration 并执行 `nb orm upgrade heads`。

可在 `.env` 增加：

```dotenv
YAWNBOT_PORT=8080
YAWNBOT_AUTO_MIGRATE=true
```

如果生产变更流程要求“先人工审查 migration，再启动新代码”，设置：

```dotenv
YAWNBOT_AUTO_MIGRATE=false
```

然后先构建镜像，再使用同一镜像人工执行 migration，确认无误后启动服务。不要同时启动旧实例和新实例处理同一群。

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

### 4.1 最稳妥方式：停机冷备份

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

### 4.2 原生部署

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

### Docker

1. 确认当前没有需要保留的进行中 RPG/狼人杀局。
2. 记录当前 Git commit、当前 migration heads 和镜像信息。
3. 停止服务并备份 `data` volume。
4. `git pull --ff-only`。
5. 阅读新增 migration 和 release/change notes。
6. `docker compose build`。
7. `docker compose up -d`。
8. 检查 `docker compose ps`、启动日志与 `/healthz`。
9. 确认 OneBot V11 已重新连接，再验证 WebUI/关键命令。

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

如果新版本只改代码、没有 migration，可停止新版本后切回旧 commit/镜像再启动。

如果已执行 schema migration：

1. 先阅读对应 downgrade 是否真实可逆；
2. 在数据库副本验证 downgrade；
3. 无法确认安全时，不要强行 downgrade；
4. 恢复发布前的完整 data/SQLite 备份，再切回对应代码版本。

## 8. 反向代理与 HTTPS

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

## 9. 网络与端口

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

## 10. 安全基线

- OneBot access token、WebUI admin token、AI API key 三者分离；
- `.env`、数据库、浏览器 profile、缓存和备份不提交 Git；
- WebUI 公网访问使用 HTTPS + Secure Cookie；
- 生产运行保持单 YawnBot 实例；
- 只给容器/服务用户必要文件权限；
- 定期检查 data volume 磁盘空间和备份可恢复性；
- Agent 外部媒体/文件域名使用 allowlist；
- 不把个人浏览器 profile 给番茄 Playwright 使用；
- 不在生产长期打开 `AGENT_DEBUG_LOG=true`。

## 11. 发布前质量门槛

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
