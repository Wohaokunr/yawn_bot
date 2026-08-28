# Open-source readiness gate

本文是把 YawnBot 仓库从 private 切换为 public 前的维护者验收清单。它与普通 Release 不同：普通 Release 只验证当前源码和可部署产物；开源验收还必须验证 **整个 Git 可达历史、旧 Actions 输出、第三方素材版权与公开协作边界**。

> 在本文所有“阻断项”关闭前，不应修改仓库 visibility。

## 1. 不得影响现有生产部署

开源准备不要求迁移生产服务器，也不要求更换现有部署目录或业务配置。

第一阶段明确保持以下边界：

- `production` GitHub Environment 继续保存服务器连接 secrets；
- 业务运行时 `.env`、AI Key、WebUI Token、OneBot Token 继续只保存在服务器；
- `/opt/yawnbot`、SQLite 数据、NapCat 登录态不进入公开仓库；
- Release 继续以不可变 `image@sha256:digest` 部署；
- forced-command SSH 权限模型保持不变；
- 生产部署前 SQLite online backup 与 migration/healthcheck 顺序保持不变。

任何开源整改 PR 如果修改 `.github/workflows/release.yml`、`.github/workflows/deploy-existing.yml` 或 `deploy/production/`，都必须单独说明并做生产 CD 回归验证。

## 2. 当前树检查

执行：

```bash
python tools/repo_guard.py
python tools/repository_content_test.py
```

这两项负责当前 checkout：拒绝数据库、`.env`、缓存、浏览器 profile、私钥、常见 Token 和超大运行时文件继续进入 Git。

## 3. 全历史检查（阻断项）

删除 HEAD 中的 secret 并不等于安全；仓库公开后，可达 branch/tag 的历史对象也可能被访问。

执行：

```bash
python tools/history_secret_audit.py
```

也可以在 GitHub Actions 手工执行 **Open-source audit** workflow。该 workflow 只做当前树检查与全历史只读审计，不执行历史重写，也不监听已经完成的开源准备阶段分支。

需要验证历史清洗方案时，单独手工执行 **History rewrite dry-run**，或修改历史审计/重写工具的 PR 时让它自动运行。该 workflow 只在 Runner 的一次性 mirror 中执行 `git-filter-repo`，不会 push 远端 refs。

扫描命中真实凭据时：

1. **先吊销/轮换**凭据；
2. 确认没有业务继续依赖旧值；
3. 使用 `git-filter-repo` 等工具删除历史对象；
4. 更新所有受影响 branches/tags；
5. 清理包含旧对象的 Release/Actions artifact（如适用）；
6. 重新执行全历史扫描；
7. 只有扫描通过后才能继续公开。

扫描命中历史数据库、NapCat/QQ 登录态、浏览器 profile 等私人运行时文件时，即使没有检测到格式化 secret，也应按潜在隐私数据处理并从公开历史删除。

实际远端重写必须遵循 [`docs/history-rewrite-runbook.md`](history-rewrite-runbook.md)：先建立并验证仓库外/offline mirror 或 bundle，再 force-update refs。不得把包含待删除私人历史的回滚包放进本仓库、GitHub Release、Issue/PR 附件或 Actions artifact。

## 4. Actions 历史与 artifact 检查（阻断项）

在 visibility 切换前人工检查历史 workflow runs，特别是生产部署相关 jobs：

- 日志是否包含不希望公开的服务器 IP/域名、用户名、网络结构或调试信息；
- 是否曾输出 `.env`、Token、SSH key、Docker auth 或 OneBot 凭据；
- artifacts 是否包含部署包以外的数据库、日志、环境文件或构建机私有状态；
- 失败日志是否因为 debug/verbose 模式包含额外基础设施信息。

发现敏感信息时应删除对应 run/artifact，并按信息类型决定是否轮换凭据。

## 5. 第三方代码与素材检查（阻断项）

Apache-2.0 只覆盖 YawnBot Contributors 有权许可的仓库内容，不会自动重新许可第三方素材。

公开前检查所有 tracked 非源码资源：

- 图片、SVG、图标；
- 字体；
- 音频/视频；
- WebUI 固定资源；
- RPG 模组或示例数据；
- 从第三方项目复制/改写的代码或模板；
- 文档截图、Logo 和品牌素材。

每项必须属于以下之一：

1. YawnBot contributors 原创且可按 Apache-2.0 发布；
2. 第三方许可证允许再分发，并保留其要求的 copyright/license/NOTICE；
3. 来源/授权不明确 —— 删除或替换后再公开。

当前仓库检索未发现直接跟踪的 `.ttf`、`.svg` 或 `.webp` 文件；`.png/.jpg` 文本检索命中主要来自测试字符串。这个结果只用于缩小复核范围，**不能替代对实际 tracked tree 和依赖许可证的最终审计**。

