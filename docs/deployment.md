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

### 3.1 Core / Agent 管理 WebUI

WebUI 与 Bot 共用同一个 FastAPI 进程，默认关闭。需要管理台时：

1. 在 `webui/` 执行 `npm ci` 和 `npm run build`，确认生成 `webui/dist/`。
2. 设置 `WEBUI_ENABLED=true` 和随机生成的 `WEBUI_ADMIN_TOKEN`（至少 32 个字符）。
3. 反向代理 HTTPS 时设置 `WEBUI_COOKIE_SECURE=true`；管理页面地址为
   `https://HOST:PORT/webui`，API 前缀为 `/webui/api/v1`。
   公网部署必须先终止 TLS（反向代理终止即可）：明文 HTTP 链路上未置
   `Secure` 的会话 Cookie 可能被截取。`WEBUI_COOKIE_SECURE=false` 仅限
   回环/内网调试。
4. 首次启用包含 WebUI 审计表的新版本时，备份 SQLite/WAL/SHM 后由维护者执行
   `uv run nb orm upgrade heads`。

WebUI 只使用单运维 Token，不接管 QQ 登录或 OneBot Token。会话使用 HttpOnly、
SameSite 严格 Cookie 和 CSRF Header；管理台不展示 Agent 原始群消息正文。WebSocket
只推送概览快照和数据变更通知，不推送群聊内容。当前仍是单进程部署，不能让多个实例
同时写入同一局或共用一个 WebSocket 广播状态。

WebUI 的「番茄小说」页面可在管理台内搜索/浏览榜单/粘贴链接选书并提交 TXT 下载
任务（提交时填写接收人 QQ，可选群号），并提供任务列表、进度、章节详情与取消/
重试/发送/删除操作；与群内命令共用同一任务队列和配额。关键词搜索走无头浏览器，
首次请求可能需要十几秒。

### 3.2 运行指标

P1-6 提供进程内运行指标，不需要额外环境变量、监控 SDK 或数据库迁移。运维适配器
可调用 `yawn_core.metrics.snapshot_metrics()` 获取 JSON 快照，或调用
`yawn_core.metrics.render_prometheus()` 获取 Prometheus 文本；指标在进程重启后清零。
指标不包含 `game_id`、群号、用户号、QQ 号、提示词、AI 响应或消息正文。完整指标

RPG 额外提供 `yawnbot_rpg_tutorial_total`、`yawnbot_rpg_deductions_total` 和
`yawnbot_rpg_terminations_total`，分别观察新手引导、联合推理和终止原因；它们只使用
步骤、结果和原因等低基数标签。
目录和标签约束见 [`docs/metrics.md`](metrics.md)。

番茄小说子插件也是可选功能。它只处理阅读页明确标记为免费的内容，不登录、不绕过
验证码或付费章节；群聊任务的 TXT 成品只会私发给请求者。默认队列上限为 20，
每用户 1 个、每群 3 个活动任务，章节请求间隔 0.5 秒，成品和章节临时文件默认保留
24 小时。搜索使用 Playwright Chromium 执行番茄公开搜索页，由页面自身初始化安全 SDK
并发起官方搜索请求；榜单仍使用公开榜单页，默认最多展示 10 本榜单书籍。首次部署
需要执行 `uv run playwright install chromium`。搜索默认在 localstore 下维护插件专用的
持久化浏览器会话，用于保留页面生成的 session/fingerprint cookie；不会读取用户已有的
Chrome/Edge 配置文件，也不保存 QQ 登录态。首次空响应会在同一持久会话内自动重试一次；
浏览器保持 Playwright 标准行为，不修改自动化标记，也不伪造账号、Cookie 或服务端令牌；
如果仍要求验证码、返回空响应或浏览器不可用，插件会提示稍后重试，不会自动提交验证码。
可用 `FANQIE_*` 环境变量调整浏览器超时、headless 模式、会话目录、页面请求、
重试、队列、榜单数量和保留时间，正文不会写入数据库。

有些明确免费的章节在公开网页只返回预览。插件会先使用固定 Reading 7.2.1.32 匿名
App 画像直连官方 `api5-normal-sinfonlinea.fqnovel.com`，完成六签名、`registerkey`、
单章 `batch_full` 和正文解密；可用 `FANQIE_APP_PROTOCOL_ENABLED=false` 关闭该来源。
App 失败后再使用开源项目公开的第三方 `raw_full` 接口
`http://101.35.133.34:5000` 补全文本，可用
`FANQIE_THIRD_PARTY_API_BASE`、`FANQIE_THIRD_PARTY_API_TIMEOUT` 和
`FANQIE_THIRD_PARTY_API_RETRIES` 覆盖地址、超时和重试次数。该服务是外部依赖，可能
变更或不可用；节点连续出现 5xx/网络错误后，本次任务会熔断该节点并切换到公开
App 内容代理 `https://api.fanqietc.com`。可用
`FANQIE_THIRD_PARTY_FALLBACK_BASE` 和 `FANQIE_THIRD_PARTY_FALLBACK_TOKEN` 覆盖或关闭
该回退源。请求只发送章节 ID 和公开前端 token，不发送 QQ、Cookie 或登录凭据。若管理员已自行安装兼容的 Tomato
Novel Downloader，可设置绝对路径 `FANQIE_MOBILE_HELPER_PATH`（Windows 示例：
`C:/tools/TomatoNovelDownloader.exe`）。插件只会对阅读页同时满足 `needPay=0`、
`isPaidPublication=false`、`isPaidStory=false`，且正文明显短于页面标明字数的单章调用
它；helper 每次任务都只监听临时 `127.0.0.1` 端口，使用临时数据/输出目录并在读取后
退出。可用
`FANQIE_MOBILE_HELPER_STARTUP_TIMEOUT` 和 `FANQIE_MOBILE_HELPER_TIMEOUT` 调整超时。
不要把远程 URL、账号 Cookie 或登录凭据配置给该项。相关来源与使用边界见
[`docs/fanqie-notice.md`](fanqie-notice.md)。

