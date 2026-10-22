# yawn_fanqie — 番茄小说下载子插件

群/私聊里模糊搜索、浏览榜单、下载番茄免费小说的**公开**章节，打包成文件发回。
由父插件的 `_load_sub_plugins()` 动态加载。来源与合规边界（不登录、不提交验证码、
不请求付费章节）见 [docs/fanqie-notice.md](../../../../docs/fanqie-notice.md)，改动前先读。

## 数据流

`commands.py`（交互与任务权限）→ `state.py`（持久化任务队列 + 单 worker）→
`provider.py`（公开页面抓取与正文获取，按序回退：公开阅读页 → `app_protocol.py` 固定 App 协议 →
第三方接口 → `mobile_helper.py` 本机 helper）→ `decoder.py` / `font_signatures.py`（页面状态与字体混淆解码）→
`delivery.py`（文件投递）。搜索走 `browser_search.py`（隔离的 Playwright Chromium）。

## 文件地图

| 文件 | 职责 |
|---|---|
| `__init__.py` | 插件元数据与命令组注册（`register_command_group`） |
| `commands.py` | `/番茄小说` 交互命令、任务管理权限 |
| `state.py` | 下载任务持久化队列与单 worker；并发/配额上限来自配置 |
| `provider.py` | 公开页面 provider 与多级回退（最大文件） |
| `app_protocol.py` / `app_signer.py` | 固定版本的匿名 App 协议客户端与请求签名 |
| `browser_search.py` | 用真实浏览器打开公开搜索页并监听官方响应 |
| `decoder.py` / `font_signatures.py` | HTML/状态解码；公开字体 glyph 轮廓哈希 → 字符映射 |
| `mobile_helper.py` | 短生命周期本机移动端 helper 桥接（可选） |
| `delivery.py` | 任务文件投递（OneBot 文件上传） |
| `models.py` | 任务与章节元数据表（正文不入库，只在 localstore 临时落盘） |
| `config.py` | `FANQIE_*` 配置 |
| `tests/README.md` | 说明测试为什么放在仓库根 `tests/` |

## 关联位置（跨目录）

- **迁移正本**：`data/nonebot_plugin_orm/migrations/yawn_core/yawn_fanqie/`，见 [data/CLAUDE.md](../../../../data/CLAUDE.md)。
- **WebUI**：后端 [../webui/fanqie.py](../webui/CLAUDE.md)，前端 `webui/src/fanqie.tsx`（[webui/CLAUDE.md](../../../../webui/CLAUDE.md)）。
- **测试**：仓库根 `tests/test_fanqie_*.py`、`tests/test_webui_fanqie.py`（插件必须经 `nonebot.init()` 正式发现，所以不放本目录）。
- **浏览器运行时**：应用镜像不含 Chromium，搜索通过可选的 Playwright sidecar（`deploy/docker/playwright-server.Dockerfile`，`FANQIE_BROWSER_WS_ENDPOINT`）提供，见 [deploy/CLAUDE.md](../../../../deploy/CLAUDE.md)。
- **配置说明**：[docs/configuration/fanqie-games.md](../../../../docs/configuration/fanqie-games.md)。
