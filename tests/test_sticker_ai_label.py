# ruff: noqa: E501, PLR2004, ARG001, ARG002, FBT003, DTZ001
"""表情包 AI 标注与偷图：解析/合并纯函数、标注缓存、统一入库、撤销、群聊 steal_sticker、
save_sticker 不写名字、私聊 stickers 工具、批量标注任务、WebUI 接口和提示词。
全部在临时目录里跑，识图模型用假的 complete 代替。
"""

from __future__ import annotations

import asyncio
import importlib
import json
import sys
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
llm = importlib.import_module("src.plugins.yawn_core.llm")
library = importlib.import_module(f"{_PKG}.sticker_library")
policy = importlib.import_module(f"{_PKG}.sticker_policy")
labeler = importlib.import_module(f"{_PKG}.sticker_labeler")
intake = importlib.import_module(f"{_PKG}.sticker_intake")
jobs = importlib.import_module(f"{_PKG}.sticker_label_jobs")
markup = importlib.import_module(f"{_PKG}.sticker_markup")
sticker_send = importlib.import_module(f"{_PKG}.sticker_send")
claims = importlib.import_module(f"{_PKG}.speech_claims")
tools = importlib.import_module(f"{_PKG}.tools")
registry = importlib.import_module(f"{_PKG}.tool_registry")
inbox = importlib.import_module(f"{_PKG}.sandbox_inbox")
handler = importlib.import_module(f"{_PKG}.tool_handlers.sticker")
ptools = importlib.import_module(f"{_PKG}.private_tools")
psticker = importlib.import_module(f"{_PKG}.private_sticker_tool")
catalog = importlib.import_module(f"{_PKG}.private_tool_catalog")
sections = importlib.import_module(f"{_PKG}.prompt_sections")
wake = importlib.import_module(f"{_PKG}.schedule_wake")
planner = importlib.import_module(f"{_PKG}.tool_planner")
webui_routes = importlib.import_module("src.plugins.yawn_core.webui.stickers")

GROUP = 123456
USER = 42
SENDER = 7


def _png(tag: str) -> bytes:
    return b"\x89PNG\r\n\x1a\n" + tag.encode() * 8


def _reply(**fields: Any) -> str:
    data = {"name": "捂脸笑", "tags": ["尴尬", "好笑"], "description": "被戳穿时", "text": "绷不住了", "kind": "sticker", "safe": True, "confidence": 0.9}
    data.update(fields)
    return json.dumps(data, ensure_ascii=False)


class FakeVision:
    """假的识图模型：记录调用次数，按队列返回。"""

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.calls: list[list[dict[str, Any]]] = []
        self.replies: list[str | None] = []
        self.default: str | None = _reply()
        monkeypatch.setattr(labeler, "vision_ready", lambda: True)
        monkeypatch.setattr(llm, "complete", self.complete)

    async def complete(self, messages: Any, **kwargs: Any) -> str | None:
        self.calls.append(messages)
        assert kwargs["task"] == "agent_image"
        assert kwargs["max_tokens"] == llm.NO_OUTPUT_LIMIT
        return self.replies.pop(0) if self.replies else self.default


