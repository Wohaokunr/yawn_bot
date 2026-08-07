# tools/ — 开发工具（不进 bot 运行时）

## rpg_module_editor — 跑团模组编辑器（TUI）

基于 [Textual](https://textual.textualize.io/) 的终端编辑器，用于编写 / 校验
`src/plugins/yawn_core/yawn_rpg/modules/*.yaml` 剧本模组。写作规范见
`modules/README.md`；编辑器复用引擎自身的 pydantic schema（合成包引导，
不启动 NoneBot），**校验口径与 bot 加载完全一致**。

### 运行

```bash
uv sync --group tools                                    # 安装 textual / pytest
uv run python -m tools.rpg_module_editor                 # 从 README 最小骨架新建
uv run python -m tools.rpg_module_editor path/to/x.yaml  # 打开指定模组
uv run python -m tools.rpg_module_editor --check x.yaml  # 无界面校验报告（退出码=错误数级别）
```

建议使用 **Windows Terminal**（旧 conhost 下中文与 TUI 渲染可能异常）。

### 快捷键

| 键 | 功能 |
|---|---|
| `ctrl+s` / `ctrl+shift+s` | 保存 / 另存为 |
| `ctrl+o` / `ctrl+n` | 打开 / 新建（空白骨架或复制现有模组） |
| `f5` | 重新校验并跳转校验页 |
| `f1` | 帮助：防剧透可见性速查表 |
| `ctrl+q` | 退出（有未保存修改时弹确认） |

### 页面

模组 / 场景（检定点、出口、在场成员）/ NPC（对白、战斗、行程 +
24 小时覆盖条）/ 怪物 / 线索（引用者地图）/ 结局（声明序=优先级）/
事件 / **YAML 源码**（整份文本编辑，应用时整体替换表单状态）/ 校验。

长文本字段全部是可直接编辑的文本框；条件表达式输入框提供实时引用
校验与「从当前模组真实 id 插入词条」面板；每个字段标签带防剧透
可见性徽章（该字段进 KP 概览 / 场景块 / 仅播报 / 永不可见）。

### 设计约定（改代码前必读）

1. **dict 是唯一持久状态**（YAML 侧键名，行程条目用 `from`）。pydantic
   模型只校验、**永不回写序列化**——schema 未开 `extra="forbid"`，经
   模型往返会静默丢未知键。未知键由 lint 报 ERROR。
2. **程序化填充一律 `widget.prevent(...)`**：Textual 的 `Changed` 消息在
   赋值时同步入队、异步处理，布尔 `_suppress` 旗标拦不住。
3. **Textual 沿 MRO 调用每一层同名 handler**：子类不得覆写
   `on_input_changed`（会双重触发）；值转型走 `_coerce_value`。
4. **勿用 `_render` / `render` 命名自定义方法**：会遮蔽 `Widget._render`
   导致 `'NoneType' object has no attribute 'render_strips'` 渲染崩溃。
5. 保存即按模型默认值**紧凑化**（`once: false`、`hp: 10` 等省略）；
   往返目标是语义等价而非字节一致，行内注释会丢（开头成块的头部
   注释保留）。
6. schema 私有接口（`_validate_condition` / `_parse_hhmm` / `_in_window`
   等）经 `schema_loader.py` 再导出；`module_schema` 改版导致缺失时
   引导会硬失败并提示同步更新。

### 测试

```bash
uv run pytest tools/rpg_module_editor/tests/ -q
```

其中 `test_app_smoke.py` 用 Textual `run_test()` 无头驱动整个应用：
遍历全部 Tab、编辑、另存、引擎口径复核。
