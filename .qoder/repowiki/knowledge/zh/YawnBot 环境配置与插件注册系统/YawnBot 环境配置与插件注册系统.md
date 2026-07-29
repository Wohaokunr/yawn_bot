---
kind: configuration_system
name: YawnBot 环境配置与插件注册系统
category: configuration_system
scope:
    - '**'
source_files:
    - pyproject.toml
    - .env.dev
    - .env.prod
---

## 配置系统概述

该项目基于 NoneBot2 框架构建，采用分层配置策略：项目级依赖与插件注册通过 `pyproject.toml` 管理，运行时环境变量通过 `.env.*` 文件注入。

## 核心配置机制

### 1. 项目元数据与依赖管理（pyproject.toml）
- 使用 PEP 621 标准定义项目信息、Python 版本要求（>=3.10, <4.0）
- 所有运行时依赖集中在 `[project.dependencies]` 中声明
- 开发工具链（pyright、ruff）通过 `[project.optional-dependencies]` 和 `[dependency-groups]` 分离管理

### 2. NoneBot2 框架配置
- **插件目录**：`plugin_dirs = ["src/plugins"]` 指定插件加载路径
- **内置插件**：`builtin_plugins = ["echo"]` 启用默认功能
- **适配器注册**：`[tool.nonebot.adapters]` 配置 OneBot V11 适配器
- **第三方插件**：`[tool.nonebot.plugins]` 映射包名到模块名，包括 status、sentry、apscheduler、localstore、alconna、orm、htmlkit 等

### 3. 环境变量管理
- **开发环境**：`.env.dev` 设置 `LOG_LEVEL=DEBUG`
- **生产环境**：`.env.prod` 存在但为空，需按需添加生产配置
- 遵循标准 `.env` 文件格式，由 NoneBot2 自动加载

### 4. 代码质量配置
- **Ruff**：统一代码风格（line-length=88）、格式化规则、lint 规则集（覆盖 F/W/E/I/C90/N/PL/UP 等 20+ 规则族）
- **Pyright**：类型检查配置，目标 Python 版本 3.9

## 架构约定

1. **插件即配置**：每个插件通过独立的 Python 包组织，在 `__init__.py` 中完成初始化
2. **ORM 模型集中管理**：数据库模型位于 `data_models/` 目录，迁移脚本独立存放
3. **环境隔离**：通过不同 `.env.*` 文件实现开发/生产环境分离
4. **依赖声明式**：所有外部依赖通过 `pyproject.toml` 统一管理，无手写 requirements.txt

## 开发者规范

- 新增插件需在 `pyproject.toml` 的 `[tool.nonebot.plugins]` 中注册
- 环境变量按环境分文件管理，敏感信息不提交至版本控制
- 代码风格统一遵循 Ruff 规则，类型注解使用 Pyright 检查
- ORM 变更需编写对应的 Alembic 迁移脚本