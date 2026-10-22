# tools — 仓库内开发工具

两类东西：仓库卫生/合规脚本（单文件），以及服务 `yawn_rpg` 模组作者的两个子工具。
CI 对 `tools/rpg_module_editor`、`tools/rpg_playtest`、`repo_guard.py`、`repository_content_test.py` 跑 ruff 与 pyright。

## 结构

| 路径 | 职责 | 使用者 |
|---|---|---|
| [rpg_module_editor/](rpg_module_editor/CLAUDE.md) | Textual TUI：编写 / 校验 / 试玩 `yawn_rpg` YAML 模组 | 模组作者 |
| `rpg_playtest/` | 固定种子离线试玩器：对模组做有界状态空间搜索，找到达指定结局的轨迹 | 编辑器的试玩页、CI `RPG playtest regression` |
| `repo_guard.py` | 生成物/运行时/私有文件误入 Git 索引时让 CI 失败 | CI、PR 模板 |
| `repository_content_test.py` | 验证全新 checkout 只含批准内容（必须存在的开源文件、Playwright 镜像版本一致性等） | CI |
| `license_audit.py` | 运行时依赖许可证未审查即失败 | `.github/workflows/license-compliance.yml` |
| `history_secret_audit.py`、`history_rewrite_plan.py` | 公开前审计全部 Git 历史 blob、生成历史重写计划 | `.github/workflows/history-security-audit.yml`，流程见 [docs/history-rewrite-runbook.md](../docs/history-rewrite-runbook.md) |

## rpg_playtest

```bash
uv run python -m tools.rpg_playtest src/plugins/yawn_core/yawn_rpg/modules/yuzhai_old_house.yaml --seed 1 --ending <结局id>
```

`simulator.py` 是确定性搜索核心：通过 `rpg_module_editor/schema_loader.py` 加载与 bot 同源的 `yawn_rpg` schema，不启动 NoneBot、不导入在线引擎、不碰 ORM 或 LLM，`output.py` 负责稳定的文本/JSON 输出；测试在 `rpg_playtest/tests/`。

## 测试与约束

- 这些测试不在默认 `pytest` 范围，按路径运行：`uv run pytest -q tools/rpg_module_editor/tests`、`uv run pytest -q tools/rpg_playtest/tests`。
- 根目录 [AGENTS.md](../AGENTS.md) 中有模组编辑器的维护约束（时间线宽度、列表移动逻辑、选择保留、改完跑 pytest/Ruff/Pyright）。
- 两个 RPG 工具都依赖 [yawn_rpg/module_schema.py](../src/plugins/yawn_core/yawn_rpg/CLAUDE.md) 与 `modules/README.md` 的写作规范；改 schema 时要同步检查这里。
