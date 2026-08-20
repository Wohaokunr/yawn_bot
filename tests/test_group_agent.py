from __future__ import annotations

import importlib.util
import sys
from datetime import datetime, timedelta
from pathlib import Path

from nonebot.adapters.onebot.v11 import Message

ROOT = (
    Path(__file__).resolve().parents[1]
    / "src"
    / "plugins"
    / "yawn_core"
    / "yawn_agent"
)


def _load(name: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / f"{name}.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


_context = _load("context")
_parser = _load("message_parser")
ActivitySnapshot = _context.ActivitySnapshot
coldness_score = _context.coldness_score
normalize_message = _parser.normalize_message


def test_normalize_media_at_and_forward_placeholders() -> None:
    message = Message(
        "hello[CQ:image,file=x.jpg,url=https://example.test/x.jpg]"
        "[CQ:file,file=x.pdf,name=x.pdf]"
    )
    normalized = normalize_message(message)

    assert "hello" in normalized.plain_text
    assert any(item["type"] in {"image", "file"} for item in normalized.media_refs)
    assert normalized.prompt_text()


def test_coldness_increases_after_idle_period() -> None:
    now = datetime(2026, 1, 1, 12, 0)  # noqa: DTZ001
    active = ActivitySnapshot(
        now - timedelta(minutes=1), messages_5m=5, messages_20m=10
    )
    idle = ActivitySnapshot(now - timedelta(minutes=60))
    assert coldness_score(idle, now) > coldness_score(active, now)


def test_proactive_policy_inputs_are_available() -> None:
    now = datetime(2026, 1, 1, 12, 0)  # noqa: DTZ001
    snapshot = ActivitySnapshot(now - timedelta(hours=1), proactive_today=0)
    assert coldness_score(snapshot, now) > 0.6  # noqa: PLR2004
