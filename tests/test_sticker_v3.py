# ruff: noqa: E501, PLR2004, ARG001, ARG002, FBT003, DTZ001
"""表情包系统 3.0：图片头解析、库的新字段/合集/回收站/拉黑/批量/索引备份、发送闸门与
按群覆盖、菜单策略、用量历史与没对上的名字、入库规则、AI 别名与重试、成员自助、体检、
导入导出、统计，以及新的 WebUI 接口。全部在临时目录里跑。
"""

from __future__ import annotations

import importlib
import io
import json
import random
import struct
import sys
import zipfile
import zlib
from datetime import datetime
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
meta = importlib.import_module(f"{_PKG}.sticker_meta")
labeler = importlib.import_module(f"{_PKG}.sticker_labeler")
intake = importlib.import_module(f"{_PKG}.sticker_intake")
jobs = importlib.import_module(f"{_PKG}.sticker_label_jobs")
speech = importlib.import_module(f"{_PKG}.speech")
sticker_send = importlib.import_module(f"{_PKG}.sticker_send")
service = importlib.import_module(f"{_PKG}.sticker_self_service")
health = importlib.import_module(f"{_PKG}.sticker_health")
archive = importlib.import_module(f"{_PKG}.sticker_archive")
insights = importlib.import_module(f"{_PKG}.sticker_insights")
claims = importlib.import_module(f"{_PKG}.speech_claims")
registry = importlib.import_module(f"{_PKG}.tool_registry")
reactions = importlib.import_module(f"{_PKG}.reactions")
psticker = importlib.import_module(f"{_PKG}.private_sticker_tool")
routes = importlib.import_module("src.plugins.yawn_core.webui.stickers")
admin = importlib.import_module("src.plugins.yawn_core.webui.sticker_admin_routes")

GROUP = 123456
OTHER_GROUP = 654321
USER = 42


def _chunk(kind: bytes, body: bytes) -> bytes:
    return struct.pack(">I", len(body)) + kind + body + struct.pack(">I", zlib.crc32(kind + body))


def png(tag: str = "a", width: int = 120, height: int = 120, *, frames: int = 0) -> bytes:
    data = b"\x89PNG\r\n\x1a\n" + _chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0))
    if frames:
        data += _chunk(b"acTL", struct.pack(">II", frames, 0))
    return data + _chunk(b"IDAT", tag.encode() * 4) + _chunk(b"IEND", b"")


def gif(tag: str = "g", width: int = 80, height: int = 60, frames: int = 3) -> bytes:
    data = b"GIF89a" + struct.pack("<HHBBB", width, height, 0, 0, 0)
    for _ in range(frames):
        data += b"\x2c" + struct.pack("<HHHHB", 0, 0, width, height, 0) + b"\x02" + b"\x02\x4c\x01" + b"\x00"
    return data + b"\x3b" + tag.encode()


