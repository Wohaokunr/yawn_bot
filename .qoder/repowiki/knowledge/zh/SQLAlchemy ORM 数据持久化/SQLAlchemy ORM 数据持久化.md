---
kind: external_dependency
name: SQLAlchemy ORM 数据持久化
slug: sqlalchemy-sqlite
category: external_dependency
category_hints:
    - sdk_real_api
scope:
    - '**'
source_files:
    - src/plugins/yawn_core/data_models/bot_user.py
    - src/plugins/yawn_core/data_models/user_group.py
    - src/plugins/yawn_core/data_models/bot_group.py
    - src/plugins/yawn_core/data_models/friend_request.py
---

使用 SQLAlchemy ORM 配合 SQLite 数据库，通过 nonebot_plugin_orm 提供异步会话管理。模型继承自 `Model`，支持 `Mapped` 类型注解。会话通过 `async_scoped_session` 参数注入到处理器中。注意：commit 后对象属性会过期，需要重新查询或提前缓存值；时区处理需使用 naive datetime 以兼容 SQLite。