# 部署与维护

本文描述 YawnBot 当前支持的单进程部署方式。配置项以 `.env.example` 和各插件的
`config.py` 为准。

## 1. 运行边界

YawnBot 使用 SQLite 保存平台数据、提醒、AI 对话历史和游戏局末摘要。RPG 与狼人杀
的房间、队列和局内状态仍在进程内：

- 只运行一个 YawnBot 进程。
- 不要让多个实例同时服务同一群的游戏命令。
- 重启前应结束正在进行的游戏；当前不支持中局恢复。
- `data/` 必须放在持久磁盘上，并纳入备份。

若以后需要多实例，必须先引入共享房间所有权、租约或 fencing token、跨实例动作
路由和幂等日志。仅把 SQLite 换成远程数据库不能解决双引擎消费问题。

## 2. 安装

在项目根目录执行：

```bash
uv sync --all-groups --locked
```

`--locked` 保证安装与提交的 `uv.lock` 一致。生产环境可以不安装工具组，但发布前的
CI 和维护检查需要 `dev`、`tools` 两组依赖。

## 3. 配置

复制 `.env.example` 为 `.env`，不要提交 `.env`。NoneBot 先读取 `.env` 中的
`ENVIRONMENT`，再合并 `.env.<environment>`；真实环境变量优先级最高。

必须核对：

- `SUPERUSERS`：JSON 字符串数组，例如 `["123456789"]`。
- `HOST` / `PORT`：反向 WebSocket 服务监听地址，默认 `127.0.0.1:8080`。
- `ONEBOT_V11_ACCESS_TOKEN`：与 OneBot 实现一致，不应为空或使用示例值。
- `LOCALSTORE_USE_CWD=true`：让默认 SQLite 文件位于项目的
  `data/nonebot_plugin_orm/db.sqlite3`。

反向 WebSocket 的默认路径是 `/onebot/v11/ws`。若 YawnBot 主动连接 OneBot，使用
`ONEBOT_V11_WS_URLS` 的 JSON 数组，并把 `DRIVER` 改为
`~fastapi+~websockets`。若使用 `ONEBOT_V11_API_ROOTS` 主动请求 HTTP API，则需要
`DRIVER=~fastapi+~httpx`。不要同时配置多个未知所有权的连接入口。

`AI_API_KEY` 可选。需要私聊 AI 对话或正常的狼人杀 AI 时才应配置。无 key 的 RPG
部署建议设置 `RPG_AI_ENABLED=false`，以明确启用确定性模式。

番茄小说子插件也是可选功能。它只处理番茄站公开可访问的内容，不登录、不绕过
验证码或付费/访问控制；群聊任务的 TXT 成品只会私发给请求者。默认队列上限为 20，
每用户 1 个、每群 3 个活动任务，章节请求间隔 0.5 秒，成品和章节临时文件默认保留
24 小时。可用 `FANQIE_*` 环境变量调整超时、重试、队列和保留时间，正文不会写入
数据库。相关来源与使用边界见 [`docs/fanqie-notice.md`](fanqie-notice.md)。

## 4. 迁移

受版本控制的 canonical 迁移位于：

```text
data/nonebot_plugin_orm/migrations/
```

其中既有根级迁移，也有 `yawn_core/` 及其子目录中的 bind 分支，因此仓库可能存在
多个 head。部署时使用 `heads`，不要假设只有一个 `head`：

```bash
uv run nb orm heads
uv run nb orm current
uv run nb orm upgrade heads
uv run nb orm current
```

迁移必须由维护者手动执行。升级前先停止 Bot 并备份 SQLite 数据库，以及同目录的
`-wal`、`-shm` 文件；在文件复制前应确认进程已退出或执行 SQLite 在线备份，避免
得到不一致副本。

模型变更后生成迁移：

```bash
uv run nb orm revision -m "简短描述"
```

仔细审查自动生成脚本，再在测试数据库运行 `uv run nb orm upgrade heads`。
`migrations/versions/` 是 ORM CLI 的工作/同步目录；发布判断以
`data/nonebot_plugin_orm/migrations/` 中已提交的 canonical 迁移为准。不要手工修改
`src/plugins/yawn_core/**/migrations/` 下的历史副本。

番茄子插件的迁移位于 `data/nonebot_plugin_orm/migrations/yawn_core/yawn_fanqie/`，
首次部署或升级时必须由维护者审查后手动执行 `uv run nb orm upgrade heads`；本项目
不会在插件启动时自动升级数据库。

## 5. 发布前检查

```bash
uv sync --all-groups --locked
uv run pytest -q
uv run ruff check src tests tools/rpg_module_editor
uv run pyright src tools/rpg_module_editor
uv run python -m compileall -q src tools/rpg_module_editor
git diff --check
```

然后验证 NoneBot 按正式 `pyproject.toml` 发现四个业务插件：

```bash
uv run python -c "import nonebot; nonebot.init(); nonebot.load_from_toml('pyproject.toml'); required=('yawn_core','yawn_core:yawn_rpg','yawn_core:yawn_werewolf','yawn_core:yawn_fanqie'); missing=[name for name in required if nonebot.get_plugin(name) is None]; assert not missing, missing"
```

此检查只验证导入和注册，不会连接 QQ 或请求 AI 服务。启动日志还应显示 yawn_core
子插件启动报告；若 RPG 或狼人杀为 `失败`，不要把该版本投入运行。

## 6. 启动与进程管理

生产启动命令：

```bash
uv run nb run
```

使用 systemd、Windows 服务管理器或其他进程守护工具时，工作目录必须是仓库根目录，
并保证环境变量和 `data/` 目录对运行用户可见。先启动 YawnBot，再启动或连接 OneBot
实现；健康检查至少应覆盖：

- 进程仍存活且监听预期端口。
- OneBot 账号已建立连接。
- 启动日志没有插件加载失败或 ORM 迁移落后。
- `data/` 所在磁盘有足够空间。

## 7. 回滚与备份

发布前记录 Git 提交、迁移 heads 和数据库备份。代码回滚不等于数据库回滚；包含
迁移的版本应先阅读 downgrade 实现并在副本上验证。无法确认 downgrade 安全时，
恢复发布前数据库备份并切回对应提交。
