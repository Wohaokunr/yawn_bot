"""yawnkit：沙箱里的小助手库。

    from yawnkit import music, charts, files

- ``music``：用音符串写旋律/和弦/鼓点，合成 WAV 并导出 MIDI；
- ``charts``：统一配色与中文字体，一行保存图表；
- ``files``：把表格存成 CSV / Excel / Markdown；
- ``caps``：一百六十多个现成能力（13 个能力包），Bot 的 ``computer_do`` 工具调用它们，
  代码里也可以直接 ``from yawnkit.caps import call; call("pdf.merge", {...})``。

子模块按需导入，``import yawnkit`` 本身不加载 numpy/matplotlib。
"""

from __future__ import annotations

__version__ = "1.1.0"
__all__ = ["caps", "charts", "files", "music"]


def __getattr__(name: str):
    if name in __all__:
        import importlib

        return importlib.import_module(f"{__name__}.{name}")
    raise AttributeError(name)
