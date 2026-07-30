---
kind: build_system
name: 构建系统 — uv + pyproject.toml 现代化 Python 工程配置
category: build_system
scope:
    - '**'
source_files:
    - pyproject.toml
    - uv.lock
    - .env.dev
    - .env.prod
    - .gitignore
---

该 YawnBot 机器人项目采用现代化的 Python 工程构建体系，基于 **uv** 作为包管理器与依赖解析器，配合 **pyproject.toml** 进行统一的项目元数据、依赖声明和工具链配置。整个构建系统简洁且高度集成，没有传统的 Makefile、Dockerfile 或 CI/CD 配置文件。

## 核心构建工具链

- **包管理与依赖锁定**: 使用 `uv`（Rust 实现的高性能 Python 包管理器）替代 pip/pipenv，通过 `pyproject.toml` 声明依赖，`uv.lock` 锁定精确版本并包含完整的哈希校验
- **Python 版本约束**: 要求 `>=3.10, <4.0`，确保环境一致性
- **NoneBot2 框架集成**: 通过 `[tool.nonebot]` 配置插件目录为 `src/plugins`，自动加载 OneBot V11 适配器及多个官方插件

## 代码质量与静态检查

- **Ruff**: 统一的代码格式化和 linting 工具，支持 30+ 规则集（Pyflakes、Pylint、isort、flake8 等），行长度限制为 88，目标版本 py39
- **Pyright**: Microsoft 的 TypeScript 风格类型检查器，启用 standard 类型检查模式，支持跨平台类型推断
- **开发依赖组**: 通过 `dependency-groups.dev` 统一管理 pyright 和 ruff 等开发工具

## 插件化架构

项目采用 NoneBot2 的插件机制，所有业务逻辑以插件形式组织在 `src/plugins/yawn_core/` 目录下，包括签到、好友请求处理、用户状态管理等模块。插件通过 `pyproject.toml` 的 `[tool.nonebot.plugins]` 部分声明式注册。

## 数据库迁移

使用 Alembic 进行数据库版本管理，迁移文件位于 `data/ninebot_plugin_orm/migrations/yawn_core/` 和 `src/plugins/yawn_core/migrations/` 两个位置，支持 SQLite 后端。

## 环境变量管理

提供 `.env.dev` 和 `.env.prod` 两个环境配置文件，用于区分开发和生产环境的配置参数。

## 构建约定

- 无传统编译步骤，Python 解释型语言直接运行
- 依赖安装：`uv sync` 或 `uv pip install -e .[dev]`
- 代码检查：`ruff check` 和 `pyright`
- 无 Docker 容器化配置，无 CI/CD 流水线定义
- 无打包发布脚本，项目以源码形式分发