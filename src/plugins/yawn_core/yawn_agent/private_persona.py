# ruff: noqa: E501, TRY003, PLR0913, PLW2901, FBT001, RUF001, RUF003
"""私聊 Agent 的每用户人设：每位用户在自己的私聊里可以有一张独立的人设卡。

形状仍是 v3 的 :class:`persona.PersonaCard`（不加第二种形状），只多了“归谁、从哪来”：

- 模式 ``follow``（跟随私聊默认）/ ``custom``（本人专属卡）/ ``library``（选用人设库模板，跟随模板更新）；
- 生效顺序：本人专属 → 本人选的模板 → 策略里的私聊默认模板 → 群聊全局人设 → 内置默认；
- 数据在本人的个人数据文件 ``persona`` 键里（``private_store``，无迁移，/删除我的数据 会一起删）；
- 策略（是否允许自定义、每天能自己改几次、哪些字段能改、私聊默认人设）在
  ``persona_policy.json``，和个人数据目录放在一起；
- 人设库模板在进程内缓存（``set_library_snapshot``，由 ``persona_library`` 在启动和每次写入后刷新），
  私聊热路径不查库。

私聊人设 **只** 作用于这位用户的私聊（主回合、汇报轮、协调轮、后台口吻、定时问候），群聊永远不读它。
写入校验：沿用 ``persona_override_reason``（拒绝覆盖系统规则、伪造消息、CQ 码）；用户在私聊里
自己改时再过一遍 ``memory_hygiene`` 的指令/隐私检测。
"""

from __future__ import annotations

import json
import os
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import private_store
from .context import now_beijing
from .log import dbg
from .persona import (
    PERSONA_PRESETS,
    PERSONA_PROMPT_TOKEN_LIMIT,
    PERSONA_SOURCE_BUILTIN,
    PERSONA_SOURCE_DRAFT,
    PERSONA_SOURCE_GLOBAL,
    PERSONA_TRAIT_FIELDS,
    PersonaCard,
    compile_persona_card,
    global_persona,
    parse_persona_card,
    persona_card_summary,
    persona_override_reason,
    persona_prompt_tokens,
    persona_trait_label,
    persona_trait_name,
    prompt_persona,
)

MODE_FOLLOW = "follow"
MODE_CUSTOM = "custom"
MODE_LIBRARY = "library"
MODES = (MODE_FOLLOW, MODE_CUSTOM, MODE_LIBRARY)

SOURCE_USER = "user"
SOURCE_USER_LIBRARY = "user_library"
SOURCE_PRIVATE_DEFAULT = "private_default"
# 有这两种来源时，私聊提示词会附上“专属人设说明”。
PERSONAL_SOURCES = frozenset({SOURCE_USER, SOURCE_USER_LIBRARY})

MAX_HISTORY = 10
BY_SELF = "self"
BY_ADMIN = "admin"

# 字段 → 中文名（历史、差异、工具结果、WebUI 都用这一份）。
FIELD_LABELS: dict[str, str] = {
    "name": "名字",
    "self_name": "自称",
    "user_title": "称呼",
    "identity": "身份",
    "personality": "性格",
    "speaking_style": "说话方式",
    "catchphrases": "口癖",
    "likes": "喜欢的话题",
    "avoid_topics": "避开的话题",
    "examples": "示例台词",
    **{key: persona_trait_name(key) for key in PERSONA_TRAIT_FIELDS},
}
EDITABLE_FIELDS = tuple(FIELD_LABELS)
_LIST_FIELDS = frozenset({"catchphrases", "likes", "avoid_topics", "examples"})


# ── 策略 ────────────────────────────────────────────────────────────────


@dataclass(slots=True)
class PersonaPolicy:
    enabled: bool = True
    self_edit: bool = True
    self_edit_daily: int = 10
    self_fields: tuple[str, ...] = EDITABLE_FIELDS
    allow_presets: bool = True
    allow_library: bool = True
    default_template_id: int | None = None
    revision: int = 0
    updated_at: str = ""
    updated_by: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "enabled": self.enabled,
            "selfEdit": self.self_edit,
            "selfEditDaily": self.self_edit_daily,
            "selfFields": list(self.self_fields),
            "allowPresets": self.allow_presets,
            "allowLibrary": self.allow_library,
            "defaultTemplateId": str(self.default_template_id)
            if self.default_template_id
            else None,
            "revision": self.revision,
            "updatedAt": self.updated_at or None,
            "updatedBy": self.updated_by or None,
        }


