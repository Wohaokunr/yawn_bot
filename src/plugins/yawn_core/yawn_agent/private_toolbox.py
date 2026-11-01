# ruff: noqa: PLR2004, E501, TRY003
"""私聊小工具箱：计算、单位换算、日期推算、骰子硬币抽签、今日运势、世界时间。

全部是纯函数、零网络、零模型成本，结果可复现（运势按“用户 + 日期”固定）。
算术用 AST 白名单求值，绝不 ``eval``。
"""

from __future__ import annotations

import ast
import hashlib
import math
import operator
import random
from datetime import date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

_MAX_EXPR = 200
_MAX_POWER = 10_000
_MAX_ABS = 1e100

_BINOPS: dict[type, Any] = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.Div: operator.truediv,
    ast.FloorDiv: operator.floordiv,
    ast.Mod: operator.mod,
    ast.Pow: operator.pow,
}
_UNARY: dict[type, Any] = {ast.UAdd: operator.pos, ast.USub: operator.neg}
_FUNCS: dict[str, Any] = {
    "sqrt": math.sqrt,
    "abs": abs,
    "round": round,
    "sin": math.sin,
    "cos": math.cos,
    "tan": math.tan,
    "log": math.log,
    "log10": math.log10,
    "ln": math.log,
    "floor": math.floor,
    "ceil": math.ceil,
}
_CONSTS = {"pi": math.pi, "e": math.e}


class ToolboxError(ValueError):
    """参数不合法或算不出来；文本直接给模型看。"""


def _eval(node: ast.AST) -> float:
    if isinstance(node, ast.Expression):
        return _eval(node.body)
    if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)):
        return node.value
    if isinstance(node, ast.Name) and node.id in _CONSTS:
        return _CONSTS[node.id]
    if isinstance(node, ast.UnaryOp) and type(node.op) in _UNARY:
        return _UNARY[type(node.op)](_eval(node.operand))
    if isinstance(node, ast.BinOp) and type(node.op) in _BINOPS:
        left, right = _eval(node.left), _eval(node.right)
        if isinstance(node.op, ast.Pow) and (abs(right) > _MAX_POWER or abs(left) > _MAX_ABS):
            raise ToolboxError("指数太大了")
        value = _BINOPS[type(node.op)](left, right)
        if isinstance(value, (int, float)) and abs(value) > _MAX_ABS:
            raise ToolboxError("结果太大了")
        return value
    if (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id in _FUNCS
        and not node.keywords
        and 1 <= len(node.args) <= 2
    ):
        return _FUNCS[node.func.id](*[_eval(arg) for arg in node.args])
    raise ToolboxError("只支持数字、+ - * / // % ** 、括号和 sqrt/abs/round/sin/cos/tan/log/ln/floor/ceil")


def calculate(expression: str) -> dict[str, Any]:
    text = (
        str(expression or "")
        .strip()
        .replace("×", "*")
        .replace("÷", "/")
        .replace("^", "**")
        .replace("（", "(")
        .replace("）", ")")
        .replace("，", ",")
    )
    if not text or len(text) > _MAX_EXPR:
        raise ToolboxError("算式为空或太长")
    try:
        tree = ast.parse(text, mode="eval")
        value = _eval(tree)
    except ZeroDivisionError as exc:
        raise ToolboxError("除数不能为 0") from exc
    except (SyntaxError, TypeError, OverflowError) as exc:
        raise ToolboxError("算式看不懂") from exc
    except ValueError as exc:
        if isinstance(exc, ToolboxError):
            raise
        raise ToolboxError("超出定义域（比如负数开方）") from exc
    if isinstance(value, float):
        if math.isnan(value) or math.isinf(value):
            raise ToolboxError("结果不是有限数")
        if value.is_integer() and abs(value) < 1e15:
            value = int(value)
        else:
            value = round(value, 10)
    return {"expression": text, "result": value}


