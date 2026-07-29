---
kind: error_handling
name: YawnBot 错误处理机制
category: error_handling
scope:
    - '**'
source_files:
    - src/plugins/yawn_core/checkin.py
    - src/plugins/yawn_core/info.py
    - src/plugins/yawn_core/__init__.py
---

## 错误处理系统概述

该 YawnBot 项目基于 NoneBot2 框架，采用轻量级的错误处理方式，主要依赖 Python 原生异常机制和特定库的异常类型。

## 使用的错误处理模式

### 1. 数据库异常处理
- **IntegrityError**: 在签到功能中捕获数据库唯一约束冲突，用于防止重复签到
- 使用 try-except 块包裹 `session.flush()` 操作
- 发生异常时执行 `session.rollback()` 回滚事务

### 2. API 调用异常处理
- **ActionFailed**: 捕获 OneBot v11 适配器中的 API 调用失败异常
- 通过检查异常的 `info` 字段获取详细的错误信息
- 根据具体的错误类型（如"同名文件夹已存在"）提供用户友好的响应

### 3. 日志记录
- 使用 `nonebot.logger` 进行结构化日志记录
- 关键操作（如新用户首次交互、签到成功等）都有相应的日志输出

## 核心文件与位置

- `src/plugins/yawn_core/checkin.py`: 包含数据库完整性错误的处理逻辑
- `src/plugins/yawn_core/info.py`: 包含 OneBot API 调用的错误处理
- `src/plugins/yawn_core/__init__.py`: 事件预处理器的错误处理

## 架构约定

1. **局部异常处理**: 每个业务模块独立处理其特定的异常情况
2. **用户友好响应**: 将技术错误转换为普通用户可理解的消息
3. **事务一致性**: 数据库操作失败时确保事务回滚
4. **详细日志记录**: 便于问题诊断和调试

## 开发者规范

- 对于数据库操作，应捕获 `IntegrityError` 并适当回滚事务
- 对于外部 API 调用，应捕获 `ActionFailed` 并解析错误信息
- 所有重要操作都应记录适当的日志信息
- 避免使用全局异常处理器，保持错误处理的局部性和明确性