class PersonaPolicyError(ValueError):
    def __init__(self, message: str, *, status: int = 422, field_name: str = "") -> None:
        super().__init__(message)
        self.status = status
        self.field_name = field_name


_POLICY_LOCK = threading.RLock()
_policy_cache: tuple[float | None, PersonaPolicy] | None = None


def policy_path() -> Path:
    raw = os.environ.get("PRIVATE_AGENT_PERSONA_POLICY_PATH", "").strip()
    if raw:
        return Path(raw)
    return private_store.data_dir().parent / "persona_policy.json"


def _clean_fields(raw: object) -> tuple[str, ...]:
    if not isinstance(raw, (list, tuple)):
        return EDITABLE_FIELDS
    return tuple(key for key in EDITABLE_FIELDS if key in {str(item) for item in raw})


def _policy_from_raw(raw: object) -> PersonaPolicy:
    policy = PersonaPolicy()
    if not isinstance(raw, dict):
        return policy
    policy.enabled = bool(raw.get("enabled", True))
    policy.self_edit = bool(raw.get("selfEdit", True))
    try:
        policy.self_edit_daily = max(0, min(200, int(raw.get("selfEditDaily", 10))))
    except (TypeError, ValueError):
        policy.self_edit_daily = 10
    policy.self_fields = _clean_fields(raw.get("selfFields", list(EDITABLE_FIELDS)))
    policy.allow_presets = bool(raw.get("allowPresets", True))
    policy.allow_library = bool(raw.get("allowLibrary", True))
    try:
        default = int(raw.get("defaultTemplateId") or 0)
    except (TypeError, ValueError):
        default = 0
    policy.default_template_id = default or None
    policy.revision = int(raw.get("revision") or 0)
    policy.updated_at = str(raw.get("updatedAt") or "")
    policy.updated_by = str(raw.get("updatedBy") or "")
    return policy


def load_policy() -> PersonaPolicy:
    """读策略（按 mtime 缓存）；文件不存在或损坏时用默认值。"""

    global _policy_cache  # noqa: PLW0603
    path = policy_path()
    with _POLICY_LOCK:
        try:
            mtime: float | None = path.stat().st_mtime
        except OSError:
            mtime = None
        if _policy_cache is not None and _policy_cache[0] == mtime:
            return _policy_cache[1]
        policy = PersonaPolicy()
        if mtime is not None:
            try:
                policy = _policy_from_raw(json.loads(path.read_text(encoding="utf-8")))
            except (OSError, ValueError):
                dbg(f"私聊人设策略文件损坏，按默认处理: {path}")
        _policy_cache = (mtime, policy)
        return policy


def save_policy(
    patch: dict[str, Any], *, actor: str = "", expected_revision: int | None = None
) -> PersonaPolicy:
    """合并写入策略（未给的字段沿用当前值）；revision 不匹配时 409。"""

    global _policy_cache  # noqa: PLW0603
    with _POLICY_LOCK:
        current = load_policy()
        if expected_revision is not None and int(expected_revision) != current.revision:
            raise PersonaPolicyError("人设策略已被其他页面修改，请刷新后再保存", status=409)
        merged = {**current.as_dict(), **{k: v for k, v in patch.items() if v is not None}}
        if "defaultTemplateId" in patch and patch["defaultTemplateId"] in {"", 0, "0", None}:
            merged["defaultTemplateId"] = None
        default = merged.get("defaultTemplateId")
        if default and int(default) not in _LIBRARY:
            raise PersonaPolicyError("人设库里没有这个人设", field_name="defaultTemplateId")
        policy = _policy_from_raw(merged)
        policy.revision = current.revision + 1
        policy.updated_at = now_beijing().isoformat(timespec="seconds")
        policy.updated_by = str(actor or "")[:32]
        path = policy_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        payload = policy.as_dict()
        payload["defaultTemplateId"] = policy.default_template_id
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(path)
        _policy_cache = None
        return load_policy()


def reset_policy_cache() -> None:
    """测试用：切换数据目录后清缓存。"""

    global _policy_cache  # noqa: PLW0603
    with _POLICY_LOCK:
        _policy_cache = None


# ── 人设库快照 ──────────────────────────────────────────────────────────

_LIBRARY: dict[int, tuple[str, PersonaCard]] = {}


