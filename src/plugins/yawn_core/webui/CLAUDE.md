# webui（后端）— 管理台 API

FastAPI 应用，挂在 NoneBot 驱动上，前缀 `/webui/api/v1`（`config.API_PATH`），并从仓库根
`webui/dist` 托管前端单页应用。前端源码与约定见 [仓库根 webui/CLAUDE.md](../../../../webui/CLAUDE.md)。

## 启用与认证

- `WEBUI_ENABLED=true` 时 `config.validate_enabled_config()` 检查 Token 非空、`webui/dist/index.html` 存在；不满足只记错误日志并**跳过挂载 WebUI，绝不让 Bot 启动失败**。Token 长度不强制，偏弱只告警（`environment_schema.admin_token_warnings`）。会话签名密钥 = `data/webui/session.key` 本地随机密钥 + Token（`auth._secret`）。
- `auth.py`：单运维 Token 登录 → 签名会话 Cookie（`yawn_webui_session`）+ CSRF + 登录限速。会话角色分 `admin` / `guest`。
- `deps.py`：路由共享依赖与响应信封；访客只能看 allowlist 内的群（`guest_access.py`）。**任何新只读接口都要想清楚访客能不能看、返回字段是否需要裁剪、opt-out 成员是否被过滤。**

## 文件地图

| 文件 | 职责 |
|---|---|
| `app.py` | 组装 FastAPI：`include_router` 所有路由、挂载静态资源、SPA 回退 |
| `config.py` | `WebUIConfig`（`WEBUI_*`）、路径常量、`DIST_DIR` |
| `auth.py` / `auth_routes.py` | 会话与登录/登出端点 |
| `deps.py` / `route_helpers.py` / `route_models.py` | 共享依赖、小 helper、请求体模型 |
| `service.py` | 查询与写入服务层（最大的文件）；路由尽量薄，逻辑放这里 |
| `hub.py` | 单进程 WebSocket 状态广播（`/stream`），前端 `api.ts` 订阅 |
| `overview_routes.py` | 概览与插件摘要（含子插件加载报告） |
| `groups.py` / `users.py` / `directory.py` | 群、群成员、用户管理。列表与统计查询在 `directory.py`（群/成员/用户的筛选排序 + 当前页补统计、`/groups/summary`、`/users/summary`、`/groups/{id}/overview`、`/groups/{id}/overrides` 成员覆盖一览、`/users/{id}` 档案与所在群明细）；批量写入 `POST /groups/batch/features`、`/groups/{id}/members/batch/features`、`/users/batch/features`（单事务、上限 100、逐条 hub 通知），`POST /groups/{id}/features/reset` 默认不动 group_agent，`DELETE /groups/{id}/members/{uid}/features` 清成员覆盖。注意 `GlobalUserFeature`（“私聊功能”）只在私聊生效，群聊权限链不查它；`users.py` 另有 `/users/{id}/affinity`（好感余额/来源/流水/各群签到，`POST …/adjust` 手动加减，记 `manual` 流水）与 `/users/{id}/private-agent`（私聊 Agent 状态、`PATCH` 暂停/恢复自动回复、`DELETE …/memories[?key=]` 删私聊记忆，不碰群记忆）；后两者只经 `../affinity.py`、`../private_chat_bridge.py`，不导入 `yawn_agent` |
| `agent.py` + `agent_*_routes.py` | 群聊 Agent：配置、人设与人设库、记忆、隐私、关系、消息、调试/dry-run；共用过滤在 `agent_route_helpers.py` |
| `audits.py` | Agent 工具审计与 WebUI 操作审计（`web_admin_audit`） |
| `games.py` | 狼人杀/跑团对局列表、详情、事件日志与回放（狼人杀快照的新字段一律 `getattr` 容错，玩法子插件先后上线都不报错） |
| `werewolf_admin.py` | 狼人杀管理台只读端点：`/games/werewolf/meta`（板子/角色/配置/命令）、`/stats`（战绩聚合，纯函数 `compute_stats`）、`/history/{id}`、`/replay/{回放编号}` |
| `rpg_modules.py` | 跑团模组库只读投影 |
| `fanqie.py` | 番茄下载任务管理 |
| `environment.py` / `environment_routes.py` | 根 `.env` 脱敏读取与原子写入、配置体检（`diagnostics`）、锁门保护、LLM 连通性测试、`/system/status` 与 `/system/restart`（仅 `YAWNBOT_SUPERVISED=1` 时可用：SIGTERM 后由容器 restart 策略拉起） |
| `environment_schema.py` | 纯函数：从目录默认值/说明推断类型、范围（如“1-168”“最大 120”）、格式（秒/字节/列表），写入校验与 Token 强度提示；前端 `environment-fields.ts` 镜像同一套规则 |
| `environment_catalog.dotenv` | 「环境配置」页的完整配置项目录（**不是** `.env.example`） |
| `guest_access.py` / `guest_access_routes.py` | 持久化访客访问策略 |
| `schedules.py` | 「定时任务」管理页：状态/任务列表与详情/手动新建（以某位群成员名义，不受数量上限）/编辑（`revision` 乐观锁，409）/暂停恢复取消/立即执行（后台）/批量/规则预览/运行记录/策略，全部仅管理员；表未迁移时只读接口返回 `ready:false`，写接口 503 |
| `sandbox.py` | 「代码沙箱」管理页：状态/统计/运行历史/工作区文件/控制台/策略，全部仅管理员；runner 离线时只读接口降级为 200 + `online:false`，写接口 503；文件下载强制 `nosniff` + CSP sandbox，HTML/SVG 强制附件 |
| `web_search.py` | 「网页搜索」管理页：状态（密钥只回显来源与掩码）/策略与限额/今日用量与费用/最近调用/试搜试读/清空缓存，全部仅管理员；试搜计入全站计费统计但不占群配额 |
| `stickers.py` | 「表情包」管理页：库（上传/编辑/启停/审核/批量/删除、按群开放）、发送与收藏策略、今日用量、提示词预览，全部仅管理员 |

