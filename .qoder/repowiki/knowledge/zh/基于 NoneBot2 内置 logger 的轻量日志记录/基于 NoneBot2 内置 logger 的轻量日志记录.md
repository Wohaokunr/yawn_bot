---
kind: logging_system
name: 基于 NoneBot2 内置 logger 的轻量日志记录
category: logging_system
scope:
    - '**'
source_files:
    - src/plugins/yawn_core/__init__.py
    - src/plugins/yawn_core/checkin.py
---

本项目未引入独立的日志框架（如 loguru、structlog），而是直接使用 NoneBot2 提供的 `nonebot.logger` 作为唯一日志输出入口。所有日志均为字符串拼接形式的 `logger.info(...)` 调用，未使用结构化字段或统一格式化器。

**系统架构**
- 日志来源：NoneBot2 内置 logger，通过 `from nonebot import logger` 导入
- 依赖关系：uv.lock 中虽出现 loguru 条目，但代码中并未实际使用，属于间接依赖
- 输出位置：由 NoneBot2/OneBot 适配器决定，项目未自定义 handler 或 formatter

**使用模式**
- 模块加载时记录初始化信息（如 `logger.info("签到模块已加载")`）
- 业务事件发生时记录 info 级别日志（新用户首次对话、群内首次发言、签到成功等）
- 全部使用 f-string 拼接参数，无结构化字段、无异常捕获日志、无 debug/warning/error 分级

**约定与限制**
- 开发者应通过 `from nonebot import logger` 获取日志实例
- 仅使用 `logger.info()`，未见其他级别的日志调用
- 日志内容为人类可读的中文消息，非 JSON 结构化格式
- 无统一的日志配置、无文件输出、无远程收集（sentry 插件存在但未用于日志）

**建议改进方向**
- 可考虑引入 structlog 实现结构化日志，便于后续接入 ELK/Loki
- 增加 error/warning/debug 分级，区分正常流程与异常情况
- 为关键路径添加异常堆栈日志，便于问题定位