def set_library_snapshot(rows: list[tuple[int, str, PersonaCard]] | None) -> None:
    """人设库加载/修改后刷新：``[(id, 名字, 卡)]``；None 表示人设库不可用。"""

    _LIBRARY.clear()
    for template_id, name, card in rows or []:
        _LIBRARY[int(template_id)] = (str(name), card)


def library_template(template_id: int | None) -> tuple[str, PersonaCard] | None:
    if not template_id:
        return None
    return _LIBRARY.get(int(template_id))


def library_choices() -> list[dict[str, Any]]:
    return [
        {"id": str(tid), "name": name, "summary": persona_card_summary(card)}
        for tid, (name, card) in sorted(_LIBRARY.items(), key=lambda item: item[1][0])
    ]


def find_library_by_name(name: str) -> tuple[int, str, PersonaCard] | None:
    cleaned = " ".join(str(name or "").split()).casefold()
    if not cleaned:
        return None
    for tid, (label, card) in _LIBRARY.items():
        if label.casefold() == cleaned or str(tid) == cleaned:
            return tid, label, card
    for tid, (label, card) in _LIBRARY.items():
        if cleaned in label.casefold():
            return tid, label, card
    return None


# ── 每人状态 ────────────────────────────────────────────────────────────


class PersonaEditError(ValueError):
    """人设写入被拒绝；``status`` 给 WebUI，``code`` 给私聊工具。"""

    def __init__(self, message: str, *, status: int = 422, code: str = "invalid_args") -> None:
        super().__init__(message)
        self.status = status
        self.code = code


def _raw_state(record: dict[str, Any]) -> dict[str, Any]:
    raw = record.get("persona")
    return raw if isinstance(raw, dict) else {}


def _mode(raw: dict[str, Any]) -> str:
    mode = str(raw.get("mode") or MODE_FOLLOW)
    return mode if mode in MODES else MODE_FOLLOW


@dataclass(frozen=True, slots=True)
class ResolvedUserPersona:
    card: PersonaCard
    source: str
    label: str
    template_id: int | None = None
    template_name: str | None = None
    # 他选的模板已经不在人设库里（被删了），这次回退到了下一级。
    missing_template: bool = False
    # 策略总开关关着，他的卡暂时不生效。
    suspended: bool = False


def _fallback(policy: PersonaPolicy) -> ResolvedUserPersona:
    default = library_template(policy.default_template_id)
    if default is not None:
        return ResolvedUserPersona(
            default[1],
            SOURCE_PRIVATE_DEFAULT,
            f"私聊默认（{default[0]}）",
            template_id=policy.default_template_id,
            template_name=default[0],
        )
    resolved = global_persona()
    if resolved.source == PERSONA_SOURCE_GLOBAL:
        label = f"全局人设（{resolved.library_name}）" if resolved.library_name else "全局人设"
    else:
        label = "内置默认"
    return ResolvedUserPersona(
        resolved.card,
        resolved.source if resolved.source in {PERSONA_SOURCE_GLOBAL, PERSONA_SOURCE_BUILTIN} else PERSONA_SOURCE_BUILTIN,
        label,
        template_name=resolved.library_name,
    )


def resolve_from_record(
    record: dict[str, Any], policy: PersonaPolicy | None = None
) -> ResolvedUserPersona:
    policy = policy or load_policy()
    raw = _raw_state(record)
    mode = _mode(raw)
    if mode != MODE_FOLLOW and not policy.enabled:
        fallback = _fallback(policy)
        return ResolvedUserPersona(
            fallback.card,
            fallback.source,
            fallback.label,
            template_id=fallback.template_id,
            template_name=fallback.template_name,
            suspended=True,
        )
    if mode == MODE_CUSTOM:
        card = parse_persona_card(raw.get("card"))
        if card is not None:
            return ResolvedUserPersona(card, SOURCE_USER, "本人专属")
    if mode == MODE_LIBRARY:
        template_id = int(raw.get("template_id") or 0) or None
        found = library_template(template_id)
        if found is not None:
            return ResolvedUserPersona(
                found[1],
                SOURCE_USER_LIBRARY,
                f"本人选的模板（{found[0]}）",
                template_id=template_id,
                template_name=found[0],
            )
        fallback = _fallback(policy)
        return ResolvedUserPersona(
            fallback.card,
            fallback.source,
            fallback.label,
            template_id=fallback.template_id,
            template_name=fallback.template_name,
            missing_template=True,
        )
    return _fallback(policy)


