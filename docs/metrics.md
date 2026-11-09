# 运行指标（P1-6 / RPG 新手引导与推理）

YawnBot 的运行指标是进程内聚合值，不依赖 Prometheus SDK、网络服务或新增数据库表。
进程重启后指标清零；事件日志和 ORM 仍是各自独立的持久化边界。

## 读取方式

管理面或外部适配器可以直接读取 JSON 快照，或读取 Prometheus text exposition：

```python
from yawn_core.metrics import render_prometheus, snapshot_metrics

snapshot = snapshot_metrics()
prometheus_text = render_prometheus()
```

当前版本只提供进程内读取 API，不新增公开 HTTP 路由，也不把指标混入 P1-7
回放投影。读取适配器应自行负责鉴权和暴露地址。

## 指标目录

| 指标 | 类型 | 标签 | 含义 |
| --- | --- | --- | --- |
| `yawnbot_queue_rejections_total` | counter | `component`, `game_kind`, `reason` | RPG/狼人杀动作队列、事件日志 writer 和对话队列拒绝数 |
| `yawnbot_event_log_write_failures_total` | counter | `game_kind` | JSONL 旁路写入失败数 |
| `yawnbot_game_phase_duration_seconds` | histogram | `game_kind`, `phase` | 已完成游戏阶段的耗时 |
| `yawnbot_ai_requests_total` | counter | `operation`, `outcome` | AI 请求结果分布 |
| `yawnbot_ai_request_duration_seconds` | histogram | `operation` | AI 请求延迟 |
| `yawnbot_ai_degradations_total` | counter | `component`, `reason` | 固定兜底、超时、并发拥塞和发送失败等降级 |
| `yawnbot_game_endings_total` | counter | `game_kind`, `outcome`, `ending`, `winner` | RPG 结局和狼人杀胜方分布 |
| `yawnbot_rpg_tutorial_total` | counter | `step`, `outcome` | RPG 引导开始、步骤展示、完成和跳过 |
| `yawnbot_rpg_deductions_total` | counter | `outcome` | 推论发起、确认、成功、失败和撤回 |
| `yawnbot_rpg_terminations_total` | counter | `reason` | RPG 正常结束、手动结束、服务器中断和引擎异常 |

未使用的可选标签不会被补成空字符串；每个指标只输出实际产生过的 series。
阶段耗时的内部计时账本使用稳定 `game_id` 做键，但 `game_id` 不会进入任何公开
指标标签。

## AI outcome

共享非流式调用记录 `complete` 和 `complete_with_tools`；对话模块记录
`chat_stream`。常见结果包括 `success`、`timeout`、`empty`、`not_configured`、
`error`、`cancelled`，流式部分响应会使用 `*_partial`，并发额度耗尽使用
`concurrency_timeout`。所有结果只记录状态和耗时，不记录提示词、模型响应或密钥。

`yawnbot_ai_degradations_total{component="llm"}` 的 `reason` 是细分后的失败原因：
`timeout`、`empty`、`rate_limited`（限流）、`server`（服务商 5xx）、`connection`、
`auth`（密钥无效/无权限）、`quota`（余额或额度用完）、`bad_request`、
`context_length`（上下文超长）、`not_configured`、`cancelled`、`unknown`。前六种会被
Agent 原样重试一次，后面的重试没有意义，直接降级。

非流式调用分两条并发车道：有人在等回复的实时回合，和后台调用（记忆整理、主动发言、
私聊后台小助手、任务汇报、主动提议）。全局同时最多 6 个请求，其中后台最多占 4 个，
剩下的始终留给实时回合。`llm.concurrency_snapshot()` 返回两条车道的调用数、排队次数、
排队超时次数和平均/最长等待。

## 标签和故障边界

- 指标标签只接受受限标识符和白名单字段。
- `game_id`、`group_id`、`user_id`、QQ 号、`actor_id` 等高基数字段会被拒绝。
- 指标更新失败会被吞掉，不改变游戏裁决、AI fallback、消息发送或事件日志写入。
- 事件日志 writer 队列满时只增加拒绝计数并丢弃旁路事件，不阻塞 RPG/狼人杀引擎。