# 单位换算：类别 → 单位 → 换算到基准单位的倍数
_UNITS: dict[str, dict[str, float]] = {
    "length": {
        "mm": 0.001, "毫米": 0.001, "cm": 0.01, "厘米": 0.01, "m": 1.0, "米": 1.0,
        "km": 1000.0, "公里": 1000.0, "千米": 1000.0, "in": 0.0254, "英寸": 0.0254,
        "ft": 0.3048, "英尺": 0.3048, "mi": 1609.344, "英里": 1609.344, "里": 500.0,
        "尺": 1 / 3, "寸": 1 / 30,
    },
    "mass": {
        "mg": 1e-6, "毫克": 1e-6, "g": 0.001, "克": 0.001, "kg": 1.0, "千克": 1.0,
        "公斤": 1.0, "斤": 0.5, "两": 0.05, "t": 1000.0, "吨": 1000.0,
        "lb": 0.45359237, "磅": 0.45359237, "oz": 0.028349523125, "盎司": 0.028349523125,
    },
    "volume": {
        "ml": 0.001, "毫升": 0.001, "l": 1.0, "升": 1.0, "gal": 3.785411784,
        "加仑": 3.785411784, "杯": 0.25,
    },
    "area": {
        "m2": 1.0, "平方米": 1.0, "km2": 1e6, "平方公里": 1e6, "亩": 666.6667,
        "公顷": 10000.0, "ha": 10000.0, "ft2": 0.09290304, "平方英尺": 0.09290304,
    },
    "time": {
        "s": 1.0, "秒": 1.0, "min": 60.0, "分钟": 60.0, "h": 3600.0, "小时": 3600.0,
        "day": 86400.0, "天": 86400.0, "week": 604800.0, "周": 604800.0,
    },
    "data": {
        "b": 1.0, "kb": 1024.0, "mb": 1024.0**2, "gb": 1024.0**3, "tb": 1024.0**4,
    },
    "speed": {
        "m/s": 1.0, "km/h": 1 / 3.6, "mph": 0.44704, "节": 0.514444,
    },
}
_TEMPS = {"c": "c", "℃": "c", "摄氏度": "c", "f": "f", "℉": "f", "华氏度": "f", "k": "k", "开尔文": "k"}


def _to_celsius(value: float, unit: str) -> float:
    if unit == "f":
        return (value - 32) * 5 / 9
    if unit == "k":
        return value - 273.15
    return value


def _from_celsius(value: float, unit: str) -> float:
    if unit == "f":
        return value * 9 / 5 + 32
    if unit == "k":
        return value + 273.15
    return value


def convert(value: float, from_unit: str, to_unit: str) -> dict[str, Any]:
    src = str(from_unit or "").strip().lower()
    dst = str(to_unit or "").strip().lower()
    if src in _TEMPS and dst in _TEMPS:
        result = _from_celsius(_to_celsius(float(value), _TEMPS[src]), _TEMPS[dst])
        return {"value": value, "from": from_unit, "to": to_unit, "result": round(result, 4)}
    for category, table in _UNITS.items():
        if src in table and dst in table:
            result = float(value) * table[src] / table[dst]
            return {
                "value": value,
                "from": from_unit,
                "to": to_unit,
                "category": category,
                "result": round(result, 6),
            }
    raise ToolboxError("这两个单位换算不了（或不是同一类）；支持长度、重量、体积、面积、时间、数据、速度、温度")


def _parse_date(text: str, today: date) -> date:
    raw = str(text or "").strip()
    if raw in {"", "今天", "today"}:
        return today
    if raw in {"明天", "tomorrow"}:
        return today + timedelta(days=1)
    if raw in {"昨天", "yesterday"}:
        return today - timedelta(days=1)
    for fmt in ("%Y-%m-%d", "%Y/%m/%d", "%Y.%m.%d", "%m-%d", "%m/%d"):
        try:
            parsed = datetime.strptime(raw, fmt)  # noqa: DTZ007
        except ValueError:
            continue
        if "%Y" not in fmt:
            return parsed.date().replace(year=today.year)
        return parsed.date()
    raise ToolboxError(f"日期「{raw}」看不懂，用 YYYY-MM-DD")


_WEEKDAYS = "一二三四五六日"


def date_diff(start: str, end: str, today: date) -> dict[str, Any]:
    a, b = _parse_date(start, today), _parse_date(end, today)
    days = (b - a).days
    return {
        "from": a.isoformat(),
        "to": b.isoformat(),
        "days": days,
        "weeks": round(days / 7, 1),
        "weekday_to": f"星期{_WEEKDAYS[b.weekday()]}",
    }


def date_add(base: str, days: int, today: date) -> dict[str, Any]:
    start = _parse_date(base, today)
    if abs(int(days)) > 365 * 200:
        raise ToolboxError("天数太大了")
    target = start + timedelta(days=int(days))
    return {
        "from": start.isoformat(),
        "days": int(days),
        "date": target.isoformat(),
        "weekday": f"星期{_WEEKDAYS[target.weekday()]}",
    }


