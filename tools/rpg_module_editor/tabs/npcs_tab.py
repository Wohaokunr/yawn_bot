"""「NPC」页：NPC 表单 + 行程编辑器 + 24 小时覆盖条 + 机密泄露实时检查。"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Optional

from rich.text import Text
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.widgets import Button, Label, OptionList, Static, TabbedContent, TabPane
from textual.widgets.option_list import Option

from ..schema_loader import in_window, parse_hhmm  # noqa: TID252
from ..state import (  # noqa: TID252
    build_condition_tokens,
    entity_label,
    generate_unique_id,
    get_list,
    new_npc_dict,
    new_schedule_entry_dict,
    rename_entity,
)
from ..validate import check_condition  # noqa: TID252
from ..widgets import (  # noqa: TID252
    ConditionInput,
    ConfirmScreen,
    FieldChanged,
    IdInput,
    IntInput,
    LabeledInput,
    LabeledSelect,
    LabeledSwitch,
    LabeledTextArea,
    StrListEditor,
    TimeInput,
)
from . import EditorTab, move_item
from .social_editor import SocialEditor

if TYPE_CHECKING:
    from textual import events

_ENTRY_COLORS = ("green", "cyan", "magenta", "yellow", "blue", "red")


def _str_text(value: Any) -> str:
    return value if isinstance(value, str) else ""


def _int_text(value: Any) -> str:
    return str(value) if isinstance(value, int) else ""


def _entry_bounds(entry: dict[str, Any]) -> Optional[tuple[int, int]]:
    frm = parse_hhmm(_str_text(entry.get("from", entry.get("frm"))))
    to = parse_hhmm(_str_text(entry.get("to")))
    if frm is None or to is None:
        return None
    return frm, to


class ScheduleCoverage(Static):
    """24 小时覆盖条：每 15 分钟一格，按声明序着色首条命中行程。

    色块=声明序第 N 条行程；``?`` 叠在条件条目上（条件不成立时该
    时段可能落入其后条目或不在场）；``·`` = 无条目命中（不在场）。
    """

    DEFAULT_CSS = """
    ScheduleCoverage { height: auto; padding: 0; overflow-x: hidden; }
    """

    def __init__(self) -> None:
        super().__init__()
        self._schedule: list[Any] = []

    @staticmethod
    def _put_label(chars: list[str], label: str, center: int) -> None:
        if not chars:
            return
        clipped = label[: len(chars)]
        start = max(0, min(len(chars) - len(clipped), center - len(clipped) // 2))
        end = min(len(chars), start + len(clipped))
        chars[start:end] = clipped[: end - start]

    def _timeline_width(self) -> int:
        # 96 列对应 15 分钟精度；窄窗口下聚合显示，但始终保持单行。
        available = self.content_size.width or self.size.width or 48
        return max(1, min(96, available))

    def _render_coverage(self) -> None:
        entries = [e for e in self._schedule if isinstance(e, dict)]
        bounds = [_entry_bounds(e) for e in entries]
        width = self._timeline_width()
        line = Text(no_wrap=True, overflow="crop")
        for column in range(width):
            # 每列取其所代表区间的中点；宽度 96 时恰为每个 15 分钟槽位。
            slot = min(24 * 60 - 1, int((column + 0.5) * 24 * 60 / width))
            mark, styled = "·", "dim"
            for idx, (entry, bound) in enumerate(zip(entries, bounds)):
                if bound is None:
                    continue
                if in_window(slot, bound[0], bound[1]):
                    color = _ENTRY_COLORS[idx % len(_ENTRY_COLORS)]
                    mark = "?" if _str_text(entry.get("condition")) else "█"
                    styled = color
                    break
            line.append(mark, styled)

        ruler_chars = [" " for _ in range(width)]
        for hour, label in ((0, "0"), (6, "6"), (12, "12"), (18, "18"), (24, "24")):
            center = round((width - 1) * hour / 24)
            self._put_label(ruler_chars, label, center)
        ruler = Text("".join(ruler_chars) + "\n", "dim", no_wrap=True)
        legend = Text()
        for idx, entry in enumerate(entries):
            color = _ENTRY_COLORS[idx % len(_ENTRY_COLORS)]
            frm = _str_text(entry.get("from", entry.get("frm")))
            to = _str_text(entry.get("to"))
            cond = "（有条件）" if _str_text(entry.get("condition")) else ""
            scene = entry.get("scene")
            where = "外出" if entry.get("away") else f"在 {scene or '?'}"
            legend.append(f"■ 条目{idx + 1} {frm}→{to} {where}{cond}\n", color)
        if not entries:
            legend.append("（无行程：常驻 scene.npcs 所列场景）\n", "dim")
        legend.append(
            "█=命中条目  ?=条件条目（条件不成立时落到后续条目/不在场）  ·=不在场",
            "dim",
        )
        self.update(ruler + line + Text("\n") + legend)

    def refresh_coverage(self, schedule: list[Any]) -> None:
        self._schedule = list(schedule)
        self._render_coverage()

    def on_resize(self, _event: events.Resize) -> None:
        self._render_coverage()


class ScheduleForm(VerticalScroll):
    """行程条目表单（键前缀 schedule.*）。"""

    def __init__(self, tokens_provider: Any) -> None:
        super().__init__()
        self._from = TimeInput("起始 from（HH:MM，含起点）", "schedule.from")
        self._to = TimeInput("截止 to（HH:MM，不含终点）", "schedule.to")
        self._scene = LabeledSelect("所在场景 scene", "schedule.scene", [])
        self._away = LabeledSwitch("外出 away（不在任何场景）", "schedule.away")
        self._activity = LabeledInput(
            "活动描述 activity",
            "schedule.activity",
            badge="进 KP 场景块「正在：…」，不得含机密",
        )
        self._condition = ConditionInput(
            "生效条件 condition",
            "schedule.condition",
            validator=lambda cond: check_condition(cond, self._data),
            tokens_provider=tokens_provider,
            badge="空=始终生效；声明序=优先级",
        )
        self._data: dict[str, Any] = {}

    def compose(self) -> Any:
        yield Label(
            "[dim]窗口为 [from, to) 半开区间，支持跨午夜；from == to = 全天[/dim]",
            markup=True,
        )
        yield self._from
        yield self._to
        yield self._scene
        yield self._away
        yield self._activity
        yield self._condition

    def fill(
        self,
        entry: dict[str, Any],
        scene_options: list[tuple[str, str]],
        data: dict[str, Any],
    ) -> None:
        self._data = data
        self._from.set_value(_str_text(entry.get("from", entry.get("frm"))))
        self._to.set_value(_str_text(entry.get("to")))
        self._scene.set_options(scene_options, entry.get("scene"))
        self._away.set_value(bool(entry.get("away", False)))
        self._activity.set_value(_str_text(entry.get("activity")))
        self._condition.set_value(_str_text(entry.get("condition")))


class NpcsTab(EditorTab):
    """NPC 页：左侧列表，右侧表单 + 行程。"""

    DEFAULT_CSS = """
    NpcsTab { height: 1fr; }
    NpcsTab Horizontal.-master { height: 1fr; }
    NpcsTab Vertical.-list-pane { width: 34; }
    NpcsTab Vertical.-form-pane { width: 1fr; }
    NpcsTab Horizontal.-row { height: 3; }
    NpcsTab Button { margin-right: 1; }
    NpcsTab OptionList.-main-list { height: 1fr; }
    NpcsTab OptionList.-sub-list { height: 8; }
    NpcsTab Label.-leak-ok { height: 1; color: $success; }
    NpcsTab Label.-leak-bad { height: auto; color: $error; }
    NpcsTab Label.-note { height: auto; color: $warning; }
    """

    def __init__(self) -> None:
        super().__init__()
        self._npc_list = OptionList(classes="-main-list")
        self._id = IdInput("NPC id", "npc.id")
        self._name = LabeledInput(
            "名称 name", "npc.name", hint="用于 /对话、/攻击 的名称子串查找"
        )
        self._public_desc = LabeledInput(
            "公开形象 public_desc", "npc.public_desc", badge="KP概览，只写安全信息"
        )
        self._persona = LabeledTextArea(
            "人格 persona", "npc.persona", badge="KP概览(压缩) + NPC 对白智能体"
        )
        self._knows = StrListEditor(
            "可透露信息 knows", "npc.knows", badge="KP概览 + NPC 智能体"
        )
        self._secrets = StrListEditor(
            "机密 secrets",
            "npc.secrets",
            badge="仅该 NPC 自己的对白智能体可见，KP 永不可见",
        )
        self._leak_feedback = Label("", classes="-leak-ok")
        self._fallback = LabeledInput(
            "罐头回复 fallback_line", "npc.fallback_line", hint="AI 关闭/失败时的回复"
        )
        self._hp = IntInput("生命值 hp", "npc.hp")
        self._attack_skill = IntInput(
            "反击命中率 attack_skill（d100）", "npc.attack_skill"
        )
        self._attack_name = LabeledInput("攻击描述名 attack_name", "npc.attack_name")
        self._damage = LabeledInput(
            "伤害骰 damage", "npc.damage", hint="如 1d3；1 ≤ N ≤ 100，1 ≤ M ≤ 1000"
        )
        self._dodge = IntInput("闪避对抗值 dodge", "npc.dodge")
        self._on_death_clue = LabeledSelect(
            "死亡线索 on_death_clue", "npc.on_death_clue", []
        )
        self._on_death_text = LabeledTextArea(
            "死亡播报 on_death_text", "npc.on_death_text"
        )
        self._schedule_list = OptionList(classes="-sub-list")
        self._schedule_form = ScheduleForm(tokens_provider=self._tokens)
        self._coverage = ScheduleCoverage()
        self._social = SocialEditor()
        self._npc_idx: Optional[int] = None
        self._entry_idx: Optional[int] = None

    # ── 布局 ──────────────────────────────────────────────

    def compose(self) -> Any:
        with Horizontal(classes="-master"):
            with Vertical(classes="-list-pane"):
                yield Label("[b]NPC 列表[/b]", markup=True)
                yield self._npc_list
                with Horizontal(classes="-row"):
                    yield Button("新增", variant="primary", classes="-npc-add")
                    yield Button("删除", variant="error", classes="-npc-del")
                    yield Button("上移", classes="-npc-up")
                    yield Button("下移", classes="-npc-down")
            with Vertical(classes="-form-pane"), TabbedContent(initial="tab-npc-base"):
                with TabPane("基本与对白", id="tab-npc-base"), VerticalScroll():
                    yield self._id
                    yield self._name
                    yield self._public_desc
                    yield self._persona
                    yield self._knows
                    yield self._secrets
                    yield self._leak_feedback
                    yield self._fallback
                with TabPane("战斗数值", id="tab-npc-combat"), VerticalScroll():
                    yield Label(
                        "[yellow]硬性后果（不可配置）：杀害 NPC 必记 "
                        "murder + npc_dead:<id>，"
                        "触发通用逮捕结局；在场存活 NPC 立即反击。[/yellow]",
                        markup=True,
                        classes="-note",
                    )
                    yield self._hp
                    yield self._attack_skill
                    yield self._attack_name
                    yield self._damage
                    yield self._dodge
                    yield self._on_death_clue
                    yield self._on_death_text
                with TabPane("行程", id="tab-npc-schedule"), Vertical():
                    yield self._schedule_list
                    with Horizontal(classes="-row"):
                        yield Button(
                            "新增条目", variant="primary", classes="-entry-add"
                        )
                        yield Button("删除条目", variant="error", classes="-entry-del")
                        yield Button("上移", classes="-entry-up")
                        yield Button("下移", classes="-entry-down")
                    yield self._coverage
                    yield self._schedule_form
                with TabPane("社交", id="tab-npc-social"), VerticalScroll():
                    yield self._social

    # ── 数据访问 ──────────────────────────────────────────

    def _npcs(self) -> list[Any]:
        return get_list(self.editor.draft.data, "npcs")

    def _current_npc(self) -> Optional[dict[str, Any]]:
        npcs = self._npcs()
        if self._npc_idx is None or not (0 <= self._npc_idx < len(npcs)):
            return None
        npc = npcs[self._npc_idx]
        return npc if isinstance(npc, dict) else None

    def _current_entry(self) -> Optional[dict[str, Any]]:
        npc = self._current_npc()
        if npc is None:
            return None
        entries = get_list(npc, "schedule")
        if self._entry_idx is None or not (0 <= self._entry_idx < len(entries)):
            return None
        entry = entries[self._entry_idx]
        return entry if isinstance(entry, dict) else None

    def _scene_options(self) -> list[tuple[str, str]]:
        return [
            (entity_label(s), str(s.get("id", "")))
            for s in get_list(self.editor.draft.data, "scenes")
            if isinstance(s, dict)
        ]

    def _clue_options(self) -> list[tuple[str, str]]:
        return [
            (entity_label(c), str(c.get("id", "")))
            for c in get_list(self.editor.draft.data, "clues")
            if isinstance(c, dict)
        ]

    def _tokens(self) -> list[tuple[str, str]]:
        return build_condition_tokens(self.editor.draft.data)

    # ── 重填 ──────────────────────────────────────────────

    def refresh_tab(self, data: dict[str, Any]) -> None:
        npcs = get_list(data, "npcs")
        current_id = None
        npc = self._current_npc()
        if npc is not None:
            current_id = npc.get("id")
        self._npc_list.clear_options()
        for i, item in enumerate(npcs):
            if isinstance(item, dict):
                self._npc_list.add_option(Option(entity_label(item), id=str(i)))
        new_idx = None
        if current_id is not None:
            new_idx = next(
                (
                    i
                    for i, n in enumerate(npcs)
                    if isinstance(n, dict) and n.get("id") == current_id
                ),
                None,
            )
        if new_idx is None and npcs:
            new_idx = 0
        self._npc_idx = new_idx
        if new_idx is not None:
            self._npc_list.highlighted = new_idx
        self._fill_npc_form()
        self._fill_schedule()

    def _fill_npc_form(self) -> None:
        npc = self._current_npc()
        if npc is None:
            return
        self._id.set_value(_str_text(npc.get("id")))
        self._name.set_value(_str_text(npc.get("name")))
        self._public_desc.set_value(_str_text(npc.get("public_desc")))
        self._persona.set_value(_str_text(npc.get("persona")))
        knows = npc.get("knows")
        self._knows.set_items(
            [str(k) for k in knows] if isinstance(knows, list) else []
        )
        secrets = npc.get("secrets")
        self._secrets.set_items(
            [str(s) for s in secrets] if isinstance(secrets, list) else []
        )
        self._fallback.set_value(_str_text(npc.get("fallback_line")))
        self._hp.set_value(_int_text(npc.get("hp", 10)))
        self._attack_skill.set_value(_int_text(npc.get("attack_skill", 40)))
        self._attack_name.set_value(_str_text(npc.get("attack_name", "攻击")))
        self._damage.set_value(_str_text(npc.get("damage", "1d3")))
        self._dodge.set_value(_int_text(npc.get("dodge", 30)))
        self._on_death_clue.set_options(self._clue_options(), npc.get("on_death_clue"))
        self._on_death_text.set_value(_str_text(npc.get("on_death_text")))
        self._social.refresh_npc(npc, self.editor.draft.data)
        self._refresh_leak_feedback()

    def _fill_schedule(self, selected_idx: Optional[int] = None) -> None:
        npc = self._current_npc()
        entries = get_list(npc, "schedule") if npc else []
        if selected_idx is None:
            selected_idx = self._entry_idx
        self._schedule_list.clear_options()
        for i, entry in enumerate(entries):
            if isinstance(entry, dict):
                frm = _str_text(entry.get("from", entry.get("frm")))
                to = _str_text(entry.get("to"))
                where = "外出" if entry.get("away") else str(entry.get("scene", "?"))
                mark = " ⚠" if _str_text(entry.get("condition")) else ""
                self._schedule_list.add_option(
                    Option(f"#{i + 1} {frm}→{to} {where}{mark}", id=str(i))
                )
        if entries:
            self._entry_idx = min(max(selected_idx or 0, 0), len(entries) - 1)
            self._schedule_list.highlighted = self._entry_idx
        else:
            self._entry_idx = None
        self._coverage.refresh_coverage(entries)
        self._fill_entry_form()

    def _fill_entry_form(self) -> None:
        entry = self._current_entry()
        if entry is None:
            return
        self._schedule_form.fill(entry, self._scene_options(), self.editor.draft.data)

    # ── 机密泄露实时检查（复用 schema 的子串判定口径）────

    def _refresh_leak_feedback(self) -> None:
        npc = self._current_npc()
        if npc is None:
            return
        haystack = " ".join(
            [
                _str_text(npc.get("persona")),
                _str_text(npc.get("public_desc")),
                _str_text(npc.get("fallback_line")),
                _str_text(npc.get("on_death_text")),
                *[str(k) for k in get_list(npc, "knows")],
                *[
                    _str_text(e.get("activity"))
                    for e in get_list(npc, "schedule")
                    if isinstance(e, dict)
                ],
                *[
                    _str_text(text)
                    for node in get_list(npc, "social_nodes")
                    if isinstance(node, dict)
                    for text in (
                        node.get("name"),
                        node.get("goal"),
                        node.get("success_text"),
                        node.get("failure_text"),
                    )
                ],
                *[
                    _str_text(text)
                    for node in get_list(npc, "social_nodes")
                    if isinstance(node, dict)
                    for strategy in get_list(node, "strategies")
                    if isinstance(strategy, dict)
                    for text in (
                        strategy.get("name"),
                        strategy.get("success_text"),
                        strategy.get("failure_text"),
                    )
                ],
            ]
        )
        secret_leaks = [
            s for s in get_list(npc, "secrets") if s and str(s) in haystack
        ]
        fact_leaks = [
            f
            for fact in get_list(npc, "facts")
            if isinstance(fact, dict)
            for f in (fact.get("name"), fact.get("text"))
            if f and str(f) in haystack
        ]
        leaks = secret_leaks + fact_leaks
        if leaks:
            self._leak_feedback.set_classes("-leak-bad")
            shown = "；".join(str(s)[:20] for s in leaks[:3])
            self._leak_feedback.update(
                f"✗ {len(leaks)} 条机密/私人情报出现在 NPC 可见文案中"
                f"（{shown}…），"
                "会随 KP 开局概览泄露，加载将被拒绝"
            )
        else:
            self._leak_feedback.set_classes("-leak-ok")
            self._leak_feedback.update("✓ 机密未出现在 KP 可见字段中")

    # ── 选择切换 ──────────────────────────────────────────

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        if event.option.id is None:
            return
        index = int(str(event.option.id))
        if event.control is self._npc_list:
            self._npc_idx = index
            self._fill_npc_form()
            self._fill_schedule()
        elif event.control is self._schedule_list:
            self._entry_idx = index
            self._fill_entry_form()

    # ── 按钮 ──────────────────────────────────────────────

    def on_button_pressed(self, event: Button.Pressed) -> None:  # noqa: C901,PLR0912
        classes = event.button.classes
        data = self.editor.draft.data

        if "-npc-add" in classes:
            new_id = generate_unique_id(
                "new_npc",
                {str(n.get("id", "")) for n in self._npcs() if isinstance(n, dict)},
            )
            npcs = data.get("npcs")
            if not isinstance(npcs, list):
                npcs = []
                data["npcs"] = npcs
            npcs.append(new_npc_dict(new_id))
            self.editor.refresh_all()
        elif "-npc-del" in classes:
            npc = self._current_npc()
            if npc is None:
                return
            self._confirm_delete_npc(npc)
        elif "-npc-up" in classes or "-npc-down" in classes:
            npcs = self._npcs()
            delta = -1 if "-npc-up" in classes else 1
            new_idx = move_item(npcs, self._npc_idx, delta)
            if new_idx is not None:
                self._npc_idx = new_idx
                self.editor.refresh_all()
        elif "-entry-add" in classes:
            npc = self._current_npc()
            if npc is None:
                return
            first_scene = next(
                (
                    str(s.get("id", ""))
                    for s in get_list(data, "scenes")
                    if isinstance(s, dict)
                ),
                "",
            )
            npc.setdefault("schedule", []).append(new_schedule_entry_dict(first_scene))
            self._fill_schedule(len(get_list(npc, "schedule")) - 1)
            self.editor.on_data_changed()
        elif "-entry-del" in classes:
            npc = self._current_npc()
            if npc is None or self._entry_idx is None:
                return
            entries = get_list(npc, "schedule")
            if 0 <= self._entry_idx < len(entries):
                del entries[self._entry_idx]
            self._fill_schedule(self._entry_idx)
            self.editor.on_data_changed()
        elif "-entry-up" in classes or "-entry-down" in classes:
            npc = self._current_npc()
            if npc is None:
                return
            entries = get_list(npc, "schedule")
            delta = -1 if "-entry-up" in classes else 1
            new_idx = move_item(entries, self._entry_idx, delta)
            if new_idx is not None:
                self._fill_schedule(new_idx)
                self.editor.on_data_changed()

    def _confirm_delete_npc(self, npc: dict[str, Any]) -> None:
        ident = str(npc.get("id", "?"))

        def _delete(confirmed: Optional[bool]) -> None:  # noqa: FBT001
            if not confirmed:
                return
            npcs = self._npcs()
            if npc in npcs:
                npcs.remove(npc)
            for scene in get_list(self.editor.draft.data, "scenes"):
                members = scene.get("npcs") if isinstance(scene, dict) else None
                if isinstance(members, list) and ident in members:
                    members.remove(ident)
            self.editor.refresh_all()

        self.app.push_screen(
            ConfirmScreen(
                "删除 NPC",
                f"确定删除 NPC {entity_label(npc)} 吗？scene.npcs 中的引用会一并移除。",
            ),
            _delete,
        )

    # ── 字段写回 ──────────────────────────────────────────

    def on_field_changed(self, event: FieldChanged) -> None:
        prefix, _, field = event.key.partition(".")
        if prefix == "npc":
            self._write_npc_field(field, event.value)
        elif prefix == "schedule":
            self._write_schedule_field(field, event.value)
        elif prefix in {"social", "fact", "node", "strategy"}:
            self._refresh_leak_feedback()

    def _write_npc_field(self, field: str, value: Any) -> None:  # noqa: C901
        npc = self._current_npc()
        if npc is None:
            return
        if field == "id":
            old = str(npc.get("id", ""))
            new = str(value)
            if new == old or not new:
                return
            sites = rename_entity(self.editor.draft.data, "npc", old, new)
            self.editor.refresh_all()
            if len(sites) > 1:
                self.notify(f"已级联更新 {len(sites) - 1} 处引用")
            return
        if field in ("hp", "attack_skill", "dodge"):
            if value is not None:
                npc[field] = value
        elif field == "on_death_clue":
            if value:
                npc["on_death_clue"] = value
            else:
                npc.pop("on_death_clue", None)
        else:
            npc[field] = value
            if field == "name":
                self._update_list_label()
        if field in ("persona", "public_desc", "knows", "secrets"):
            self._refresh_leak_feedback()
        self.editor.on_data_changed()

    def _update_list_label(self) -> None:
        npc = self._current_npc()
        if npc is None or self._npc_idx is None:
            return
        self._npc_list.replace_option_prompt_at_index(self._npc_idx, entity_label(npc))

    def _write_schedule_field(self, field: str, value: Any) -> None:  # noqa: PLR0912
        entry = self._current_entry()
        npc = self._current_npc()
        if entry is None or npc is None:
            return
        if field == "away":
            entry["away"] = bool(value)
            if value:
                entry.pop("scene", None)
        elif field == "condition":
            text = str(value).strip()
            if text:
                entry["condition"] = text
            else:
                entry.pop("condition", None)
        elif field == "scene":
            if value:
                entry["scene"] = value
            else:
                entry.pop("scene", None)
        else:
            entry[field] = value
        if field in ("from", "to", "scene", "away", "condition"):
            self._fill_schedule(self._entry_idx)
        else:
            self._coverage.refresh_coverage(get_list(npc, "schedule"))
        if field == "activity":
            self._refresh_leak_feedback()
        self.editor.on_data_changed()
