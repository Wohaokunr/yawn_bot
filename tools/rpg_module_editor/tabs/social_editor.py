"""NPC 社交嵌套编辑器：私人情报、社交节点与交涉策略。"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Optional, cast

from textual.containers import Horizontal, Vertical
from textual.widgets import Button, Label, OptionList
from textual.widgets.option_list import Option

from ..state import (  # noqa: TID252
    build_reference_options_for_field,
    duplicate_item,
    get_list,
    new_npc_fact_dict,
    new_social_node_dict,
    new_social_strategy_dict,
    rename_npc_fact,
)
from ..widgets import (  # noqa: TID252
    FieldChanged,
    IdInput,
    IntInput,
    LabeledInput,
    LabeledSelect,
    LabeledTextArea,
    ReferenceListEditor,
    StrListEditor,
)
from . import move_item

if TYPE_CHECKING:
    from tools.rpg_module_editor.app import ModuleEditorApp

_DIFFICULTY_OPTIONS = [
    ("常规 regular", "regular"),
    ("困难 hard（技能值 ×½）", "hard"),
    ("极难 extreme（技能值 ×⅕）", "extreme"),
]
_SOCIAL_SKILL_OPTIONS = [
    ("说服 persuade", "persuade"),
    ("花言巧语 fast_talk", "fast_talk"),
    ("威吓 intimidate", "intimidate"),
]
_NODE_DEFAULTS = {
    "min_rapport": -100,
    "min_attitude": -100,
    "max_attempts": 3,
    "retry_rapport_penalty": 2,
    "retry_attitude_penalty": 1,
    "success_rapport_delta": 15,
    "success_attitude_delta": 5,
    "failure_rapport_delta": -5,
    "failure_attitude_delta": -2,
}
_STRATEGY_OPTIONAL_INT_FIELDS = {
    "success_rapport_delta",
    "success_attitude_delta",
    "failure_rapport_delta",
    "failure_attitude_delta",
}
_STRATEGY_OPTIONAL_TEXT_FIELDS = {"success_text", "failure_text"}
_ENTITY_PATH_PARTS = 2
_NESTED_PATH_PARTS = 4


def _str_text(value: Any) -> str:
    return value if isinstance(value, str) else ""


def _int_text(value: Any) -> str:
    return str(value) if isinstance(value, int) else ""


def _list_text(value: Any) -> list[str]:
    return [str(item) for item in value] if isinstance(value, list) else []


def _selected_id(items: list[Any], index: Optional[int], key: str = "id") -> Any:
    if index is None or not (0 <= index < len(items)):
        return None
    item = items[index]
    return item.get(key) if isinstance(item, dict) else None


def _select_index(
    items: list[Any], selected: Any, previous: Optional[int]
) -> Optional[int]:
    if not items:
        return None
    if selected is not None:
        for index, item in enumerate(items):
            if isinstance(item, dict) and item.get("id") == selected:
                return index
    return min(max(previous or 0, 0), len(items) - 1)


class SocialEditor(Vertical):
    """单个 NPC 的社交字段编辑器。"""

    DEFAULT_CSS = """
    SocialEditor { height: auto; }
    SocialEditor .-section-title { height: 1; margin-top: 1; color: $accent; }
    SocialEditor .-split { height: auto; }
    SocialEditor .-list-box { width: 30; margin-right: 2; }
    SocialEditor .-detail-box { width: 1fr; }
    SocialEditor OptionList { height: 8; }
    SocialEditor .-buttons { height: 3; }
    SocialEditor Button { margin-right: 1; }
    SocialEditor .-strategy-list { height: 6; }
    SocialEditor .-hint { height: auto; }
    """

    def __init__(self) -> None:
        super().__init__()
        self._npc: Optional[dict[str, Any]] = None
        self._data: dict[str, Any] = {}
        self._fact_idx: Optional[int] = None
        self._node_idx: Optional[int] = None
        self._strategy_idx: Optional[int] = None

        self._rapport = IntInput(
            "初始个人好感 initial_rapport",
            "social.initial_rapport",
            hint="范围 -100~100；运行时按局内行动变化",
        )
        self._attitude = IntInput(
            "初始公共态度 initial_attitude",
            "social.initial_attitude",
            hint="范围 -100~100；影响 NPC 对全队的判断",
        )

        self._fact_list = OptionList()
        self._fact_id = IdInput("情报 id", "fact.id", badge="当前 NPC 内唯一")
        self._fact_name = LabeledInput("情报名称 name", "fact.name")
        self._fact_text = LabeledTextArea(
            "情报正文 text", "fact.text", tall=True, badge="仅通过社交解锁"
        )

        self._node_list = OptionList()
        self._node_id = IdInput("节点 id", "node.id", badge="当前 NPC 内唯一")
        self._node_name = LabeledInput("节点名称 name", "node.name")
        self._node_goal = LabeledTextArea(
            "安全目标 goal", "node.goal", badge="只给社交路由器，不写隐藏奖励"
        )
        self._requires_facts = ReferenceListEditor(
            "前置情报 requires_facts",
            "node.requires_facts",
            hint="可用情报 ID：",
        )
        self._min_rapport = IntInput("最低好感 min_rapport", "node.min_rapport")
        self._min_attitude = IntInput("最低态度 min_attitude", "node.min_attitude")
        self._max_attempts = IntInput("最多尝试 max_attempts", "node.max_attempts")
        self._retry_rapport = IntInput(
            "重试好感惩罚 retry_rapport_penalty", "node.retry_rapport_penalty"
        )
        self._retry_attitude = IntInput(
            "重试态度惩罚 retry_attitude_penalty", "node.retry_attitude_penalty"
        )
        self._success_rapport = IntInput(
            "成功好感变化 success_rapport_delta", "node.success_rapport_delta"
        )
        self._success_attitude = IntInput(
            "成功态度变化 success_attitude_delta", "node.success_attitude_delta"
        )
        self._failure_rapport = IntInput(
            "失败好感变化 failure_rapport_delta", "node.failure_rapport_delta"
        )
        self._failure_attitude = IntInput(
            "失败态度变化 failure_attitude_delta", "node.failure_attitude_delta"
        )
        self._node_success = LabeledTextArea(
            "成功文案 success_text", "node.success_text", tall=True
        )
        self._node_failure = LabeledTextArea(
            "失败文案 failure_text", "node.failure_text", tall=True
        )
        self._unlock_facts = ReferenceListEditor(
            "成功解锁情报 unlock_facts",
            "node.unlock_facts",
            hint="可用情报 ID：",
        )
        self._private_clues = ReferenceListEditor(
            "私人线索 private_clues",
            "node.private_clues",
            hint="可用线索 ID：",
        )
        self._public_clues = ReferenceListEditor(
            "公开线索 public_clues",
            "node.public_clues",
            hint="可用线索 ID：",
        )
        self._success_flags = StrListEditor(
            "成功 flags success_flags",
            "node.success_flags",
            hint="引擎消费的 flag 名称；不会自动创建",
        )
        self._failure_flags = StrListEditor(
            "失败 flags failure_flags",
            "node.failure_flags",
            hint="引擎消费的 flag 名称；不会自动创建",
        )

        self._strategy_list = OptionList(classes="-strategy-list")
        self._strategy_skill = LabeledSelect(
            "技能 skill", "strategy.skill", _SOCIAL_SKILL_OPTIONS, allow_blank=False
        )
        self._strategy_difficulty = LabeledSelect(
            "难度 difficulty",
            "strategy.difficulty",
            _DIFFICULTY_OPTIONS,
            allow_blank=False,
        )
        self._strategy_name = LabeledInput("策略名称 name", "strategy.name")
        self._strategy_success_rapport = IntInput(
            "成功好感覆写 success_rapport_delta",
            "strategy.success_rapport_delta",
            hint="留空继承节点默认值",
        )
        self._strategy_success_attitude = IntInput(
            "成功态度覆写 success_attitude_delta",
            "strategy.success_attitude_delta",
            hint="留空继承节点默认值",
        )
        self._strategy_failure_rapport = IntInput(
            "失败好感覆写 failure_rapport_delta",
            "strategy.failure_rapport_delta",
            hint="留空继承节点默认值",
        )
        self._strategy_failure_attitude = IntInput(
            "失败态度覆写 failure_attitude_delta",
            "strategy.failure_attitude_delta",
            hint="留空继承节点默认值",
        )
        self._strategy_success = LabeledTextArea(
            "成功文案覆写 success_text",
            "strategy.success_text",
            tall=True,
            badge="留空继承节点文案",
        )
        self._strategy_failure = LabeledTextArea(
            "失败文案覆写 failure_text",
            "strategy.failure_text",
            tall=True,
            badge="留空继承节点文案",
        )

    @property
    def editor(self) -> "ModuleEditorApp":
        return cast("ModuleEditorApp", self.app)

    def compose(self) -> Any:  # noqa: PLR0915
        yield Label("[b]社交状态[/b]", markup=True, classes="-section-title")
        yield self._rapport
        yield self._attitude

        yield Label("[b]私人情报 facts[/b]", markup=True, classes="-section-title")
        with Horizontal(classes="-split"):
            with Vertical(classes="-list-box"):
                yield self._fact_list
                with Horizontal(classes="-buttons"):
                    yield Button("新增", variant="primary", classes="-social-fact-add")
                    yield Button("删除", variant="error", classes="-social-fact-del")
                with Horizontal(classes="-buttons"):
                    yield Button("上移", classes="-social-fact-up")
                    yield Button("下移", classes="-social-fact-down")
                    yield Button("复制", classes="-social-fact-copy")
            with Vertical(classes="-detail-box"):
                yield self._fact_id
                yield self._fact_name
                yield self._fact_text

        yield Label(
            "[b]社交节点 social_nodes[/b]", markup=True, classes="-section-title"
        )
        with Horizontal(classes="-split"):
            with Vertical(classes="-list-box"):
                yield self._node_list
                with Horizontal(classes="-buttons"):
                    yield Button("新增", variant="primary", classes="-social-node-add")
                    yield Button("删除", variant="error", classes="-social-node-del")
                with Horizontal(classes="-buttons"):
                    yield Button("上移", classes="-social-node-up")
                    yield Button("下移", classes="-social-node-down")
                    yield Button("复制", classes="-social-node-copy")
            with Vertical(classes="-detail-box"):
                yield self._node_id
                yield self._node_name
                yield self._node_goal
                yield self._requires_facts
                yield self._min_rapport
                yield self._min_attitude
                yield self._max_attempts
                yield self._retry_rapport
                yield self._retry_attitude
                yield self._success_rapport
                yield self._success_attitude
                yield self._failure_rapport
                yield self._failure_attitude
                yield self._node_success
                yield self._node_failure
                yield self._unlock_facts
                yield self._private_clues
                yield self._public_clues
                yield self._success_flags
                yield self._failure_flags

                yield Label(
                    "[b]策略 strategies[/b]", markup=True, classes="-section-title"
                )
                yield self._strategy_list
                with Horizontal(classes="-buttons"):
                    yield Button(
                        "新增策略", variant="primary", classes="-social-strategy-add"
                    )
                    yield Button(
                        "删除策略", variant="error", classes="-social-strategy-del"
                    )
                    yield Button("上移", classes="-social-strategy-up")
                    yield Button("下移", classes="-social-strategy-down")
                    yield Button("复制", classes="-social-strategy-copy")
                yield self._strategy_skill
                yield self._strategy_difficulty
                yield self._strategy_name
                yield self._strategy_success_rapport
                yield self._strategy_success_attitude
                yield self._strategy_failure_rapport
                yield self._strategy_failure_attitude
                yield self._strategy_success
                yield self._strategy_failure

    # ── 数据访问与刷新 ────────────────────────────────────

    def _facts(self) -> list[Any]:
        return get_list(self._npc or {}, "facts")

    def _social_nodes(self) -> list[Any]:
        return get_list(self._npc or {}, "social_nodes")

    def _current_fact(self) -> Optional[dict[str, Any]]:
        facts = self._facts()
        if self._fact_idx is None or not (0 <= self._fact_idx < len(facts)):
            return None
        fact = facts[self._fact_idx]
        return fact if isinstance(fact, dict) else None

    def _current_node(self) -> Optional[dict[str, Any]]:
        nodes = self._social_nodes()
        if self._node_idx is None or not (0 <= self._node_idx < len(nodes)):
            return None
        node = nodes[self._node_idx]
        return node if isinstance(node, dict) else None

    def _strategies(self) -> list[Any]:
        return get_list(self._current_node() or {}, "strategies")

    def locate_path(self, path: tuple[Any, ...]) -> None:
        if len(path) < _ENTITY_PATH_PARTS:
            return
        if path[0] == "facts":
            self._fact_idx = int(path[1])
            self._fill_facts(self._fact_idx)
        elif path[0] == "social_nodes":
            self._node_idx = int(path[1])
            self._fill_nodes(self._node_idx)
            if len(path) >= _NESTED_PATH_PARTS and path[2] == "strategies":
                self._strategy_idx = int(path[3])
                self._fill_strategies(self._strategy_idx)

    def duplicate_current(self) -> bool:
        if self._strategy_idx is not None and self._current_strategy() is not None:
            return self._duplicate_strategy()
        if self._node_idx is not None and self._current_node() is not None:
            return self._duplicate_node()
        return self._duplicate_fact()

    def duplicate_fact(self) -> bool:
        return self._duplicate_fact()

    def duplicate_node(self) -> bool:
        return self._duplicate_node()

    def duplicate_strategy(self) -> bool:
        return self._duplicate_strategy()

    def _duplicate_strategy(self) -> bool:
        strategies = self._strategies()
        used = {
            str(strategy.get("skill"))
            for strategy in strategies
            if isinstance(strategy, dict)
        }
        available = [skill for _, skill in _SOCIAL_SKILL_OPTIONS if skill not in used]
        if not available:
            return False
        new_idx = duplicate_item(strategies, self._strategy_idx, id_key=None)
        if new_idx is None:
            return False
        strategies[new_idx]["skill"] = available[0]
        self._strategy_idx = new_idx
        self._fill_strategies(new_idx)
        return True

    def _duplicate_node(self) -> bool:
        nodes = self._social_nodes()
        new_idx = duplicate_item(
            nodes,
            self._node_idx,
            id_scope={
                str(item.get("id", "")) for item in nodes if isinstance(item, dict)
            },
        )
        if new_idx is None:
            return False
        self._node_idx = new_idx
        self._fill_nodes(new_idx)
        return True

    def _duplicate_fact(self) -> bool:
        facts = self._facts()
        new_idx = duplicate_item(
            facts,
            self._fact_idx,
            id_scope={
                str(item.get("id", "")) for item in facts if isinstance(item, dict)
            },
        )
        if new_idx is None:
            return False
        self._fact_idx = new_idx
        self._fill_facts(new_idx)
        return True

    def _current_strategy(self) -> Optional[dict[str, Any]]:
        strategies = self._strategies()
        if self._strategy_idx is None or not (
            0 <= self._strategy_idx < len(strategies)
        ):
            return None
        strategy = strategies[self._strategy_idx]
        return strategy if isinstance(strategy, dict) else None

    def refresh_npc(self, npc: Optional[dict[str, Any]], data: dict[str, Any]) -> None:
        """从当前 NPC dict 重填全部嵌套表单并保留可用选择。"""
        if npc is not self._npc:
            self._fact_idx = None
            self._node_idx = None
            self._strategy_idx = None
        self._npc = npc
        self._data = data
        if npc is None:
            self._rapport.set_value("0")
            self._attitude.set_value("0")
        else:
            self._rapport.set_value(_int_text(npc.get("initial_rapport", 0)))
            self._attitude.set_value(_int_text(npc.get("initial_attitude", 0)))
        self._set_reference_hints()
        self._fill_facts()
        self._fill_nodes()

    def _set_reference_hints(self) -> None:
        fact_ids = [
            str(fact.get("id"))
            for fact in self._facts()
            if isinstance(fact, dict) and fact.get("id")
        ]
        clue_ids = [
            str(clue.get("id"))
            for clue in get_list(self._data, "clues")
            if isinstance(clue, dict) and clue.get("id")
        ]
        fact_hint = f"可用情报 ID：{'、'.join(fact_ids) or '（暂无）'}"
        clue_hint = f"可用线索 ID：{'、'.join(clue_ids) or '（暂无）'}"
        fact_options = build_reference_options_for_field(
            self._data, "node.requires_facts", context=self._npc
        )
        clue_options = build_reference_options_for_field(
            self._data, "node.private_clues"
        )
        for control in (self._requires_facts, self._unlock_facts):
            control.set_reference_options(fact_options)
        for control in (self._private_clues, self._public_clues):
            control.set_reference_options(clue_options)
        self._requires_facts.set_hint(fact_hint)
        self._unlock_facts.set_hint(fact_hint)
        self._private_clues.set_hint(clue_hint)
        self._public_clues.set_hint(clue_hint)

    def _fill_facts(self, selected_idx: Optional[int] = None) -> None:
        self._set_reference_hints()
        facts = self._facts()
        selected = _selected_id(facts, self._fact_idx)
        if selected_idx is not None:
            selected = _selected_id(facts, selected_idx)
        self._fact_list.clear_options()
        for index, fact in enumerate(facts):
            if isinstance(fact, dict):
                self._fact_list.add_option(
                    Option(self._item_label(fact), id=str(index))
                )
        self._fact_idx = _select_index(facts, selected, selected_idx or self._fact_idx)
        if self._fact_idx is not None:
            self._fact_list.highlighted = self._fact_idx
        self._fill_fact_form()

    def _fill_fact_form(self) -> None:
        fact = self._current_fact()
        self._fact_id.set_value(_str_text(fact.get("id")) if fact else "")
        self._fact_name.set_value(_str_text(fact.get("name")) if fact else "")
        self._fact_text.set_value(_str_text(fact.get("text")) if fact else "")

    def _fill_nodes(self, selected_idx: Optional[int] = None) -> None:
        nodes = self._social_nodes()
        selected = _selected_id(nodes, self._node_idx)
        if selected_idx is not None:
            selected = _selected_id(nodes, selected_idx)
        self._node_list.clear_options()
        for index, node in enumerate(nodes):
            if isinstance(node, dict):
                self._node_list.add_option(
                    Option(self._item_label(node), id=str(index))
                )
        self._node_idx = _select_index(nodes, selected, selected_idx or self._node_idx)
        if self._node_idx is not None:
            self._node_list.highlighted = self._node_idx
        self._fill_node_form()

    def _fill_node_form(self) -> None:
        node = self._current_node()
        self._node_id.set_value(_str_text(node.get("id")) if node else "")
        self._node_name.set_value(_str_text(node.get("name")) if node else "")
        self._node_goal.set_value(_str_text(node.get("goal")) if node else "")
        self._requires_facts.set_items(
            _list_text(node.get("requires_facts")) if node else []
        )
        for field, control in (
            ("min_rapport", self._min_rapport),
            ("min_attitude", self._min_attitude),
            ("max_attempts", self._max_attempts),
            ("retry_rapport_penalty", self._retry_rapport),
            ("retry_attitude_penalty", self._retry_attitude),
            ("success_rapport_delta", self._success_rapport),
            ("success_attitude_delta", self._success_attitude),
            ("failure_rapport_delta", self._failure_rapport),
            ("failure_attitude_delta", self._failure_attitude),
        ):
            control.set_value(
                _int_text(node.get(field, _NODE_DEFAULTS[field])) if node else ""
            )
        self._node_success.set_value(
            _str_text(node.get("success_text")) if node else ""
        )
        self._node_failure.set_value(
            _str_text(node.get("failure_text")) if node else ""
        )
        for field, control in (
            ("unlock_facts", self._unlock_facts),
            ("private_clues", self._private_clues),
            ("public_clues", self._public_clues),
            ("success_flags", self._success_flags),
            ("failure_flags", self._failure_flags),
        ):
            control.set_items(_list_text(node.get(field)) if node else [])
        self._fill_strategies()

    def _fill_strategies(self, selected_idx: Optional[int] = None) -> None:
        strategies = self._strategies()
        selected = _selected_id(strategies, self._strategy_idx, "skill")
        if selected_idx is not None:
            selected = _selected_id(strategies, selected_idx, "skill")
        self._strategy_list.clear_options()
        for index, strategy in enumerate(strategies):
            if isinstance(strategy, dict):
                label = str(strategy.get("skill", f"策略 #{index + 1}"))
                if strategy.get("name"):
                    label = f"{label} · {strategy['name']}"
                self._strategy_list.add_option(Option(label, id=str(index)))
        self._strategy_idx = _select_index_by_key(
            strategies, selected, selected_idx or self._strategy_idx, "skill"
        )
        if self._strategy_idx is not None:
            self._strategy_list.highlighted = self._strategy_idx
        self._fill_strategy_form()

    def _fill_strategy_form(self) -> None:
        strategy = self._current_strategy()
        self._strategy_skill.set_value(
            strategy.get("skill") if strategy else "persuade"
        )
        self._strategy_difficulty.set_value(
            strategy.get("difficulty", "regular") if strategy else "regular"
        )
        self._strategy_name.set_value(
            _str_text(strategy.get("name")) if strategy else ""
        )
        for field, control in (
            ("success_rapport_delta", self._strategy_success_rapport),
            ("success_attitude_delta", self._strategy_success_attitude),
            ("failure_rapport_delta", self._strategy_failure_rapport),
            ("failure_attitude_delta", self._strategy_failure_attitude),
        ):
            control.set_value(_int_text(strategy.get(field)) if strategy else "")
        self._strategy_success.set_value(
            _str_text(strategy.get("success_text")) if strategy else ""
        )
        self._strategy_failure.set_value(
            _str_text(strategy.get("failure_text")) if strategy else ""
        )

    @staticmethod
    def _item_label(item: dict[str, Any]) -> str:
        ident = str(item.get("id", "?"))
        name = str(item.get("name", ""))
        return f"{name}({ident})" if name else ident

    # ── 选择、按钮与排序 ─────────────────────────────────

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        if event.option.id is None:
            return
        index = int(str(event.option.id))
        if event.control is self._fact_list:
            self._fact_idx = index
            self._fill_fact_form()
        elif event.control is self._node_list:
            self._node_idx = index
            self._strategy_idx = None
            self._fill_node_form()
        elif event.control is self._strategy_list:
            self._strategy_idx = index
            self._fill_strategy_form()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        classes = event.button.classes
        self._handle_collection_button(classes)

    def _handle_collection_button(  # noqa: C901,PLR0912,PLR0915
        self, classes: Any
    ) -> None:
        npc = self._npc
        if npc is None:
            return
        if "-social-fact-add" in classes:
            facts = npc.setdefault("facts", [])
            if not isinstance(facts, list):
                facts = []
                npc["facts"] = facts
            existing = {str(f.get("id", "")) for f in facts if isinstance(f, dict)}
            ident = _unique_id("new_fact", existing)
            facts.append(new_npc_fact_dict(ident))
            self._fill_facts(len(facts) - 1)
            self.editor.on_data_changed()
        elif "-social-fact-del" in classes:
            fact = self._current_fact()
            if fact is None or self._fact_idx is None:
                return
            ident = str(fact.get("id", ""))
            refs = self._fact_referrers(ident)
            if refs:
                self.notify(
                    f"情报 {ident} 仍被 {len(refs)} 个社交节点引用；"
                    "将保留引用并由校验报告提示"
                )
            del self._facts()[self._fact_idx]
            self._fill_facts(self._fact_idx)
            self._fill_nodes(self._node_idx)
            self.editor.on_data_changed()
        elif "-social-fact-up" in classes or "-social-fact-down" in classes:
            delta = -1 if "-social-fact-up" in classes else 1
            new_index = move_item(self._facts(), self._fact_idx, delta)
            if new_index is not None:
                self._fill_facts(new_index)
                self.editor.on_data_changed()
        elif "-social-fact-copy" in classes:
            if self.duplicate_fact():
                self.editor.on_data_changed()
        elif "-social-node-add" in classes:
            nodes = npc.setdefault("social_nodes", [])
            if not isinstance(nodes, list):
                nodes = []
                npc["social_nodes"] = nodes
            existing = {str(n.get("id", "")) for n in nodes if isinstance(n, dict)}
            ident = _unique_id("new_social_node", existing)
            nodes.append(new_social_node_dict(ident))
            self._fill_nodes(len(nodes) - 1)
            self.editor.on_data_changed()
        elif "-social-node-del" in classes:
            if self._current_node() is None or self._node_idx is None:
                return
            del self._social_nodes()[self._node_idx]
            self._fill_nodes(self._node_idx)
            self.editor.on_data_changed()
        elif "-social-node-up" in classes or "-social-node-down" in classes:
            delta = -1 if "-social-node-up" in classes else 1
            new_index = move_item(self._social_nodes(), self._node_idx, delta)
            if new_index is not None:
                self._fill_nodes(new_index)
                self.editor.on_data_changed()
        elif "-social-node-copy" in classes:
            if self.duplicate_node():
                self.editor.on_data_changed()
        elif "-social-strategy-add" in classes:
            node = self._current_node()
            if node is None:
                return
            strategies = node.setdefault("strategies", [])
            if not isinstance(strategies, list):
                strategies = []
                node["strategies"] = strategies
            strategies.append(new_social_strategy_dict())
            self._fill_strategies(len(strategies) - 1)
            self.editor.on_data_changed()
        elif "-social-strategy-del" in classes:
            if self._current_strategy() is None or self._strategy_idx is None:
                return
            del self._strategies()[self._strategy_idx]
            self._fill_strategies(self._strategy_idx)
            self.editor.on_data_changed()
        elif "-social-strategy-up" in classes or "-social-strategy-down" in classes:
            delta = -1 if "-social-strategy-up" in classes else 1
            new_index = move_item(self._strategies(), self._strategy_idx, delta)
            if new_index is not None:
                self._fill_strategies(new_index)
                self.editor.on_data_changed()
        elif "-social-strategy-copy" in classes:
            if self.duplicate_strategy():
                self.editor.on_data_changed()

    def _fact_referrers(self, fact_id: str) -> list[str]:
        refs: list[str] = []
        for node in self._social_nodes():
            if not isinstance(node, dict):
                continue
            for field in ("requires_facts", "unlock_facts"):
                if fact_id in get_list(node, field):
                    refs.extend([f"{node.get('id', '?')}.{field}"])
        return refs

    # ── 字段写回 ──────────────────────────────────────────

    def on_field_changed(self, event: FieldChanged) -> None:
        prefix, _, field = event.key.partition(".")
        if prefix == "social":
            self._write_social_field(field, event.value)
        elif prefix == "fact":
            self._write_fact_field(field, event.value)
        elif prefix == "node":
            self._write_node_field(field, event.value)
        elif prefix == "strategy":
            self._write_strategy_field(field, event.value)

    def _write_social_field(self, field: str, value: Any) -> None:
        if self._npc is None or field not in ("initial_rapport", "initial_attitude"):
            return
        self._npc[field] = 0 if value is None else value
        self.editor.on_data_changed()

    def _write_fact_field(self, field: str, value: Any) -> None:
        fact = self._current_fact()
        if fact is None:
            return
        if field == "id":
            old = str(fact.get("id", ""))
            new = str(value).strip()
            if not new or new == old:
                return
            sites = rename_npc_fact(self._npc or {}, old, new)
            self._fill_facts(self._fact_idx)
            self._fill_nodes(self._node_idx)
            if len(sites) > 1:
                self.notify(f"已级联更新 {len(sites) - 1} 处情报依赖")
        else:
            fact[field] = value
        self.editor.on_data_changed()

    def _write_node_field(self, field: str, value: Any) -> None:
        node = self._current_node()
        if node is None:
            return
        if field == "id":
            ident = str(value).strip()
            if not ident:
                return
            node[field] = ident
            self._fill_nodes(self._node_idx)
        elif field in {
            "requires_facts",
            "unlock_facts",
            "private_clues",
            "public_clues",
            "success_flags",
            "failure_flags",
        }:
            node[field] = list(value) if isinstance(value, list) else []
        elif field in _NODE_DEFAULTS and value is None:
            node.pop(field, None)
        else:
            node[field] = value
        self.editor.on_data_changed()

    def _write_strategy_field(self, field: str, value: Any) -> None:
        strategy = self._current_strategy()
        if strategy is None:
            return
        if field in _STRATEGY_OPTIONAL_INT_FIELDS:
            if value is None:
                strategy.pop(field, None)
            else:
                strategy[field] = value
        elif field in _STRATEGY_OPTIONAL_TEXT_FIELDS:
            text = str(value or "").strip()
            if text:
                strategy[field] = value
            else:
                strategy.pop(field, None)
        else:
            strategy[field] = value
        if field in {"skill", "name"}:
            self._fill_strategies(self._strategy_idx)
        self.editor.on_data_changed()


def _select_index_by_key(
    items: list[Any], selected: Any, previous: Optional[int], key: str
) -> Optional[int]:
    if not items:
        return None
    if selected is not None:
        for index, item in enumerate(items):
            if isinstance(item, dict) and item.get(key) == selected:
                return index
    return min(max(previous or 0, 0), len(items) - 1)


def _unique_id(base: str, existing: set[str]) -> str:
    if base not in existing:
        return base
    index = 2
    while f"{base}_{index}" in existing:
        index += 1
    return f"{base}_{index}"


__all__ = ["SocialEditor"]
