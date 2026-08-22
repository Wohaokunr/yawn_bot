from __future__ import annotations

import importlib.util
import types
from pathlib import Path

import pytest

ROOT = (
    Path(__file__).resolve().parents[1]
    / "src"
    / "plugins"
    / "yawn_core"
    / "yawn_agent"
)


def _load_log():
    spec = importlib.util.spec_from_file_location(
        "yawn_agent_log_test", ROOT / "log.py"
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


log = _load_log()


class _Logger:
    def __init__(self) -> None:
        self.messages: list[str] = []
        self.options: list[dict[str, object]] = []

    def info(self, message: str) -> None:
        self.messages.append(message)

    def opt(self, **options: object) -> _Logger:
        self.options.append(options)
        return self


@pytest.mark.parametrize(
    ("configured", "enabled"),
    [(True, True), (False, False), ("true", True), ("off", False)],
)
def test_debug_switch_reads_nonebot_config(
    monkeypatch: pytest.MonkeyPatch, configured: object, enabled: object
) -> None:
    output = _Logger()
    monkeypatch.setattr(
        log,
        "get_driver",
        lambda: types.SimpleNamespace(
            config=types.SimpleNamespace(agent_debug_log=configured)
        ),
    )
    monkeypatch.setattr(log, "logger", output)

    log.dbg("config probe")

    assert bool(output.messages) is bool(enabled)
    if enabled:
        assert output.messages == ["[agent-debug] config probe"]


def test_debug_switch_falls_back_to_process_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    output = _Logger()

    def uninitialized_driver() -> object:
        raise ValueError

    monkeypatch.setattr(log, "get_driver", uninitialized_driver)
    monkeypatch.setenv("AGENT_DEBUG_LOG", "yes")
    monkeypatch.setattr(log, "logger", output)

    log.dbg("environment probe")

    assert output.messages == ["[agent-debug] environment probe"]


def test_debug_exception_uses_info_level_and_exception_option(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    output = _Logger()
    monkeypatch.setattr(
        log,
        "get_driver",
        lambda: types.SimpleNamespace(
            config=types.SimpleNamespace(agent_debug_log=True)
        ),
    )
    monkeypatch.setattr(log, "logger", output)

    def raise_probe() -> None:
        raise RuntimeError

    try:
        raise_probe()
    except RuntimeError:
        log.dbg_exc("exception probe")

    assert output.messages == ["[agent-debug] exception probe"]
    assert output.options == [{"exception": True}]
