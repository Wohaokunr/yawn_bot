---
kind: dependency_management
name: Python 依赖管理（uv + pyproject.toml）
category: dependency_management
scope:
    - '**'
source_files:
    - pyproject.toml
    - uv.lock
---

本项目使用 **uv** 作为 Python 包管理器，配合标准的 **PEP 517/621** 项目配置进行依赖声明与锁定。核心流程如下：

### 1. 使用的工具与系统
- **包管理器**: `uv`（现代、极速的 Python 包安装器，替代 pip/pipenv）
- **项目元数据与依赖声明**: `pyproject.toml`（PEP 517/621 标准）
- **依赖锁定文件**: `uv.lock`（由 uv 生成，包含所有依赖及其精确版本与哈希校验）
- **Python 版本约束**: `requires-python = ">=3.10, <4.0"`

### 2. 关键文件与位置
- `pyproject.toml` — 定义项目名称、版本、运行时依赖、可选依赖（dev）、NoneBot 插件注册、Ruff/Pyright 等工具配置
- `uv.lock` — 完整的依赖解析结果，包含每个包的精确版本、来源（PyPI）、sdist/wheel 哈希值，确保可重复构建
- `.env.dev` / `.env.prod` — 开发/生产环境配置分离（非依赖文件，但影响运行期行为）

### 3. 架构与约定
- **依赖分组**:
  - `dependencies`: 生产环境必需依赖（nonebot2 生态及 ORM、调度、状态监控等插件）
  - `dependency-groups.dev`: 开发工具链（pyright、ruff），通过 `uv sync --group dev` 安装
- **插件即依赖**: NoneBot 插件以普通 PyPI 包形式声明在 `dependencies` 中，并在 `[tool.nonebot.plugins]` 中映射模块名，实现插件与依赖的统一管理
- **适配器注册**: `[tool.nonebot.adapters]` 中显式注册 OneBot V11 适配器，避免自动发现的不确定性
- **本地插件扩展点**: `"@local"` 占位符预留本地插件目录，便于团队内共享未发布插件
- **无 vendoring**: 不将第三方包复制到仓库，完全依赖 uv.lock 锁定版本
- **无私有源**: 所有包均从 `https://pypi.org/simple` 获取，未配置私有镜像或代理

### 4. 开发者应遵循的规则
- **新增依赖**: 直接在 `pyproject.toml` 的 `dependencies` 或 `dependency-groups.dev` 中添加，然后运行 `uv sync` 更新 `uv.lock`
- **不要手动编辑 `uv.lock`**: 该文件由 uv 自动生成和验证，手动修改会导致一致性检查失败
- **依赖版本策略**: 使用语义化版本范围（如 `>=2.5.0`），由 uv 解析最新兼容版本并锁定到 lock 文件
- **插件开发**: 若开发自定义 NoneBot 插件，需同时声明为依赖并在 `[tool.nonebot.plugins]` 中注册
- **环境隔离**: 使用 `uv sync --group dev` 仅安装开发依赖，生产部署时只同步生产依赖
- **代码质量工具**: Ruff 和 Pyright 的配置集中在 `pyproject.toml` 中，无需额外配置文件