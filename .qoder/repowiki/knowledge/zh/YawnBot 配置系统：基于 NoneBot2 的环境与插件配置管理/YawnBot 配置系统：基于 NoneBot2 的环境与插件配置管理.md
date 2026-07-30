---
kind: configuration_system
name: YawnBot 配置系统：基于 NoneBot2 的环境与插件配置管理
category: configuration_system
scope:
    - '**'
source_files:
    - pyproject.toml
    - .env.dev
    - .env.prod
    - src/plugins/yawn_core/friend_approve.py
    - README.md
---

## 系统概述

该项目使用 **NoneBot2** 作为机器人框架，其配置系统围绕 `pyproject.toml`、`.env.*` 环境变量文件以及 NoneBot2 内置的驱动配置机制构建。项目采用多环境分离策略（开发/生产），通过 `.env.dev` 和 `.env.prod` 区分不同运行环境的配置。

## 核心配置文件与位置

- **`pyproject.toml`**：项目元数据、依赖声明、NoneBot2 插件注册、Ruff/Pyright 代码规范配置的核心文件
- **`.env.dev` / `.env.prod`**：环境变量配置文件，当前仅包含日志级别设置（`LOG_LEVEL=DEBUG`）
- **`src/plugins/yawn_core/`**：插件目录，通过 NoneBot2 的 `plugin_dirs` 机制加载

## 架构与设计决策

### 1. NoneBot2 插件化配置
项目在 `pyproject.toml` 的 `[tool.nonebot]` 段中声明插件目录为 `src/plugins`，并通过 `[tool.nonebot.adapters]` 和 `[tool.nonebot.plugins]` 分别注册 OneBot V11 适配器及多个官方插件（status、sentry、apscheduler、localstore、alconna、orm、htmlkit）。

### 2. 运行时配置获取
项目通过 `get_driver().config.superusers` 直接访问 NoneBot2 驱动的配置对象，用于权限控制（如好友申请审批命令的超级用户校验）。这表明核心配置项由 NoneBot2 框架统一管理，而非自定义配置解析逻辑。

### 3. 数据库配置
使用 `nonebot-plugin-orm[sqlite]` 提供 SQLite 数据库支持，数据库文件位于 `data/nonebot_plugin_orm/db.sqlite3`，迁移脚本存放在 `data/nonebot_plugin_orm/migrations/yawn_core/` 目录下。

### 4. 日志配置
通过 `.env.dev` 中的 `LOG_LEVEL=DEBUG` 控制日志级别，生产环境 `.env.prod` 为空，暗示生产环境应通过外部环境变量注入或默认值管理。

## 开发者约定与约束

1. **插件开发**：所有插件必须放在 `src/plugins/` 目录下，每个插件以独立子模块形式组织
2. **配置来源**：优先使用 NoneBot2 驱动配置（`get_driver().config`），避免硬编码配置值
3. **环境变量**：敏感配置（如 API Key、数据库连接串）应通过 `.env.*` 文件或外部环境变量注入
4. **数据库模型**：数据模型定义在 `src/plugins/yawn_core/data_models/` 下，遵循 SQLAlchemy ORM 规范
5. **代码规范**：统一使用 Ruff 进行 linting/formatting，Pyright 进行类型检查，配置集中在 `pyproject.toml` 中

## 注意事项

- 项目未实现自定义配置加载器，完全依赖 NoneBot2 的内置配置机制
- 环境变量文件目前仅包含日志级别，其他配置项（如 OneBot 连接信息、Sentry DSN 等）可能通过 NoneBot2 的标准方式配置
- 缺少统一的配置验证和文档化机制，新增配置项需手动确保格式正确性