# YawnBot

YawnBot 是基于 NoneBot2 与 OneBot V11 的 QQ 机器人。`yawn_core` 提供统一的
权限、帮助、管理、签到、好友审批、定时提醒和 AI 对话能力，并加载可选的
跑团、狼人杀和番茄小说子插件。

## 当前能力

- 平台：三级功能开关、统一帮助/管理面板、用户与群活跃记录、定时提醒。
- RPG：YAML 模组、运行时 schema、Textual 编辑器、AI/KP 工具调用和确定性降级。
- 狼人杀：四种板子、完整昼夜流程、AI 玩家、安全超时与托管。
- 番茄小说：按书名/作者模糊搜索、浏览公开阅读榜/新书榜或输入链接/book ID 选择章节，后台生成 UTF-8 TXT，成品私发。
- 持久化：SQLite + nonebot-plugin-orm，保存平台数据、局末摘要和番茄任务断点；正文只短期落盘。

当前游戏房间、队列和局内状态只存在单进程内存中。项目不支持多实例共同消费同一
群的游戏动作，也不能在进程重启后恢复未结束的游戏。

## 环境要求

- Python 3.10 及以上（`pyproject.toml` 当前允许 `<4.0`）
- [uv](https://docs.astral.sh/uv/)
- OneBot V11 实现（反向 WebSocket 或正向 WebSocket 均可）
- 可选：OpenAI 兼容的模型服务

## 快速开始

```bash
git clone <repository-url>
cd YawnBot
uv sync --all-groups --locked
```

复制 `.env.example` 为 `.env`，至少确认 `SUPERUSERS` 和 OneBot 连接方式。项目默认
使用反向 WebSocket，OneBot 实现应连接：

```text
ws://127.0.0.1:8080/onebot/v11/ws
```

正向 WebSocket/HTTP 连接还需启用对应的 NoneBot driver mixin，示例见
`.env.example`。

首次部署或拉取含迁移的版本后，先停止 Bot 并备份数据库，再由维护者手动应用全部
迁移分支：

```bash
uv run nb orm heads
uv run nb orm upgrade heads
uv run nb orm current
```

启动机器人：

```bash
uv run nb run
```

开发时可使用 `uv run nb run --reload`。完整的生产部署、迁移和备份说明见
[部署与维护](docs/deployment.md)。

## AI 配置

`AI_API_KEY` 可选。未配置时机器人仍可启动：

- 私聊 AI 对话会提示服务未配置。
- 狼人杀 AI 无法请求模型时按现有安全策略托管，不会阻塞游戏。
- RPG 建议同时设置 `RPG_AI_ENABLED=false`，明确使用关键词检定和固定文案的
  确定性模式。

启用 AI 时设置：

```dotenv
AI_API_KEY=your-api-key
AI_BASE_URL=https://your-openai-compatible-endpoint/v1
AI_MODEL=your-model
```

不要提交真实密钥；`.env` 已被 Git 忽略。

## RPG 模组

模组位于 `src/plugins/yawn_core/yawn_rpg/modules/`。编辑器和校验器命令：

```bash
uv run python -m tools.rpg_module_editor
uv run python -m tools.rpg_module_editor --check \
  src/plugins/yawn_core/yawn_rpg/modules/before_tide_departs.yaml
```

打开编辑器后切换到“试玩”页（也可按 `F6`），可以选择当前草稿或已保存文件，
填写固定 `seed` 和目标结局，然后查看可复现的动作轨迹与 JSON。试玩器在后台运行，
不会启动 NoneBot、ORM 或 LLM；命令行也可以单独运行：

```bash
uv run python -m tools.rpg_playtest MODULE.yaml \
  --seed SEED --ending ENDING_ID [--players N] [--json]
```

模组格式说明见
[`src/plugins/yawn_core/yawn_rpg/modules/README.md`](src/plugins/yawn_core/yawn_rpg/modules/README.md)。

## 开发检查

```bash
uv run pytest -q
uv run ruff check src tests tools/rpg_module_editor tools/rpg_playtest
uv run pyright src tools/rpg_module_editor tools/rpg_playtest
uv run python -m compileall -q src tools/rpg_module_editor tools/rpg_playtest
git diff --check
```

正式插件发现冒烟检查：

```bash
uv run python -c "import nonebot; nonebot.init(); nonebot.load_from_toml('pyproject.toml'); required=('yawn_core','yawn_core:yawn_rpg','yawn_core:yawn_werewolf','yawn_core:yawn_fanqie'); missing=[name for name in required if nonebot.get_plugin(name) is None]; assert not missing, missing"
```

CI 在 Python 3.10 上执行相同的测试、静态检查、字节码编译和插件加载检查。

## 代码结构

```text
src/plugins/yawn_core/                 平台插件与共享基础能力
  data_models/                         平台 ORM 模型
  yawn_rpg/                            RPG 引擎、模组与测试
  yawn_werewolf/                       狼人杀引擎与 AI 驱动
  yawn_fanqie/                         番茄公开小说 provider、任务与 TXT 投递
tools/rpg_module_editor/               Textual 模组编辑器
tests/                                 跨模块与回归测试
data/nonebot_plugin_orm/migrations/    受版本控制的 canonical 迁移
migrations/versions/                   ORM CLI 同步/生成迁移时使用的工作目录
docs/                                  部署说明与架构图
```

`.qoder/repowiki` 是历史生成文档，可能滞后；涉及配置、路径和行为时以源码、
本 README 与 `docs/` 为准。
