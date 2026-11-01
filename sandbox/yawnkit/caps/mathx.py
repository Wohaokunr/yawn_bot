"""math 包：符号计算（解方程、化简、求导积分、极限、级数、因式分解）、矩阵、统计、函数作图、单位换算。"""

from __future__ import annotations

from typing import Any

from . import CapError, as_list, cap, dest, ref


def _sympy(expr: str) -> Any:
    import sympy
    from sympy.parsing.sympy_parser import (
        convert_xor,
        implicit_multiplication_application,
        parse_expr,
        standard_transformations,
    )

    text = (
        str(expr)
        .replace("×", "*")
        .replace("÷", "/")
        .replace("（", "(")
        .replace("）", ")")
    )
    try:
        return parse_expr(
            text,
            transformations=(
                *standard_transformations,
                implicit_multiplication_application,
                convert_xor,
            ),
            local_dict={"e": sympy.E, "pi": sympy.pi},
        )
    except Exception as exc:
        raise CapError(f"表达式看不懂：{exc}（写法如 x^2 + 2x - 3、sin(x)/x）") from exc


def _symbols(names: Any, expr: Any) -> list[Any]:
    import sympy

    wanted = [str(n) for n in as_list(names) if str(n)]
    if wanted:
        return [sympy.Symbol(n) for n in wanted]
    return sorted(expr.free_symbols, key=str) if hasattr(expr, "free_symbols") else []


def _out(value: Any) -> dict[str, Any]:
    import sympy

    result: dict[str, Any] = {"result": str(value), "latex": sympy.latex(value)}
    try:
        number = complex(sympy.N(value))
        result["numeric"] = (
            round(number.real, 10) if abs(number.imag) < 1e-12 else str(number)
        )
    except (TypeError, ValueError):
        pass
    return result


@cap("math.solve")
def solve(equations: list[str], variables: list[str] | None = None) -> dict[str, Any]:
    """解方程/方程组：["x^2 - 4 = 0"] 或 ["x + y = 3", "x - y = 1"]。"""

    import sympy

    parsed = []
    for item in as_list(equations):
        left, sep, right = str(item).partition("=")
        parsed.append(sympy.Eq(_sympy(left), _sympy(right)) if sep else _sympy(left))
    symbols = _symbols(variables, sympy.Tuple(*parsed))
    solutions = sympy.solve(parsed, symbols, dict=True)
    return {
        "solutions": [{str(k): str(v) for k, v in s.items()} for s in solutions],
        "numeric": [
            {str(k): str(sympy.N(v, 8)) for k, v in s.items()} for s in solutions
        ],
    }


@cap("math.simplify")
def simplify(expression: str, mode: str = "simplify") -> dict[str, Any]:
    import sympy

    expr = _sympy(expression)
    funcs = {
        "simplify": sympy.simplify,
        "expand": sympy.expand,
        "factor": sympy.factor,
        "trig": sympy.trigsimp,
        "together": sympy.together,
        "apart": sympy.apart,
    }
    if mode not in funcs:
        raise CapError(f"mode 只能是 {'/'.join(funcs)}")
    return _out(funcs[mode](expr))


@cap("math.diff")
def diff(expression: str, variable: str = "x", order: int = 1) -> dict[str, Any]:
    import sympy

    return _out(
        sympy.diff(
            _sympy(expression), sympy.Symbol(str(variable)), max(1, min(int(order), 10))
        )
    )


@cap("math.integrate")
def integrate(
    expression: str, variable: str = "x", lower: str = "", upper: str = ""
) -> dict[str, Any]:
    import sympy

    symbol = sympy.Symbol(str(variable))
    expr = _sympy(expression)
    if str(lower) != "" and str(upper) != "":
        return _out(sympy.integrate(expr, (symbol, _sympy(lower), _sympy(upper))))
    return _out(sympy.integrate(expr, symbol))


@cap("math.limit")
def limit(
    expression: str, variable: str = "x", to: str = "0", direction: str = "+-"
) -> dict[str, Any]:
    import sympy

    if direction not in {"+", "-", "+-"}:
        raise CapError("direction 只能是 + / - / +-")
    return _out(
        sympy.limit(
            _sympy(expression),
            sympy.Symbol(str(variable)),
            _sympy(str(to).replace("inf", "oo")),
            direction,
        )
    )


@cap("math.series")
def series(
    expression: str, variable: str = "x", at: str = "0", order: int = 6
) -> dict[str, Any]:
    import sympy

    return _out(
        sympy.series(
            _sympy(expression),
            sympy.Symbol(str(variable)),
            _sympy(at),
            max(2, min(int(order), 20)),
        )
    )