WebUI 通过 `@fontsource/zcool-kuaile` 再分发 ZCOOL KuaiLe 字体；其 OFL-1.1 copyright/license 文本已记录在 `THIRD_PARTY_NOTICES.md`，并随 Docker runtime image 一起复制。Python/npm 其他依赖仍按各自许可证发布，不因 YawnBot 使用 Apache-2.0 而改变。若项目未来 vendoring 第三方源码或静态资源，需要同步维护 NOTICE/third-party attribution。

## 6. 社区治理文件（阻断项）

公开前确认以下文件存在且 GitHub 能识别：

```text
LICENSE
CONTRIBUTING.md
SECURITY.md
CODE_OF_CONDUCT.md
.github/CODEOWNERS
.github/PULL_REQUEST_TEMPLATE.md
.github/ISSUE_TEMPLATE/bug_report.yml
.github/ISSUE_TEMPLATE/feature_request.yml
.github/ISSUE_TEMPLATE/deployment_help.yml
.github/ISSUE_TEMPLATE/config.yml
```

安全漏洞不能被 Issue Form 引导到公开 Bug Report。

## 7. 陌生用户 clean-deploy 验收（阻断项）

CI 已包含 fresh-checkout 和 Docker clean-deploy smoke；切换 public 前仍建议从一台没有 YawnBot 开发状态的新环境按 README 操作一次。

### 源码构建路径

至少验证：

```bash
git clone https://github.com/Wohaokunr/yawn_bot.git
cd yawn_bot
cp .env.example .env
# 修改 SUPERUSERS 与 ONEBOT_V11_ACCESS_TOKEN
docker compose build
docker compose up -d
docker compose ps
curl --fail http://127.0.0.1:8080/healthz
```

### 公共 Release 镜像路径

还要验证普通用户不需要本地构建即可启动已发布 GHCR 镜像：

```bash
export YAWNBOT_IMAGE='ghcr.io/wohaokunr/yawn_bot@sha256:<release-digest>'
docker compose -f deploy/docker/compose.release.yaml pull
docker compose -f deploy/docker/compose.release.yaml up -d
curl --fail http://127.0.0.1:8080/healthz
```

详细步骤见 [公共 Docker / GHCR 部署](public-docker-deployment.md)。正式公开后确认 GHCR package visibility 允许匿名/普通用户拉取公开 Release 镜像。

验收条件：

- 不依赖维护者电脑上的 `.env`、npm/uv cache、数据库或浏览器 profile；
- 空 SQLite 能迁移到 heads；
- WebUI 构建来自仓库源码或已发布 GitHub Actions 镜像；
- 没有 AI Key 时仍能按最小配置启动；
- 文档能够让陌生用户理解 OneBot V11 仍需要单独实现；
- 源码 Compose 与 Release Compose 都使用持久 data volume；
- `docker compose down` 后 named volume 数据保持存在。

## 8. 开源当天顺序

1. 合并开源准备 PR；
2. 主分支 CI 全绿；
3. 完成正式 Git 历史清洗并重新手工运行 `Open-source audit`，确认全历史通过；
4. 完成 Actions logs/artifacts 人工审计；
5. 完成第三方素材/许可证审计；
6. 在干净环境分别完成 README 源码 clean-deploy 和公共 GHCR Release Compose 验收；
7. 打一个**不跳过 quality gates** 的开源前稳定 Release，并确认现有生产自动部署成功；
8. 再把 repository visibility 切换为 public；
9. 切换后检查 GitHub Community Standards、Issue Forms、Security 页面与 GHCR package visibility。

## 9. 三条部署路径的边界

开源后保持三条互不覆盖的部署路径：

```text
compose.yaml
  -> 开发者 / 源码构建 / CI clean-deploy

deploy/docker/compose.release.yaml
  -> 普通公共用户 / GHCR 已发布镜像

deploy/production/compose.yaml
  -> 维护者自己的生产服务器 / 不可变 digest + 受限 CD
```

公共 Compose 不使用 production Environment，不需要服务器 SSH secrets，也不会改变 `/opt/yawnbot` 控制面。维护者生产服务器即使 GHCR package 变为 public，也继续保持现有 digest、认证、备份、migration 与 forced-command SSH 逻辑。

## 10. 明确不在第一次开源前强制重构的事项

为了降低生产风险，第一次开源不以以下重构作为前置条件：

- 不拆 `/opt/yawnbot`；
- 不迁移生产数据库；
- 不更换 NapCat 生命周期；
- 不重写 forced-command SSH；
- 不把维护者生产 secrets 下发给公共用户；
- 不因为公共 GHCR 可匿名拉取而改变生产服务器现有镜像认证方式。

这些可以在仓库公开后通过独立 PR 逐步解耦公共 Release 与维护者 production deploy。