def resolve_user_persona(user_id: int) -> ResolvedUserPersona:
    try:
        return resolve_from_record(private_store.load(user_id))
    except Exception:  # noqa: BLE001
        dbg(f"私聊人设解析失败，回退全局: 用户 {user_id}")
        return _fallback(PersonaPolicy())


def _payload(resolved: ResolvedUserPersona, card: PersonaCard | None = None) -> dict[str, Any]:
    result = compile_persona_card(card or resolved.card, source=resolved.source)
    result["source_label"] = resolved.label
    result["personal"] = resolved.source in PERSONAL_SOURCES
    return result


def resolve_private_persona(
    user_id: int, *, draft: PersonaCard | None = None
) -> dict[str, Any]:
    """私聊提示词用的人设（和 ``persona.resolve_persona`` 同形状，多 source_label/personal）。"""

    if draft is not None:
        result = compile_persona_card(draft, source=PERSONA_SOURCE_DRAFT)
        result["source_label"] = "未保存草稿"
        result["personal"] = True
        return result
    return _payload(resolve_user_persona(user_id))


def persona_scope_needed(persona: dict[str, Any] | None) -> bool:
    return bool((persona or {}).get("personal"))


def _today() -> str:
    return now_beijing().date().isoformat()


def self_edits_today(raw: dict[str, Any]) -> int:
    edits = raw.get("self_edits")
    if isinstance(edits, dict) and edits.get("date") == _today():
        return int(edits.get("count") or 0)
    return 0


def card_changes(before: PersonaCard | None, after: PersonaCard | None) -> list[str]:
    """改了哪些字段（中文名）；用于历史、工具结果与页面。"""

    if before is None or after is None:
        return []
    old = before.model_dump()
    new = after.model_dump()
    return [label for key, label in FIELD_LABELS.items() if old.get(key) != new.get(key)]


def _snapshot(raw: dict[str, Any]) -> dict[str, Any]:
    return {
        "mode": _mode(raw),
        "card": raw.get("card") if isinstance(raw.get("card"), dict) else None,
        "template_id": raw.get("template_id"),
        "template_name": raw.get("template_name"),
    }


def _apply(
    user_id: int,
    *,
    mode: str,
    card: PersonaCard | None,
    template_id: int | None,
    template_name: str | None,
    by: str,
    actor: str,
    action: str,
    expected_revision: int | None,
    count_self: bool,
) -> dict[str, Any]:
    """统一写入：校验锁/次数/版本 → 记历史 → 写回；返回新状态里的要点。"""

    policy = load_policy()

    def change(record: dict[str, Any]) -> dict[str, Any]:
        raw = dict(_raw_state(record))
        revision = int(raw.get("revision") or 0)
        if expected_revision is not None and int(expected_revision) != revision:
            raise PersonaEditError("这位用户的私聊人设已被其他地方修改，请刷新后再保存", status=409, code="conflict")
        if by == BY_SELF:
            if raw.get("locked"):
                raise PersonaEditError("管理员锁定了你的私聊人设，暂时不能自己改", status=403, code="permission")
            if count_self and self_edits_today(raw) >= policy.self_edit_daily:
                raise PersonaEditError(
                    f"今天已经改了 {policy.self_edit_daily} 次人设，明天再来吧", status=429, code="quota"
                )
        before = resolve_from_record(record, policy)
        history = [item for item in raw.get("history") or [] if isinstance(item, dict)]
        history.append(
            {
                **_snapshot(raw),
                "at": now_beijing().isoformat(timespec="seconds"),
                "by": by,
                "actor": str(actor or "")[:32],
                "action": action,
            }
        )
        raw["history"] = history[-MAX_HISTORY:]
        raw["mode"] = mode
        if card is not None:
            raw["card"] = card.storage_dict()
        if mode == MODE_LIBRARY:
            raw["template_id"] = template_id
            raw["template_name"] = template_name
        elif mode == MODE_CUSTOM:
            raw.pop("template_id", None)
            raw.pop("template_name", None)
        raw["revision"] = revision + 1
        raw["updated_at"] = now_beijing().isoformat(timespec="seconds")
        raw["updated_by"] = by
        raw["updated_actor"] = str(actor or "")[:32]
        if by == BY_SELF and count_self:
            raw["self_edits"] = {"date": _today(), "count": self_edits_today(raw) + 1}
        record["persona"] = raw
        after = resolve_from_record(record, policy)
        changed = card_changes(before.card, after.card)
        raw["history"][-1]["changed"] = changed
        return {
            "revision": raw["revision"],
            "source": after.source,
            "label": after.label,
            "changed": changed,
            "summary": persona_card_summary(after.card),
        }

    return private_store.update(user_id, change)