## 关联位置（跨目录）

| 后端 | 依赖的业务模块 | 前端页面（`webui/src/`） |
|---|---|---|
| `agent*.py`（`agent_prompt_routes.py`：提示词分段目录、覆盖读写/批量/导出、Trace 完整提示词） | [yawn_agent/](../yawn_agent/CLAUDE.md)（`execution_trace.py`、`prompt_sections.py`、`prompt_capture.py`、`speech_runtime.py` 等）、Agent 相关 [data_models](../data_models/CLAUDE.md) | `agent.tsx`、`agent-panels/`、`agent-workspace/`、`agent-debug/`、`agent-prompts/` |
| `games.py` | [yawn_werewolf/](../yawn_werewolf/CLAUDE.md)（`game_log.py`）、[yawn_rpg/](../yawn_rpg/CLAUDE.md)、`../event_log.py`、`../replay.py` | `games.tsx`、`werewolf/` |
| `werewolf_admin.py` | `yawn_werewolf/roles.py`（`BOARDS`/`ROLE_FACTION`/`GOD_ROLES`/`ROLE_SKILL_TEXT`）、`config.py`、`command_definitions.py`、`models.py`、`../replay.py` | `werewolf/` |
| `rpg_modules.py` | `yawn_rpg/module_schema.py`、`yawn_rpg/modules/` | `modules.tsx` |
| `fanqie.py` | [yawn_fanqie/](../yawn_fanqie/CLAUDE.md) | `fanqie.tsx` |
| `environment*.py`、`environment_catalog.dotenv` | 各插件 `config.py`；文档 [docs/configuration/](../../../../docs/configuration/README.md) | `environment.tsx`、`environment-config.ts` |
| `sandbox.py` | `yawn_agent/sandbox_client.py`、`sandbox_policy.py`；runner 在仓库根 `sandbox/` | `sandbox/` |
| `web_search.py` | `yawn_agent/web_search_client.py`、`web_search_policy.py`、`web_search_output.py`、`tool_handlers/web.py` | — |
| `stickers.py` | `yawn_agent/sticker_library.py`、`sticker_policy.py`、`sticker_send.py` | — |
| `schedules.py` | `yawn_agent/schedule_store.py`、`schedule_policy.py`、`schedule_runner.py`、`schedule_spec.py` | `schedules/` |
| `groups.py`、`users.py`、`directory.py`、`guest_access*` | `../permission.py`、`../checkin_service.py`、核心 data_models | `group-workspace.tsx`、`directory/`、`guest-*.tsx` |

- 新增插件 `Config` 字段时，要登记进 `environment_catalog.dotenv`，`tests/test_webui_environment.py` 会检查。
- 测试：`tests/test_webui*.py`（`test_webui_spa.py` 需要先构建 `webui/dist`）。改 Agent 部分时另见 [yawn_agent/AGENTS.md](../yawn_agent/AGENTS.md) 的「质量门槛」。