@cap("math.factor_int")
def factor_int(number: str) -> dict[str, Any]:
    import sympy

    try:
        value = int(str(number).strip())
    except ValueError as exc:
        raise CapError("给一个整数") from exc
    if abs(value) > 10**40:
        raise CapError("数太大了")
    return {
        "factors": {str(k): v for k, v in sympy.factorint(value).items()},
        "is_prime": bool(sympy.isprime(value)),
        "divisors": len(sympy.divisors(value)) if abs(value) < 10**15 else None,
    }


@cap("math.matrix")
def matrix(
    rows: list[list[Any]], operation: str = "all", other: list[list[Any]] | None = None
) -> dict[str, Any]:
    """矩阵：det / inverse / rank / eigen / transpose / multiply（给 other）/ solve（other 为列向量）/ all。"""

    import sympy

    try:
        m = sympy.Matrix([[_sympy(str(c)) for c in row] for row in rows])
    except (TypeError, ValueError) as exc:
        raise CapError(f"矩阵格式不对：{exc}") from exc
    result: dict[str, Any] = {"shape": list(m.shape)}
    square = m.shape[0] == m.shape[1]
    if operation in {"det", "all"} and square:
        result["det"] = str(m.det())
    if operation in {"rank", "all"}:
        result["rank"] = int(m.rank())
    if operation in {"transpose", "all"}:
        result["transpose"] = str(m.T.tolist())
    if operation in {"inverse", "all"} and square:
        result["inverse"] = str(m.inv().tolist()) if m.det() != 0 else "不可逆"
    if operation in {"eigen", "all"} and square and m.shape[0] <= 6:
        result["eigenvalues"] = {str(k): v for k, v in m.eigenvals().items()}
    if operation in {"multiply", "solve"}:
        if not other:
            raise CapError("这个操作要给 other")
        o = sympy.Matrix([[_sympy(str(c)) for c in row] for row in other])
        result[operation] = str(
            (m * o).tolist() if operation == "multiply" else m.LUsolve(o).tolist()
        )
    return result


@cap("math.stats")
def stats(values: list[float]) -> dict[str, Any]:
    import numpy as np
    from scipy import stats as st

    try:
        data = np.array([float(v) for v in as_list(values)])
    except (TypeError, ValueError) as exc:
        raise CapError("values 要是一串数字") from exc
    if not len(data):
        raise CapError("values 为空")
    mode = st.mode(data, keepdims=False)
    return {
        "count": len(data),
        "mean": float(np.mean(data)),
        "median": float(np.median(data)),
        "mode": float(mode.mode),
        "std": float(np.std(data, ddof=1)) if len(data) > 1 else 0.0,
        "min": float(np.min(data)),
        "max": float(np.max(data)),
        "q1": float(np.percentile(data, 25)),
        "q3": float(np.percentile(data, 75)),
        "sum": float(np.sum(data)),
        "skew": float(st.skew(data)) if len(data) > 2 else None,
    }


@cap("math.regression")
def regression(x: list[float], y: list[float], degree: int = 1) -> dict[str, Any]:
    """多项式拟合（degree=1 即线性回归），给出系数、R² 和拟合图。"""

    import matplotlib as mpl

    mpl.use("Agg")
    import numpy as np
    from matplotlib import pyplot as plt

    xs = np.array([float(v) for v in as_list(x)])
    ys = np.array([float(v) for v in as_list(y)])
    if len(xs) != len(ys) or len(xs) < 2:
        raise CapError("x、y 要等长且至少两个点")
    deg = max(1, min(int(degree), 8))
    coeffs = np.polyfit(xs, ys, deg)
    fitted = np.polyval(coeffs, xs)
    ss_res = float(np.sum((ys - fitted) ** 2))
    ss_tot = float(np.sum((ys - ys.mean()) ** 2)) or 1.0
    fig, ax = plt.subplots(figsize=(8, 5))
    ax.scatter(xs, ys, label="数据")
    line = np.linspace(xs.min(), xs.max(), 200)
    ax.plot(line, np.polyval(coeffs, line), color="C1", label=f"{deg} 次拟合")
    ax.legend()
    target = dest("", "拟合.png")
    fig.savefig(target)
    plt.close(fig)
    return {
        "coefficients_high_to_low": [round(float(c), 8) for c in coeffs],
        "r2": round(1 - ss_res / ss_tot, 6),
        "file": ref(target),
    }