def _validate(card: PersonaCard, *, by: str) -> None:
    reason = persona_override_reason(card)
    if reason:
        raise PersonaEditError(reason)
    if by == BY_SELF:
        from .memory_hygiene import contains_pii, looks_like_instruction

        texts = [
            card.name,
            card.self_name,
            card.user_title,
            card.identity,
            card.personality,
            card.speaking_style,
            *card.catchphrases,
            *card.likes,
            *card.avoid_topics,
            *card.examples,
        ]
        for text in texts:
            if text and looks_like_instruction(text):
                raise PersonaEditError("人设里不能写让我无视规则的话")
            if text and contains_pii(text):
                raise PersonaEditError("人设里别放手机号、证件号这类隐私")


def set_custom(
    user_id: int,
    card: PersonaCard,
    *,
    by: str = BY_ADMIN,
    actor: str = "",
    expected_revision: int | None = None,
    action: str = "set",
) -> dict[str, Any]:
    _validate(card, by=by)
    return _apply(
        user_id,
        mode=MODE_CUSTOM,
        card=card,
        template_id=None,
        template_name=None,
        by=by,
        actor=actor,
        action=action,
        expected_revision=expected_revision,
        count_self=True,
    )


def use_template(
    user_id: int,
    template_id: int,
    *,
    by: str = BY_ADMIN,
    actor: str = "",
    expected_revision: int | None = None,
) -> dict[str, Any]:
    found = library_template(template_id)
    if found is None:
        raise PersonaEditError("人设库里没有这个人设", status=404, code="not_found")
    if by == BY_SELF and not load_policy().allow_library:
        raise PersonaEditError("管理员没有开放自己选人设库模板", status=403, code="permission")
    return _apply(
        user_id,
        mode=MODE_LIBRARY,
        card=None,
        template_id=int(template_id),
        template_name=found[0],
        by=by,
        actor=actor,
        action="template",
        expected_revision=expected_revision,
        count_self=True,
    )


def use_preset(
    user_id: int,
    preset_id: str,
    *,
    by: str = BY_ADMIN,
    actor: str = "",
    expected_revision: int | None = None,
) -> dict[str, Any]:
    preset = PERSONA_PRESETS.get(str(preset_id).strip().lower())
    if preset is None:
        raise PersonaEditError(f"没有模板 {preset_id}", status=404, code="not_found")
    if by == BY_SELF and not load_policy().allow_presets:
        raise PersonaEditError("管理员没有开放自己套用模板", status=403, code="permission")
    current = resolve_user_persona(user_id).card
    card = preset.card.model_copy(update={"name": current.name})
    return set_custom(
        user_id, card, by=by, actor=actor, expected_revision=expected_revision, action="preset"
    )


def patch_fields(
    user_id: int,
    fields: dict[str, Any],
    *,
    by: str = BY_SELF,
    actor: str = "",
) -> dict[str, Any]:
    """按字段改（私聊工具用）：以当前生效的卡为起点，只改给出的字段。"""

    policy = load_policy()
    if not fields:
        raise PersonaEditError("没说要改什么")
    if by == BY_SELF:
        if not policy.enabled:
            raise PersonaEditError("管理员关闭了私聊人设自定义", status=403, code="permission")
        if not policy.self_edit:
            raise PersonaEditError("管理员没有开放自己改人设", status=403, code="permission")
        blocked = [FIELD_LABELS.get(key, key) for key in fields if key not in policy.self_fields]
        if blocked:
            raise PersonaEditError(f"这些不能自己改：{'、'.join(blocked)}", status=403, code="permission")
    unknown = [key for key in fields if key not in FIELD_LABELS]
    if unknown:
        raise PersonaEditError(f"没有这些人设字段：{'、'.join(unknown)}")
    current = resolve_user_persona(user_id).card
    data = current.model_dump()
    for key, value in fields.items():
        if key in _LIST_FIELDS and isinstance(value, str) and value.strip().lower() in {"off", "清空", "无"}:
            value = []
        data[key] = value
    data["preset_id"] = "custom"
    card = PersonaCard.model_validate(data)
    return set_custom(user_id, card, by=by, actor=actor, action="patch")


