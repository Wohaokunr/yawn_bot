# 局后回放（P1-7）

P1-7 基于 P1-5 的 JSONL 事件日志提供只读回放，不读取 ORM 中的角色卡、聊天
正文、提示词、个人线索或 NPC 私密上下文。回放投影位于
`src/plugins/yawn_core/replay.py`，适配器可以直接调用：

```python
from yawn_core.replay import load_replay, render_replay

projection = load_replay("GAME_ID", view="public")
text = render_replay(projection)
payload = projection.as_dict()
```

## 视角

- `public`：显示创建、开局、公开阶段、RPG 场景、公开结局和可公开的结构化行动。
  狼人杀夜间子阶段折叠为“夜间”，夜间行动不进入公开回放。
- `personal`：在公开视角基础上增加指定座位自己的结构化行动；不会增加其他座位的
  夜间行动，也不会恢复任何原始正文。可信 API 适配器传入 `viewer_seat` 前应自行
  完成身份鉴权。
- OneBot `/回放` 命令只在群内提供公开视角。个人视角必须私聊发送，并使用当前进程
  在开局时登记的参与者映射解析当前账号座位；重启后没有该映射时不会把个人数据发出。

## 命令

正常结局的 RPG 系统回顾和狼人杀终局公示会显示回放编号。复制编号后：

```text
/回放 GAME_ID
/回放 GAME_ID 公开
/回放 GAME_ID 个人
```

也可以在私聊中附带座位号用于校验：`/回放 GAME_ID 个人 2号`。`GAME_ID` 是事件
日志中的稳定编号，不是 ORM 自增主键。

## 不可回放

`projection.available` 为 `false` 时，`reason` 会明确说明原因。没有事件文件、只有
部分事件、缺少创建事件或缺少终局事件的旧局均显示“本局不可回放”，不会把 ORM 局末
摘要伪装成事件重建结果。事件序列有缺口但仍包含完整创建/终局边界时可以生成回放，
同时在 `warnings` 中标注“回放可能不完整”。
