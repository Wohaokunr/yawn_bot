"""把表格/文本存成适合发到群里的文件。

    from yawnkit import files
    files.save_table(df, "结果.xlsx")   # 按后缀选择 csv / xlsx / md / json
    files.save_text("报告.md", text)
"""

from __future__ import annotations

from pathlib import Path
from typing import Any


def _ensure_parent(name: str) -> Path:
    path = Path(name)
    if path.is_absolute() or ".." in path.parts:
        raise ValueError("只能保存到工作区里的相对路径")
    if path.parent != Path():
        path.parent.mkdir(parents=True, exist_ok=True)
    return path


def save_table(table: Any, name: str = "table.csv", *, index: bool = False) -> str:
    """保存 DataFrame（或能转成 DataFrame 的对象）。CSV 用 UTF-8 BOM，Excel 打开不乱码。"""

    import pandas as pd

    frame = table if isinstance(table, pd.DataFrame) else pd.DataFrame(table)
    path = _ensure_parent(name)
    suffix = path.suffix.lower()
    if suffix in {".xlsx", ".xls"}:
        frame.to_excel(path, index=index)
    elif suffix == ".md":
        path.write_text(frame.to_markdown(index=index), encoding="utf-8")
    elif suffix == ".json":
        frame.to_json(path, orient="records", force_ascii=False, indent=2)
    else:
        frame.to_csv(path, index=index, encoding="utf-8-sig")
    print(f"[文件已保存] {path}（{len(frame)} 行）")
    return str(path)


def save_text(name: str, text: str) -> str:
    path = _ensure_parent(name)
    path.write_text(text, encoding="utf-8")
    print(f"[文件已保存] {path}")
    return str(path)


def workspace_files() -> list[str]:
    """列出工作区里的文件（不含隐藏文件），方便确认上次生成了什么。"""

    return sorted(
        str(item)
        for item in Path().rglob("*")
        if item.is_file() and not any(part.startswith(".") for part in item.parts)
    )


__all__ = ["save_table", "save_text", "workspace_files"]