def roll_dice(sides: int = 6, count: int = 1, *, rng: random.Random | None = None) -> dict[str, Any]:
    sides = max(2, min(int(sides or 6), 1000))
    count = max(1, min(int(count or 1), 20))
    picker = rng or random.SystemRandom()
    rolls = [picker.randint(1, sides) for _ in range(count)]
    return {"sides": sides, "rolls": rolls, "total": sum(rolls)}


def flip_coin(*, rng: random.Random | None = None) -> dict[str, Any]:
    picker = rng or random.SystemRandom()
    return {"result": picker.choice(["正面", "反面"])}


def pick_one(options: list[str], *, rng: random.Random | None = None) -> dict[str, Any]:
    cleaned = [str(item).strip() for item in options if str(item).strip()][:30]
    if len(cleaned) < 2:
        raise ToolboxError("至少给两个选项")
    picker = rng or random.SystemRandom()
    return {"options": cleaned, "picked": picker.choice(cleaned)}


_FORTUNE_LEVELS = ("大吉", "中吉", "小吉", "吉", "末吉", "平", "小凶")
_FORTUNE_GOOD = ("早睡", "喝水", "整理房间", "学点新东西", "出门散步", "给朋友发消息", "听歌", "吃顿好的", "运动半小时", "写点东西")
_FORTUNE_BAD = ("熬夜", "冲动消费", "拖延", "空腹喝咖啡", "和人抬杠", "久坐", "立 flag", "刷手机到半夜")
_LUCKY_COLORS = ("红色", "橙色", "黄色", "绿色", "青色", "蓝色", "紫色", "白色", "粉色", "黑色")


def daily_fortune(user_id: int, day: date) -> dict[str, Any]:
    """同一人同一天结果固定。"""

    digest = hashlib.sha256(f"{int(user_id)}:{day.isoformat()}".encode()).digest()
    rng = random.Random(int.from_bytes(digest[:8], "big"))
    good = rng.sample(_FORTUNE_GOOD, 2)
    bad = rng.sample(_FORTUNE_BAD, 2)
    return {
        "day": day.isoformat(),
        "level": rng.choice(_FORTUNE_LEVELS),
        "good_for": good,
        "avoid": bad,
        "lucky_color": rng.choice(_LUCKY_COLORS),
        "lucky_number": rng.randint(0, 99),
        "note": "娱乐向，结果按日期固定，别当真",
    }


_CITY_ZONES = {
    "北京": "Asia/Shanghai", "上海": "Asia/Shanghai", "东京": "Asia/Tokyo",
    "首尔": "Asia/Seoul", "新加坡": "Asia/Singapore", "伦敦": "Europe/London",
    "巴黎": "Europe/Paris", "柏林": "Europe/Berlin", "莫斯科": "Europe/Moscow",
    "纽约": "America/New_York", "洛杉矶": "America/Los_Angeles", "芝加哥": "America/Chicago",
    "悉尼": "Australia/Sydney", "迪拜": "Asia/Dubai", "曼谷": "Asia/Bangkok",
    "香港": "Asia/Hong_Kong", "台北": "Asia/Taipei", "温哥华": "America/Vancouver",
    "多伦多": "America/Toronto", "UTC": "UTC",
}


def world_time(place: str, now_utc: datetime) -> dict[str, Any]:
    raw = str(place or "").strip()
    zone_name = _CITY_ZONES.get(raw, raw)
    try:
        zone = ZoneInfo(zone_name)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        cities = "、".join(list(_CITY_ZONES)[:12])
        raise ToolboxError(f"不认识「{raw}」，可以用城市名（{cities}…）或 IANA 时区名") from exc
    local = now_utc.astimezone(zone)
    offset = local.utcoffset() or timedelta()
    hours = offset.total_seconds() / 3600
    return {
        "place": raw,
        "zone": zone_name,
        "time": f"{local:%Y-%m-%d %H:%M}",
        "weekday": f"星期{_WEEKDAYS[local.weekday()]}",
        "utc_offset": f"UTC{hours:+g}",
    }


__all__ = [
    "ToolboxError",
    "calculate",
    "convert",
    "daily_fortune",
    "date_add",
    "date_diff",
    "flip_coin",
    "pick_one",
    "roll_dice",
    "world_time",
]
