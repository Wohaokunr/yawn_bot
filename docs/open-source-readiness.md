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

也可以在 GitHub Actions 手工执行 **Open-source audit** workflow。该 workflow 使用 `fetch-depth: 0` 并额外抓取所有远端 branches/tags。

扫描命中真实凭据时：

1. **先吊销/轮换**凭据；
2. 确认没有业务继续依赖旧值；
3. 使用 `git-filter-repo` 等工具删除历史对象；
4. 更新所有受影响 branches/tags；
5. 清理包含旧对象的 Release/Actions artifact（如适用）；
6. 重新执行全历史扫描；
7. 只有扫描通过后才能继续公开。

扫描命中历史数据库、NapCat/QQ 登录态、浏览器 profile 等私人运行时文件时，即使没有检测到格式化 secret，也应按潜在隐私数据处理并从公开历史删除。

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

Python/npm 依赖仍按各自许可证发布，不因 YawnBot 使用 Apache-2.0 而改变。若项目未来 vendoring 第三方源码或静态资源，需要同步维护 NOTICE/third-party attribution。

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

验收条件：

- 不依赖维护者电脑上的 `.env`、npm/uv cache、数据库或浏览器 profile；
- 空 SQLite 能迁移到 heads；
- WebUI 构建来自仓库源码；
- 没有 AI Key 时仍能按最小配置启动；
- 文档能够让陌生用户理解 OneBot V11 仍需要单独实现；
- `docker compose down` 后 named volume 数据保持存在。

## 8. 开源当天顺序

1. 合并开源准备 PR；
2. 主分支 CI 全绿；
3. 手工运行 `Open-source audit` 并确认全历史通过；
4. 完成 Actions logs/artifacts 人工审计；
5. 完成第三方素材/许可证审计；
6. 在干净环境完成 README clean-deploy；
7. 打一个开源前稳定 Release，并确认现有生产自动部署成功；
8. 再把 repository visibility 切换为 public；
9. 切换后检查 GitHub Community Standards、Issue Forms、Security 页面与 GHCR package visibility。

## 9. 明确不在第一阶段做的事情

为了降低生产风险，第一次开源不以以下重构作为前置条件：

- 不拆 `/opt/yawnbot`；
- 不迁移生产数据库；
- 不更换 NapCat 生命周期；
- 不重写 forced-command SSH；
- 不把维护者生产 secrets 下发给公共用户；
- 不因为公共 GHCR 可匿名拉取而改变生产服务器现有镜像认证方式。

这些可以在仓库公开后通过独立 PR 逐步解耦公共 Release 与维护者 production deploy。
