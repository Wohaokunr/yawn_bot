"""跑团模组编辑器：基于 Textual 的 TUI，用于编写 / 校验 yawn_rpg YAML 模组。

编辑器复用引擎自身的 pydantic 模式（module_schema.py）做校验：
通过合成包引导在不启动 NoneBot 的前提下直接加载 schema 三件套
（charsheet / module_schema / dice），保证与 bot 加载口径完全一致。
"""
