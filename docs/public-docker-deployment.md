# 公共 Docker / GHCR 部署

本文面向从 GitHub 获取 YawnBot 的普通用户。它与维护者自己的生产服务器部署链路完全分离：不会使用 `production` Environment、forced-command SSH、`/opt/yawnbot` 或 `deploy/production/compose.yaml`。

## 两种 Docker 入口

YawnBot 保留两条公共 Docker 路径：

1. **源码构建**：根目录 `compose.yaml`，适合开发、修改源码和从当前 checkout 构建。
2. **Release 镜像**：`deploy/docker/compose.release.yaml`，适合普通用户直接拉取 GitHub Container Registry (GHCR) 中已经由 GitHub Actions 构建的版本化 YawnBot 镜像。

Playwright Chromium 不再嵌入 YawnBot 主镜像。番茄浏览器搜索改为可选的 `fanqie-browser` sidecar；不使用该功能的用户无需下载 Chromium，普通应用升级也不会重复传输浏览器层。公共 sidecar 同样由 GitHub Actions 单独发布到 GHCR，并以 Playwright 版本 + runtime 内容 hash 固定 tag。

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

独立 Playwright runtime workflow 会发布类似：

```text
ghcr.io/wohaokunr/yawn_bot:browser-pw-1.62.0-e611a498af6e84c5
```

浏览器 tag 的最后一段由 `playwright-version.txt` 与 `playwright-server.Dockerfile` 共同计算，普通业务源码变化不会改变它。

每个 GitHub Release 还包含 `yawnbot-<version>-image.txt`，其中记录版本镜像、不可变 digest、commit，以及维护者生产环境对应的稳定 Playwright runtime 信息。

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

如果部署的是旧 Release，建议同时 checkout 到相同版本 tag；这样 Compose 中默认的 Playwright runtime tag 与该版本代码保持一致：

```bash
git checkout v0.1.0
```

## 3. 拉取并启动

不使用番茄浏览器搜索时：

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

### 启用番茄 Playwright sidecar

先在 `.env` 增加：

```dotenv
FANQIE_BROWSER_WS_ENDPOINT=ws://playwright:3000/
```

然后直接拉取并启用浏览器 profile：

```bash
docker compose -f deploy/docker/compose.release.yaml \
  --profile fanqie-browser pull
docker compose -f deploy/docker/compose.release.yaml \
  --profile fanqie-browser up -d
```

默认浏览器镜像是当前 checkout 已固定的 GHCR `browser-pw-<version>-<runtime-hash>`。需要测试其他兼容 runtime 时，也可以显式设置 `YAWNBOT_BROWSER_IMAGE` 覆盖它。

sidecar 只包含与项目 pin 匹配的 Chromium headless shell，且不发布 `3000` 端口到宿主机/公网。YawnBot 通过 Compose 内部网络连接 `ws://playwright:3000/`。

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

如果启用了 `fanqie-browser`，新 checkout 在 Playwright 版本或 sidecar runtime 定义变化时会指向新的 hash tag；再次执行 `--profile fanqie-browser pull` 即可。普通业务代码升级不会制造新的 Chromium runtime。

数据库 schema 已发生 migration 时，代码回滚不等于数据库回滚。详细备份/回滚注意事项见 [生产部署、升级与回滚](deployment.md)。

## 5. GHCR 可见性

在仓库仍为 private 的开源准备阶段，GHCR package 也可能仍要求 GitHub 身份认证。正式切换 public 后，维护者需要确认 `ghcr.io/wohaokunr/yawn_bot` 的 package visibility 允许普通用户拉取公开 Release 镜像和 Playwright runtime。

公共用户不需要、也不应获得维护者的：

- `DEPLOY_HOST`；
- `DEPLOY_SSH_PRIVATE_KEY`；
- `DEPLOY_HOST_KEY`；
- production Environment secrets；
- 服务器 `/opt/yawnbot/.env`；
- 生产数据库或 NapCat 登录态。

公共 GHCR 部署与维护者 production CD 是两条独立路径。