def reset(
    user_id: int, *, by: str = BY_ADMIN, actor: str = "", expected_revision: int | None = None
) -> dict[str, Any]:
    """回到跟随私聊默认；保留卡片暂存（历史里也有），不算自助次数。"""

    return _apply(
        user_id,
        mode=MODE_FOLLOW,
        card=None,
        template_id=None,
        template_name=None,
        by=by,
        actor=actor,
        action="reset",
        expected_revision=expected_revision,
        count_self=False,
    )


def undo(user_id: int, *, by: str = BY_ADMIN, actor: str = "") -> dict[str, Any]:
    """撤销上一次修改：恢复历史里最近一版，并把这一条历史弹出。"""

    record = private_store.load(user_id)
    raw = _raw_state(record)
    history = [item for item in raw.get("history") or [] if isinstance(item, dict)]
    if not history:
        raise PersonaEditError("没有可以撤销的修改", status=404, code="not_found")
    if by == BY_SELF and raw.get("locked"):
        raise PersonaEditError("管理员锁定了你的私聊人设，暂时不能自己改", status=403, code="permission")
    last = history[-1]

    def change(rec: dict[str, Any]) -> dict[str, Any]:
        state = dict(_raw_state(rec))
        before = resolve_from_record(rec)
        items = [item for item in state.get("history") or [] if isinstance(item, dict)]
        if not items:
            raise PersonaEditError("没有可以撤销的修改", status=404, code="not_found")
        prev = items.pop()
        state["history"] = items
        state["mode"] = prev.get("mode") if prev.get("mode") in MODES else MODE_FOLLOW
        if isinstance(prev.get("card"), dict):
            state["card"] = prev["card"]
        else:
            state.pop("card", None)
        if prev.get("template_id"):
            state["template_id"] = prev.get("template_id")
            state["template_name"] = prev.get("template_name")
        else:
            state.pop("template_id", None)
            state.pop("template_name", None)
        state["revision"] = int(state.get("revision") or 0) + 1
        state["updated_at"] = now_beijing().isoformat(timespec="seconds")
        state["updated_by"] = by
        state["updated_actor"] = str(actor or "")[:32]
        rec["persona"] = state
        after = resolve_from_record(rec)
        return {
            "revision": state["revision"],
            "source": after.source,
            "label": after.label,
            "changed": card_changes(before.card, after.card),
            "summary": persona_card_summary(after.card),
            "undone": last.get("action"),
        }

    return private_store.update(user_id, change)


def set_locked(user_id: int, locked: bool, *, actor: str = "") -> dict[str, Any]:
    def change(record: dict[str, Any]) -> dict[str, Any]:
        raw = dict(_raw_state(record))
        raw["locked"] = bool(locked)
        raw["revision"] = int(raw.get("revision") or 0) + 1
        raw["locked_by"] = str(actor or "")[:32] if locked else ""
        record["persona"] = raw
        return {"revision": raw["revision"], "locked": bool(locked)}

    return private_store.update(user_id, change)


def clear(user_id: int) -> bool:
    """彻底删掉这位用户的私聊人设（含历史）；返回是否真的有东西被删。"""

    def change(record: dict[str, Any]) -> bool:
        had = bool(record.get("persona"))
        record["persona"] = {}
        return had

    return private_store.update(user_id, change)


def export_state(user_id: int) -> dict[str, Any]:
    raw = _raw_state(private_store.load(user_id))
    resolved = resolve_user_persona(user_id)
    return {
        "kind": "yawnbot.private-persona",
        "schemaVersion": 1,
        "userId": str(user_id),
        "exportedAt": now_beijing().isoformat(timespec="seconds"),
        "mode": _mode(raw),
        "templateName": raw.get("template_name"),
        "card": resolved.card.api_dict(),
    }


# ── 序列化（WebUI 与私聊工具） ─────────────────────────────────────────


def history_payload(raw: dict[str, Any]) -> list[dict[str, Any]]:
    items = [item for item in raw.get("history") or [] if isinstance(item, dict)]
    result = []
    for item in reversed(items):
        card = parse_persona_card(item.get("card"))
        result.append(
            {
                "at": item.get("at"),
                "by": item.get("by"),
                "actor": item.get("actor") or None,
                "action": item.get("action"),
                "changed": list(item.get("changed") or []),
                "before": {
                    "mode": item.get("mode"),
                    "templateName": item.get("template_name"),
                    "summary": persona_card_summary(card) if card is not None else None,
                },
            }
        )
    return result


