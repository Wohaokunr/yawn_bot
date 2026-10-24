# deploy — 部署与运维

Docker 镜像、Compose 入口、NapCat（OneBot 实现端）、生产发布脚本与主机初始化。
YawnBot 按**单实例**设计（SQLite + 进程内对局状态），边界与升级/回滚流程见
[docs/deployment.md](../docs/deployment.md)。

## 三个 Compose 入口（不要混用）

| 文件 | 用途 |
|---|---|
| 仓库根 `compose.yaml` | 源码构建、本地开发、clean-deploy CI smoke |
| [docker/compose.release.yaml](docker/README.md) | 公共用户用预构建 GHCR 镜像部署；可选 Playwright sidecar 给番茄搜索 |
| [production/compose.yaml](production/compose.yaml) | 维护者生产服务器，只接受不可变 image digest |

## 结构

| 路径 | 职责 |
|---|---|
| `docker-entrypoint.sh` | 镜像入口（由仓库根 `Dockerfile` 使用；Dockerfile 先在 node 阶段构建 `webui/dist`） |
| `docker/` | 公共发布 Compose、`playwright-server.Dockerfile` 与锁定的 `playwright-version.txt`；说明见 [docker/README.md](docker/README.md)、[docs/public-docker-deployment.md](../docs/public-docker-deployment.md) |
| `napcat/` | NapCat 的 Compose 与 YawnBot OneBot 配置模板渲染（`render-yawnbot-config.py`） |
| `production/` | 生产发布：`deploy-release.sh`、forced-command SSH 入口 `deploy-ssh-command`、`sync-control-plane.sh`、部署记录 |
| `host/` | OpenCloudOS 9 主机分阶段初始化与 SSH 加固脚本，见 [host/README.md](host/README.md) |

## 代码沙箱服务

三个 Compose 都带 `sandbox` 服务：与 bot 同一镜像，入口 `python3 -s -m yawn_sandbox serve`，`network_mode: none`、只读根、`init: true`、`cap_drop: ALL` 后只加回降权所需的 SETUID/SETGID/CHOWN/FOWNER/DAC_OVERRIDE/KILL，`no-new-privileges`，内存/CPU/pids 上限。bot 与它只共享 `sandbox-run` 卷里的 Unix socket（`/run/yawnbot-sandbox/runner.sock`），**不要**给它开端口或网络、不要挂 `.env`。生产发布脚本先在旧 bot 运行时拉起沙箱并等健康，失败只告警不阻断发布。

## 规则

- `production/` 与 `.github/workflows/release.yml`、`deploy-existing.yml` 属于高风险路径：PR 模板要求说明为什么必须改，并保证不可变 digest、服务器本地 secrets、SQLite 部署前备份、forced-command SSH 权限边界都不被削弱。
- 迁移在部署时如何执行见 [docs/deployment.md](../docs/deployment.md)；开发中**不要**替用户跑 `nb orm`（见 [根 CLAUDE.md](../CLAUDE.md)）。
- 改 Playwright 版本要同时改 `docker/playwright-version.txt` 与 `docker/playwright-server.Dockerfile`，`tools/repository_content_test.py` 会核对两者。

## 关联位置（跨目录）

- **CI/CD**：`.github/workflows/release.yml`（构建发布镜像）、`deploy-existing.yml`（部署已有版本）、`production-control-plane.yml`、`host-bootstrap.yml`、`playwright-runtime.yml`。
- **契约测试**：`tests/test_production_bootstrap_contract.py`、`tests/test_production_deployment_control_plane.py`、`tests/test_deployment_cache_contract.py`（[tests/CLAUDE.md](../tests/CLAUDE.md)）。
- **健康检查**：`/healthz` 由 `src/plugins/yawn_core/health.py` 提供。
- **配置**：`.env.example` 与 [docs/configuration/](../docs/configuration/README.md)。
