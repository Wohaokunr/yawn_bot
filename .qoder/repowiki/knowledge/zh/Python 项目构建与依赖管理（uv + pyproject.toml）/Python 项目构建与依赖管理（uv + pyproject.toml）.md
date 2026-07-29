---
kind: build_system
name: Python 项目构建与依赖管理（uv + pyproject.toml）
category: build_system
scope:
    - '**'
source_files:
    - pyproject.toml
    - uv.lock
    - .env.dev
    - .env.prod
---

该项目采用现代 Python 包管理方案，基于 `pyproject.toml` 作为唯一构建配置入口，使用 `uv` 作为依赖解析与锁定工具，配合 Ruff 进行代码质量检查、Pyright 进行类型检查。

**核心系统与工具链**
- 包元数据与依赖声明：`pyproject.toml` 中通过 `[project]` 定义项目名称、版本、Python 版本约束（>=3.10, <4.0）及运行时依赖；`[dependency-groups]` 和 `[project.optional-dependencies]` 管理开发依赖（pyright、ruff）。
- 依赖锁定：`uv.lock` 记录所有依赖的精确版本与哈希值，确保构建可重现。
- NoneBot2 框架集成：`[tool.nonebot]` 配置插件目录为 `src/plugins`，注册 OneBot V11 适配器及多个官方/第三方插件（status、sentry、apscheduler、localstore、alconna、orm、htmlkit）。
- 代码质量：Ruff 统一负责 lint 与格式化，启用 Pyflakes、Pylint、isort、pyupgrade、flake8-bugbear 等规则集；Pyright 配置类型为 standard 模式。

**构建与运行约定**
- 无传统 Makefile/Dockerfile/CI 流水线，项目以纯 Python 脚本形式运行，通过 `uv run` 或 `python -m nonebot` 启动。
- 环境隔离通过 `.env.dev` / `.env.prod` 环境变量文件区分开发与生产配置。
- 数据库迁移由 Alembic 驱动（见 `data/nonebot_plugin_orm/migrations`），ORM 插件使用 SQLite 后端。

**开发者规范**
- 新增依赖需修改 `pyproject.toml` 后执行 `uv lock` 更新锁定文件。
- 代码提交前需通过 `ruff check` 与 `ruff format` 校验，类型检查使用 `pyright`。
- 插件必须放在 `src/plugins` 目录下，并在 `[tool.nonebot.plugins]` 中注册模块映射。
- 禁止直接安装依赖到全局环境，始终通过 uv 虚拟环境工作。