def state_payload(user_id: int) -> dict[str, Any]:
    record = private_store.load(user_id)
    raw = _raw_state(record)
    policy = load_policy()
    resolved = resolve_from_record(record, policy)
    stored = parse_persona_card(raw.get("card"))
    compiled = _payload(resolved)
    prefs = record.get("prefs") if isinstance(record.get("prefs"), dict) else {}
    call_me = str(prefs.get("call_me") or "")
    return {
        "userId": str(user_id),
        "mode": _mode(raw),
        "card": (stored or resolved.card).api_dict(),
        "effectiveCard": resolved.card.api_dict(),
        "source": resolved.source,
        "sourceLabel": resolved.label,
        "personal": resolved.source in PERSONAL_SOURCES,
        "templateId": str(raw.get("template_id")) if raw.get("template_id") else None,
        "templateName": raw.get("template_name") or None,
        "missingTemplate": resolved.missing_template,
        "suspended": resolved.suspended,
        "summary": persona_card_summary(resolved.card),
        "compiled": prompt_persona(compiled),
        "promptTokens": persona_prompt_tokens(compiled),
        "promptTokenLimit": PERSONA_PROMPT_TOKEN_LIMIT,
        "trimmed": bool(compiled.get("trimmed")),
        "locked": bool(raw.get("locked")),
        "revision": int(raw.get("revision") or 0),
        "updatedAt": raw.get("updated_at") or None,
        "updatedBy": raw.get("updated_by") or None,
        "selfEditsToday": self_edits_today(raw),
        "selfEditDaily": policy.self_edit_daily,
        # 最终怎么称呼他：他在聊天偏好里设的 call_me 优先于人设卡里的称呼。
        "callThem": call_me or resolved.card.user_title or None,
        "callThemSource": "chat_prefs" if call_me else ("persona" if resolved.card.user_title else None),
        "history": history_payload(raw),
    }


def user_rows(limit: int = 500) -> list[dict[str, Any]]:
    """有私聊人设记录（不是一直跟随）的用户列表，最近修改在前。"""

    policy = load_policy()
    rows: list[dict[str, Any]] = []
    for user_id in private_store.known_user_ids():
        record = private_store.load(user_id)
        raw = _raw_state(record)
        if not raw:
            continue
        resolved = resolve_from_record(record, policy)
        rows.append(
            {
                "userId": str(user_id),
                "mode": _mode(raw),
                "source": resolved.source,
                "sourceLabel": resolved.label,
                "name": resolved.card.name,
                "summary": persona_card_summary(resolved.card),
                "templateName": raw.get("template_name") or None,
                "missingTemplate": resolved.missing_template,
                "locked": bool(raw.get("locked")),
                "revision": int(raw.get("revision") or 0),
                "updatedAt": raw.get("updated_at") or None,
                "updatedBy": raw.get("updated_by") or None,
                "selfEditsToday": self_edits_today(raw),
            }
        )
    rows.sort(key=lambda row: str(row.get("updatedAt") or ""), reverse=True)
    return rows[:limit]


def stats(rows: list[dict[str, Any]] | None = None) -> dict[str, int]:
    rows = rows if rows is not None else user_rows()
    return {
        "custom": sum(1 for row in rows if row["mode"] == MODE_CUSTOM),
        "library": sum(1 for row in rows if row["mode"] == MODE_LIBRARY),
        "follow": sum(1 for row in rows if row["mode"] == MODE_FOLLOW),
        "locked": sum(1 for row in rows if row["locked"]),
        "missingTemplate": sum(1 for row in rows if row["missingTemplate"]),
        "selfEditsToday": sum(int(row["selfEditsToday"]) for row in rows),
    }


def template_usage() -> dict[str, int]:
    """人设库每张模板被多少位私聊用户选用：``{模板 id: 人数}``。"""

    usage: dict[str, int] = {}
    for user_id in private_store.known_user_ids():
        raw = _raw_state(private_store.load(user_id))
        if _mode(raw) == MODE_LIBRARY and raw.get("template_id"):
            key = str(raw["template_id"])
            usage[key] = usage.get(key, 0) + 1
    return usage


