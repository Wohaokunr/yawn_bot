"""合成包引导：不启动 NoneBot 直接加载 yawn_rpg 的 schema 三件套。

`yawn_core` / `yawn_rpg` 的包级 `__init__` 依赖已初始化的 NoneBot
与整个机器人栈，普通 import 会拖起 ORM 与 LLM 客户端。本模块用
`types.ModuleType` 伪造一个父包，按 charsheet → module_schema →
dice 的次序逐文件加载（dice 依赖 module_schema 的 CheckDifficulty），
从而拿到与 bot 完全同源的模型与校验函数。

module_schema 仅从 nonebot 取 logger（无需 nonebot.init()）；若运行
环境连 nonebot2 都没装，这里注入一个 logging 版桩，让编辑器在最小
环境也能跑。
"""

from __future__ import annotations

import importlib.util
import logging
import sys
import types
from importlib.machinery import SourceFileLoader
from pathlib import Path
from types import ModuleType

# 合成父包名：仅用于 sys.modules 注册，避免与真实插件包名冲突
_PKG = "_rpg_module_editor_schema"


def find_yawn_rpg_dir() -> Path:
    """定位 yawn_rpg 插件目录（自本文件上溯至仓库根）。"""
    root = Path(__file__).resolve().parents[2]
    candidate = root / "src" / "plugins" / "yawn_core" / "yawn_rpg"
    if (candidate / "module_schema.py").is_file():
        return candidate
    msg = (
        f"找不到跑团插件目录 {candidate}：模组编辑器必须置于仓库的 "
        "tools/ 下运行，或确认 src/plugins/yawn_core/yawn_rpg 存在"
    )
    raise FileNotFoundError(msg)


def _ensure_nonebot_logger() -> None:
    """nonebot 可导入则用真 logger；否则注入 logging 桩。"""
    if "nonebot" in sys.modules:
        return
    try:
        import nonebot  # noqa: F401
    except Exception:  # noqa: BLE001 —— 最小环境下允许缺席
        stub = types.ModuleType("nonebot")
        stub.logger = logging.getLogger("rpg_module_editor")  # type: ignore[attr-defined]
        sys.modules["nonebot"] = stub


def _load(name: str, directory: Path) -> ModuleType:
    """以合成包子模块的身份加载单个文件并注册进 sys.modules。"""
    path = directory / f"{name}.py"
    if not path.is_file():
        msg = f"合成包引导失败：缺少 {path}"
        raise FileNotFoundError(msg)
    spec = importlib.util.spec_from_file_location(f"{_PKG}.{name}", path)
    if spec is None or not isinstance(spec.loader, SourceFileLoader):
        msg = f"合成包引导失败：无法为 {path} 创建加载规格"
        raise ImportError(msg)
    module = importlib.util.module_from_spec(spec)
    sys.modules[f"{_PKG}.{name}"] = module
    spec.loader.exec_module(module)
    return module


def bootstrap() -> ModuleType:
    """执行引导并返回 module_schema 模块对象（幂等）。"""
    if f"{_PKG}.module_schema" in sys.modules:
        return sys.modules[f"{_PKG}.module_schema"]
    _ensure_nonebot_logger()
    directory = find_yawn_rpg_dir()
    parent = types.ModuleType(_PKG)
    parent.__path__ = [str(directory)]
    sys.modules[_PKG] = parent
    _load("charsheet", directory)
    schema = _load("module_schema", directory)
    _load("dice", directory)
    return schema


_schema = bootstrap()
_charsheet = sys.modules[f"{_PKG}.charsheet"]
_dice = sys.modules[f"{_PKG}.dice"]

# ── 再导出：编辑器其余模块一律从本模块取，勿直接碰合成包 ──

ModuleDef = _schema.ModuleDef
Scene = _schema.Scene
CheckPoint = _schema.CheckPoint
CheckMode = _schema.CheckMode
Exit = _schema.Exit
ScheduleEntry = _schema.ScheduleEntry
TimeConfig = _schema.TimeConfig
NPC = _schema.NPC
NPCFact = _schema.NPCFact
SocialNode = _schema.SocialNode
SocialStrategy = _schema.SocialStrategy
Monster = _schema.Monster
Clue = _schema.Clue
Ending = _schema.Ending
PlotEvent = _schema.PlotEvent
ConditionContext = _schema.ConditionContext
CheckDifficulty = _schema.CheckDifficulty

evaluate_condition = _schema.evaluate_condition
is_trivially_true = _schema.is_trivially_true
load_modules = _schema.load_modules

SKILLS = _charsheet.SKILLS

is_valid_dice_expr = _dice.is_valid_dice_expr

# 私有符号是编辑器实时反馈的必需件；schema 改版缺失时在此硬失败
try:
    validate_condition = _schema._validate_condition
    parse_hhmm = _schema._parse_hhmm
    in_window = _schema._in_window
    is_dice_expr = _schema._is_dice_expr
    is_san_loss = _schema._is_san_loss
    valid_check_skills = _schema._VALID_CHECK_SKILLS
except AttributeError as e:  # pragma: no cover - schema 改版防御
    msg = f"module_schema 私有接口缺失，编辑器需同步更新：{e}"
    raise RuntimeError(msg) from e


def modules_dir() -> Path:
    """模组目录（yawn_rpg/modules）。"""
    return find_yawn_rpg_dir() / "modules"


__all__ = [
    "NPC",
    "SKILLS",
    "CheckDifficulty",
    "CheckMode",
    "CheckPoint",
    "Clue",
    "ConditionContext",
    "Ending",
    "Exit",
    "ModuleDef",
    "Monster",
    "NPCFact",
    "PlotEvent",
    "Scene",
    "ScheduleEntry",
    "SocialNode",
    "SocialStrategy",
    "TimeConfig",
    "evaluate_condition",
    "in_window",
    "is_dice_expr",
    "is_san_loss",
    "is_trivially_true",
    "is_valid_dice_expr",
    "load_modules",
    "modules_dir",
    "parse_hhmm",
    "valid_check_skills",
    "validate_condition",
]
