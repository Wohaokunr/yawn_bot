---
kind: external_dependency
name: NoneBot2 框架
slug: nonebot2
category: external_dependency
scope:
    - '**'
source_files:
    - pyproject.toml
    - README.md
---

YawnBot 基于 NoneBot2 框架构建，作为 OneBot 协议的 Python 适配器运行。项目通过 `nb create` 脚手架生成，插件位于 `src/plugins` 目录，使用 `nb run --reload` 启动。核心能力包括事件预处理（`event_preprocessor`）、插件机制、以及与其他 nonebot 插件的集成。