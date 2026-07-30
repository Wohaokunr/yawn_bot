---
kind: dependency_management
name: 基于 uv 的 Python 依赖管理
category: dependency_management
scope:
    - '**'
source_files:
    - pyproject.toml
    - uv.lock
---

本项目使用 **uv** 作为 Python 包管理器，结合 `pyproject.toml` 声明依赖，并通过 `uv.lock` 锁定精确版本，确保构建可重复性。

### 系统与工具
- **包管理器**: uv（现代 Python 包解析与安装工具）
- **依赖声明**: PEP 621 标准的 `pyproject.toml`
- **锁文件**: `uv.lock`，记录所有依赖及其子依赖的精确版本、哈希值与来源
- **Python 版本约束**: `requires-python = ">=3.10, <4.0"`

### 关键文件与结构
- `pyproject.toml`: 项目元数据、运行时依赖、可选依赖（dev）、NoneBot2 插件配置、Ruff/Pyright 代码规范配置
- `uv.lock`: 完整依赖树锁定文件，包含每个包的版本、源码 URL、SHA256 校验和及多平台 wheel
- `.env.dev` / `.env.prod`: 开发/生产环境配置分离

### 架构与约定
- **运行时依赖**集中在 `[project].dependencies`，包括 NoneBot2 生态插件（onebot 适配器、status、sentry、apscheduler、localstore、alconna、orm、htmlkit）
- **开发依赖**通过 `[dependency-groups].dev` 或 `[project.optional-dependencies].dev` 管理，包含 pyright 与 ruff
- **NoneBot2 插件注册**在 `[tool.nonebot.plugins]` 中集中声明，将包名映射到模块名
- **无 vendoring**：依赖直接从 PyPI（`https://pypi.org/simple`）拉取，未使用私有仓库或本地 vendor 目录
- **无 requirements.txt**：完全采用 PEP 621 + uv.lock 的现代方案

### 开发者规则
- 新增依赖时修改 `pyproject.toml` 的 `dependencies` 列表，然后运行 `uv lock` 更新 `uv.lock`
- 不要手动编辑 `uv.lock`，它由 uv 自动生成
- 开发环境与生产环境通过 `.env.*` 文件区分，但依赖本身不区分环境
- 插件依赖通过 `[tool.nonebot.plugins]` 显式注册，避免动态导入问题
- 保持 `requires-python` 与实际运行环境一致