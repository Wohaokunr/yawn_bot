---
kind: error_handling
name: YawnBot 错误处理策略
category: error_handling
scope:
    - '**'
source_files:
    - src/plugins/yawn_core/checkin.py
    - src/plugins/yawn_core/presence.py
    - src/plugins/yawn_core/friend_approve.py
---

## 错误处理概述

该 YawnBot 机器人项目基于 NoneBot2 框架，采用**轻量级、分散式**的错误处理方式，没有统一的异常类型定义或全局错误中间件。错误处理主要依赖 Python 原生 `try/except` 和日志记录。

## 核心模式

### 1. 数据库操作错误
- **唯一约束冲突**: 在 `checkin.py` 中捕获 `IntegrityError`，通过 `session.rollback()` 回滚后返回友好提示 "你今天已经签到过了哦~"
- **会话管理**: 使用 `async_scoped_session` 自动管理事务生命周期，成功路径通过 `session.commit()` 提交

### 2. 外部 API 调用容错
- **降级处理**: 在 `presence.py` 中调用 OneBot API 获取群信息时，使用 `try/except Exception` 捕获所有异常，记录警告日志后继续执行
- **静默失败**: 对于非关键操作（如补填缺失的群名），异常被直接忽略，不影响主流程

### 3. 参数验证错误
- **即时反馈**: 在 `friend_approve.py` 中，对命令参数进行严格校验（如检查是否为数字），不合法时立即通过 `finish()` 返回错误提示
- **权限控制**: 通过检查用户是否在 `superusers` 列表中实现访问控制，无权限时直接返回

### 4. 日志记录策略
- **分层记录**: 使用 `logger.info()` 记录正常业务流程，`logger.warning()` 记录可恢复异常，`logger.debug()` 记录调试信息
- **上下文信息**: 日志包含关键业务标识（用户ID、群组ID等）便于问题追踪

## 架构特点

- **无统一异常体系**: 每个模块独立处理各自领域的异常
- **防御性编程**: 对外部依赖（API 调用、数据库约束）做充分保护
- **用户体验优先**: 用户可见的错误都转换为友好的中文提示
- **监控友好**: 关键路径都有日志记录，便于运维监控

## 开发者规范

1. **数据库操作**: 对可能违反约束的操作使用 `try/except IntegrityError` 包裹
2. **外部调用**: 所有 API 调用必须包裹在 `try/except Exception` 中
3. **参数验证**: 命令参数必须在处理前完成合法性检查
4. **日志记录**: 重要业务节点和异常都要有相应级别的日志
5. **用户反馈**: 所有用户可见的错误都必须提供清晰的中文提示