# 对局事件日志（P1-5）

RPG 与狼人杀共用 `yawn_core.event_log` 的 JSONL 事件 envelope。事件日志是
旁路观测数据，不替代 ORM 对局表，也不参与引擎裁决。

## 存储与导出

运行时文件位于 `nonebot-plugin-localstore` 的插件目录：

```text
<plugin-data-dir>/game-events/{rpg|werewolf}-{game_id}.jsonl
```

每个内存对局在创建时生成独立的 `event_log_id`，因此即使 ORM 开局写入失败，
事件仍能在同一局内保持稳定关联。程序内导出接口为：

```python
from yawn_core.event_log import export_events, export_events_jsonl

events = export_events("GAME_ID", game_kind="rpg")
jsonl = export_events_jsonl("GAME_ID", game_kind="rpg")
```

调用导出前，若代码刚记录过事件，应在异步上下文中先执行
`await flush_events()`；导出结果按 `sequence` 排序。

## Envelope

每行包含以下稳定字段：

| 字段 | 含义 |
| --- | --- |
| `schema_version` | envelope 版本，当前为 `1` |
| `event_id` | 事件唯一标识 |
| `game_kind` / `game_id` | 玩法和单局标识 |
| `sequence` | 单局内单调递增序号 |
| `occurred_at` | UTC ISO-8601 时间 |
| `event_type` | 结构化事件类型 |
| `phase` / `round` | 事件发生时的玩法阶段和回合快照 |
| `actor_seat` | 可选座位号，不记录 QQ 号 |
| `payload` | 仅包含白名单 ID、枚举、计数和布尔值 |

当前接入的事件包括：对局创建/开始/结束、阶段切换、RPG 场景进入、RPG
具名事件触发，以及两种玩法的结构化行动接收。行动只记录类型和座位，
不记录 `aux` 正文。

RPG 还记录服务中断、新手引导步骤和联合推理的发起/确认/成败/撤回。推理事件
只包含推论或线索 id，不记录玩家输入的原始结论；引导事件只包含步骤 id。

## 隐私与故障语义

- 不写入提示词、AI 原始响应、密钥、签名、Cookie、QQ `user_id`、私聊正文、
  群聊正文、个人线索/秘密正文或角色卡正文。
- payload 使用固定白名单；未知字段会被丢弃，字符串字段只接受受限标识符。
- writer 使用有界队列和单顺序 worker；磁盘不可写时记录诊断并丢弃该事件，
  不把异常传播回 RPG/狼人杀引擎。
- 队列满时只丢弃事件并记录告警，不等待背压；拒绝数量和写入失败分别记录到
  `yawnbot_queue_rejections_total` 与 `yawnbot_event_log_write_failures_total`。
- 阶段耗时、AI 延迟/超时/降级和结局分布见
  [`docs/metrics.md`](metrics.md)；公开/个人视角回放见
  [`docs/game-replay.md`](game-replay.md)。
