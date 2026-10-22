# yawn_core — 主插件包

YawnBot 唯一的本地 NoneBot 插件。这里放核心功能模块（权限、帮助、面板、签到、
提醒、私聊 AI 对话、共享 LLM 客户端、对局日志/回放/指标），以及四个可选子插件和
WebUI 后端。全局规则（迁移、权限注入写法、Ruff 约定）见 [根 CLAUDE.md](../../../CLAUDE.md)。

## 加载方式

- `__init__.py` 显式 `from . import ...` 每个核心模块；**新模块不在这里导入就不会加载**。
- 子插件由 `_load_sub_plugins()` 按 `_SUB_PLUGIN_SPECS` 动态加载（狼人杀、跑团、番茄、
  群聊 Agent），失败只记日志并写入 `get_sub_plugin_load_report()`，不拖垮核心。
- 末尾 `from . import webui` 挂载 WebUI 后端；`on_startup` 给所有 SQLite 引擎开 WAL。

## 目录结构

| 路径 | 职责 | 文档 |
|---|---|---|
| `data_models/` | ORM 模型（核心 + Agent 的表都在这里） | [data_models/CLAUDE.md](data_models/CLAUDE.md) |
| `webui/` | WebUI 后端 FastAPI 路由与服务层 | [webui/CLAUDE.md](webui/CLAUDE.md) |
| `yawn_agent/` | 群聊 Agent 子插件 | [yawn_agent/CLAUDE.md](yawn_agent/CLAUDE.md) |
| `yawn_werewolf/` | 狼人杀子插件 | [yawn_werewolf/CLAUDE.md](yawn_werewolf/CLAUDE.md) |
| `yawn_rpg/` | 跑团子插件 | [yawn_rpg/CLAUDE.md](yawn_rpg/CLAUDE.md) |
| `yawn_fanqie/` | 番茄小说下载子插件 | [yawn_fanqie/CLAUDE.md](yawn_fanqie/CLAUDE.md) |
| `templates/` | HTMLKit 渲染用 HTML/CSS（帮助面板、命令面板、个人面板） | 由 `ui/panel_renderer.py`、`help_panel.py`、`panel.py` 使用 |
| `ui/` | `panel_renderer.py`：把 `templates/` 渲染成 QQ 图片卡片 | — |
| `migrations/` | **副本，勿改**。正本在 [data/nonebot_plugin_orm/migrations/](../../../data/CLAUDE.md) | — |

## 核心模块地图

**命令与权限**

| 模块 | 职责 |
|---|---|
| `permission.py` | 功能注册表 `FEATURE_REGISTRY`、三级权限解析链、`require_feature()` 依赖 |
| `command_definition.py` | 命令单一真相源：一份声明同时生成 matcher 与帮助元数据 |
| `command_catalog.py` | 类型化指令目录与已加载插件注册表（子插件加载失败时 `unregister_command_group`） |
| `command_access.py` | 命令权限与帮助可见性的共享上下文 |
| `command_ux.py`、`session_interaction.py` | 交互文案 helper；多轮命令的会话约定 |
| `help_panel.py` | 渐进式 `/help`：按分类和当前场景筛选可用命令 |
| `panel.py` / `panel_router.py` / `panel_menu.py` / `panel_data.py` | 统一管理面板：命令注册 / 交互路由 / 纯菜单定义 / 只读数据查询 |

**用户与群功能**

| 模块 | 职责 |
|---|---|
| `presence.py` | `event_preprocessor`：每条消息 upsert `BotUser`/`BotGroup`/`UserGroup`，其他表的外键都挂在这上面 |
| `checkin.py` | 签到 |
| `friend_approve.py` | 好友申请审批 |
| `reminder.py` | 群聊定时提醒（单文件较大，含解析、调度与持久化） |

**AI**

| 模块 | 职责 |
|---|---|
| `llm.py` | 共享 OpenAI 兼容客户端：懒加载、模型路由、非流式 `complete()` / `complete_with_tools()`、全局并发上限。被 `ai_chat`、[yawn_agent](yawn_agent/CLAUDE.md)、[yawn_rpg](yawn_rpg/CLAUDE.md)、[yawn_werewolf](yawn_werewolf/CLAUDE.md) 共用 |
| `ai_chat.py` | 私聊 `/对话` 模式（流式、按块空闲超时） |
| `chat_state.py` | 对话模式的每用户队列与后台 worker（自建 DB session） |
| `reply_chain.py` | QQ 嵌套引用链解析，供 `ai_chat` 构造上下文 |

**跨玩法基础设施**（狼人杀/跑团依赖）

| 模块 | 职责 |
|---|---|
| `game_registry.py` | 进程内跨玩法的群/用户占用登记（一群一局、一人一局） |
| `game_command_routing.py` | 同名短命令按群当前活跃玩法分发 |
| `event_log.py` | 轻量对局事件日志，设计见 [docs/game-event-log.md](../../../docs/game-event-log.md) |
| `replay.py` / `replay_commands.py` | 局后只读回放投影与命令，见 [docs/game-replay.md](../../../docs/game-replay.md) |
| `metrics.py` | 进程内运行指标，见 [docs/metrics.md](../../../docs/metrics.md) |
| `health.py` | 容器/反代用的健康检查端点 |

## 关联位置

- WebUI 对这些模块的管理视图在 [webui/](webui/CLAUDE.md)，前端页面在 [仓库根 webui/](../../../webui/CLAUDE.md)。
- 这些模块的测试大多在 [仓库根 tests/](../../../tests/CLAUDE.md)（如 `test_reminder.py`、`test_command_catalog.py`、`test_llm_lazy.py`、`test_plugin_startup_report.py`）。
- 配置项说明：[docs/configuration/core.md](../../../docs/configuration/core.md)、[ai-agent.md](../../../docs/configuration/ai-agent.md)。
