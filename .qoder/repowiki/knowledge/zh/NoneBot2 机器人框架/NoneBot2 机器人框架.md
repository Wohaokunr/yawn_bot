---
kind: external_dependency
name: NoneBot2 机器人框架
slug: nonebot2
category: external_dependency
category_hints:
    - framework_behavior
scope:
    - '**'
source_files:
    - pyproject.toml
    - src/plugins/yawn_core/__init__.py
---

项目基于 NoneBot2 构建，通过 `nb create` 初始化，插件位于 `src/plugins` 目录。使用 FastAPI 驱动，OneBot V11 适配器连接 QQ。事件处理通过 `on_request`、`on_command`、`event_preprocessor` 等装饰器注册，依赖注入通过函数参数自动解析（如 `async_scoped_session`、`Bot`）。配置通过 `.env` 文件加载，`superusers` 为 `set[str]` 类型。