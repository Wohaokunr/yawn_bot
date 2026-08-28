# Contributing to YawnBot

感谢你愿意参与 YawnBot。项目目前仍处于快速迭代阶段；在 1.0 之前，配置、数据库 migration 和部分插件内部 API 可能继续调整。

## 开始之前

- Bug、功能建议和部署问题请优先使用仓库提供的 Issue Forms。
- 安全漏洞不要提交公开 Issue，请按 `SECURITY.md` 进行私下报告。
- 不要在 Issue、PR、日志或截图中提交 QQ 登录态、Cookie、Token、API Key、SSH 私钥、数据库或真实用户隐私数据。
- 对较大的架构改动，建议先建立 Issue 说明目标、兼容性和迁移策略。

## 开发环境

推荐使用 Python 3.10+、`uv`、Node.js 22 和 npm。

```bash
git clone https://github.com/Wohaokunr/yawn_bot.git
cd yawn_bot
uv sync --all-groups --locked
cd webui
npm ci
```

需要启动本地实例时：

```bash
cp .env.example .env
uv run nb orm upgrade heads
uv run nb run
```

也可以使用根目录 Docker Compose 进行源码构建和 clean-deploy 验证。

## 提交前质量门槛

后端：

```bash
python tools/repo_guard.py
uv run pytest -q
uv run ruff check src tests tools
uv run pyright src tools
uv run python -m compileall -q src tools
git diff --check
```

WebUI：

```bash
cd webui
npm test -- --run
npm run typecheck
npm run build
```

涉及容器或部署入口时还应执行：

```bash
docker compose config
docker compose build
docker compose up -d
curl --fail http://127.0.0.1:8080/healthz
docker compose down
```

## 数据库与 migration

`data/nonebot_plugin_orm/migrations/` 是源码的一部分，其他 `data/` 内容都是运行时数据。

修改 ORM model 时：

1. 生成对应 migration；
2. 人工审查 migration，尤其是 SQLite table rebuild、外键、nullable/default 变化；
3. 在空数据库上执行 `nb orm upgrade heads`；
4. 执行 `nb orm check` 检查 model/migration drift；
5. 在 PR 中明确说明 schema 兼容性和回滚风险。

不要提交数据库、WAL/SHM、备份文件或本地 migration 临时产物。

## 配置变更

`.env.example` 只保留最小可运行配置。新增配置项时：

- 提供安全默认值；
- 在 `docs/configuration/` 中补充完整说明；
- 如果新配置是 secret，明确标记并禁止记录到日志；
- 不要把真实 Token 或生产地址写进示例。

## 插件与命令

新增或修改 YawnBot 子插件时，尽量复用现有 NoneBot2、权限、命令声明、会话和 WebUI 基础设施，避免创建只服务单个功能的重复框架。

命令、权限、帮助目录和 matcher 元数据应保持单一真相源；如果改动会影响现有 QQ 指令，请在 PR 中列出兼容性变化。

## WebUI

WebUI 使用 React/Vite。新增管理能力时必须同时考虑：

- 管理员与访客权限边界；
- 只读页面是否真正禁止后端写操作；
- CSRF/session/鉴权；
- 移动端与现有视觉体系；
- 不向访客投影调试字段、证据 ID 或其他内部治理数据。

## Repository hygiene

仓库只接受源码、固定资源、migration、可复现配置示例和必要文档。

禁止提交：

- `.env` 与真实密钥；
- SQLite/DB/WAL/SHM；
- `node_modules`、虚拟环境和工具缓存；
- Playwright/Chromium profile；
- WebUI `dist`；
- QQ/NapCat 登录数据；
- 媒体下载缓存；
- IDE/AI 编码工具私有状态。

`tools/repo_guard.py` 会在 CI 中检查这些规则。

## Pull Request

请让一个 PR 聚焦一个清晰目标。PR 描述至少说明：

- 改了什么；
- 为什么改；
- 如何验证；
- 是否修改数据库、配置、权限或生产部署行为；
- 是否存在 breaking change。

不要把格式化整个仓库、无关重命名或生成文件夹进功能 PR。

## 生产部署边界

`deploy/production/`、`.github/workflows/release.yml` 及相关 forced-command SSH 控制面服务于维护者生产发布，也作为可审计的参考实现。

外部贡献不得假设能够访问维护者的 GitHub Environment、Actions secrets、生产服务器或 `/opt/yawnbot`。修改这些路径时必须保持：

- 业务运行时 secret 不进入仓库或镜像；
- 生产镜像使用不可变 digest；
- 数据库迁移前有一致性备份；
- SSH deploy identity 保持最小权限；
- 第三方 PR 在没有生产 secrets 的情况下也能安全执行 CI。

## License

除非明确说明，向本仓库提交的贡献按仓库 `LICENSE` 中的 Apache License 2.0 提供。
