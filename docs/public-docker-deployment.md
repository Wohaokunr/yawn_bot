# 公共 Docker / GHCR 部署

本文面向从 GitHub 获取 YawnBot 的普通用户。它与维护者自己的生产服务器部署链路完全分离：不会使用 `production` Environment、forced-command SSH、`/opt/yawnbot` 或 `deploy/production/compose.yaml`。

## 两种 Docker 入口

YawnBot 保留两条公共 Docker 路径：

1. **源码构建**：根目录 `compose.yaml`，适合开发、修改源码和从当前 checkout 构建。
2. **Release 镜像**：`deploy/docker/compose.release.yaml`，适合普通用户直接拉取 GitHub Container Registry (GHCR) 中已经由 GitHub Actions 构建的版本化镜像。

如果只是部署使用，公开仓库后优先选择 Release 镜像；如果需要开发或验证本地改动，使用根目录 Compose。

## 1. 准备运行配置

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

至少修改：

```dotenv
SUPERUSERS=["你的QQ号"]
ONEBOT_V11_ACCESS_TOKEN=换成独立随机Token
```

AI Key 不是首次启动必需项。OneBot V11 实现仍需单独部署；YawnBot 镜像不包含 NapCat/Lagrange。

## 2. 选择一个已发布镜像

Release workflow 会发布：

```text
ghcr.io/wohaokunr/yawn_bot:<version>
ghcr.io/wohaokunr/yawn_bot:sha-<commit-prefix>
```

每个 GitHub Release 还包含 `yawnbot-<version>-image.txt`，其中记录版本镜像、不可变 digest 与 commit。

推荐优先使用 Release 中记录的不可变 digest：

```bash
export YAWNBOT_IMAGE='ghcr.io/wohaokunr/yawn_bot@sha256:<release-digest>'
```

也可以使用版本 Tag：

```bash
export YAWNBOT_IMAGE='ghcr.io/wohaokunr/yawn_bot:v0.1.0'
```

PowerShell：

```powershell
$env:YAWNBOT_IMAGE='ghcr.io/wohaokunr/yawn_bot:v0.1.0'
```

`compose.release.yaml` 故意不提供隐式 `latest` 默认值，避免用户在没有注意版本变化时自动跨版本升级。

## 3. 拉取并启动

```bash
docker compose -f deploy/docker/compose.release.yaml pull
docker compose -f deploy/docker/compose.release.yaml up -d
```

检查：

```bash
docker compose -f deploy/docker/compose.release.yaml ps
curl --fail http://127.0.0.1:8080/healthz
```

该 Compose 使用与源码构建路径相同的 `yawnbot-data` named volume，并在容器启动时自动同步 canonical ORM migrations、执行 `nb orm upgrade heads`。

停止但保留数据：

```bash
docker compose -f deploy/docker/compose.release.yaml down
```

不要在需要保留数据时使用 `down -v`。

## 4. 升级

升级前先备份数据。然后把 `YAWNBOT_IMAGE` 改成新 Release 的版本或 digest，再执行：

```bash
docker compose -f deploy/docker/compose.release.yaml pull
docker compose -f deploy/docker/compose.release.yaml up -d
curl --fail http://127.0.0.1:8080/healthz
```

数据库 schema 已发生 migration 时，代码回滚不等于数据库回滚。详细备份/回滚注意事项见 [生产部署、升级与回滚](deployment.md)。

## 5. GHCR 可见性

`ghcr.io/wohaokunr/yawn_bot` 的 package visibility 允许普通用户拉取公开 Release 镜像。

公共用户不需要、也不应获得维护者的：

- `DEPLOY_HOST`；
- `DEPLOY_SSH_PRIVATE_KEY`；
- `DEPLOY_HOST_KEY`；
- production Environment secrets；
- 服务器 `/opt/yawnbot/.env`；
- 生产数据库或 NapCat 登录态。

公共 GHCR 部署与维护者 production CD 是两条独立路径。
