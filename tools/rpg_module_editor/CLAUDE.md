# rpg_module_editor — 跑团模组 TUI 编辑器

基于 Textual 的终端编辑器，用来编写、校验和固定种子试玩 `yawn_rpg` 的 YAML 模组。
维护约束（时间线按控件实际宽度渲染、列表移动用返回新索引的共享逻辑、重建列表时保留选择、
改完跑 pytest/Ruff/Pyright）在仓库根 [AGENTS.md](../../AGENTS.md)，根 `CLAUDE.md` 已导入。

```bash
uv run python -m tools.rpg_module_editor [path.yaml]        # 打开编辑器
uv run python -m tools.rpg_module_editor --check FILE.yaml  # 无界面校验，有 ERROR 返回 1
uv run pytest -q tools/rpg_module_editor/tests
```

## 结构

| 文件 | 职责 |
|---|---|
| `__main__.py` | CLI 入口（含 Windows 控制台 UTF-8 守护） |
| `app.py` | 主应用：TabbedContent 外壳、文件流、脏状态保护 |
| `state.py` | 内存草稿：以 dict 为唯一持久表示，实体增删改与排序 |
| `yaml_io.py` | 模组 YAML 加载与序列化管线 |
| `schema_loader.py` | 不启动 NoneBot，用合成父包逐文件加载 `yawn_rpg` 的 `charsheet` → `module_schema` → `dice`；[tools/rpg_playtest](../CLAUDE.md) 也复用它 |
| `validate.py` | 把引擎 pydantic 校验结果翻译成编辑器诊断 |
| `lint.py` | 把 `yawn_rpg/modules/README.md` 写作规范变成诊断 |
| `widgets.py` | 共享表单控件，统一用冒泡 `FieldChanged` 写回 dict |
| `dialogs.py` | 打开/另存为/新建/帮助模态框 |
| `tabs/` | 各分区页：`module_tab`、`scenes_tab`、`npcs_tab`（含行程与 24h 覆盖条）、`social_editor`（NPC 私人情报/社交节点/交涉策略）、`clues_tab`、`deductions_tab`、`events_tab`、`endings_tab`、`monsters_tab`、`report_tab`（校验汇总）、`playtest_tab`（调用 `tools.rpg_playtest`）、`yaml_tab`（源码整体替换） |
| `tests/` | `test_validate_lint.py`、`test_yaml_io.py`（CI core job）；`test_app_smoke.py`、`test_playtest_tab.py`、`test_responsive_references.py`、`test_social_features.py`（CI UI job） |

## 关联位置（跨目录）

- **引擎侧真相源**：[yawn_rpg/module_schema.py](../../src/plugins/yawn_core/yawn_rpg/CLAUDE.md)（模型、条件表达式）与 `dice.py`、`charsheet.py`。schema 改动后编辑器的表单、`lint.py` 和测试都要同步。
- **写作规范**：[yawn_rpg/modules/README.md](../../src/plugins/yawn_core/yawn_rpg/modules/README.md)；范例模组在同目录 `*.yaml`。
- **WebUI 模组库**只读展示同一批模组：`src/plugins/yawn_core/webui/rpg_modules.py`。