@pytest.fixture(autouse=True)
def _isolated(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AGENT_FILE_ROOT", str(tmp_path / "agent_files"))
    monkeypatch.setattr(policy, "STATE_DIR", tmp_path / "stickers_state")
    library.reset_cache_for_tests()
    policy.reset_for_tests()
    labeler.reset_for_tests()
    jobs.reset_for_tests()
    psticker.reset_for_tests()


@pytest.fixture
def vision(monkeypatch: pytest.MonkeyPatch) -> FakeVision:
    return FakeVision(monkeypatch)


def _save(**changes: Any) -> None:
    policy.save_settings(policy.StickerSettings(**changes))


# ---- 纯函数 -----------------------------------------------------------------


def test_parse_label_tolerates_fences_noise_and_bad_types() -> None:
    label = labeler.parse_label("好的：\n```json\n" + _reply(tags="尴尬，好笑、捂脸笑", confidence="85%") + "\n```")
    assert label.ok and label.name == "捂脸笑"
    # 和名字重复的标签去掉，字符串标签按分隔符拆开，百分数换成小数
    assert label.tags == ("尴尬", "好笑") and label.confidence == 0.85
    assert label.text == "绷不住了" and label.kind == "sticker" and label.safe is True
    assert not labeler.parse_label("").ok and labeler.parse_label("").reason == "empty_result"
    assert labeler.parse_label("不是 JSON").reason == "bad_json"
    assert labeler.parse_label("[1,2]").reason == "bad_json"


def test_parse_label_falls_back_and_cleans_names() -> None:
    from_tag = labeler.parse_label(_reply(name="表情包", tags=["无语", "表情"]))
    assert from_tag.ok and from_tag.name == "无语" and from_tag.warnings
    from_text = labeler.parse_label(_reply(name="", tags=[], text="我真的会谢"))
    assert from_text.ok and from_text.name == "我真的会谢"
    nothing = labeler.parse_label(_reply(name="", tags=[], text=""))
    assert not nothing.ok and nothing.reason == "no_name"
    weird = labeler.parse_label(_reply(name="[表情:狗头]|", safe="false", kind="meme", confidence=7))
    assert weird.name == "表情 狗头" and weird.safe is False and weird.kind == "other" and weird.confidence == 0.07


def test_merge_label_modes() -> None:
    label = labeler.StickerLabel(True, name="震惊", tags=("惊讶", "离谱"), description="看到离谱事")
    current = {"name": "IMG_2033", "tags": ["吃瓜"], "description": "", "name_is_placeholder": True}
    fill = labeler.merge_label(current, label, mode="fill")
    assert fill == {"name": "震惊", "description": "看到离谱事"}
    merge = labeler.merge_label(current, label, mode="merge")
    assert merge["tags"] == ["吃瓜", "惊讶", "离谱"]
    named = {"name": "我的猫", "tags": [], "description": "已有", "name_is_placeholder": False}
    assert "name" not in labeler.merge_label(named, label, mode="fill")
    replace = labeler.merge_label(named, label, mode="replace", fields=("name", "tags"))
    assert replace == {"name": "震惊", "tags": ["惊讶", "离谱"]}
    assert labeler.merge_label(named, labeler.StickerLabel(False), mode="replace") == {}


def test_placeholder_names() -> None:
    for name in ("IMG_2033", "微信图片_20261101", "s1a2b3c4d5", "123456", "", "mmexport1700000000"):
        assert library.looks_like_placeholder(name), name
    for name in ("狗头", "捂脸笑", "img好看"):
        assert not library.looks_like_placeholder(name), name


# ---- 标注 -------------------------------------------------------------------


@pytest.mark.asyncio
async def test_label_image_requires_vision_and_image(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(labeler, "vision_ready", lambda: False)
    assert (await labeler.label_image(_png("a"))).reason == "no_vision"
    assert (await labeler.label_image(b"not an image")).reason == "not_image"


@pytest.mark.asyncio
async def test_label_image_sends_image_block_and_caches(vision: FakeVision) -> None:
    first = await labeler.label_image(_png("a"), hint="文件名：dog.png", known_names=["狗头", "狗头", "震惊"], style="四个字以内")
    assert first.ok and not first.cached
    messages = vision.calls[0]
    assert messages[0]["role"] == "system" and "JSON" in messages[0]["content"]
    blocks = messages[1]["content"]
    assert blocks[1]["type"] == "image_url" and blocks[1]["image_url"]["url"].startswith("data:image/png;base64,")
    request = blocks[0]["text"]
    assert "dog.png" in request and "狗头、震惊" in request and "四个字以内" in request
    second = await labeler.label_image(_png("a"), hint="别的", known_names=["狗头", "震惊"], style="四个字以内")
    assert second.cached and len(vision.calls) == 1
    # 命名要求变了不能吃旧缓存
    await labeler.label_image(_png("a"), known_names=["狗头", "震惊"], style="随便")
    assert len(vision.calls) == 2
    assert labeler.stats_snapshot()["cached"] == 1


@pytest.mark.asyncio
async def test_label_prompt_sections_are_registered_and_overridable(vision: FakeVision) -> None:
    for section_id in ("sticker.label", "sticker.label_request"):
        assert sections.get_section(section_id) is not None
    assert sections.get_section("sticker.label").default_text() == labeler._LABEL_SYSTEM_PROMPT
    assert labeler.label_system_prompt() == labeler._LABEL_SYSTEM_PROMPT


# ---- 入库与撤销 ---------------------------------------------------------------


@pytest.mark.asyncio
async def test_intake_names_with_ai_and_holds_unsafe(vision: FakeVision) -> None:
    result = await intake.intake_image(_png("x"), source="upload", status="active")
    assert result.created and result.named_by == "ai"
    sticker = result.sticker
    assert sticker.name == "捂脸笑" and sticker.tags == ("尴尬", "好笑") and sticker.description == "被戳穿时"
    assert sticker.ai["text"] == "绷不住了" and sticker.ocr_text == "绷不住了"
    # 同一张图第二次：不再标注、不重复入库
    again = await intake.intake_image(_png("x"), source="upload", status="active")
    assert not again.created and len(vision.calls) == 1
    vision.default = _reply(name="危险", safe=False)
    unsafe = await intake.intake_image(_png("y"), source="stolen", status="active")
    assert unsafe.held_for_review and unsafe.sticker.status == "pending"
    # 给了名字和标签就不调模型
    given = await intake.intake_image(_png("z"), name="狗头", tags=["调侃"], source="upload", status="active")
    assert given.named_by == "given" and len(vision.calls) == 2
    assert policy.usage_snapshot()["totals"]["labeled"] == 2


@pytest.mark.asyncio
async def test_intake_without_vision_uses_fallback_or_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(labeler, "vision_ready", lambda: False)
    result = await intake.intake_image(_png("f"), source="upload", status="active", fallback_name="cat_meme")
    assert result.sticker.name == "cat_meme" and result.named_by == "fallback"
    with pytest.raises(library.StickerError):
        await intake.intake_image(_png("g"), source="upload", status="active")


def test_apply_and_undo_label_roundtrip() -> None:
    sticker, _ = library.add_sticker(_png("u"), name="IMG_1", tags=["旧"], description="旧说明")
    updated = library.apply_label(sticker.sticker_id, {"name": "新名字", "tags": ["新"], "description": "新说明"}, {"model": "m", "text": "字"})
    assert updated.name == "新名字" and updated.can_undo_label and updated.ai["model"] == "m"
    restored = library.undo_label(sticker.sticker_id)
    assert (restored.name, restored.tags, restored.description) == ("IMG_1", ("旧",), "旧说明")
    assert restored.ai["undone"] is True and not restored.can_undo_label
    with pytest.raises(library.StickerError):
        library.undo_label(sticker.sticker_id)
    # 元数据持久化在 index.json 里，重读还在
    library.reset_cache_for_tests()
    assert library.get_sticker(sticker.sticker_id).ai["text"] == "字"


def test_same_name_stickers_rotate() -> None:
    library.add_sticker(_png("a"), name="狗头")
    library.add_sticker(_png("b"), name="狗头")
    how, candidates = library.match_stickers("狗头", None)
    assert how == "name" and len(candidates) == 2


def test_forget_member_clears_steal_origin() -> None:
    sticker, _ = library.add_sticker(_png("o"), name="偷来的", source="stolen", origin={"via": "steal", "group_id": GROUP, "sender_id": SENDER, "sender_name": "小明"})
    assert library.forget_member(GROUP, SENDER) == 1
    origin = library.get_sticker(sticker.sticker_id).origin
    assert "sender_id" not in origin and "sender_name" not in origin and origin["group_id"] == GROUP


# ---- 群聊工具 -------------------------------------------------------------------


class _Rows:
    def __init__(self, values: list[int]) -> None:
        self._values = values

    def scalars(self) -> list[int]:
        return list(self._values)


class _MemberSession:
    def __init__(self, opted_out: list[int] | None = None) -> None:
        self.opted_out = opted_out or []

    async def scalar(self, _statement: Any) -> int:
        return USER

    async def execute(self, _statement: Any) -> _Rows:
        return _Rows(self.opted_out)

    def add(self, item: Any) -> None:
        return None

    async def flush(self) -> None:
        return None

    async def commit(self) -> None:
        return None

    async def rollback(self) -> None:
        return None


class _Bot:
    self_id = "10000"


def _context(*, attachments: tuple[Any, ...] = (), opted_out: list[int] | None = None, admin: bool = False) -> Any:
    context = tools.ToolExecutionContext(
        bot=_Bot(),
        group_id=GROUP,
        actor_user_id=USER,
        session=_MemberSession(opted_out),
        capabilities=tools.BotGroupCapabilities(role="member", can_manage=False, actions=frozenset()),
        actor_can_manage=admin,
        privileged_allowlist=frozenset(),
    )
    context.attachments = attachments
    return context


def _attachment(*, sticker: bool = True, sender: int = SENDER, key: str = "1") -> Any:
    ref = {"type": "image", "file": f"{key}.image"}
    if sticker:
        ref["sub_type"] = 1
    return inbox.Attachment(key=key, media_type="image", source="reply", ref=ref, index=1, sender_id=sender, sender_name="小明", message_id=99)


@pytest.fixture
def fake_fetch(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fetch(_bot: Any, _group: int, attachment: Any, *, max_bytes: int) -> Any:
        return inbox._Fetch(_png(f"fetched-{attachment.key}-{attachment.ref.get('file')}"), "test")

    monkeypatch.setattr(handler, "fetch_bytes", fetch)
    monkeypatch.setattr(inbox, "fetch_bytes", fetch)


async def _steal(context: Any, **args: Any) -> Any:
    return await tools.execute_tool_with_meta("steal_sticker", args, context=context)


@pytest.mark.asyncio
async def test_steal_sticker_saves_with_ai_name_and_origin(fake_fetch: None, vision: FakeVision) -> None:
    result = await _steal(_context(attachments=(_attachment(),)), reason="太好笑了")
    data = result.payload["result"]
    assert result.payload["ok"] is True and data["stolen"] is True and data["status"] == "可用"
    assert data["name"] == "捂脸笑" and "named_by" in data and "tone" in data
    sticker = library.all_stickers()[0]
    assert sticker.source == "stolen" and sticker.added_by is None
    assert sticker.origin["sender_id"] == SENDER and sticker.origin["message_id"] == 99 and sticker.origin["qq_sticker"] is True
    assert "太好笑了" in vision.calls[0][1]["content"][0]["text"]
    assert policy.steals_today(policy.steal_key(GROUP)) == 1
    assert [item.name for item in library.available_stickers(GROUP)] == ["捂脸笑"]


@pytest.mark.asyncio
async def test_steal_sticker_gates(fake_fetch: None, vision: FakeVision) -> None:
    _save(steal_mode="off")
    off = await _steal(_context(attachments=(_attachment(),)))
    assert off.payload["ok"] is False
    _save(steal_mode="auto")
    photo = await _steal(_context(attachments=(_attachment(sticker=False),)))
    assert photo.payload["ok"] is False and "不是表情包" in photo.payload["error"]
    _save(steal_mode="auto", steal_protected_users=[SENDER])
    protected = await _steal(_context(attachments=(_attachment(),)))
    assert protected.payload["ok"] is False
    _save(steal_mode="auto")
    opted = await _steal(_context(attachments=(_attachment(),), opted_out=[SENDER]))
    assert opted.payload["ok"] is False
    mine = await _steal(_context(attachments=(_attachment(sender=10000),)))
    assert mine.payload["ok"] is False
    _save(steal_mode="review", steal_cooldown_seconds=0, group_daily_steals=1)
    review = await _steal(_context(attachments=(_attachment(),)))
    assert review.payload["result"]["status"] == "待管理员审核"
    over = await _steal(_context(attachments=(_attachment(key="2"),)))
    assert over.payload["ok"] is False and over.payload["error_code"] == "quota"


@pytest.mark.asyncio
async def test_steal_sticker_cooldown_and_photos(fake_fetch: None, vision: FakeVision) -> None:
    _save(steal_photos=True, steal_cooldown_seconds=600)
    first = await _steal(_context(attachments=(_attachment(sticker=False),)))
    assert first.payload["ok"] is True
    second = await _steal(_context(attachments=(_attachment(key="2"),)))
    assert second.payload["ok"] is False and "秒" in second.payload["error"]


@pytest.mark.asyncio
async def test_save_sticker_without_name_uses_ai(fake_fetch: None, vision: FakeVision) -> None:
    _save(collect_mode="auto")
    result = await tools.execute_tool_with_meta("save_sticker", {}, context=_context(attachments=(_attachment(),)))
    data = result.payload["result"]
    assert result.payload["ok"] is True and data["name"] == "捂脸笑" and data["named_by"]
    assert library.all_stickers()[0].origin["via"] == "collect"
    _save(collect_mode="auto", ai_label_on_collect=False)
    refused = await tools.execute_tool_with_meta("save_sticker", {}, context=_context(attachments=(_attachment(key="9"),)))
    assert refused.payload["ok"] is False


def test_steal_tool_registered_counted_and_blocked_in_wake() -> None:
    definition = registry.TOOL_BY_NAME["steal_sticker"]
    assert definition.family == "reaction" and definition.permission_level == "state_write"
    assert "steal_sticker" in handler.NAMES
    assert "steal_sticker" in wake.WAKE_BLOCKED_TOOLS
    assert "偷" in planner.TOOLPACK_CATALOG["reaction"] or "偷来" in planner.TOOLPACK_CATALOG["reaction"]


def test_steal_claims_are_verified() -> None:
    checked = claims.check_action_claims("嘿嘿，这张表情包我偷走了", frozenset())
    assert checked.changed and "sticker_steal" in checked.families
    assert not claims.check_action_claims("嘿嘿，这张表情包我偷走了", frozenset({"steal_sticker"})).changed


def test_menu_mentions_stealing_only_when_allowed() -> None:
    library.add_sticker(_png("m"), name="狗头", tags=["调侃"])
    text = sticker_send.sticker_turn_instruction(GROUP, scene="conversation")
    assert markup.STEAL_HINT in text
    assert markup.STEAL_HINT not in sticker_send.sticker_turn_instruction(GROUP, scene="warmup")
    _save(steal_mode="off")
    assert markup.STEAL_HINT not in sticker_send.sticker_turn_instruction(GROUP, scene="conversation")


# ---- 私聊 -------------------------------------------------------------------


def _pctx(media: list[dict[str, Any]] | None = None, *, superuser: bool = False) -> Any:
    from datetime import datetime

    return ptools.PrivateToolContext(
        session=None,
        user_id=USER,
        bot_id=None,
        groups=[],
        now=datetime(2026, 11, 10, 12, 0),
        nickname="阿明",
        is_superuser=superuser,
        bot=_Bot(),
        media=media or [],
    )


_IMG = {"type": "image", "file": "p.image", "sub_type": 1}


@pytest.mark.asyncio
async def test_private_stickers_steal_current_and_recent(fake_fetch: None, vision: FakeVision) -> None:
    ctx = _pctx([_IMG])
    result = await ptools.execute_private_tool("stickers", {"action": "steal"}, ctx)
    assert result["ok"] is True and result["result"]["done"] is True and result["result"]["status"] == "可用"
    assert "stickers.steal" in ctx.succeeded and "stickers.done" in ctx.succeeded
    sticker = library.all_stickers()[0]
    assert sticker.origin["via"] == "private" and sticker.added_by == USER
    # 下一轮没发图：用 10 分钟内记住的那张
    psticker.remember_media(USER, [{"type": "image", "file": "q.image", "sub_type": 1}])
    _save(steal_cooldown_seconds=0)
    later = await ptools.execute_private_tool("stickers", {"action": "steal"}, _pctx())
    assert later["ok"] is True and later["result"]["done"] is True
    nothing = await ptools.execute_private_tool("stickers", {"action": "steal"}, _pctx())
    assert nothing["ok"] is True and nothing["result"]["already_in_library"] is True
    psticker.reset_for_tests()
    missing = await ptools.execute_private_tool("stickers", {"action": "steal"}, _pctx())
    assert missing["ok"] is False


@pytest.mark.asyncio
async def test_private_stickers_photo_goes_to_review_and_search(fake_fetch: None, vision: FakeVision) -> None:
    refused = await ptools.execute_private_tool("stickers", {"action": "steal"}, _pctx([{"type": "image", "file": "photo.jpg"}]))
    assert refused["ok"] is False
    _save(steal_photos=True)
    photo = await ptools.execute_private_tool("stickers", {"action": "steal", "name": "我家猫"}, _pctx([{"type": "image", "file": "photo.jpg"}]))
    assert photo["result"]["status"] == "待管理员审核"
    library.add_sticker(_png("s"), name="狗头", tags=["调侃"])
    found = await ptools.execute_private_tool("stickers", {"action": "search", "query": "调侃"}, _pctx())
    assert found["result"]["found"] == [{"name": "狗头", "tags": ["调侃"]}]
    mine = await ptools.execute_private_tool("stickers", {"action": "mine"}, _pctx())
    assert mine["result"]["count"] == 1 and mine["result"]["stickers"][0]["name"] == "我家猫"


def test_private_stickers_tool_in_catalog() -> None:
    assert "stickers" in ptools.TOOLS_BY_NAME and ptools.TOOLS_BY_NAME["stickers"].writes
    assert catalog.FAMILY_OF["stickers"] == "stickers"


# ---- 批量任务 -----------------------------------------------------------------


@pytest.mark.asyncio
async def test_label_job_labels_and_can_be_undone(vision: FakeVision) -> None:
    a, _ = library.add_sticker(_png("j1"), name="IMG_1")
    b, _ = library.add_sticker(_png("j2"), name="我的名字", tags=["自己的"])
    vision.replies = [_reply(name="震惊"), _reply(name="开心", tags=["快乐"], safe=False)]
    job = jobs.start_job([a.sticker_id, b.sticker_id, "missing"], mode="fill")
    with pytest.raises(jobs.LabelJobError):
        jobs.start_job([a.sticker_id])
    await job.task
    summary = job.summary()
    assert summary["status"] == "done" and summary["total"] == 3 and summary["failed"] == 1
    renamed = library.get_sticker(a.sticker_id)
    kept = library.get_sticker(b.sticker_id)
    assert renamed.name in {"震惊", "开心"} and kept.name == "我的名字" and kept.tags == ("自己的",)
    assert summary["held"] == 1 and any(item.status == "pending" for item in (renamed, kept))
    assert jobs.unlabeled_ids() == []
    library.undo_label(a.sticker_id)
    assert library.get_sticker(a.sticker_id).name == "IMG_1"
    assert jobs.unlabeled_ids() == [a.sticker_id]


@pytest.mark.asyncio
async def test_label_job_refuses_without_vision_and_cancels(monkeypatch: pytest.MonkeyPatch, vision: FakeVision) -> None:
    a, _ = library.add_sticker(_png("c1"), name="IMG_1")
    b, _ = library.add_sticker(_png("c2"), name="IMG_2")
    gate = asyncio.Event()

    async def slow(messages: Any, **kwargs: Any) -> str:
        await gate.wait()
        return _reply()

    monkeypatch.setattr(llm, "complete", slow)
    job = jobs.start_job([a.sticker_id, b.sticker_id])
    jobs.cancel_job(job.job_id)
    gate.set()
    await job.task
    assert job.status == "cancelled"
    monkeypatch.setattr(labeler, "vision_ready", lambda: False)
    with pytest.raises(jobs.LabelJobError) as error:
        jobs.start_job([a.sticker_id])
    assert error.value.reason == "no_vision"


# ---- WebUI -------------------------------------------------------------------


class _UploadRequest:
    def __init__(self, data: bytes) -> None:
        self.headers = {"content-length": str(len(data))}
        self._data = data

    async def stream(self) -> Any:
        yield self._data


@pytest.mark.asyncio
async def test_webui_upload_auto_label_preview_and_undo(vision: FakeVision) -> None:
    preview = (await webui_routes.sticker_label_preview(_UploadRequest(_png("w1")), None, hint="dog.png"))["data"]
    assert preview["label"]["ok"] is True and preview["label"]["name"] == "捂脸笑" and preview["duplicate"] is None
    uploaded = (await webui_routes.sticker_upload(_UploadRequest(_png("w1")), None, name="", tags="", description="", auto=True, fallback="dog"))["data"]
    assert uploaded["created"] and uploaded["namedBy"] == "ai" and uploaded["sticker"]["name"] == "捂脸笑"
    # 预览过的同一张图上传时吃缓存
    assert len(vision.calls) == 1
    sticker = uploaded["sticker"]
    assert sticker["ai"]["text"] == "绷不住了" and sticker["origin"] is None
    dup = (await webui_routes.sticker_label_preview(_UploadRequest(_png("w1")), None, hint=""))["data"]
    assert dup["duplicate"]["id"] == sticker["id"]
    manual = (await webui_routes.sticker_upload(_UploadRequest(_png("w2")), None, name="", tags="", description="", auto=False, fallback="cat"))["data"]
    assert manual["sticker"]["name"] == "cat" and manual["namedBy"] == "fallback"
    vision.default = _reply(name="猫猫震惊")
    suggestion = (await webui_routes.sticker_label(manual["sticker"]["id"], webui_routes.LabelBody(), None))["data"]
    assert suggestion["changes"]["name"] == "猫猫震惊"
    assert library.get_sticker(manual["sticker"]["id"]).name == "cat"
    applied = (await webui_routes.sticker_label(manual["sticker"]["id"], webui_routes.LabelBody(apply=True), None))["data"]
    assert applied["sticker"]["name"] == "猫猫震惊" and applied["sticker"]["canUndoLabel"]
    undone = (await webui_routes.sticker_undo_label(manual["sticker"]["id"], None))["data"]
    assert undone["sticker"]["name"] == "cat"
    with pytest.raises(webui_routes.HTTPException):
        await webui_routes.sticker_undo_label(manual["sticker"]["id"], None)
    overview = (await webui_routes.stickers_overview(None))["data"]
    assert overview["counts"]["aiLabeled"] == 1 and overview["counts"]["unlabeled"] == 1
    assert overview["vision"]["ready"] is True and overview["sourceLabels"]["stolen"] == "Bot 偷的"


@pytest.mark.asyncio
async def test_webui_label_jobs_and_settings(vision: FakeVision) -> None:
    library.add_sticker(_png("k1"), name="IMG_1")
    started = (await webui_routes.sticker_label_job_start(webui_routes.LabelJobBody(scope="unlabeled"), None))["data"]["job"]
    assert started["total"] == 1
    await jobs.get_job(started["id"]).task
    detail = (await webui_routes.sticker_label_job(started["id"], None))["data"]["job"]
    assert detail["status"] == "done" and detail["results"][0]["after"]["name"] == "捂脸笑"
    listed = (await webui_routes.sticker_label_jobs(None))["data"]
    assert listed["jobs"][0]["id"] == started["id"]
    with pytest.raises(webui_routes.HTTPException):
        await webui_routes.sticker_label_job_start(webui_routes.LabelJobBody(scope="selected", ids=[]), None)
    payload = webui_routes._settings_payload(policy.StickerSettings())
    body = webui_routes.SettingsBody.model_validate({**payload, "stealMode": "review", "aiLabelStyle": "四个字", "stealProtectedUsers": ["10001"]})
    saved = (await webui_routes.sticker_update_settings(body, None))["data"]["settings"]
    assert saved["stealMode"] == "review" and saved["aiLabelStyle"] == "四个字" and saved["stealProtectedUsers"] == ["10001"]
    # 旧前端不带新字段：沿用当前值
    old = {key: value for key, value in payload.items() if not key.startswith(("steal", "aiLabel", "privateSteal", "privateDailySteals", "groupDailySteals"))}
    kept = (await webui_routes.sticker_update_settings(webui_routes.SettingsBody.model_validate(old), None))["data"]["settings"]
    assert kept["stealMode"] == "review" and kept["stealProtectedUsers"] == ["10001"]
    bad = webui_routes.SettingsBody.model_validate({**payload, "stealProtectedUsers": ["abc"]})
    with pytest.raises(webui_routes.HTTPException):
        await webui_routes.sticker_update_settings(bad, None)


@pytest.mark.asyncio
async def test_webui_bulk_undo_label() -> None:
    sticker, _ = library.add_sticker(_png("b1"), name="IMG_9")
    library.apply_label(sticker.sticker_id, {"name": "开心"}, {"model": "m"})
    done = (await webui_routes.sticker_bulk(webui_routes.BulkBody(ids=[sticker.sticker_id, "nope"], action="undo_label"), None))["data"]
    assert done["done"] == [sticker.sticker_id] and done["missing"] == ["nope"]
    assert library.get_sticker(sticker.sticker_id).name == "IMG_9"
