"""HTMLKit renderers for lightweight QQ visual cards.

This module contains presentation only. Business queries and matcher state stay in
``panel.py`` / ``help_panel.py``. Rendering failures are intentionally converted to
``None`` so callers can fall back to plain text.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from nonebot import logger, require

_TEMPLATE_DIR = Path(__file__).resolve().parent.parent / "templates"


@dataclass(frozen=True, slots=True)
class PanelStat:
    label: str
    value: str
    hint: str = ""


@dataclass(frozen=True, slots=True)
class PanelFeature:
    name: str
    enabled: bool
    source: str = ""


@dataclass(frozen=True, slots=True)
class PersonalPanelView:
    user_id: int
    nickname: str
    mode_label: str
    subtitle: str
    last_active: str
    stats: tuple[PanelStat, ...] = ()
    features: tuple[PanelFeature, ...] = ()
    actions: tuple[str, ...] = ()
    avatar_url: str | None = None
    accent_note: str = "YawnBot · Personal Panel"


@dataclass(frozen=True, slots=True)
class HelpMenuCard:
    index: int
    title: str
    summary: str
    entrypoint: str


async def _render(
    template_name: str,
    context: dict[str, Any],
    *,
    width: int,
) -> bytes | None:
    try:
        require("nonebot_plugin_htmlkit")
        from nonebot_plugin_htmlkit import template_to_pic

        return await template_to_pic(
            _TEMPLATE_DIR,
            template_name,
            context,
            max_width=width,
            device_height=10,
            allow_refit=True,
            image_format="png",
        )
    except Exception:  # noqa: BLE001
        logger.warning(
            f"HTMLKit 渲染 {template_name} 失败，已降级为纯文本",
            exc_info=True,
        )
        return None


async def render_personal_panel(view: PersonalPanelView) -> bytes | None:
    """Render the personal panel; return ``None`` on any renderer failure."""

    return await _render(
        "personal_panel.html",
        {"panel": view},
        width=760,
    )


async def render_help_menu(cards: tuple[HelpMenuCard, ...]) -> bytes | None:
    """Render only the first-level help menu as a compact visual card."""

    return await _render(
        "help_panel.html",
        {"cards": cards},
        width=760,
    )


__all__ = [
    "HelpMenuCard",
    "PanelFeature",
    "PanelStat",
    "PersonalPanelView",
    "render_help_menu",
    "render_personal_panel",
]
