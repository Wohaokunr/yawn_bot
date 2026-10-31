# ruff: noqa: E501, PLR2004, ARG001, ARG002
"""表情包系统：[表情:名字] 标记解析、表情包库、频率闸门、发送链路（自成一条、
表情包外显）、提示词菜单、主动发言的人设约束、save_sticker 工具、声明核验和
WebUI 接口。全部在临时目录里跑，不碰仓库 data/。
"""

from __future__ import annotations

import importlib
import json
import sys
from dataclasses import replace
from pathlib import Path
from typing import Any

import nonebot
import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))
try:
    nonebot.get_driver()
except ValueError:
    nonebot.init()
if nonebot.get_plugin("yawn_core") is None:
    nonebot.load_from_toml("pyproject.toml")

_PKG = "src.plugins.yawn_core.yawn_agent"
markup = importlib.import_module(f"{_PKG}.sticker_markup")
library = importlib.import_module(f"{_PKG}.sticker_library")
policy = importlib.import_module(f"{_PKG}.sticker_policy")
sticker_send = importlib.import_module(f"{_PKG}.sticker_send")
speech = importlib.import_module(f"{_PKG}.speech")
speech_policy = importlib.import_module(f"{_PKG}.speech_policy")
speech_runtime = importlib.import_module(f"{_PKG}.speech_runtime")
outbound = importlib.import_module(f"{_PKG}.outbound")
reactions = importlib.import_module(f"{_PKG}.reactions")
proactive_policy = importlib.import_module(f"{_PKG}.proactive_policy")
claims = importlib.import_module(f"{_PKG}.speech_claims")
tools = importlib.import_module(f"{_PKG}.tools")
registry = importlib.import_module(f"{_PKG}.tool_registry")
sections = importlib.import_module(f"{_PKG}.prompt_sections")
inbox = importlib.import_module(f"{_PKG}.sandbox_inbox")
handler = importlib.import_module(f"{_PKG}.tool_handlers.sticker")
webui_routes = importlib.import_module("src.plugins.yawn_core.webui.stickers")

GROUP = 123456
USER = 42


def _png(tag: str) -> bytes:
    return b"\x89PNG\r\n\x1a\n" + tag.encode() * 8