App 协议固定参考 `ZreXoc/fanqie-rs` 提交
`906c6fd5744af0ef49e529102cdb64a250c067f7`；该提交的 `Cargo.toml` 声明 MIT，但提交树
没有单独的 `LICENSE` 文件。它提供的是抓包匿名画像，不包含网络 `device_register`
端点；本插件因此把固定 `device_id`、`iid`、`cdid`、`x-tt-dt` 样本作为版本化画像，
在首次使用时原子写入 localstore。画像结构损坏或服务端明确判定失效时，只会清除并重新
初始化一次，不会无限轮换设备。协议只允许固定 HTTPS 主机和路径，不跟随重定向；日志不
记录设备 ID、签名 URL、请求头或正文密钥。`registerkey` 返回的 AES key 只保留在当前
进程内，章节声明的 key version 变化时最多刷新并重取一次，不写入 localstore 或数据库。

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

然后验证 NoneBot 按正式 `pyproject.toml` 发现五个业务插件：

```bash
uv run python -c "import nonebot; nonebot.init(); nonebot.load_from_toml('pyproject.toml'); required=('yawn_core','yawn_core:yawn_rpg','yawn_core:yawn_werewolf','yawn_core:yawn_fanqie','yawn_core:yawn_agent'); missing=[name for name in required if nonebot.get_plugin(name) is None]; assert not missing, missing"
```

此检查只验证导入和注册，不会连接 QQ 或请求 AI 服务。启动日志还应显示 yawn_core
子插件启动报告；若 RPG 或狼人杀为 `失败`，不要把该版本投入运行。

### 5.1 群聊 Agent 人设

全局默认人设可通过 `.env` 中的 `AGENT_PERSONA_*` 字段配置；群主或管理员可在群内使用：

```text
/Agent人设 查看
/Agent人设 设置 name=Yawn tone=温和 style=短句为主
/Agent人设 重置
/Agent隐私 退出
/Agent隐私 恢复
```

群级设置只覆盖人设表达字段，不改变隐私边界、工具权限或系统规则。Agent 原始消息按
群配置保留，并支持成员主动退出；迁移升级仍由维护者手动执行。

### 5.2 共享 LLM 模型路由与媒体缓存

Core、Agent、RPG 与狼人杀共用默认、轻量、识图三个模型档位。轻量模型留空时
回退到默认模型；识图模型留空时无法为不支持图片的模型生成独立图片转述：

```text
AI_MODEL=高级默认模型
AI_LIGHT_MODEL=轻量模型
AI_VISION_MODEL=图片识别模型
AI_DEFAULT_THINKING=auto
AI_LIGHT_THINKING=disabled
AI_VISION_THINKING=disabled
AI_DEFAULT_MULTIMODAL=auto
AGENT_DIALOGUE_LLM_PROFILE=default
AGENT_MEMORY_LLM_PROFILE=light
AGENT_IMAGE_LLM_PROFILE=vision
AGENT_MEDIA_CACHE_TTL=86400
AGENT_MEDIA_CACHE_DIR=data/agent_media
AGENT_MEDIA_ALLOWED_HOSTS=图片来源域名
```

模型、推理策略和 Agent/RPG/狼人杀任务路由可在 WebUI 的“环境配置”页面统一修改，
保存后重启生效。图片能力设为 `auto` 时会先尝试任务模型；端点明确拒绝图片后，
调用识图任务生成带来源标记的文本转述。群管理员可使用 `/Agent设置 媒体缓存 开|关` 控制短期缓存。
缓存按群和媒体内容哈希隔离，过期任务会清理原图与识别记录。启动调试日志会对
API key 等凭据脱敏。

## 6. 启动与进程管理

### 6.1 RPG 首次验证

首次报名会优先私聊发送分阶段引导；私聊失败时群内只提示玩家加机器人好友，不回退角色卡、
个人线索或引导正文。`/跑团帮助` 可随时重看当前阶段，`/跳过引导` 停止自动提示，
`/重新引导` 清除当前版本状态。服务器重启后旧局不恢复，新局重新开设。

回放从 JSONL 事件日志读取，不修改游戏状态；公开回放只展示已公开线索和推论，个人回放
额外展示该玩家获授权的信息。事件日志不可用时回放明确返回不可回放。

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
