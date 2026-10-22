# docs — 项目文档

面向使用者、运维和开发者的说明文档。代码行为的权威描述仍在各目录的 `CLAUDE.md`；
这里的文档改动通常伴随功能、配置或部署变更一起提交。

## 结构

| 文件 | 内容 | 对应代码 |
|---|---|---|
| [configuration/README.md](configuration/README.md) | 配置参考入口，下分 [core.md](configuration/core.md)、[ai-agent.md](configuration/ai-agent.md)、[webui.md](configuration/webui.md)、[fanqie-games.md](configuration/fanqie-games.md) | 各插件 `config.py`、`src/plugins/yawn_core/webui/environment_catalog.dotenv`、`.env.example` |
| [deployment.md](deployment.md) | 生产部署、迁移、升级、回滚、单实例边界 | [deploy/](../deploy/CLAUDE.md) |
| [public-docker-deployment.md](public-docker-deployment.md) | 公共 GHCR / Docker 部署 | `deploy/docker/` |
| [game-event-log.md](game-event-log.md) | 跨玩法对局事件日志（P1-5） | `yawn_core/event_log.py` |
| [game-replay.md](game-replay.md) | 局后回放（P1-7） | `yawn_core/replay.py`、`replay_commands.py` |
| [metrics.md](metrics.md) | 运行指标（P1-6） | `yawn_core/metrics.py` |
| [rpg-gameplay-guide.md](rpg-gameplay-guide.md) | RPG 新手与联合推理指南（玩家向） | [yawn_rpg/](../src/plugins/yawn_core/yawn_rpg/CLAUDE.md) |
| [fanqie-notice.md](fanqie-notice.md) | 番茄子插件来源与合规边界 | [yawn_fanqie/](../src/plugins/yawn_core/yawn_fanqie/CLAUDE.md) |
| `agent-speech-p6-p8.md`、`agent-speech-p9-p11.md`、`agent-speech-p7-p12-completion.md` | 群聊 Agent 发言系统分阶段设计记录 | [yawn_agent/](../src/plugins/yawn_core/yawn_agent/CLAUDE.md) 的 `speech*.py`；对应测试 `tests/test_agent_speech_p*.py` |
| [development-roadmap.md](development-roadmap.md) | 开发路线图与当前状态 | — |
| [repository-hygiene.md](repository-hygiene.md) | 哪些文件可以进 Git | `tools/repo_guard.py` |
| [open-source-readiness.md](open-source-readiness.md)、[history-rewrite-runbook.md](history-rewrite-runbook.md) | 开源前验收与公开历史重写 | `tools/history_*.py`、`.github/workflows/history-security-audit.yml` |
| `yawnbot-architecture.html`、`YawnBot-system-architecture.pptx` | 架构图/演示稿（二进制或独立页面，不要手改结构） | — |

## 规则

- 新增或改名配置项时，同步更新 `configuration/` 下对应文件和 `environment_catalog.dotenv`；只有最小启动必需项才进 `.env.example`。
- 文档里不要出现真实 Token、Cookie、私有地址。
