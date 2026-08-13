from __future__ import annotations

import importlib
import sys
from pathlib import Path
from typing import Any

import nonebot
import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="module")
def core_module() -> Any:
    if str(PROJECT_ROOT) not in sys.path:
        sys.path.insert(0, str(PROJECT_ROOT))
    try:
        nonebot.get_driver()
    except ValueError:
        nonebot.init()
    if nonebot.get_plugin("yawn_core") is None:
        nonebot.load_from_toml("pyproject.toml")
    return importlib.import_module("src.plugins.yawn_core")


class _Logger:
    def __init__(self) -> None:
        self.info_messages: list[str] = []
        self.warning_messages: list[str] = []
        self.error_messages: list[str] = []

    def info(self, message: str) -> None:
        self.info_messages.append(message)

    def warning(self, message: str, **_kwargs: object) -> None:
        self.warning_messages.append(message)

    def error(self, message: str) -> None:
        self.error_messages.append(message)


class _BrokenDependencyError(RuntimeError):
    def __init__(self) -> None:
        super().__init__("broken dependency")


def test_sub_plugins_are_loaded_independently(
    core_module: Any,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    for dirname in ("yawn_werewolf", "yawn_rpg", "yawn_fanqie"):
        (tmp_path / dirname).mkdir()

    calls: list[str] = []

    def load_plugin(module_name: str) -> object:
        calls.append(module_name)
        if module_name.endswith("yawn_werewolf"):
            raise _BrokenDependencyError
        return object()

    logger = _Logger()
    monkeypatch.setattr(core_module, "logger", logger)
    report = core_module._load_sub_plugins(
        load_plugin=load_plugin,
        package_dir=tmp_path,
    )

    assert calls == [
        "src.plugins.yawn_core.yawn_werewolf",
        "src.plugins.yawn_core.yawn_rpg",
        "src.plugins.yawn_core.yawn_fanqie",
    ]
    assert [item.state for item in report] == ["failed", "loaded", "loaded"]
    assert report[0].detail == "_BrokenDependencyError: broken dependency"
    assert "已跳过" in logger.warning_messages[0]


def test_missing_and_unregistered_plugins_are_reported(
    core_module: Any,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    (tmp_path / "yawn_werewolf").mkdir()
    logger = _Logger()
    monkeypatch.setattr(core_module, "logger", logger)

    report = core_module._load_sub_plugins(
        load_plugin=lambda _module_name: None,
        package_dir=tmp_path,
    )

    assert [item.state for item in report] == ["failed", "missing", "missing"]
    assert report[0].detail == "NoneBot 未返回已注册插件"
    assert report[1].detail == "目录不存在"


@pytest.mark.asyncio
async def test_startup_report_surfaces_failures(
    core_module: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    logger = _Logger()
    monkeypatch.setattr(core_module, "logger", logger)
    monkeypatch.setattr(
        core_module,
        "_SUB_PLUGIN_LOAD_REPORT",
        (
            core_module.SubPluginLoadStatus("pkg.werewolf", "狼人杀", "loaded"),
            core_module.SubPluginLoadStatus(
                "pkg.rpg",
                "跑团",
                "failed",
                "NoneBot 未返回已注册插件",
            ),
        ),
    )

    await core_module._report_sub_plugin_status()

    assert not logger.info_messages
    assert len(logger.error_messages) == 1
    assert "已加载=狼人杀" in logger.error_messages[0]
    assert "失败=跑团" in logger.error_messages[0]
    assert "NoneBot 未返回已注册插件" in logger.error_messages[0]
