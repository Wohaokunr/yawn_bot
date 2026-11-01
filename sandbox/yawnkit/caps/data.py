"""data 包：表格（CSV/Excel/JSON）的预览、统计、筛选排序、分组透视、合并、作图、SQL。"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from . import CapError, cap, clip_text, dest, ref, src


def load_table(path: str, sheet: str = "") -> Any:
    import pandas as pd

    target = src(path, kind="表格")
    suffix = target.suffix.lower()
    try:
        if suffix in {".xlsx", ".xlsm", ".xls"}:
            return pd.read_excel(target, sheet_name=sheet or 0)
        if suffix == ".json":
            return pd.read_json(target)
        if suffix == ".parquet":
            return pd.read_parquet(target)
        for encoding in ("utf-8-sig", "gb18030"):
            try:
                return pd.read_csv(target, encoding=encoding, sep=None, engine="python")
            except UnicodeDecodeError:
                continue
    except (ValueError, OSError) as exc:
        raise CapError(f"读不了这个表格：{exc}") from exc
    raise CapError("表格编码认不出来")


def _save(frame: Any, output: str, default: str) -> dict[str, Any]:
    target = dest(output, default)
    if target.suffix.lower() in {".xlsx", ".xls"}:
        frame.to_excel(target, index=False)
    else:
        frame.to_csv(target, index=False, encoding="utf-8-sig")
    return {"file": ref(target), "rows": len(frame)}


def _show(frame: Any, rows: int = 15) -> str:
    return frame.head(rows).to_string(max_cols=20, max_colwidth=30)


def _stem(path: str) -> str:
    return Path(str(path).removeprefix("sandbox:")).stem


def _columns(frame: Any, names: Any) -> list[str]:
    wanted = [str(n) for n in (names if isinstance(names, list) else [names]) if str(n)]
    missing = [n for n in wanted if n not in frame.columns]
    if missing:
        raise CapError(
            f"没有列 {missing}，现有列：{list(map(str, frame.columns))[:30]}"
        )
    return wanted


@cap("data.preview")
def preview(path: str, rows: int = 10, sheet: str = "") -> dict[str, Any]:
    frame = load_table(path, sheet)
    return {
        "shape": list(frame.shape),
        "columns": {str(c): str(t) for c, t in frame.dtypes.items()},
        **clip_text(_show(frame, max(1, min(int(rows), 50)))),
    }


@cap("data.describe")
def describe(path: str) -> dict[str, Any]:
    frame = load_table(path)
    missing = {str(k): int(v) for k, v in frame.isna().sum().items() if v}
    return {
        "shape": list(frame.shape),
        "missing": missing,
        **clip_text(
            frame.describe(include="all").transpose().to_string(max_colwidth=20)
        ),
    }


@cap("data.filter")
def filter_rows(path: str, query: str, output: str = "") -> dict[str, Any]:
    frame = load_table(path)
    try:
        result = frame.query(str(query), engine="python")
    except Exception as exc:
        raise CapError(
            f'筛选条件写错了：{exc}（写法如 `分数 > 80 and 班级 == "一班"`，列名有空格用反引号）'
        ) from exc
    payload = _save(result, output, f"{_stem(path)}_筛选.csv")
    payload.update(clip_text(_show(result)))
    return payload


@cap("data.sort")
def sort(
    path: str, by: list[str], descending: bool = False, output: str = ""
) -> dict[str, Any]:
    frame = load_table(path)
    result = frame.sort_values(_columns(frame, by), ascending=not descending)
    payload = _save(result, output, f"{_stem(path)}_排序.csv")
    payload.update(clip_text(_show(result)))
    return payload


@cap("data.top")
def top(path: str, column: str, n: int = 10, smallest: bool = False) -> dict[str, Any]:
    frame = load_table(path)
    _columns(frame, column)
    result = (
        frame.nsmallest(int(n), column) if smallest else frame.nlargest(int(n), column)
    )
    return clip_text(_show(result, int(n)))


@cap("data.groupby")
def groupby(
    path: str, by: list[str], column: str = "", agg: str = "sum", output: str = ""
) -> dict[str, Any]:
    if agg not in {"sum", "mean", "count", "max", "min", "median", "nunique"}:
        raise CapError("agg 只能是 sum/mean/count/max/min/median/nunique")
    frame = load_table(path)
    keys = _columns(frame, by)
    grouped = frame.groupby(keys)
    result = (
        grouped[_columns(frame, column)[0]].agg(agg)
        if column
        else grouped.size().rename("count")
    ).reset_index()
    payload = _save(result, output, f"{_stem(path)}_分组.csv")
    payload.update(clip_text(_show(result, 30)))
    return payload


@cap("data.pivot")
def pivot(
    path: str, index: str, columns: str, values: str, agg: str = "sum", output: str = ""
) -> dict[str, Any]:
    import pandas as pd

    frame = load_table(path)
    _columns(frame, [index, columns, values])
    result = pd.pivot_table(
        frame, index=index, columns=columns, values=values, aggfunc=agg
    ).reset_index()
    payload = _save(result, output, f"{_stem(path)}_透视.csv")
    payload.update(clip_text(_show(result, 30)))
    return payload


@cap("data.join")
def join(
    left: str, right: str, on: list[str], how: str = "inner", output: str = ""
) -> dict[str, Any]:
    if how not in {"inner", "left", "right", "outer"}:
        raise CapError("how 只能是 inner/left/right/outer")
    a = load_table(left)
    b = load_table(right)
    keys = _columns(a, on)
    _columns(b, on)
    result = a.merge(b, on=keys, how=how)
    payload = _save(result, output, "合并结果.csv")
    payload.update(clip_text(_show(result)))
    return payload


@cap("data.concat")
def concat(paths: list[str], output: str = "") -> dict[str, Any]:
    import pandas as pd

    frames = [load_table(p) for p in paths]
    if len(frames) < 2:
        raise CapError("至少两个表")
    result = pd.concat(frames, ignore_index=True)
    return _save(result, output, "拼接结果.csv")


@cap("data.dedupe")
def dedupe(
    path: str, columns: list[str] | None = None, output: str = ""
) -> dict[str, Any]:
    frame = load_table(path)
    subset = _columns(frame, columns) if columns else None
    result = frame.drop_duplicates(subset=subset)
    payload = _save(result, output, f"{_stem(path)}_去重.csv")
    payload["removed"] = len(frame) - len(result)
    return payload


@cap("data.fill_missing")
def fill_missing(
    path: str, method: str = "drop", value: str = "", output: str = ""
) -> dict[str, Any]:
    frame = load_table(path)
    before = int(frame.isna().sum().sum())
    if method == "drop":
        result = frame.dropna()
    elif method == "value":
        result = frame.fillna(value)
    elif method == "mean":
        result = frame.fillna(frame.mean(numeric_only=True))
    elif method == "ffill":
        result = frame.ffill()
    else:
        raise CapError("method 只能是 drop/value/mean/ffill")
    payload = _save(result, output, f"{_stem(path)}_补缺.csv")
    payload["missing_before"] = before
    return payload


@cap("data.value_counts")
def value_counts(path: str, column: str, top: int = 20) -> dict[str, Any]:
    frame = load_table(path)
    _columns(frame, column)
    counts = frame[column].value_counts().head(int(top))
    return {
        "counts": {str(k): int(v) for k, v in counts.items()},
        "distinct": int(frame[column].nunique()),
    }


@cap("data.correlation")
def correlation(path: str) -> dict[str, Any]:
    frame = load_table(path).select_dtypes("number")
    if frame.shape[1] < 2:
        raise CapError("至少要两列数字")
    return clip_text(frame.corr().round(3).to_string())


@cap("data.chart")
def chart(
    path: str,
    kind: str = "line",
    x: str = "",
    y: list[str] | None = None,
    title: str = "",
    output: str = "",
) -> dict[str, Any]:
    import matplotlib as mpl

    mpl.use("Agg")
    from matplotlib import pyplot as plt

    if kind not in {"line", "bar", "barh", "scatter", "pie", "hist", "area", "box"}:
        raise CapError("kind 只能是 line/bar/barh/scatter/pie/hist/area/box")
    frame = load_table(path)
    columns = (
        _columns(frame, y)
        if y
        else [str(c) for c in frame.select_dtypes("number").columns[:4]]
    )
    if not columns:
        raise CapError("没有数字列可画")
    if x:
        _columns(frame, x)
    fig, ax = plt.subplots(figsize=(9, 5.5))
    if kind == "pie":
        data = frame.set_index(x)[columns[0]] if x else frame[columns[0]]
        data.head(12).plot.pie(ax=ax, autopct="%1.1f%%")
        ax.set_ylabel("")
    elif kind == "scatter":
        if not x:
            raise CapError("scatter 要给 x")
        frame.plot.scatter(x=x, y=columns[0], ax=ax)
    elif kind == "hist":
        frame[columns].plot.hist(ax=ax, bins=20, alpha=0.7)
    else:
        frame.plot(kind=kind, x=x or None, y=columns, ax=ax)
    ax.set_title(title or f"{Path(str(path)).stem}")
    target = dest(output, f"{title or _stem(path)}_{kind}.png")
    fig.savefig(target)
    plt.close(fig)
    return {"file": ref(target)}


@cap("data.sql")
def sql(paths: list[str], query: str) -> dict[str, Any]:
    """把几个表格各自装进内存 SQLite（表名 t1、t2…，也可以用文件名），执行一条 SELECT。"""

    import sqlite3

    text = str(query).strip()
    if not text.lower().startswith(("select", "with")):
        raise CapError("只支持 SELECT 查询")
    connection = sqlite3.connect(":memory:")
    names = []
    for index, path in enumerate(
        paths if isinstance(paths, list) else [paths], start=1
    ):
        frame = load_table(path)
        frame.to_sql(f"t{index}", connection, index=False)
        alias = "".join(ch if ch.isalnum() else "_" for ch in _stem(path))
        if alias and not alias[0].isdigit():
            connection.execute(
                f'CREATE VIEW IF NOT EXISTS "{alias}" AS SELECT * FROM t{index}'
            )
        names.append(
            {
                "table": f"t{index}",
                "alias": alias,
                "columns": list(map(str, frame.columns))[:30],
            }
        )
    import pandas as pd

    try:
        result = pd.read_sql_query(text, connection)
    except Exception as exc:
        raise CapError(f"SQL 出错：{exc}") from exc
    finally:
        connection.close()
    target = dest("", "查询结果.csv")
    result.to_csv(target, index=False, encoding="utf-8-sig")
    return {
        "tables": names,
        "rows": len(result),
        "file": ref(target),
        **clip_text(_show(result, 30)),
    }


@cap("data.sample")
def sample(path: str, n: int = 10, seed: int = 0) -> dict[str, Any]:
    frame = load_table(path)
    result = frame.sample(min(int(n), len(frame)), random_state=int(seed) or None)
    return clip_text(_show(result, int(n)))