@pytest.fixture(autouse=True)
def _isolated(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AGENT_FILE_ROOT", str(tmp_path / "agent_files"))
    monkeypatch.setattr(policy, "STATE_DIR", tmp_path / "stickers_state")
    library.reset_cache_for_tests()
    policy.reset_for_tests()
    labeler.reset_for_tests()
    jobs.reset_for_tests()
    psticker.reset_for_tests()


def add(name: str, **fields: Any) -> Any:
    data = fields.pop("data", None) or png(name)
    sticker, created = library.add_sticker(data, name=name, **fields)
    assert created
    return sticker


def save(**changes: Any) -> None:
    policy.save_settings(policy.StickerSettings(**changes))


# ---- 图片头 -----------------------------------------------------------------


def test_image_info_reads_png_apng_and_gif_headers() -> None:
    still = meta.image_info(png(width=200, height=100))
    assert (still.width, still.height, still.kind) == (200, 100, "png") and still.frames <= 1
    moving = meta.image_info(png(frames=6))
    assert moving.frames == 6
    animated = meta.image_info(gif(frames=4))
    assert (animated.width, animated.height, animated.frames, animated.kind) == (80, 60, 4, "gif")
    broken = meta.image_info(b"not an image")
    assert broken.width == 0 and broken.height == 0
    assert meta.image_info(b"\x89PNG\r\n\x1a\n").width == 0


def test_shape_and_dimension_text() -> None:
    assert meta.shape_label(100, 100) != meta.shape_label(300, 100)
    assert meta.shape_label(0, 0) == ""
    assert "120" in meta.describe_dimensions(120, 90)
    assert meta.describe_dimensions(0, 0) == ""
    assert "帧" in meta.describe_dimensions(80, 60, 4)


# ---- 库：新字段 --------------------------------------------------------------


def test_new_fields_round_trip_and_defaults_stay_out_of_the_index() -> None:
    plain = add("普通")
    rich = add("狗头", aliases=["doge", "狗头保命"], data=gif("r"))
    library.update_sticker(rich.sticker_id, pinned=True, weight=3, note="群主最爱", daily_cap=2)
    library.reset_cache_for_tests()
    loaded = library.get_sticker(rich.sticker_id)
    assert loaded.aliases == ("doge", "狗头保命") and loaded.pinned and loaded.weight == 3
    assert loaded.note == "群主最爱" and loaded.daily_cap == 2 and loaded.animated and loaded.frames == 3
    assert (loaded.width, loaded.height) == (80, 60)
    raw = json.loads(library.index_path().read_text("utf-8"))
    rows = {row["id"]: row for row in raw["reactions"]}
    assert "pinned" not in rows[plain.sticker_id] and "aliases" not in rows[plain.sticker_id]
    assert rows[rich.sticker_id]["pinned"] is True


def test_aliases_are_cleaned_and_matched_after_names() -> None:
    assert library.clean_aliases(["狗头", " doge ", "doge", "a", "b", "c", "d"], name="狗头") == ("doge", "a", "b", "c")
    add("狗头", aliases=["doge"])
    add("doge")
    how, by_name = library.match_stickers("doge", GROUP)
    assert [item.name for item in by_name] == ["doge"] and how == "name"
    library.update_sticker(by_name[0].sticker_id, name="别的")
    how, by_alias = library.match_stickers("DOGE", GROUP)
    assert [item.name for item in by_alias] == ["狗头"] and how == "alias"


def test_weight_and_note_are_clamped() -> None:
    assert library.clean_weight(9) == 3 and library.clean_weight(-1) == 0 and library.clean_weight("x") == 1
    assert len(library.clean_note("好" * 500)) == library.MAX_NOTE_CHARS
    assert set(library.WEIGHT_LABELS) == {0, 1, 2, 3}


# ---- 库：合集 ----------------------------------------------------------------


def test_packs_scope_stickers_to_groups_and_can_be_switched_off() -> None:
    pack = library.create_pack("猫猫", description="全是猫", groups=[GROUP])
    cat = add("猫猫拳", packs=[pack.pack_id])
    add("通用")
    assert {item.name for item in library.available_stickers(GROUP)} == {"猫猫拳", "通用"}
    assert {item.name for item in library.available_stickers(OTHER_GROUP)} == {"通用"}
    assert not library.is_available(cat, OTHER_GROUP) and library.is_available(cat, GROUP)
    library.update_pack(pack.pack_id, enabled=False)
    assert {item.name for item in library.available_stickers(GROUP)} == {"通用"}
    library.delete_pack(pack.pack_id)
    assert library.get_sticker(cat.sticker_id).packs == ()
    assert {item.name for item in library.available_stickers(OTHER_GROUP)} == {"猫猫拳", "通用"}


def test_pack_names_are_unique_and_unknown_packs_are_rejected() -> None:
    library.create_pack("猫猫")
    with pytest.raises(library.StickerError) as duplicate:
        library.create_pack(" 猫猫 ")
    assert duplicate.value.reason == "duplicate"
    with pytest.raises(library.StickerError):
        library.create_pack("")
    with pytest.raises(library.StickerError) as missing:
        library.update_pack("p99", name="x")
    assert missing.value.reason == "pack_not_found"
    item = add("狗头", packs=["p99"])
    assert item.packs == ()


# ---- 库：回收站、拉黑 --------------------------------------------------------


def test_trash_restore_and_purge() -> None:
    item = add("狗头")
    library.trash_sticker(item.sticker_id)
    assert library.get_sticker(item.sticker_id) is None and library.all_stickers() == ()
    assert library.get_sticker(item.sticker_id, include_trashed=True).deleted
    assert [row.sticker_id for row in library.trashed_stickers()] == [item.sticker_id]
    assert library.match_stickers("狗头", GROUP) == ("none", [])
    assert library.sticker_path(library.get_sticker(item.sticker_id, include_trashed=True)).is_file()
    library.restore_sticker(item.sticker_id)
    assert library.get_sticker(item.sticker_id).usable
    library.trash_sticker(item.sticker_id)
    assert library.purge_trash(older_than_days=7) == []
    removed = library.purge_trash()
    assert [row.sticker_id for row in removed] == [item.sticker_id] and library.trashed_stickers() == ()
    assert not (library.library_root() / item.file_name).exists()


def test_reuploading_a_trashed_image_restores_or_refuses() -> None:
    item = add("狗头")
    library.trash_sticker(item.sticker_id)
    with pytest.raises(library.StickerError) as refused:
        library.add_sticker(png("狗头"), name="狗头")
    assert refused.value.reason == "trashed"
    restored, created = library.add_sticker(png("狗头"), name="狗头", restore_trashed=True)
    assert restored.sticker_id == item.sticker_id and not created and not restored.deleted


def test_blocked_images_are_refused_until_unblocked() -> None:
    item = add("坏图")
    library.delete_sticker(item.sticker_id, block=True)
    assert library.is_blocked(item.sha256) and library.blocked_hashes() == (item.sha256,)
    with pytest.raises(library.StickerError) as refused:
        library.add_sticker(png("坏图"), name="坏图")
    assert refused.value.reason == "blocked"
    forced, created = library.add_sticker(png("坏图"), name="坏图", allow_blocked=True)
    assert created and forced.usable and not library.is_blocked(item.sha256)
    library.delete_sticker(forced.sticker_id, block=True)
    assert library.unblock_hash(item.sha256) and not library.unblock_hash(item.sha256)


# ---- 库：批量、标签整理、换图 -------------------------------------------------


def test_bulk_update_writes_once_and_reports_missing() -> None:
    first, second = add("一"), add("二")
    pack = library.create_pack("合集")
    ids = [first.sticker_id, second.sticker_id, "nope"]
    done, missing = library.bulk_update(ids, "add_tags", ["好笑", "无语"])
    assert done == ids[:2] and missing == ["nope"]
    library.bulk_update(ids, "remove_tags", ["无语"])
    library.bulk_update(ids, "set_groups", [GROUP])
    library.bulk_update(ids, "add_pack", pack.pack_id)
    library.bulk_update(ids, "pin")
    library.bulk_update(ids, "set_weight", 2)
    library.bulk_update([first.sticker_id], "hold")
    one, two = library.get_sticker(first.sticker_id), library.get_sticker(second.sticker_id)
    assert two.tags == ("好笑",) and two.groups == (GROUP,) and two.packs == (pack.pack_id,) and two.pinned and two.weight == 2
    assert one.status == library.STATUS_PENDING and two.status == library.STATUS_ACTIVE
    with pytest.raises(library.StickerError):
        library.bulk_update(ids, "add_pack", "p99")
    with pytest.raises(library.StickerError):
        library.bulk_update(ids, "explode")
    with pytest.raises(library.StickerError):
        library.bulk_update(ids, "add_tags", [])


def test_rename_and_delete_tag_across_the_library() -> None:
    add("一", tags=["搞笑", "猫"])
    add("二", tags=["好笑", "搞笑"])
    assert library.rename_tag("搞笑", "好笑") == 2
    assert {item.name: item.tags for item in library.all_stickers()} == {"一": ("好笑", "猫"), "二": ("好笑",)}
    assert library.delete_tag("猫") == 1 and library.delete_tag("没有") == 0
    with pytest.raises(library.StickerError):
        library.rename_tag("好笑", "")


def test_replace_image_keeps_identity_and_refuses_duplicates() -> None:
    item = add("狗头", tags=["好笑"])
    other = add("别的")
    policy.note_sent(GROUP, [item.sticker_id])
    updated = library.replace_image(item.sticker_id, png("高清", 512, 512))
    assert updated.sticker_id == item.sticker_id and updated.name == "狗头" and updated.tags == ("好笑",)
    assert updated.sha256 != item.sha256 and (updated.width, updated.height) == (512, 512)
    assert policy.stats_snapshot()[item.sticker_id]["uses"] == 1
    with pytest.raises(library.StickerError):
        library.replace_image(item.sticker_id, png("别的"))
    with pytest.raises(library.StickerError):
        library.replace_image(other.sticker_id, b"junk")


def test_backfill_meta_fills_missing_dimensions() -> None:
    item = add("狗头", data=png("x", 64, 48))
    raw = json.loads(library.index_path().read_text("utf-8"))
    for row in raw["reactions"]:
        row.pop("width", None)
        row.pop("height", None)
    library.index_path().write_text(json.dumps(raw), "utf-8")
    library.reset_cache_for_tests()
    assert library.get_sticker(item.sticker_id).width == 0
    assert library.backfill_meta() == 1 and library.backfill_meta() == 0
    assert library.get_sticker(item.sticker_id).width == 64


# ---- 库：索引备份 ------------------------------------------------------------


def test_corrupt_index_recovers_from_backup_instead_of_starting_empty() -> None:
    add("一")
    add("二")
    assert library.backup_path().is_file()
    library.index_path().write_text("{broken", "utf-8")
    library.reset_cache_for_tests()
    names = {item.name for item in library.all_stickers()}
    assert "一" in names
    state = library.library_health()
    assert state["corrupt"] and state["recoveredFromBackup"]
    add("三")
    assert any(path.name.startswith("index.corrupt-") for path in library.library_root().iterdir())
    library.reset_cache_for_tests()
    assert "三" in {item.name for item in library.all_stickers()} and not library.library_health()["corrupt"]


# ---- 策略：闸门 --------------------------------------------------------------


def test_quiet_hours_cross_midnight() -> None:
    settings = policy.StickerSettings(quiet_enabled=True, quiet_start="23:00", quiet_end="07:00")
    assert policy.in_quiet_hours(settings, datetime(2026, 11, 10, 23, 30))
    assert policy.in_quiet_hours(settings, datetime(2026, 11, 10, 6, 59))
    assert not policy.in_quiet_hours(settings, datetime(2026, 11, 10, 12, 0))
    assert not policy.in_quiet_hours(policy.StickerSettings(quiet_start="23:00", quiet_end="07:00"), datetime(2026, 11, 10, 23, 30))
    whole_day = policy.StickerSettings(quiet_enabled=True, quiet_start="08:00", quiet_end="08:00")
    assert policy.in_quiet_hours(whole_day, datetime(2026, 11, 10, 15, 0))


def test_quiet_hours_close_the_menu() -> None:
    add("狗头")
    save(quiet_enabled=True, quiet_start="00:00", quiet_end="00:00")
    state = policy.exposure(GROUP)
    assert state.reason == "quiet" and not state.open and "安静" in policy.REASON_TEXT["quiet"]


def test_hourly_limit_and_spacing_gates() -> None:
    item = add("狗头")
    save(group_cooldown_seconds=0, group_hourly_limit=2, min_messages_between=2)
    assert policy.exposure(GROUP).open and policy.exposure(GROUP).left_hour == 2
    policy.note_sent(GROUP, [item.sticker_id])
    assert policy.exposure(GROUP).reason == "spacing"
    policy.note_plain(GROUP)
    assert policy.exposure(GROUP).reason == "spacing"
    policy.note_plain(GROUP)
    assert policy.exposure(GROUP).open and policy.exposure(GROUP).left_hour == 1
    policy.note_sent(GROUP, [item.sticker_id])
    blocked = policy.exposure(GROUP)
    assert blocked.reason == "hourly_limit" and blocked.as_dict()["left_hour"] == 0
    assert policy.exposure(OTHER_GROUP).open


def test_group_override_changes_only_that_group() -> None:
    add("狗头")
    save(frequency="rare", group_daily_limit=60, group_overrides={str(GROUP): {"frequency": "often", "group_daily_limit": 1, "menu_size": 5}})
    here, there = policy.effective_settings(GROUP), policy.effective_settings(OTHER_GROUP)
    assert (here.frequency, here.group_daily_limit, here.menu_size) == ("often", 1, 5)
    assert (there.frequency, there.group_daily_limit) == ("rare", 60)
    assert policy.effective_settings(None).frequency == "rare"
    assert policy.exposure(GROUP).left_today == 1 and policy.exposure(OTHER_GROUP).left_today == 60


def test_group_override_enabled_wins_over_the_group_lists() -> None:
    add("狗头")
    save(group_mode="allowlist", allowed_groups=[], blocked_groups=[OTHER_GROUP], group_overrides={str(GROUP): {"enabled": True}, str(OTHER_GROUP): {"enabled": False}})
    settings = policy.load_settings()
    assert settings.group_open(GROUP) and not settings.group_open(OTHER_GROUP) and not settings.group_open(999999)
    assert policy.exposure(GROUP).open and policy.exposure(OTHER_GROUP).reason == "group_closed"


def test_empty_overrides_are_not_saved() -> None:
    save(group_overrides={str(GROUP): {}, str(OTHER_GROUP): {"menu_size": 6}})
    assert list(policy.load_settings().group_overrides) == [str(OTHER_GROUP)]


def test_per_sticker_cooldown_and_daily_cap() -> None:
    first, second = add("一"), add("二")
    library.update_sticker(second.sticker_id, daily_cap=1)
    save(group_cooldown_seconds=0, sticker_cooldown_minutes=30)
    stickers = library.available_stickers(GROUP)
    assert len(policy.sendable_now(GROUP, stickers)) == 2
    policy.note_sent(GROUP, [first.sticker_id])
    assert [item.name for item in policy.sendable_now(GROUP, stickers)] == ["二"]
    assert len(policy.sendable_now(OTHER_GROUP, stickers)) == 2
    policy.note_sent(OTHER_GROUP, [second.sticker_id])
    assert [item.name for item in policy.sendable_now(OTHER_GROUP, stickers)] == ["一"]
    one = library.get_sticker(first.sticker_id)
    assert policy.pick_sticker(GROUP, [one], strict=True) is None
    assert policy.pick_sticker(GROUP, [one]).sticker_id == first.sticker_id


def test_pick_respects_weight() -> None:
    often, never = add("笑", tags=["开心"]), add("笑2", tags=["开心"])
    library.update_sticker(often.sticker_id, weight=3)
    library.update_sticker(never.sticker_id, weight=0)
    candidates = [library.get_sticker(often.sticker_id), library.get_sticker(never.sticker_id)]
    rng = random.Random(1)
    assert {policy.pick_sticker(GROUP, candidates, rng=rng).sticker_id for _ in range(30)} == {often.sticker_id}
    assert policy.pick_sticker(GROUP, [candidates[1]]).sticker_id == never.sticker_id


# ---- 策略：菜单 --------------------------------------------------------------


def test_menu_puts_pinned_first_and_explains_each_entry() -> None:
    for index in range(8):
        add(f"图{index}", tags=[f"标签{index}"])
    pinned = add("置顶的", tags=["置顶"])
    library.update_sticker(pinned.sticker_id, pinned=True)
    hidden = add("不进菜单", tags=["藏"])
    library.update_sticker(hidden.sticker_id, weight=0)
    save(new_boost_days=0)
    plan = policy.menu_plan(GROUP, library.available_stickers(GROUP), size=6)
    assert plan[0]["name"] == "置顶的" and plan[0]["why"] == "pinned" and len(plan) == 6
    assert "不进菜单" not in {row["name"] for row in plan}
    assert {row["why"] for row in plan[1:]} == {"fresh"}
    entries = policy.menu_entries(GROUP, library.available_stickers(GROUP), size=6)
    assert entries[0][0] == "置顶的" and len(entries) == 6


def test_menu_strategies_order_by_usage() -> None:
    hot, cold = add("常用", tags=["a"]), add("冷门", tags=["b"])
    for _ in range(5):
        policy.note_sent(OTHER_GROUP, [hot.sticker_id])
    stickers = library.available_stickers(GROUP)
    popular = policy.menu_plan(GROUP, stickers, size=5, settings=policy.StickerSettings(menu_strategy="popular", new_boost_days=0))
    explore = policy.menu_plan(GROUP, stickers, size=5, settings=policy.StickerSettings(menu_strategy="explore", new_boost_days=0))
    assert [row["name"] for row in popular] == ["常用", "冷门"] and [row["name"] for row in explore] == ["冷门", "常用"]
    assert set(policy.MENU_STRATEGY_LABELS) == {"fresh", "popular", "explore", "balanced"}
    balanced = policy.menu_plan(GROUP, stickers, size=5, settings=policy.StickerSettings(menu_strategy="balanced", new_boost_days=0))
    assert {row["name"] for row in balanced} == {"常用", "冷门"}
    assert cold.sticker_id in {row["id"] for row in balanced}


def test_menu_boosts_new_and_favorite_and_trims_hints() -> None:
    old = add("老图", tags=["老"], description="很久以前的图，说明特别特别长长长长长长长")
    raw = json.loads(library.index_path().read_text("utf-8"))
    raw["reactions"][0]["created_at"] = "2020-01-01 00:00:00"
    library.index_path().write_text(json.dumps(raw, ensure_ascii=False), "utf-8")
    library.reset_cache_for_tests()
    add("新图", tags=["新"])
    fav = add("最爱", tags=["爱"])
    library.update_sticker(fav.sticker_id, weight=3)
    raw = json.loads(library.index_path().read_text("utf-8"))
    for row in raw["reactions"]:
        if row["name"] == "最爱":
            row["created_at"] = "2020-01-01 00:00:00"
    library.index_path().write_text(json.dumps(raw, ensure_ascii=False), "utf-8")
    library.reset_cache_for_tests()
    plan = policy.menu_plan(GROUP, library.available_stickers(GROUP), size=9, settings=policy.StickerSettings(new_boost_days=3, menu_hint_chars=6))
    why = {row["name"]: row["why"] for row in plan}
    assert why == {"新图": "new", "最爱": "favorite", "老图": "fresh"}
    assert [row["name"] for row in plan][-1] == "老图"
    assert len(next(row for row in plan if row["id"] == old.sticker_id)["hint"]) == 6
    bare = policy.menu_plan(GROUP, library.available_stickers(GROUP), size=9, settings=policy.StickerSettings(menu_hint_chars=0))
    assert {row["hint"] for row in bare} == {""}


def test_menu_limits_one_tag_from_taking_every_slot() -> None:
    for index in range(10):
        add(f"猫{index}", tags=["猫"])
    for index in range(4):
        add(f"狗{index}", tags=["狗"])
    plan = policy.menu_plan(GROUP, library.available_stickers(GROUP), size=8, settings=policy.StickerSettings(new_boost_days=0))
    names = [row["name"] for row in plan]
    assert len(names) == 8 and sum(1 for name in names if name.startswith("狗")) >= 2


def test_menu_without_tag_line_when_switched_off() -> None:
    add("狗头", tags=["好笑"])
    add("捂脸", tags=["好笑"])
    with_tags = sticker_send.sticker_turn_instruction(GROUP, scene="dialogue")
    save(menu_show_tags=False)
    without = sticker_send.sticker_turn_instruction(GROUP, scene="dialogue")
    assert "狗头" in without and len(without) < len(with_tags)


# ---- 策略：用量、历史、没对上的名字 -------------------------------------------


def test_stats_track_first_use_and_per_group_counts() -> None:
    item = add("狗头")
    policy.note_sent(GROUP, [item.sticker_id])
    policy.note_sent(GROUP, [item.sticker_id])
    policy.note_sent(OTHER_GROUP, [item.sticker_id])
    row = policy.stats_snapshot()[item.sticker_id]
    assert row["uses"] == 3 and row["first_used"] and row["groups"] == {str(GROUP): 2, str(OTHER_GROUP): 1}
    assert policy.usage_snapshot()["stickers"][item.sticker_id] == 3
    log = policy.send_log()
    assert len(log) == 3 and log[0]["stickerId"] == item.sticker_id and log[0]["groupId"] == GROUP


def test_private_sends_count_usage_without_touching_group_quota() -> None:
    item = add("狗头")
    save(group_daily_limit=1, group_cooldown_seconds=0)
    policy.note_private_sent(USER, [item.sticker_id])
    usage = policy.usage_snapshot()
    assert usage["totals"]["private_sent"] == 1 and usage["totals"]["sent"] == 0
    row = policy.stats_snapshot()[item.sticker_id]
    assert row["uses"] == 1 and row["groups"] == {policy.PRIVATE_STAT_KEY: 1}
    assert str(USER) not in json.dumps(row)
    assert policy.exposure(GROUP).open
    assert policy.recent_sent(policy.private_scope(USER)) == (item.sticker_id,)


def test_history_keeps_past_days_and_fills_gaps(monkeypatch: pytest.MonkeyPatch) -> None:
    item = add("狗头")
    clock = {"now": datetime(2026, 11, 8, 12, 0)}
    monkeypatch.setattr(policy, "now_beijing", lambda: clock["now"])
    policy.note_sent(GROUP, [item.sticker_id])
    policy.note_sent(GROUP, [item.sticker_id])
    clock["now"] = datetime(2026, 11, 10, 9, 0)
    policy.note_sent(GROUP, [item.sticker_id])
    history = policy.history_snapshot(4)
    assert [row["day"] for row in history] == ["2026-11-07", "2026-11-08", "2026-11-09", "2026-11-10"]
    assert [row["sent"] for row in history] == [0, 2, 0, 1]
    assert policy.usage_snapshot()["totals"]["sent"] == 1


def test_misses_are_counted_merged_and_dismissable() -> None:
    policy.note_miss("狗 头", GROUP)
    policy.note_miss("狗头", OTHER_GROUP)
    policy.note_miss("猫猫", GROUP)
    policy.note_miss("  ", GROUP)
    rows = policy.misses_snapshot()
    assert [(row["name"], row["count"]) for row in rows] == [("狗 头", 2), ("猫猫", 1)]
    assert policy.dismiss_miss("狗头") == 1 and policy.dismiss_miss("狗头") == 0
    assert policy.dismiss_miss(None) == 1 and policy.misses_snapshot() == []


def test_forgetting_a_member_clears_their_usage_and_protection() -> None:
    save(steal_protected_users=[USER, 7])
    policy.note_steal(f"g{GROUP}", USER)
    assert policy.sender_steals_today(USER) == 1
    policy.forget_member_usage(USER)
    assert policy.sender_steals_today(USER) == 0
    assert policy.set_protected(7, False) and policy.load_settings().steal_protected_users == [USER]
    assert not policy.set_protected(7, False)


# ---- 发送层 ------------------------------------------------------------------


def _resolve(text: str) -> tuple[list[Any], list[str]]:
    resolved = sticker_send.resolve_plan_stickers(speech.speech_plan_from_text(text), group_id=GROUP)
    segments = list(resolved.segments)
    return segments, [segment["reaction_id"] for segment in segments if segment["type"] == "reaction"]


def test_unknown_names_are_dropped_and_remembered() -> None:
    add("狗头")
    segments, ids = _resolve("好的[表情:不存在的]")
    assert ids == [] and all(segment["type"] != "sticker" for segment in segments)
    assert [row["name"] for row in policy.misses_snapshot()] == ["不存在的"]
    assert policy.usage_snapshot()["drops"].get("unknown") == 1


def test_alias_resolves_and_same_sticker_is_not_sent_twice() -> None:
    item = add("狗头", aliases=["doge"])
    save(max_per_message=3, group_cooldown_seconds=0)
    segments, ids = _resolve("哈[表情:doge]哈[表情:狗头]")
    assert ids == [item.sticker_id]
    assert sum(1 for segment in segments if segment["type"] == "reaction") == 1


def test_sticker_in_its_cooldown_is_dropped_as_repeat() -> None:
    item = add("狗头")
    save(group_cooldown_seconds=0, sticker_cooldown_minutes=60)
    policy.note_sent(GROUP, [item.sticker_id])
    _segments, ids = _resolve("哈[表情:狗头]")
    assert ids == [] and policy.usage_snapshot()["drops"].get("repeat") == 1


def test_reaction_search_covers_aliases_and_ocr_text() -> None:
    item = add("狗头", aliases=["doge"])
    library.apply_label(item.sticker_id, {}, {"text": "保命要紧", "model": "m"})
    assert reactions.search_reactions("doge", group_id=GROUP)
    assert reactions.search_reactions("保命", group_id=GROUP)


# ---- 入库 --------------------------------------------------------------------


def test_shape_rules_for_stolen_images() -> None:
    settings = policy.StickerSettings(steal_animated=False, steal_min_side=64)
    with pytest.raises(library.StickerError) as animated:
        intake.check_image_shape(gif(), settings)
    assert animated.value.reason == "animated"
    with pytest.raises(library.StickerError) as small:
        intake.check_image_shape(png(width=40, height=200), settings)
    assert small.value.reason == "too_small"
    intake.check_image_shape(png(width=64, height=64), settings)
    intake.check_image_shape(b"unknown", settings)


@pytest.mark.asyncio
async def test_intake_scopes_to_origin_group_and_uploads_restore_trash() -> None:
    save(steal_scope="origin", ai_label_enabled=False)
    stolen = await intake.intake_image(png("偷"), name="偷来的", source="stolen", status="active", added_group=GROUP, use_ai=False)
    assert stolen.sticker.groups == (GROUP,) and stolen.scoped_to_origin
    upload = await intake.intake_image(png("传"), name="上传的", source="upload", status="active", added_group=GROUP, use_ai=False)
    assert upload.sticker.groups == ()
    library.trash_sticker(upload.sticker.sticker_id)
    with pytest.raises(library.StickerError):
        await intake.intake_image(png("传"), name="再偷", source="stolen", status="active", use_ai=False)
    again = await intake.intake_image(png("传"), name="上传的", source="upload", status="active", use_ai=False)
    assert again.restored and again.sticker.usable


@pytest.mark.asyncio
async def test_blocked_image_cannot_be_stolen_again() -> None:
    item = add("坏图")
    library.delete_sticker(item.sticker_id, block=True)
    with pytest.raises(library.StickerError) as refused:
        await intake.intake_image(png("坏图"), name="坏图", source="stolen", status="active", use_ai=False)
    assert refused.value.reason == "blocked"


# ---- AI 标注 -----------------------------------------------------------------


def _reply(**fields: Any) -> str:
    data = {"name": "捂脸笑", "tags": ["尴尬", "好笑"], "description": "被戳穿时", "text": "绷不住了", "kind": "sticker", "safe": True, "confidence": 0.9}
    data.update(fields)
    return json.dumps(data, ensure_ascii=False)


class FakeVision:
    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.calls = 0
        self.replies: list[str | None] = []
        self.default: str | None = _reply()
        monkeypatch.setattr(labeler, "vision_ready", lambda: True)
        monkeypatch.setattr(llm, "complete", self.complete)

    async def complete(self, messages: Any, **kwargs: Any) -> str | None:
        self.calls += 1
        return self.replies.pop(0) if self.replies else self.default


@pytest.fixture
def vision(monkeypatch: pytest.MonkeyPatch) -> FakeVision:
    return FakeVision(monkeypatch)


def test_label_parses_aliases_and_mood() -> None:
    label = labeler.parse_label(_reply(aliases=["捂脸", "捂脸笑", "笑哭"], mood="尴尬"))
    assert label.aliases == ("捂脸", "笑哭") and label.mood == "尴尬"
    assert labeler.parse_label(_reply(mood="乱写的")).mood == ""
    assert "尴尬" in labeler.MOODS and len(labeler.MOODS) >= 12
    merged = labeler.merge_label({"name": "x", "tags": [], "description": "", "aliases": ["已有"], "name_is_placeholder": True}, label, mode="merge", fields=("name",))
    assert "aliases" not in merged
    replaced = labeler.merge_label({"name": "x", "tags": [], "description": "", "aliases": ["已有"], "name_is_placeholder": True}, label, mode="replace", fields=("name",))
    assert replaced["aliases"] == ["捂脸", "笑哭"]


@pytest.mark.asyncio
async def test_label_retries_once_on_garbage(vision: FakeVision) -> None:
    vision.replies = ["这不是 JSON", _reply()]
    label = await labeler.label_image(png("r1"), retry=True)
    assert label.ok and label.retried and vision.calls == 2
    assert labeler.stats_snapshot()["retried"] == 1
    vision.replies = ["这不是 JSON"]
    vision.calls = 0
    failed = await labeler.label_image(png("r2"), retry=False)
    assert not failed.ok and vision.calls == 1


@pytest.mark.asyncio
async def test_low_confidence_goes_to_review(vision: FakeVision) -> None:
    save(review_below_confidence=0.6)
    vision.default = _reply(confidence=0.3, aliases=["小声笑"])
    result = await intake.intake_image(png("低"), source="stolen", status="active", added_group=GROUP)
    assert result.sticker.status == library.STATUS_PENDING and result.sticker.aliases == ("小声笑",)
    vision.default = _reply(name="稳的", confidence=0.95)
    sure = await intake.intake_image(png("高"), source="stolen", status="active", added_group=GROUP)
    assert sure.sticker.status == library.STATUS_ACTIVE


def test_label_job_scopes() -> None:
    named = add("我的猫", tags=["猫"])
    placeholder = add("IMG_2033")
    low = add("不确定")
    library.apply_label(low.sticker_id, {}, {"model": "m", "confidence": 0.2})
    assert set(jobs.SCOPE_LABELS) >= {"selected", "unlabeled", "placeholder", "low_confidence"}
    assert jobs.placeholder_ids() == [placeholder.sticker_id]
    assert jobs.low_confidence_ids() == [low.sticker_id]
    assert jobs.scope_ids("selected", [named.sticker_id]) == [named.sticker_id]
    assert named.sticker_id in jobs.scope_ids("unlabeled") and low.sticker_id not in jobs.scope_ids("unlabeled")


# ---- 成员自助 ----------------------------------------------------------------


def test_member_can_rename_and_remove_only_their_own() -> None:
    mine = add("我的", added_by=USER, added_group=GROUP, source="collected")
    add("别人的", added_by=7, added_group=GROUP, source="collected")
    taken = add("被偷的", source="stolen", origin={"group_id": GROUP, "sender_id": USER})
    listing = service.list_own(USER, GROUP)
    assert listing["count"] == 2 and listing["collected"] == 1 and listing["stolen_from_him"] == 1
    renamed = service.rename_own(USER, GROUP, "我的", "我的猫")
    assert renamed["name"] == "我的猫" and library.get_sticker(mine.sticker_id).name == "我的猫"
    with pytest.raises(Exception, match="偷"):
        service.rename_own(USER, GROUP, "被偷的", "x")
    with pytest.raises(Exception, match="没有"):
        service.remove_own(USER, GROUP, "别人的")
    removed = service.remove_own(USER, GROUP, "被偷的")
    assert removed["removed"] and library.get_sticker(taken.sticker_id) is None
    assert library.get_sticker(taken.sticker_id, include_trashed=True) is not None
    assert service.list_own(USER, OTHER_GROUP)["count"] == 0


def test_member_removal_is_permanent_without_a_trash() -> None:
    mine = add("我的", added_by=USER, added_group=GROUP, source="collected")
    save(trash_days=0)
    service.remove_own(USER, GROUP, "我的")
    assert library.get_sticker(mine.sticker_id, include_trashed=True) is None


def test_member_self_service_can_be_switched_off() -> None:
    add("我的", added_by=USER, added_group=GROUP, source="collected")
    save(member_manage_own=False, member_self_protect=False)
    with pytest.raises(Exception, match="管理员"):
        service.remove_own(USER, GROUP, "我的")
    with pytest.raises(Exception, match="管理员"):
        service.protect(USER, on=True)


def test_member_protects_and_unprotects_their_images() -> None:
    on = service.protect(USER, on=True)
    assert on["protected"] and on["changed"] and USER in policy.load_settings().steal_protected_users
    assert not service.protect(USER, on=True)["changed"]
    off = service.protect(USER, on=False)
    assert off["changed"] and USER not in policy.load_settings().steal_protected_users


def test_my_stickers_tool_is_registered_and_claims_are_checked() -> None:
    definition = registry.get_tool_definition("my_stickers") if hasattr(registry, "get_tool_definition") else None
    names = {tool.name for tool in registry.TOOL_DEFINITIONS} if hasattr(registry, "TOOL_DEFINITIONS") else set()
    assert definition is not None or "my_stickers" in names
    assert "my_stickers" in claims.CLAIM_TOOL_FAMILIES["sticker_manage"] if hasattr(claims, "CLAIM_TOOL_FAMILIES") else True
    assert {"rename", "remove", "protect", "unprotect", "stats"} <= set(psticker.ACTIONS)


# ---- 体检 --------------------------------------------------------------------


def test_health_check_finds_and_fixes_problems() -> None:
    clean = health.run_check()
    assert clean["score"] == 100 and clean["issues"] == []
    lost = add("丢了文件")
    (library.library_root() / lost.file_name).unlink()
    (library.library_root() / "stray.png").write_bytes(png("stray"))
    add("IMG_0001")
    add("同名", data=png("d1"))
    add("同名", data=png("d2"))
    report = health.run_check()
    kinds = {issue["kind"]: issue for issue in report["issues"]}
    assert kinds["missing_file"]["ids"] == [lost.sticker_id] and kinds["missing_file"]["severity"] == "error"
    assert kinds["orphan_file"]["files"] == ["stray.png"] and kinds["orphan_file"]["fixable"]
    assert report["score"] < 100 and report["total"] == 4
    assert set(kinds) <= set(health.ISSUES)
    assert health.fix("orphan_file")["fixed"] == 1 and not (library.library_root() / "stray.png").exists()
    assert health.fix("missing_file")["fixed"] == 1 and library.get_sticker(lost.sticker_id) is None
    after = {issue["kind"] for issue in health.run_check()["issues"]}
    assert "missing_file" not in after and "orphan_file" not in after
    with pytest.raises(library.StickerError):
        health.fix("never_used")


def test_health_does_not_treat_index_files_as_orphans() -> None:
    add("一")
    add("二")
    assert health.orphan_files() == []


# ---- 导入导出 ----------------------------------------------------------------


def test_archive_round_trip_keeps_labels_and_packs_but_not_private_origin() -> None:
    pack = library.create_pack("猫猫", description="全是猫")
    add("猫猫拳", tags=["猫"], aliases=["喵喵拳"], packs=[pack.pack_id], description="挥拳", origin={"group_id": GROUP, "sender_id": USER})
    add("狗头", tags=["狗"])
    data, info = archive.export_archive()
    assert info["count"] == 2 and info["skipped"] == 0
    manifest = json.loads(zipfile.ZipFile(io.BytesIO(data)).read("manifest.json"))
    assert manifest["format"] == archive.FORMAT and str(USER) not in json.dumps(manifest)
    assert archive.inspect_archive(data) == {"count": 2, "existing": 2, "new": 0, "packs": ["猫猫"], "exportedAt": manifest["exported_at"], "includePrivate": False}
    for item in library.all_stickers():
        library.delete_sticker(item.sticker_id)
    library.delete_pack(pack.pack_id)
    assert archive.inspect_archive(data)["new"] == 2
    result = archive.import_archive(data, max_bytes=1024 * 1024)
    assert result["added"] == 2 and result["packsCreated"] == 1 and result["failed"] == []
    cat = next(item for item in library.all_stickers() if item.name == "猫猫拳")
    assert cat.tags == ("猫",) and cat.aliases == ("喵喵拳",) and cat.description == "挥拳" and len(cat.packs) == 1
    assert cat.origin.get("sender_id") is None
    again = archive.import_archive(data, max_bytes=1024 * 1024)
    assert again["added"] == 0 and again["skipped"] == 2


def test_archive_export_by_pack_and_import_as_pending() -> None:
    pack = library.create_pack("猫猫")
    add("猫猫拳", packs=[pack.pack_id])
    add("狗头")
    data, info = archive.export_archive(pack_id=pack.pack_id)
    assert info["count"] == 1
    with pytest.raises(library.StickerError):
        archive.export_archive(ids=["nope"])
    for item in library.all_stickers():
        library.delete_sticker(item.sticker_id)
    result = archive.import_archive(data, max_bytes=1024 * 1024, as_pending=True)
    assert result["added"] == 1 and library.all_stickers()[0].status == library.STATUS_PENDING


def test_archive_rejects_junk_and_unsafe_paths() -> None:
    with pytest.raises(library.StickerError):
        archive.inspect_archive(b"not a zip")
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as bundle:
        bundle.writestr("manifest.json", json.dumps({"format": "something-else", "version": 1, "stickers": []}))
    with pytest.raises(library.StickerError):
        archive.inspect_archive(buffer.getvalue())
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as bundle:
        bundle.writestr("manifest.json", json.dumps({"format": archive.FORMAT, "version": 1, "stickers": [{"file": "../evil.png", "name": "x", "sha256": "0" * 64}]}))
        bundle.writestr("../evil.png", png("evil"))
    result = archive.import_archive(buffer.getvalue(), max_bytes=1024 * 1024)
    # 图片名只当包里的键用，落盘的文件名由库按内容哈希重新起，写不到库目录外面。
    assert result["added"] == 1
    stored = library.sticker_path(library.all_stickers()[0])
    assert stored.parent == library.library_root() and "evil" not in stored.name


# ---- 统计 --------------------------------------------------------------------


def test_trend_and_growth_helpers() -> None:
    history = [{"date": f"d{index}", "sent": 1} for index in range(7)] + [{"date": f"e{index}", "sent": 3} for index in range(7)]
    result = insights.trend(history)
    assert result == {"recent": 21, "previous": 7, "changePercent": 200}
    assert insights.trend(history[:7])["changePercent"] is None
    add("一")
    growth = insights.weekly_growth(library.all_stickers(), weeks=4)
    assert len(growth) == 4 and growth[-1]["total"] == 1 and growth[-1]["upload"] == 1


def test_miss_suggestions_come_from_names_aliases_and_tags() -> None:
    add("狗头保命", tags=["狗头"])
    add("猫猫拳", aliases=["喵拳"])
    stickers = library.all_stickers()
    assert insights.suggest_for_miss("狗头", stickers)[0]["name"] == "狗头保命"
    assert insights.suggest_for_miss("喵拳头", stickers)[0]["name"] == "猫猫拳"
    assert insights.suggest_for_miss("完全无关的东西", stickers) == []


def test_insights_summarise_usage_misses_and_gaps() -> None:
    used = add("狗头", tags=["好笑"])
    library.apply_label(used.sticker_id, {}, {"model": "m", "mood": "开心"})
    add("没用过", tags=["无语"])
    policy.note_sent(GROUP, [used.sticker_id])
    policy.note_private_sent(USER, [used.sticker_id])
    policy.note_miss("狗头保", GROUP)
    data = insights.build_insights(days=7)
    assert len(data["history"]) == 7 and data["history"][-1]["sent"] == 1 and data["totals"]["uses"] == 2 and data["totals"]["neverUsed"] == 1
    assert data["top"][0]["id"] == used.sticker_id and data["top"][0]["uses"] == 2
    assert [row["name"] for row in data["neverUsed"]] == ["没用过"]
    assert data["misses"][0]["name"] == "狗头保" and data["misses"][0]["suggestions"][0]["name"] == "狗头"
    assert "开心" not in data["moodGaps"] and len(data["moodGaps"]) == len(labeler.MOODS) - 2
    assert {row["groupId"] for row in data["groups"]} >= {str(GROUP)}
    assert str(USER) not in json.dumps(data, ensure_ascii=False)


# ---- WebUI -------------------------------------------------------------------


class _Request:
    def __init__(self, data: bytes) -> None:
        self.headers = {"content-length": str(len(data))}
        self._data = data

    async def stream(self) -> Any:
        yield self._data


@pytest.mark.asyncio
async def test_webui_payload_carries_new_fields_and_delete_goes_to_trash() -> None:
    item = add("狗头", aliases=["doge"], data=gif("w"))
    patch = routes.StickerPatch(pinned=True, weight=3, note="备注", aliases=["doge", "保命"], daily_cap=4)
    row = (await routes.sticker_patch(item.sticker_id, patch, None))["data"]["sticker"]
    assert row["pinned"] and row["weight"] == 3 and row["note"] == "备注" and row["aliases"] == ["doge", "保命"]
    assert row["dailyCap"] == 4 and row["animated"] and row["width"] == 80 and row["dimensions"]
    overview = (await routes.stickers_overview(None))["data"]
    assert overview["counts"]["pinned"] == 1 and overview["counts"]["animated"] == 1
    assert overview["menuStrategyLabels"] and overview["weightLabels"]["3"] and overview["moods"] and overview["labelScopes"]
    deleted = (await routes.sticker_delete(item.sticker_id, None))["data"]
    assert deleted["trashed"] is True
    trash = (await admin.sticker_trash(None))["data"]
    assert [row["id"] for row in trash["stickers"]] == [item.sticker_id] and trash["stickers"][0]["deletedAt"]
    image = await routes.sticker_image(item.sticker_id, None)
    assert image.body.startswith(b"GIF89a")
    restored = (await admin.sticker_trash_restore(item.sticker_id, None))["data"]["sticker"]
    assert restored["deletedAt"] is None
    gone = (await routes.sticker_delete(item.sticker_id, None, purge=True))["data"]
    assert gone["trashed"] is False and library.get_sticker(item.sticker_id, include_trashed=True) is None


@pytest.mark.asyncio
async def test_webui_block_trash_empty_and_unblock() -> None:
    bad, other = add("坏图"), add("别的")
    assert (await routes.sticker_delete(bad.sticker_id, None, block=True))["data"]["blocked"] is True
    assert (await admin.sticker_blocked(None))["data"]["blocked"] == [bad.sha256]
    await routes.sticker_delete(other.sticker_id, None)
    assert (await admin.sticker_trash_empty(None))["data"]["removed"] == 1
    assert (await admin.sticker_unblock(bad.sha256, None))["data"]["blocked"] == []
    with pytest.raises(admin.HTTPException) as missing:
        await admin.sticker_unblock(bad.sha256, None)
    assert missing.value.status_code == 404
    with pytest.raises(admin.HTTPException):
        await admin.sticker_trash_restore("nope", None)


@pytest.mark.asyncio
async def test_expired_trash_is_purged_when_the_trash_is_opened(monkeypatch: pytest.MonkeyPatch) -> None:
    item = add("狗头")
    library.trash_sticker(item.sticker_id)
    raw = json.loads(library.index_path().read_text("utf-8"))
    raw["reactions"][0]["deleted_at"] = "2020-01-01 00:00:00"
    library.index_path().write_text(json.dumps(raw, ensure_ascii=False), "utf-8")
    library.reset_cache_for_tests()
    trash = (await admin.sticker_trash(None))["data"]
    assert trash["autoPurged"] == 1 and trash["stickers"] == []


@pytest.mark.asyncio
async def test_webui_bulk_actions() -> None:
    first, second = add("一"), add("二")
    ids = [first.sticker_id, second.sticker_id]
    pack_id = (await admin.sticker_pack_create(admin.PackBody(name="合集"), None))["data"]["id"]
    await routes.sticker_bulk(routes.BulkBody(ids=ids, action="add_tags", tags=["好笑"]), None)
    await routes.sticker_bulk(routes.BulkBody(ids=ids, action="set_groups", groups=[str(GROUP)]), None)
    await routes.sticker_bulk(routes.BulkBody(ids=ids, action="add_pack", pack=pack_id), None)
    await routes.sticker_bulk(routes.BulkBody(ids=ids, action="set_weight", weight=2), None)
    done = (await routes.sticker_bulk(routes.BulkBody(ids=[*ids, "nope"], action="pin"), None))["data"]
    assert done == {"done": ids, "missing": ["nope"]}
    item = library.get_sticker(second.sticker_id)
    assert item.tags == ("好笑",) and item.groups == (GROUP,) and item.packs == (pack_id,) and item.weight == 2 and item.pinned
    with pytest.raises(routes.HTTPException):
        await routes.sticker_bulk(routes.BulkBody(ids=ids, action="add_tags", tags=[]), None)
    await routes.sticker_bulk(routes.BulkBody(ids=[first.sticker_id], action="delete"), None)
    assert library.get_sticker(first.sticker_id) is None and len(library.trashed_stickers()) == 1
    await routes.sticker_bulk(routes.BulkBody(ids=[second.sticker_id], action="block"), None)
    assert library.is_blocked(second.sha256)


@pytest.mark.asyncio
async def test_webui_packs_crud_and_members() -> None:
    item = add("猫猫拳")
    created = (await admin.sticker_pack_create(admin.PackBody(name="猫猫", description="全是猫", groups=[str(GROUP)]), None))["data"]
    pack_id = created["id"]
    assert created["packs"][0]["groups"] == [str(GROUP)] and created["packs"][0]["count"] == 0
    with pytest.raises(admin.HTTPException) as duplicate:
        await admin.sticker_pack_create(admin.PackBody(name="猫猫"), None)
    assert duplicate.value.status_code == 409
    members = (await admin.sticker_pack_members(pack_id, admin.PackMembersBody(ids=[item.sticker_id, "nope"]), None))["data"]
    assert members["done"] == [item.sticker_id] and members["missing"] == ["nope"]
    assert members["packs"][0]["count"] == 1 and members["packs"][0]["cover"] == [item.sticker_id]
    updated = (await admin.sticker_pack_update(pack_id, admin.PackBody(enabled=False), None))["data"]["packs"][0]
    assert updated["enabled"] is False and updated["name"] == "猫猫"
    await admin.sticker_pack_members(pack_id, admin.PackMembersBody(ids=[item.sticker_id], op="remove"), None)
    assert library.get_sticker(item.sticker_id).packs == ()
    with pytest.raises(admin.HTTPException) as missing:
        await admin.sticker_pack_members("p99", admin.PackMembersBody(ids=[item.sticker_id]), None)
    assert missing.value.status_code == 404
    assert (await admin.sticker_pack_delete(pack_id, None))["data"]["packs"] == []
    with pytest.raises(admin.HTTPException):
        await admin.sticker_pack_update(pack_id, admin.PackBody(name="x"), None)


@pytest.mark.asyncio
async def test_webui_insights_and_miss_resolution() -> None:
    item = add("狗头")
    policy.note_sent(GROUP, [item.sticker_id])
    policy.note_miss("doge", GROUP)
    policy.note_miss("别的", GROUP)
    data = (await admin.sticker_insights_view(None, days=14))["data"]
    assert len(data["history"]) == 14 and {row["name"] for row in data["misses"]} == {"doge", "别的"}
    body = admin.MissBody(name="doge", action="alias", sticker_id=item.sticker_id)
    assert (await admin.sticker_miss_resolve(body, None))["data"]["removed"] == 1
    assert library.get_sticker(item.sticker_id).aliases == ("doge",)
    assert library.match_stickers("doge", GROUP)[1][0].sticker_id == item.sticker_id
    with pytest.raises(admin.HTTPException):
        await admin.sticker_miss_resolve(admin.MissBody(name="别的", action="alias", sticker_id="nope"), None)
    assert (await admin.sticker_miss_resolve(admin.MissBody(name="别的", action="dismiss"), None))["data"]["removed"] == 1
    policy.note_miss("又一个", GROUP)
    assert (await admin.sticker_miss_resolve(admin.MissBody(action="clear"), None))["data"]["removed"] == 1


@pytest.mark.asyncio
async def test_webui_health_tags_and_replace() -> None:
    item = add("狗头", tags=["搞笑"])
    (library.library_root() / "stray.png").write_bytes(png("stray"))
    report = (await admin.sticker_health_view(None))["data"]
    assert "orphan_file" in {issue["kind"] for issue in report["issues"]}
    fixed = (await admin.sticker_health_fix(admin.FixBody(kind="orphan_file"), None))["data"]
    assert fixed["fixed"] == 1 and "orphan_file" not in {issue["kind"] for issue in fixed["health"]["issues"]}
    with pytest.raises(admin.HTTPException):
        await admin.sticker_health_fix(admin.FixBody(kind="never_used"), None)
    renamed = (await admin.sticker_tag_rename(admin.TagBody.model_validate({"from": "搞笑", "to": "好笑"}), None))["data"]
    assert renamed["changed"] == 1 and library.get_sticker(item.sticker_id).tags == ("好笑",)
    assert (await admin.sticker_tag_delete(admin.TagBody.model_validate({"from": "好笑"}), None))["data"]["changed"] == 1
    replaced = (await admin.sticker_replace(item.sticker_id, _Request(png("新", 256, 256)), None))["data"]["sticker"]
    assert replaced["id"] == item.sticker_id and replaced["width"] == 256
    with pytest.raises(admin.HTTPException):
        await admin.sticker_replace(item.sticker_id, _Request(b"junk"), None)


@pytest.mark.asyncio
async def test_webui_export_and_import() -> None:
    add("狗头", tags=["好笑"])
    response = await admin.sticker_export(admin.ExportBody(), None)
    assert response.media_type == "application/zip" and response.headers["x-sticker-count"] == "1"
    data = bytes(response.body)
    for item in library.all_stickers():
        library.delete_sticker(item.sticker_id)
    look = (await admin.sticker_import(_Request(data), None, apply=False, update_existing=False, as_pending=False, keep_private=False))["data"]
    assert look["applied"] is False and look["info"]["new"] == 1 and library.all_stickers() == ()
    done = (await admin.sticker_import(_Request(data), None, apply=True, update_existing=False, as_pending=True, keep_private=False))["data"]
    assert done["applied"] is True and done["result"]["added"] == 1
    assert library.all_stickers()[0].status == library.STATUS_PENDING
    with pytest.raises(admin.HTTPException):
        await admin.sticker_import(_Request(b"junk"), None, apply=False, update_existing=False, as_pending=False, keep_private=False)
    with pytest.raises(admin.HTTPException):
        await admin.sticker_export(admin.ExportBody(ids=["nope"]), None)


@pytest.mark.asyncio
async def test_webui_settings_accept_overrides_and_reject_bad_groups() -> None:
    base = routes._settings_payload(policy.StickerSettings())
    body = routes.SettingsBody.model_validate(
        {**base, "menuStrategy": "balanced", "groupHourlyLimit": 6, "quietEnabled": True, "quietStart": "23:00", "quietEnd": "07:30", "stealBlockedGroups": [str(OTHER_GROUP)], "groupOverrides": {str(GROUP): {"frequency": "often", "menuSize": 6}}}
    )
    saved = (await routes.sticker_update_settings(body, None))["data"]["settings"]
    assert saved["menuStrategy"] == "balanced" and saved["groupHourlyLimit"] == 6 and saved["quietEnd"] == "07:30"
    assert saved["stealBlockedGroups"] == [str(OTHER_GROUP)]
    assert saved["groupOverrides"][str(GROUP)]["frequency"] == "often" and saved["groupOverrides"][str(GROUP)]["menuSize"] == 6
    assert not policy.load_settings().steal_open(OTHER_GROUP) and policy.load_settings().steal_open(GROUP)
    bad = routes.SettingsBody.model_validate({**base, "groupOverrides": {"abc": {"frequency": "often"}}})
    with pytest.raises(routes.HTTPException):
        await routes.sticker_update_settings(bad, None)


@pytest.mark.asyncio
async def test_webui_preview_shows_menu_reasons_and_resolve_tester() -> None:
    pinned = add("置顶的", aliases=["顶"])
    library.update_sticker(pinned.sticker_id, pinned=True)
    add("普通", tags=["好笑"])
    save(group_overrides={str(GROUP): {"menu_size": 5}})
    preview = (await routes.sticker_prompt_preview(None, group_id=str(GROUP)))["data"]
    assert preview["open"] and preview["menu"][0]["why"] == "pinned" and preview["chars"] == len(preview["text"])
    assert preview["overridden"] == ["menuSize"] and preview["effective"]["menuSize"] == 5
    by_alias = (await routes.sticker_resolve(None, group_id=str(GROUP), name="顶"))["data"]
    assert by_alias["match"] == "alias" and by_alias["candidates"][0]["id"] == pinned.sticker_id and by_alias["gateOpen"]
    by_tag = (await routes.sticker_resolve(None, group_id=str(GROUP), name="好笑"))["data"]
    assert by_tag["match"] == "tag" and by_tag["candidates"][0]["name"] == "普通"
    nothing = (await routes.sticker_resolve(None, group_id=str(GROUP), name="没有"))["data"]
    assert nothing["candidates"] == [] and nothing["match"] == "none" and nothing["matchText"] == "对不上"


def test_admin_router_is_mounted_without_shadowing_the_main_one() -> None:
    paths = {(route.path, method) for route in admin.router.routes for method in route.methods}
    main = {(route.path, method) for route in routes.router.routes for method in route.methods}
    assert not paths & main
    prefix = admin.router.prefix
    single = {path for path, method in paths if path.count("/") == prefix.count("/") + 1 and "{" in path.rsplit("/", 1)[-1]}
    assert single == set()
    app = importlib.import_module("src.plugins.yawn_core.webui.app")
    source = Path(app.__file__).read_text("utf-8")
    assert source.index("include_router(sticker_admin_router)") < source.index("include_router(stickers_router)")
