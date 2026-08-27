from __future__ import annotations

import importlib
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import nonebot
import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="module")
def visual_modules() -> SimpleNamespace:
    if str(PROJECT_ROOT) not in sys.path:
        sys.path.insert(0, str(PROJECT_ROOT))
    try:
        nonebot.get_driver()
    except ValueError:
        nonebot.init()
    if nonebot.get_plugin("yawn_core") is None:
        nonebot.load_from_toml("pyproject.toml")
    return SimpleNamespace(
        renderer=importlib.import_module(
            "src.plugins.yawn_core.ui.panel_renderer"
        ),
        ux=importlib.import_module("src.plugins.yawn_core.command_ux"),
    )


@pytest.mark.asyncio
async def test_personal_panel_htmlkit_renders_png(
    visual_modules: SimpleNamespace,
) -> None:
    renderer = visual_modules.renderer
    view = renderer.PersonalPanelView(
        user_id=123456,
        nickname="Yawn Tester",
        mode_label="个人模式",
        subtitle="HTMLKit test",
        last_active="2026-08-27 10:57",
        avatar_url=None,
        stats=(
            renderer.PanelStat("累计签到", "12 天"),
            renderer.PanelStat("积分", "340"),
            renderer.PanelStat("AI 对话", "7 个"),
        ),
        actions=("1 我的群聊", "2 对话管理", "0 退出"),
    )

    data = await renderer.render_personal_panel(view)

    assert data is not None
    assert data.startswith(b"\x89PNG\r\n\x1a\n")


@pytest.mark.asyncio
async def test_help_home_htmlkit_renders_png(visual_modules: SimpleNamespace) -> None:
    renderer = visual_modules.renderer
    cards = (
        renderer.HelpMenuCard(1, "个人与基础功能", "签到、个人面板", "面板"),
        renderer.HelpMenuCard(2, "群聊 Agent", "查看群聊智能助手", "Agent状态"),
    )

    data = await renderer.render_help_menu(cards)

    assert data is not None
    assert data.startswith(b"\x89PNG\r\n\x1a\n")


@pytest.mark.asyncio
async def test_renderer_failure_returns_none_for_text_fallback(
    visual_modules: SimpleNamespace,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    renderer = visual_modules.renderer

    def fail_require(_name: str) -> Any:
        raise RuntimeError

    monkeypatch.setattr(renderer, "require", fail_require)

    data = await renderer.render_help_menu(
        (renderer.HelpMenuCard(1, "基础", "说明", "面板"),)
    )

    assert data is None


def test_command_ux_explains_reason_and_next_step(
    visual_modules: SimpleNamespace,
) -> None:
    ux = visual_modules.ux

    message = ux.condition_unmet(
        "当前没有狼人杀房间",
        "发送 /狼人杀 创建一局，或 /help 狼人杀 查看玩法。",
    )

    assert message == (
        "当前没有狼人杀房间。"
        "发送 /狼人杀 创建一局，或 /help 狼人杀 查看玩法。"
    )
    assert "没有这个选项" in ux.invalid_choice(valid="1-3")