def tool_view(user_id: int) -> dict[str, Any]:
    """私聊工具 my_persona view 的结果：给模型看，不含内部 id。"""

    state = state_payload(user_id)
    policy = load_policy()
    card = PersonaCard.model_validate(state["effectiveCard"])
    levels = {
        persona_trait_name(key): f"{getattr(card, key)}/4 {persona_trait_label(key, getattr(card, key))}"
        for key in PERSONA_TRAIT_FIELDS
    }
    can_edit = policy.enabled and policy.self_edit and not state["locked"]
    return {
        "source": state["sourceLabel"],
        "name": card.name,
        "self_name": card.self_name or "我",
        "calls_him": state["callThem"] or "（没设）",
        "identity": card.identity,
        "personality": card.personality,
        "speaking_style": card.speaking_style,
        "catchphrases": card.catchphrases,
        "likes": card.likes,
        "avoid_topics": card.avoid_topics,
        "levels": levels,
        "can_edit": can_edit,
        "edits_left_today": max(0, policy.self_edit_daily - state["selfEditsToday"]) if can_edit else 0,
        "editable_fields": [FIELD_LABELS[key] for key in policy.self_fields] if can_edit else [],
        "locked": state["locked"],
        "note": "这是你们私聊里的人设，只在和他的私聊里生效，群里照旧",
    }


def tool_options() -> dict[str, Any]:
    policy = load_policy()
    return {
        "presets": [
            {"id": preset.id, "label": preset.label, "description": preset.description}
            for preset in PERSONA_PRESETS.values()
        ]
        if policy.allow_presets
        else [],
        "library": [item["name"] for item in library_choices()] if policy.allow_library else [],
        "fields": {key: FIELD_LABELS[key] for key in policy.self_fields},
    }


# ── 定时问候（E1） ─────────────────────────────────────────────────────


def greeting_voice(record: dict[str, Any]) -> tuple[str, str]:
    """(自称, 口癖)：定时问候的固定句子按他的私聊人设带一点口吻；跟随默认时都为空。"""

    try:
        resolved = resolve_from_record(record)
    except Exception:  # noqa: BLE001
        return "", ""
    if resolved.source not in PERSONAL_SOURCES:
        return "", ""
    card = resolved.card
    return card.self_name, (card.catchphrases[0] if card.catchphrases else "")


def personalize_greeting(record: dict[str, Any], text: str) -> str:
    """把固定问候里的第一个“我”换成他人设的自称、句尾带一个口癖（不过模型）。"""

    self_name, catchphrase = greeting_voice(record)
    result = text
    if self_name and self_name != "我":
        result = result.replace("我", self_name, 1)
    if catchphrase and catchphrase not in result:
        result = f"{result.rstrip('。！!～~')}～{catchphrase}"
    return result


# ── 表情包频率（E2） ───────────────────────────────────────────────────

# 他专属人设的表现力 0–4 档 → 私聊每日表情包额度的倍率：只会收窄，最高就是后台上限。
STICKER_SCALE = (0.3, 1.0, 1.0, 1.0, 1.0)


def sticker_scale(user_id: int) -> float:
    try:
        resolved = resolve_user_persona(user_id)
    except Exception:  # noqa: BLE001
        return 1.0
    if resolved.source not in PERSONAL_SOURCES:
        return 1.0
    level = max(0, min(4, int(resolved.card.expressiveness)))
    return STICKER_SCALE[level]


__all__ = [
    "BY_ADMIN",
    "BY_SELF",
    "EDITABLE_FIELDS",
    "FIELD_LABELS",
    "MODES",
    "MODE_CUSTOM",
    "MODE_FOLLOW",
    "MODE_LIBRARY",
    "PERSONAL_SOURCES",
    "SOURCE_PRIVATE_DEFAULT",
    "SOURCE_USER",
    "SOURCE_USER_LIBRARY",
    "PersonaEditError",
    "PersonaPolicy",
    "PersonaPolicyError",
    "ResolvedUserPersona",
    "card_changes",
    "clear",
    "export_state",
    "find_library_by_name",
    "greeting_voice",
    "library_choices",
    "library_template",
    "load_policy",
    "patch_fields",
    "persona_scope_needed",
    "personalize_greeting",
    "reset",
    "reset_policy_cache",
    "resolve_from_record",
    "resolve_private_persona",
    "resolve_user_persona",
    "save_policy",
    "set_custom",
    "set_library_snapshot",
    "set_locked",
    "state_payload",
    "stats",
    "sticker_scale",
    "template_usage",
    "tool_options",
    "tool_view",
    "undo",
    "use_preset",
    "use_template",
    "user_rows",
]
