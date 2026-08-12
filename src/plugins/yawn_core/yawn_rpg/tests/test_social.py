"""NPC 自然语言路由、上下文隔离与社交配置回归测试。"""

import sys
import types
from pathlib import Path

import pytest

PLUGIN_ROOT = Path(__file__).resolve().parents[2]
PACKAGE = types.ModuleType("yawn_core")
PACKAGE.__path__ = [str(PLUGIN_ROOT)]
sys.modules.setdefault("yawn_core", PACKAGE)
RPG_PACKAGE = types.ModuleType("yawn_core.yawn_rpg")
RPG_PACKAGE.__path__ = [str(PLUGIN_ROOT / "yawn_rpg")]
sys.modules.setdefault("yawn_core.yawn_rpg", RPG_PACKAGE)

from yawn_core.yawn_rpg import ai_social
from yawn_core.yawn_rpg.ai_social import (
    build_router_user_message,
    parse_route,
)
from yawn_core.yawn_rpg.config import Config
from yawn_core.yawn_rpg.module_schema import (
    NPC,
    CheckDifficulty,
    ModuleDef,
    NPCFact,
    Scene,
    SocialNode,
    SocialStrategy,
)
from yawn_core.yawn_rpg.state import (
    Game,
    relationship_band,
)


def _module() -> ModuleDef:
    npc = NPC(
        id="butler",
        name="周伯",
        public_desc="年迈的管家。",
        persona="胆小而忠诚。",
        knows=["宅子已经荒废。"],
        secrets=["他私下藏着一把旧钥匙。"],
        facts=[
            NPCFact(
                id="key_secret",
                name="旧钥匙下落",
                text="周伯告诉当前玩家，旧钥匙藏在钟摆后面。",
            )
        ],
        social_nodes=[
            SocialNode(
                id="ask_key",
                name="询问钥匙",
                goal="让周伯说明旧宅钥匙的去向",
                strategies=[
                    SocialStrategy(skill="persuade"),
                    SocialStrategy(
                        skill="fast_talk", difficulty=CheckDifficulty.HARD
                    ),
                ],
                unlock_facts=["key_secret"],
            )
        ],
    )
    other = NPC(
        id="neighbor",
        name="林女士",
        public_desc="紧张的邻居。",
        persona="热心但胆小。",
    )
    return ModuleDef(
        id="test_module",
        name="测试模组",
        opening="测试开场。",
        start_scene="room",
        scenes=[
            Scene(
                id="room",
                name="客厅",
                narration="一间客厅。",
                npcs=["butler", "neighbor"],
            )
        ],
        npcs=[npc, other],
    )


def test_relationship_bands_hide_raw_values() -> None:
    assert [
        relationship_band(value)
        for value in (-100, -60, -59, -21, -20, 20, 21, 60, 61, 100)
    ] == [
        "敌对",
        "敌对",
        "警惕",
        "警惕",
        "中立",
        "中立",
        "友善",
        "友善",
        "信任",
        "信任",
    ]


def test_game_keeps_npc_contexts_and_relations_independent() -> None:
    module = _module()
    game = Game(group_id=1, host_user_id=10, module=module, current_scene="room")
    game.append_npc_context("butler", "玩家：周伯，钥匙在哪里？")
    game.append_npc_context("neighbor", "玩家：林女士，你看见了什么？")

    game.npc_rapport["butler"] = {10: 101}
    game.npc_attitude["butler"] = -101

    assert list(game.npc_context("butler")) == ["玩家：周伯，钥匙在哪里？"]
    assert list(game.npc_context("neighbor")) == ["玩家：林女士，你看见了什么？"]
    assert game.npc_rapport_value("butler", 10) == 100  # noqa: PLR2004
    assert game.npc_attitude_value("butler") == -100  # noqa: PLR2004
    assert game.npc_rapport_band("butler", 10) == "信任"
    assert game.npc_attitude_band("butler") == "敌对"


def test_npc_context_keeps_six_public_turns() -> None:
    game = Game(group_id=1, host_user_id=10, module=_module(), current_scene="room")
    for index in range(14):
        game.append_npc_context("butler", f"公开内容 {index}")

    context = list(game.npc_context("butler"))
    assert len(context) == 12  # noqa: PLR2004
    assert context[0] == "公开内容 2"
    assert context[-1] == "公开内容 13"


def test_router_message_only_contains_related_public_context() -> None:
    game = Game(group_id=1, host_user_id=10, module=_module(), current_scene="room")
    game.npc_focus[10] = "butler"
    game.append_npc_context("butler", "玩家：我想了解这座房子的旧事。")
    game.append_npc_context("neighbor", "这段不应进入周伯的路由上下文。")

    message = build_router_user_message(game, 10, "周伯，请再说一点。")

    assert "周伯" in message
    assert "我想了解这座房子的旧事" in message
    assert "这段不应进入周伯的路由上下文" not in message
    assert "周伯告诉当前玩家" not in message
    assert "私下藏着一把旧钥匙" not in message


def test_router_json_is_strict_and_validates_route_fields() -> None:
    route = parse_route(
        "```json\n"
        '{"route":"social_action","npc_id":"butler",'
        '"node_id":"ask_key","skill":"persuade",'
        '"emotion":"empathetic","confidence":0.91,'
        '"emotion_confidence":0.8}\n```'
    )
    assert route is not None
    assert route.route == "social_action"
    assert route.skill == "persuade"
    assert route.confidence == pytest.approx(0.91)
    assert parse_route('{"route":"social_action","strategy":"intimidate"}') is not None

    assert parse_route('前缀 {"route":"kp_say"}') is None
    assert parse_route('{"route":"social_action","skill":"damage"}') is not None
    invalid = parse_route('{"route":"unknown","confidence":1}')
    assert invalid is None


@pytest.mark.asyncio
async def test_router_timeout_and_invalid_json_fail_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    game = Game(group_id=1, host_user_id=10, module=_module(), current_scene="room")
    cfg = Config()

    async def invalid_json(*args: object, **kwargs: object) -> str:  # noqa: ARG001
        return "not json"

    monkeypatch.setattr(ai_social, "complete", invalid_json)
    assert await ai_social.classify_message(game, cfg, 10, "周伯，请说说钥匙") is None

    async def timed_out(*args: object, **kwargs: object) -> str:  # noqa: ARG001
        raise TimeoutError

    monkeypatch.setattr(ai_social, "complete", timed_out)
    assert await ai_social.classify_message(game, cfg, 10, "周伯，请说说钥匙") is None