@cap("math.plot")
def plot(
    expressions: list[str],
    variable: str = "x",
    start: float = -10,
    end: float = 10,
    title: str = "",
    output: str = "",
) -> dict[str, Any]:
    import matplotlib as mpl

    mpl.use("Agg")
    import numpy as np
    import sympy
    from matplotlib import pyplot as plt

    symbol = sympy.Symbol(str(variable))
    xs = np.linspace(float(start), float(end), 800)
    fig, ax = plt.subplots(figsize=(8, 5))
    for item in as_list(expressions)[:6]:
        fn = sympy.lambdify(symbol, _sympy(item), "numpy")
        with np.errstate(all="ignore"):
            ys = np.asarray(fn(xs), dtype=float) * np.ones_like(xs)
        ys[np.abs(ys) > 1e6] = np.nan
        ax.plot(xs, ys, label=str(item))
    ax.axhline(0, color="#888", linewidth=0.6)
    ax.axvline(0, color="#888", linewidth=0.6)
    ax.grid(alpha=0.3)
    ax.legend()
    ax.set_title(title or "函数图像")
    target = dest(output, f"{title or '函数图像'}.png")
    fig.savefig(target)
    plt.close(fig)
    return {"file": ref(target)}


@cap("math.evaluate")
def evaluate(expression: str, digits: int = 15) -> dict[str, Any]:
    """高精度计算一个算式（可含 sqrt、sin、log、pi、e、阶乘 !）。"""

    import sympy

    expr = _sympy(str(expression).replace("!", " factorial_marker "))
    if "factorial_marker" in str(expression):
        raise CapError("阶乘请写 factorial(5)")
    return {
        "result": str(sympy.N(expr, max(1, min(int(digits), 100)))),
        "exact": str(sympy.nsimplify(expr)),
    }


@cap("math.convert_unit")
def convert_unit(value: float, from_unit: str, to_unit: str) -> dict[str, Any]:
    """常见单位换算（长度、质量、时间、速度、面积、体积、数据量、温度），如 5 mile → km。"""

    temps = {"c", "f", "k", "摄氏度", "华氏度", "开尔文"}
    src_unit, dst_unit = str(from_unit).strip().lower(), str(to_unit).strip().lower()
    number = float(value)
    if src_unit in temps and dst_unit in temps:
        alias = {"摄氏度": "c", "华氏度": "f", "开尔文": "k"}
        a, b = alias.get(src_unit, src_unit), alias.get(dst_unit, dst_unit)
        celsius = (
            number
            if a == "c"
            else (number - 32) * 5 / 9
            if a == "f"
            else number - 273.15
        )
        result = (
            celsius
            if b == "c"
            else celsius * 9 / 5 + 32
            if b == "f"
            else celsius + 273.15
        )
        return {"result": round(result, 6), "unit": to_unit}
    import sympy.physics.units as u

    table: dict[str, Any] = {
        "m": u.meter,
        "km": u.kilometer,
        "cm": u.centimeter,
        "mm": u.millimeter,
        "mile": u.mile,
        "inch": u.inch,
        "ft": u.foot,
        "yard": u.yard,
        "米": u.meter,
        "公里": u.kilometer,
        "厘米": u.centimeter,
        "毫米": u.millimeter,
        "英里": u.mile,
        "英寸": u.inch,
        "英尺": u.foot,
        "kg": u.kilogram,
        "g": u.gram,
        "mg": u.milligram,
        "lb": u.pound,
        "t": u.tonne,
        "千克": u.kilogram,
        "克": u.gram,
        "斤": u.kilogram / 2,
        "吨": u.tonne,
        "s": u.second,
        "min": u.minute,
        "h": u.hour,
        "day": u.day,
        "秒": u.second,
        "分钟": u.minute,
        "小时": u.hour,
        "天": u.day,
        "km/h": u.kilometer / u.hour,
        "m/s": u.meter / u.second,
        "mph": u.mile / u.hour,
        "l": u.liter,
        "ml": u.milliliter,
        "升": u.liter,
        "毫升": u.milliliter,
        "m2": u.meter**2,
        "km2": u.kilometer**2,
        "亩": 2000 * u.meter**2 / 3,
        "公顷": 10000 * u.meter**2,
        "b": u.byte,
        "kb": 1024 * u.byte,
        "mb": 1024**2 * u.byte,
        "gb": 1024**3 * u.byte,
        "tb": 1024**4 * u.byte,
    }
    if src_unit not in table or dst_unit not in table:
        raise CapError(f"不认识的单位，支持：{', '.join(sorted(table))} 和温度 c/f/k")
    converted = (
        u.convert_to(number * table[src_unit], table[dst_unit]) / table[dst_unit]
    )
    try:
        return {"result": round(float(converted), 8), "unit": to_unit}
    except TypeError as exc:
        raise CapError("这两个单位不是同一种量") from exc
