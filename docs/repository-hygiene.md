# Repository hygiene

YawnBot 仓库只保存可审查、可复现的源码输入。任何文件在提交前都应归入下面四类之一。

| 类别 | 示例 | Git 策略 |
| --- | --- | --- |
| 源码 / 固定资源 | `src/`、`webui/src/`、`tests/`、`docs/`、`package-lock.json`、ORM migration | 默认允许提交 |
| 可再生构建

提交前运行：

```bash
python tools/repo_guard.py
git diff --check
```

`tools/repo_guard.py` 只检查 Git 已跟踪文件，不会删除或检查本地未跟踪的数据库和缓存。
它会拒绝：

- 已知的系统缓存、IDE/Agent 私有目录、`node_modules` 和 `webui/di产物 | `webui/dist/`、Vite/TypeScript 缓存、coverage、Playwright 报告 | 禁止提交，由 CI/发布流程生成 |
| 运行时数据 | SQLite/WAL/SHM、备份、下载 TXT、媒体缓存、日志、localstore 数据 | 禁止提交，部署时放在持久卷并单独备份 |
| 开发工具私有状态 | `.qoder/`、`.zcode/`、`.claude/`、`.mimosa/`、`.vs/`、`.vscode/`、`node_modules/` | 禁止提交，只保留在开发机 |

`data/` 是特殊目录：运行数据默认全部忽略，但
`data/nonebot_plugin_orm/migrations/**` 是数据库 schema 的源码，必须版本控制。
根目录旧的 `migrations/` 副本不是 canonical migration，不应恢复。

## 自动门槛st`；
- `data/` 下除 canonical migration 以外的运行数据；
- SQLite、WAL/SHM、临时文件、tsbuildinfo 等生成文件；
- 单文件超过 5 MiB 的新跟踪文件；
- 常见真实 API Token、私钥和高风险 secret 赋值模式。

如果确实需要提交大于 5 MiB 的固定资源，应先评估是否应使用外部制品存储，再显式调整
`repo_guard.py` 的规则；不要用 `git add -f` 绕过门槛。

## WebUI 构建契约

`webui/dist/` 永远由 `npm run build` 生成，不进入 Git。普通 Python/API 测试不依赖
前端产物；CI 的 `webui-quality` job 构建 dist 并上传 `webui-dist` artifact，随后
`webui-spa-integration` 下载该 artifact 验证 FastAPI 的 SPA fallback 和静态资源服务。

生产部署若启用 WebUI，必须先构建 dist。`WEBUI_ENABLED=true` 时后端仍会在启动阶段
检查 `webui/dist/index.html`，缺失时直接拒绝启动，避免部署出只有 API 没有管理台的半成品。

## 历史清理原则

默认不为了目录观感重写 Git 历史。只有历史审计发实现真凭据、用户数据库、敏感 Cookie
或明显异常的大 blob 时，才单独使用 `git filter-repo` 清理，并同步轮换已经暴露的凭据。
历史重写需要通知所有协作者重新同步分支。