@pytest.fixture(autouse=True)
def _isolated(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AGENT_FILE_ROOT", str(tmp_path / "agent_files"))
    monkeypatch.setattr(policy, "STATE_DIR", tmp_path / "stickers_state")
    library.reset_cache_for_tests()
    policy.reset_for_tests()


def _add(name: str, *, tags: tuple[str, ...] = (), groups: tuple[int, ...] = (), status: str = "active") -> Any:
    sticker, created = library.add_sticker(_png(name), name=name, tags=list(tags), groups=list(groups), status=status)
    assert created
    return sticker


def _save(**changes: Any) -> None:
    policy.save_settings(policy.StickerSettings(**changes))


# ---- 标记解析 ----------------------------------------------------------------


def test_markup_expands_each_sticker_into_its_own_part() -> None:
    segments, count = markup.expand_sticker_segments([{"type": "text", "text": "哈哈哈[表情:捂脸笑]你也太离谱了"}])
    assert count == 1
    assert [item["type"] for item in segments] == ["text", "break", "sticker", "break", "text"]
    assert segments[2]["name"] == "捂脸笑"
    for variant in ("【表情：狗头】", "[表情包: 狗头]", "[sticker:狗头]"):
        assert markup.sticker_names(variant) == ["狗头"]
    # 代码块里的方括号原样保留
    code = "```\nx = [表情:不是表情]\n```"
    assert markup.expand_sticker_segments([{"type": "text", "text": code}])[1] == 0
    assert markup.strip_sticker_markup("好的[表情:ok]") == "好的"
    assert markup.describe_sticker_markup("好[表情:ok]") == "好[表情包: ok]"


def test_dropping_a_sticker_rejoins_the_text_but_keeps_model_breaks() -> None:
    plan = speech.speech_plan_from_text("你那张[表情包: 狗头]笑死")
    index = next(i for i, item in enumerate(plan.segments) if item["type"] == "sticker")
    merged = markup.drop_sticker_at(list(plan.segments), {index})
    assert merged == [{"type": "text", "text": "你那张笑死"}]
    plan = speech.speech_plan_from_text("哈哈|||[表情:不存在]|||好的")
    index = next(i for i, item in enumerate(plan.segments) if item["type"] == "sticker")
    kept = markup.drop_sticker_at(list(plan.segments), {index})
    assert [item["type"] for item in kept] == ["text", "break", "text"]


def test_text_plan_with_sticker_becomes_segment_sequence() -> None:
    plan = speech.speech_plan_from_text("【节奏:连发】不会吧|||[表情:震惊]|||真的假的")
    types = [item["type"] for item in plan.segments]
    assert types == ["text", "break", "sticker", "break", "text"]
    assert plan.rhythm == "burst"
    assert plan.should_speak
    only = speech.speech_plan_from_text("[表情:狗头]")
    assert [item["type"] for item in only.segments] == ["sticker"] and only.should_speak


# ---- 表情包库 ----------------------------------------------------------------


def test_library_dedupes_by_content_and_matches_menu_choices() -> None:
    first = _add("捂脸笑", tags=("尴尬", "好笑"))
    again, created = library.add_sticker(_png("捂脸笑"), name="别的名字")
    assert not created and again.sticker_id == first.sticker_id
    _add("尬笑", tags=("尴尬",))
    assert library.match_stickers("捂脸笑", GROUP)[0] == "name"
    assert library.match_stickers(first.sticker_id, GROUP)[0] == "id"
    how, candidates = library.match_stickers("尴尬", GROUP)
    assert how == "tag" and len(candidates) == 2
    # 不做模糊匹配
    assert library.match_stickers("捂脸", GROUP) == ("none", [])
    with pytest.raises(library.StickerError):
        library.add_sticker(b"not an image", name="x")
    with pytest.raises(library.StickerError):
        library.add_sticker(_png("noname"), name="")


def test_library_respects_status_groups_and_enabled() -> None:
    _add("待审", status="pending")
    only_other = _add("别的群", groups=(999,))
    off = _add("停用")
    library.update_sticker(off.sticker_id, enabled=False)
    assert library.available_stickers(GROUP) == []
    assert [item.name for item in library.available_stickers(999)] == [only_other.name]
    with pytest.raises(ValueError, match="没有对本群开放"):
        reactions.resolve_reaction(only_other.sticker_id, group_id=GROUP)


def test_legacy_reaction_index_still_works(tmp_path: Path) -> None:
    root = tmp_path / "agent_files" / "reactions"
    root.mkdir(parents=True)
    (root / "speechless.png").write_bytes(_png("old"))
    (root / "index.json").write_text(
        json.dumps({"reactions": [{"id": "speechless_01", "file": "speechless.png", "tags": ["无语", "沉默"], "description": "无语摊手"}]}, ensure_ascii=False),
        encoding="utf-8",
    )
    library.reset_cache_for_tests()
    entry = library.get_sticker("speechless_01")
    assert entry is not None and entry.name == "无语" and entry.usable
    assert reactions.search_reactions("无语")[0]["reaction_id"] == "speechless_01"
    # 改一下再写回，旧条目保留、新字段补上
    library.update_sticker("speechless_01", name="无语摊手")
    raw = json.loads((root / "index.json").read_text(encoding="utf-8"))
    assert raw["reactions"][0]["file"] == "speechless.png" and raw["reactions"][0]["name"] == "无语摊手"


def test_forget_member_clears_owner_only_in_that_group() -> None:
    sticker, _ = library.add_sticker(_png("mine"), name="我的", source="member", added_by=USER, added_group=GROUP)
    other, _ = library.add_sticker(_png("other"), name="别群", source="member", added_by=USER, added_group=777)
    assert library.forget_member(GROUP, USER) == 1
    assert library.get_sticker(sticker.sticker_id).added_by is None
    assert library.get_sticker(other.sticker_id).added_by == USER


# ---- 频率闸门 ----------------------------------------------------------------


def test_exposure_reasons_and_cooldown() -> None:
    assert policy.exposure(GROUP).reason == "empty"
    _add("狗头")
    assert policy.exposure(GROUP).open
    _save(enabled=False)
    assert policy.exposure(GROUP).reason == "closed"
    _save(group_mode="allowlist", allowed_groups=[1])
    assert policy.exposure(GROUP).reason == "group_closed"
    _save(allow_proactive=False)
    assert policy.exposure(GROUP, proactive=True).reason == "proactive_off"
    _save(group_cooldown_seconds=60)
    policy.note_sent(GROUP, ["x"])
    gate = policy.exposure(GROUP)
    assert gate.reason == "cooldown" and gate.cooldown_seconds > 0
    _save(group_cooldown_seconds=0, group_daily_limit=1)
    assert policy.exposure(GROUP).reason == "daily_limit"


def test_pick_prefers_stickers_not_sent_recently() -> None:
    first = _add("a1", tags=("开心",))
    second = _add("a2", tags=("开心",))
    policy.note_sent(GROUP, [first.sticker_id])
    for _ in range(10):
        assert policy.pick_sticker(GROUP, [first, second]).sticker_id == second.sticker_id


# ---- 发送前解析 --------------------------------------------------------------


def test_resolve_turns_names_into_reaction_segments_and_drops_the_rest() -> None:
    dog = _add("狗头", tags=("调侃",))
    _save(max_per_message=1)
    plan = speech.speech_plan_from_text("你说得对[表情:狗头]|||[表情:狗头]")
    resolved = sticker_send.resolve_plan_stickers(plan, group_id=GROUP)
    reactions_ = [item for item in resolved.segments if item["type"] == "reaction"]
    assert reactions_ == [{"type": "reaction", "reaction_id": dog.sticker_id}]
    assert not any(item["type"] == "sticker" for item in resolved.segments)


def test_resolve_falls_back_to_text_when_only_an_unknown_sticker() -> None:
    _add("狗头")
    plan = speech.speech_plan_from_text("[表情:不存在的表情]")
    resolved = sticker_send.resolve_plan_stickers(plan, group_id=GROUP)
    assert resolved.segments == ({"type": "text", "text": "[不存在的表情]"},)


def test_resolve_drops_everything_during_cooldown() -> None:
    _add("狗头")
    _save(group_cooldown_seconds=600)
    policy.note_sent(GROUP, ["prev"])
    plan = speech.speech_plan_from_text("行吧[表情:狗头]")
    resolved = sticker_send.resolve_plan_stickers(plan, group_id=GROUP)
    assert resolved.segments == ({"type": "text", "text": "行吧"},)



def test_dropping_one_of_two_adjacent_stickers_keeps_the_other_on_its_own() -> None:
    _add("捂脸")
    _add("狗头")
    _save(max_per_message=1)
    plan = speech.speech_plan_from_text("哈哈[表情:捂脸][表情:狗头]好的")
    resolved = sticker_send.resolve_plan_stickers(plan, group_id=GROUP)
    types = [item["type"] for item in resolved.segments]
    assert types == ["text", "break", "reaction", "break", "text"]


def test_resolve_caps_stickers_at_what_is_left_today() -> None:
    _add("狗头")
    _add("捂脸")
    _save(max_per_message=3, group_daily_limit=2, group_cooldown_seconds=0)
    policy.note_sent(GROUP, ["prev"])
    plan = speech.speech_plan_from_text("[表情:狗头]|||[表情:捂脸]|||[表情:狗头]")
    resolved = sticker_send.resolve_plan_stickers(plan, group_id=GROUP)
    assert sum(item["type"] == "reaction" for item in resolved.segments) == 1


def test_resolve_respects_persona_that_forbids_stickers() -> None:
    _add("狗头")
    plan = speech.speech_plan_from_text("行吧[表情:狗头]")
    quiet = replace(plan, style=replace(plan.style, allow_spontaneous_reaction=False))
    resolved = sticker_send.resolve_plan_stickers(quiet, group_id=GROUP)
    assert resolved.segments == ({"type": "text", "text": "行吧"},)


def test_search_reactions_finds_stickers_scoped_to_this_group() -> None:
    _add("本群狗头", groups=(GROUP,))
    found = reactions.search_reactions("本群狗头", group_id=GROUP)
    assert [item["name"] for item in found] == ["本群狗头"]


class _Bot:
    self_id = "10000"

    def __init__(self) -> None:
        self.sent: list[Any] = []

    async def call_api(self, action: str, **params: Any) -> Any:
        if action == "send_group_msg":
            self.sent.append(params["message"])
            return {"message_id": 100 + len(self.sent)}
        return {}

    async def send_group_msg(self, *, group_id: int, message: Any) -> Any:
        self.sent.append(message)
        return {"message_id": 100 + len(self.sent)}


@pytest.mark.asyncio
async def test_sticker_is_its_own_message_and_marked_as_sticker() -> None:
    dog = _add("狗头")
    plan = speech.speech_plan_from_text("懂了|||[表情:狗头]")
    prepared = await outbound.prepare_speech_parts(plan, session=None, group_id=GROUP)
    assert len(prepared.messages) == 2
    first, second = prepared.messages
    assert [segment.type for segment in first.message] == ["text"]
    image = next(iter(second.message))
    assert image.type == "image"
    assert image.data["sub_type"] == 1 and image.data["summary"] == "[狗头]"
    assert str(image.data["file"]).startswith("base64://")
    assert second.normalized_text == "[表情包: 狗头]"
    assert second.media_refs == ({"type": "image", "reaction_id": dog.sticker_id, "source": "reaction"},)
    assert "agent_files" not in repr(second.segment_records)


@pytest.mark.asyncio
async def test_successful_send_starts_cooldown_and_counts_usage(monkeypatch: pytest.MonkeyPatch) -> None:
    dog = _add("狗头")
    _save(group_cooldown_seconds=120)
    prepared = await outbound.prepare_outbound_message(
        [{"type": "reaction", "reaction_id": dog.sticker_id}], session=None, group_id=GROUP
    )

    async def fake_send(_bot: Any, _group: int, _message: Any) -> Any:
        return {"message_id": 5}

    monkeypatch.setattr(outbound, "_call_group_send", fake_send)
    monkeypatch.setattr(outbound, "get_segment_capabilities", lambda *_a, **_k: type("C", (), {"supported": frozenset({"image"}), "runtime_unsupported": frozenset()})())
    result = await outbound.send_prepared_outbound(_Bot(), GROUP, prepared)
    assert result.sent
    assert policy.exposure(GROUP).reason == "cooldown"
    assert policy.stats_snapshot()[dog.sticker_id]["uses"] == 1
    assert policy.usage_snapshot()["groups"][str(GROUP)] == 1


def test_plain_text_channel_strips_markers() -> None:
    prepared = outbound.prepare_text_message("收到[表情:ok]")
    assert prepared.normalized_text == "收到"
    only = outbound.prepare_text_message("[表情:ok]")
    assert only.normalized_text == "[ok]"


def test_rhythm_preview_labels_sticker_parts() -> None:
    preview = speech_runtime.rhythm_preview(text="哈哈|||[表情:狗头]")
    assert [item["text"] for item in preview["parts"]] == ["哈哈", "[表情包: 狗头]"]


# ---- 提示词 ------------------------------------------------------------------


def _speech_sections(group_id: int) -> dict[str, str]:
    return dict(speech_policy.build_speech_parts(None, None, group_id=group_id))


def test_speech_sticker_section_shows_menu_only_when_usable() -> None:
    assert {"speech.sticker", "proactive.sticker"} <= {item.id for item in sections.SECTIONS}
    assert _speech_sections(GROUP)["speech.sticker"] == ""
    _add("狗头", tags=("调侃", "无语"))
    _add("白眼", tags=("无语",))
    text = _speech_sections(GROUP)["speech.sticker"]
    assert "[表情:名字]" in text and "狗头" in text and "白眼" in text
    assert "无语×2" in text
    _save(group_cooldown_seconds=300)
    policy.note_sent(GROUP, ["x"])
    assert _speech_sections(GROUP)["speech.sticker"] == ""


def test_menu_is_bounded_and_rotates_recently_sent_to_the_back() -> None:
    created = [_add(f"图{i}") for i in range(20)]
    menu = policy.menu_entries(GROUP, tuple(created), size=5)
    assert len(menu) == 5
    few = created[:4]
    policy.note_sent(GROUP, [item.sticker_id for item in few[:3]])
    menu = policy.menu_entries(GROUP, tuple(few), size=1)
    assert menu[0][0] == "图3"


def test_proactive_prompt_has_sticker_section_and_persona_can_veto() -> None:
    _add("狗头")
    config = type("Cfg", (), {"group_id": GROUP, "recent_response_fingerprints": [], "persona_enabled": False, "persona_profile": None})()
    parts = dict(proactive_policy.build_user_prompt_parts("active", config))
    assert "狗头" in parts.get("proactive.sticker", "")


def test_low_reaction_persona_strips_sticker_markup_from_proactive(monkeypatch: pytest.MonkeyPatch) -> None:
    behavior = type("B", (), {"allow_spontaneous_reaction": False})()
    monkeypatch.setattr(proactive_policy, "persona_behavior", lambda _config: behavior)
    decision = proactive_policy.ProactiveDecision("speak", "哈哈[表情:狗头]", None, "r", ({"type": "text", "text": "哈哈[表情:狗头]"},))
    result = proactive_policy.apply_persona_behavior_to_decision(object(), decision)
    assert result.text == "哈哈"
    assert result.segments == ({"type": "text", "text": "哈哈"},)
    only = proactive_policy.ProactiveDecision("speak", "[表情:狗头]", None, "r")
    assert proactive_policy.apply_persona_behavior_to_decision(object(), only).action == "wait"


# ---- save_sticker -----------------------------------------------------------


class _Rows:
    def __init__(self, values: list[int]) -> None:
        self._values = values

    def scalars(self) -> list[int]:
        return list(self._values)


class _MemberSession:
    """调用者是本群成员；``opted_out`` 是隐私退出名单；审计写入直接吞掉。"""

    def __init__(self, opted_out: list[int] | None = None) -> None:
        self.opted_out = opted_out or []
        self.added: list[Any] = []

    async def scalar(self, _statement: Any) -> int:
        return USER

    async def execute(self, _statement: Any) -> _Rows:
        return _Rows(self.opted_out)

    def add(self, item: Any) -> None:
        self.added.append(item)

    async def flush(self) -> None:
        return None

    async def commit(self) -> None:
        return None

    async def rollback(self) -> None:
        return None


def _context(*, admin: bool = False, attachments: tuple[Any, ...] = (), opted_out: list[int] | None = None) -> Any:
    context = tools.ToolExecutionContext(
        bot=object(),
        group_id=GROUP,
        actor_user_id=USER,
        session=_MemberSession(opted_out),
        capabilities=tools.BotGroupCapabilities(role="member", can_manage=False, actions=frozenset()),
        actor_can_manage=admin,
        privileged_allowlist=frozenset(),
    )
    context.attachments = attachments
    return context


def _attachment(source: str = "reply") -> Any:
    return inbox.Attachment(key="1", media_type="image", source=source, ref={"type": "image", "file": "abc.image", "sub_type": 1}, index=1, sender_id=7)


@pytest.fixture
def fake_fetch(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fetch(_bot: Any, _group: int, attachment: Any, *, max_bytes: int) -> Any:
        return inbox._Fetch(_png(f"fetched-{attachment.key}"), "test")

    monkeypatch.setattr(handler, "fetch_bytes", fetch)


@pytest.mark.asyncio
async def test_save_sticker_goes_to_review_then_admin_saves_are_active(fake_fetch: None) -> None:
    result = await tools.execute_tool_with_meta("save_sticker", {"name": "狗头", "tags": ["调侃"]}, context=_context(attachments=(_attachment(),)))
    data = result.payload["result"]
    assert result.payload["ok"] is True and data["saved"] is True and data["status"] == "待管理员审核"
    assert library.available_stickers(GROUP) == []
    assert library.all_stickers()[0].added_by == USER
    library.delete_sticker(library.all_stickers()[0].sticker_id)
    admin = await tools.execute_tool_with_meta("save_sticker", {"name": "狗头"}, context=_context(admin=True, attachments=(_attachment(),)))
    assert admin.payload["result"]["status"] == "可用"
    assert [item.name for item in library.available_stickers(GROUP)] == ["狗头"]


@pytest.mark.asyncio
async def test_save_sticker_policy_and_missing_image(fake_fetch: None) -> None:
    _save(collect_mode="off")
    off = await tools.execute_tool_with_meta("save_sticker", {"name": "x"}, context=_context(attachments=(_attachment(),)))
    assert off.payload["ok"] is False
    _save(collect_mode="auto", member_daily_collect=1)
    first = await tools.execute_tool_with_meta("save_sticker", {"name": "x"}, context=_context(attachments=(_attachment(),)))
    assert first.payload["result"]["status"] == "可用"
    second = await tools.execute_tool_with_meta("save_sticker", {"name": "y"}, context=_context(attachments=(_attachment(),)))
    assert second.payload["ok"] is False
    _save(collect_mode="auto")
    missing = await tools.execute_tool_with_meta("save_sticker", {"name": "z"}, context=_context())
    assert missing.payload["ok"] is False
    private = await tools.execute_tool_with_meta("save_sticker", {"name": "z"}, context=_context(attachments=(_attachment(),), opted_out=[7]))
    assert private.payload["ok"] is False


def test_save_sticker_registered_in_reaction_family_and_claims_verified() -> None:
    definition = registry.TOOL_BY_NAME["save_sticker"]
    assert definition.family == "reaction" and definition.permission_level == "state_write"
    checked = claims.check_action_claims("好了，表情包收藏好了！", frozenset())
    assert checked.changed and "sticker_save" in checked.families
    assert not claims.check_action_claims("表情包收藏好了！", frozenset({"save_sticker"})).changed


# ---- WebUI -------------------------------------------------------------------


class _UploadRequest:
    def __init__(self, data: bytes) -> None:
        self.headers = {"content-length": str(len(data))}
        self._data = data

    async def stream(self) -> Any:
        yield self._data


@pytest.mark.asyncio
async def test_webui_upload_edit_review_and_delete() -> None:
    uploaded = (await webui_routes.sticker_upload(_UploadRequest(_png("u1")), None, name="捂脸笑", tags="尴尬,好笑", description=""))["data"]
    assert uploaded["created"] is True and uploaded["sticker"]["tags"] == ["尴尬", "好笑"]
    sticker_id = uploaded["sticker"]["id"]
    image = await webui_routes.sticker_image(sticker_id, None)
    assert image.body.startswith(b"\x89PNG") and image.headers["x-content-type-options"] == "nosniff"
    patched = (await webui_routes.sticker_patch(sticker_id, webui_routes.StickerPatch(name="尬笑", groups=[str(GROUP)]), None))["data"]["sticker"]
    assert patched["name"] == "尬笑" and patched["groups"] == [str(GROUP)]
    pending, _ = library.add_sticker(_png("p"), name="待审", status="pending")
    overview = (await webui_routes.stickers_overview(None))["data"]
    assert overview["counts"] == {"total": 2, "active": 1, "pending": 1, "disabled": 0}
    assert "file" not in json.dumps(overview["stickers"], ensure_ascii=False).replace("fileMissing", "")
    done = (await webui_routes.sticker_bulk(webui_routes.BulkBody(ids=[pending.sticker_id, "nope"], action="approve"), None))["data"]
    assert done == {"done": [pending.sticker_id], "missing": ["nope"]}
    await webui_routes.sticker_delete(sticker_id, None)
    assert library.get_sticker(sticker_id) is None
    with pytest.raises(webui_routes.HTTPException):
        await webui_routes.sticker_upload(_UploadRequest(b"nope"), None, name="x", tags="", description="")


@pytest.mark.asyncio
async def test_webui_settings_and_prompt_preview() -> None:
    body = webui_routes.SettingsBody.model_validate({**webui_routes._settings_payload(policy.StickerSettings()), "frequency": "often", "allowedGroups": [str(GROUP)]})
    saved = (await webui_routes.sticker_update_settings(body, None))["data"]["settings"]
    assert saved["frequency"] == "often" and saved["allowedGroups"] == [str(GROUP)]
    bad = webui_routes.SettingsBody.model_validate({**webui_routes._settings_payload(policy.StickerSettings()), "blockedGroups": ["abc"]})
    with pytest.raises(webui_routes.HTTPException):
        await webui_routes.sticker_update_settings(bad, None)
    empty = (await webui_routes.sticker_prompt_preview(None, group_id=str(GROUP)))["data"]
    assert empty["open"] is False and empty["reason"] == "empty" and empty["text"] == ""
    _add("狗头")
    preview = (await webui_routes.sticker_prompt_preview(None, group_id=str(GROUP)))["data"]
    assert preview["open"] is True and "狗头" in preview["text"] and "爱发表情包" in preview["text"]
