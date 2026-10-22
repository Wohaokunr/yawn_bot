# tests — 主 pytest 套件

仓库级回归测试。`pyproject.toml` 的 `testpaths = ["src/plugins", "tests"]`，所以
`uv run pytest -q` 会跑本目录加各插件包内的 `tests/`（如 `yawn_rpg/tests/`、`yawn_agent/tests/`）。
RPG 编辑器与试玩器的测试在 `tools/*/tests/`，不在默认范围，要按路径显式运行（见 [tools/CLAUDE.md](../tools/CLAUDE.md)）。

## 运行

```bash
uv sync --all-groups --locked --python 3.10
uv run --python 3.10 pytest -q                                   # CI 同款（CI 另行忽略 test_webui_spa.py）
uv run --python 3.10 pytest -q tests/test_agent_outbound.py      # 单文件
```

- CI 全部用 **Python 3.10**（`.github/workflows/ci.yml`），并按改动范围拆分 job；`test_ci_pytest_partition.py` 会检查这套拆分和 `testpaths`/markers 配置，改 CI 或 pytest 配置时一起看。
- `test_webui_spa.py` 需要先在 `webui/` 执行 `npm run build`，否则本地失败属正常。
- Ruff 也检查 `tests/`，新测试文件要过 lint；pyright 不查 `tests/`。
- 插件测试需要经 `nonebot.init()` 正式发现插件，所以多数子插件测试放在这里而不是插件目录内。

## 按领域分组

| 前缀 | 覆盖 | 被测代码 |
|---|---|---|
| `test_agent_*`、`test_group_agent.py` | 群聊 Agent：上下文、记忆、发言、媒体、安全、出站发送、追踪 | [yawn_agent/](../src/plugins/yawn_core/yawn_agent/CLAUDE.md)（必跑项见其 AGENTS.md「质量门槛」） |
| `test_werewolf_*` | 狼人杀流程、AI、命令体验、泄漏修复、game_log | [yawn_werewolf/](../src/plugins/yawn_core/yawn_werewolf/CLAUDE.md) |
| `test_rpg_*` | 跑团推理与回归 | [yawn_rpg/](../src/plugins/yawn_core/yawn_rpg/CLAUDE.md)（另有插件内 `yawn_rpg/tests/`） |
| `test_fanqie_*` | 番茄 App 协议/签名、浏览器 sidecar、投递、回归 | [yawn_fanqie/](../src/plugins/yawn_core/yawn_fanqie/CLAUDE.md) |
| `test_webui*` | WebUI 后端各路由、Hub、环境配置目录、写操作回归、SPA 托管 | [yawn_core/webui/](../src/plugins/yawn_core/webui/CLAUDE.md) |
| `test_ai_chat_concurrency.py`、`test_chat_state.py`、`test_llm_lazy.py` | 私聊 AI 与共享 LLM 客户端 | `yawn_core/ai_chat.py`、`chat_state.py`、`llm.py` |
| `test_command_catalog.py`、`test_command_visuals.py`、`test_htmlkit_font_contract.py`、`test_session_expiry_regressions.py` | 命令目录、帮助/面板渲染、多轮会话 | [yawn_core/](../src/plugins/yawn_core/CLAUDE.md) 命令与 `ui/`、`templates/` |
| `test_game_event_log.py`、`test_game_replay.py`、`test_metrics.py`、`test_reminder.py`、`test_plugin_startup_report.py` | 跨玩法基础设施、提醒、子插件加载报告 | `yawn_core/event_log.py`、`replay.py`、`metrics.py`、`reminder.py`、`__init__.py` |
| `test_production_*`、`test_deployment_cache_contract.py` | 部署脚本与发布链路契约 | [deploy/](../deploy/CLAUDE.md)、`Dockerfile`、`.github/workflows/` |
| `test_ci_pytest_partition.py` | CI 与 pytest 配置自检 | `.github/workflows/ci.yml`、`pyproject.toml` |
