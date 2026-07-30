---
kind: logging_system
name: 基于 NoneBot2 内置日志器的统一日志系统
category: logging_system
scope:
    - '**'
source_files:
    - src/plugins/yawn_core/checkin.py
    - src/plugins/yawn_core/friend_approve.py
    - src/plugins/yawn_core/presence.py
---

本项目采用 NoneBot2 框架自带的日志系统，未引入第三方日志库（如 loguru、structlog），所有插件模块统一通过 `nonebot` 或 `nonebot.log` 提供的 `logger` 对象进行日志输出。

**使用的框架与工具**
- 日志来源：`from nonebot import logger` 或 `from nonebot.log import logger`
- 日志级别：项目中主要使用 `logger.info()` 和 `logger.debug()`，在异常场景下使用 `logger.warning()`
- 无自定义日志配置：项目未在 `pyproject.toml`、`.env.dev`、`.env.prod` 中定义任何日志格式、输出目标或级别配置，完全依赖 NoneBot2 的默认行为

**核心使用模式**
- 模块加载时记录初始化信息：如 `checkin.py` 中的 `logger.info("签到模块已加载")`
- 业务事件记录：用户操作、好友申请、首次对话等关键节点均通过 `logger.info()` 记录结构化文本消息
- 调试信息：如 `friend_approve.py` 中使用 `logger.debug(superusers)` 输出超级用户列表
- 异常降级：获取群信息等外部 API 调用失败时使用 `logger.warning()` 记录警告而非抛出异常

**日志内容约定**
- 采用 f-string 拼接的纯文本格式，包含用户 ID、群组 ID、操作结果等上下文信息
- 未使用结构化字段（JSON 格式）或专用字段名，日志可读性依赖于固定模板
- 时间戳由 NoneBot2 自动添加，无需手动注入

**架构决策**
- 日志器作为全局单例在各模块间共享，无需显式传递
- 未实现日志分级输出（开发/生产环境区分）、未配置文件输出或远程收集
- 错误处理以 try/except + warning 为主，未使用专门的错误日志通道

**开发者应遵循的规范**
1. 统一从 `nonebot` 或 `nonebot.log` 导入 `logger`，避免使用 Python 标准库 `logging` 模块
2. 关键业务节点（用户注册、签到成功、好友申请处理等）必须记录 `info` 级别日志
3. 外部 API 调用失败时使用 `warning` 级别记录，保证服务可用性
4. 调试信息使用 `debug` 级别，便于开发阶段排查问题
5. 日志消息应包含足够的上下文信息（user_id、group_id 等标识符）