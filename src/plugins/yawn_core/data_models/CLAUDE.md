# data_models — 核心 ORM 模型

yawn_core 与群聊 Agent 共用的 SQLAlchemy 2.0 模型（`nonebot_plugin_orm.Model`）。
子插件自己的表不在这里：狼人杀、跑团、番茄各在自己的 `models.py`。

## 规则

- 表名自动加插件前缀（`UserGroup` → `yawn_core_usergroup`），`__table_args__` 里的外键要写带前缀的表名。
- 时间一律用 naive 北京时间，默认值走 `timeutil.py`；不要引入 aware datetime 或 UTC。
- **新模型文件必须在 `__init__.py` 导入**，否则不会注册、autogenerate 也看不到。
- 改模型后只提醒用户生成并应用迁移，**不要自己跑 `nb orm`**。迁移正本与多分支的坑见 [data/CLAUDE.md](../../../../data/CLAUDE.md) 和 [根 CLAUDE.md](../../../../CLAUDE.md)。
- commit 后读取 ORM 属性（包括 `.id`）会触发 `MissingGreenlet`（`expire_on_commit=True`）：需要的值在 commit 前缓存或 `refresh`。

## 文件分组

| 分组 | 文件 | 主要使用者 |
|---|---|---|
| 用户/群身份 | `bot_user.py`、`bot_group.py`、`user_group.py` | `presence.py` 写入，几乎所有表的外键根 |
| 权限开关 | `group_feature.py`、`user_feature.py`、`global_user_feature.py` | `permission.py` |
| 签到 | `checkin_user.py`、`checkin_record.py` | `checkin.py` |
| 好友申请 | `friend_request.py` | `friend_approve.py` |
| 定时提醒 | `scheduled_reminder.py` | `reminder.py` |
| 私聊 AI 对话 | `chat_session.py`、`chat_message.py`（`is_deleted` 软删除） | `ai_chat.py`、`chat_state.py` |
| 群聊 Agent | `group_agent_config.py`、`group_agent_message.py`、`agent_memory.py`、`agent_audit.py`、`agent_media_asset.py`、`agent_media_cache.py` | [yawn_agent/](../yawn_agent/CLAUDE.md)、WebUI `agent*_routes.py` |
| WebUI | `web_admin_audit.py`、`guest_access.py` | [webui/](../webui/CLAUDE.md) |
| 工具 | `timeutil.py` | 所有模型的时间默认值 |

## 关联位置

- WebUI 读写这些表主要经 [webui/service.py](../webui/CLAUDE.md)。
- 迁移测试：`tests/test_agent_persona_migration.py`、`tests/test_agent_persona_cleanup_migration.py`。
