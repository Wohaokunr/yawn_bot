"""图表主题：中性底色 + 樱花粉强调色，中文字体，一行保存。

    from yawnkit import charts
    charts.use()                       # 套主题（可选）
    fig, ax = charts.figure("每日消息数")
    ax.bar(days, counts, color=charts.ACCENT)
    charts.save(fig, "messages.png")   # 返回文件名，可直接发到群里
"""

from __future__ import annotations

from typing import Any

ACCENT = "#e4558a"
PALETTE = ["#e4558a", "#4c78a8", "#f2a541", "#54a24b", "#9d7fd1", "#7f7f7f", "#2fb5c6", "#d4a373"]
INK = "#2d2a32"
GRID = "#e6e1e8"


def use() -> None:
    import matplotlib as mpl

    mpl.rcParams.update(
        {
            "axes.prop_cycle": mpl.cycler(color=PALETTE),
            "axes.edgecolor": "#c9c2cc",
            "axes.labelcolor": INK,
            "axes.titleweight": "bold",
            "axes.titlesize": 14,
            "axes.spines.top": False,
            "axes.spines.right": False,
            "axes.grid": True,
            "grid.color": GRID,
            "grid.linewidth": 0.8,
            "xtick.color": INK,
            "ytick.color": INK,
            "text.color": INK,
            "figure.facecolor": "white",
            "axes.facecolor": "white",
            "legend.frameon": False,
            "font.sans-serif": ["WenQuanYi Micro Hei", "Noto Sans CJK SC", "DejaVu Sans"],
            "axes.unicode_minus": False,
        }
    )


def figure(title: str | None = None, *, size: tuple[float, float] = (8, 5), **kwargs: Any) -> tuple[Any, Any]:
    import matplotlib.pyplot as plt

    use()
    fig, ax = plt.subplots(figsize=size, **kwargs)
    if title:
        ax.set_title(title)
    return fig, ax


def save(fig: Any = None, name: str = "chart.png", *, dpi: int = 144) -> str:
    import matplotlib.pyplot as plt

    target = fig if fig is not None else plt.gcf()
    target.savefig(name, dpi=dpi, bbox_inches="tight")
    plt.close(target)
    print(f"[图表已保存] {name}")
    return name


__all__ = ["ACCENT", "PALETTE", "figure", "save", "use"]
