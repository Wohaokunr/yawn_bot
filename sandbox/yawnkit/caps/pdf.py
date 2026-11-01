"""pdf 包：信息、提取文字、合并拆分、页面挑选旋转、加密解密、渲染页面图片。"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from . import CapError, as_list, cap, clip_text, dest, human, ref, src


def _reader(path: str, password: str = "") -> Any:
    from pypdf import PdfReader
    from pypdf.errors import PdfReadError

    target = src(path, kind="PDF")
    try:
        reader = PdfReader(str(target))
        if reader.is_encrypted and (not password or not reader.decrypt(password)):
            raise CapError("PDF 有密码，给 password")
    except PdfReadError as exc:
        raise CapError(f"读不了这个 PDF：{exc}") from exc
    return reader


def _pages(spec: str, total: int) -> list[int]:
    """'1-3,5' → [0,1,2,4]（从 1 开始写，越界忽略）。"""

    result: list[int] = []
    for raw in str(spec or "").replace("，", ",").split(","):
        part = raw.strip()
        if not part:
            continue
        if "-" in part:
            start, _, end = part.partition("-")
            first = int(start or 1)
            last = int(end or total)
            result.extend(range(first - 1, min(last, total)))
        else:
            result.append(int(part) - 1)
    pages = [p for p in result if 0 <= p < total]
    if not pages:
        raise CapError(f"pages 没选中任何页（共 {total} 页），写法如 1-3,5")
    return pages


@cap("pdf.info")
def info(path: str, password: str = "") -> dict[str, Any]:
    reader = _reader(path, password)
    meta = reader.metadata or {}
    first = reader.pages[0].mediabox if reader.pages else None
    return {
        "pages": len(reader.pages),
        "title": meta.get("/Title"),
        "author": meta.get("/Author"),
        "producer": meta.get("/Producer"),
        "encrypted": reader.is_encrypted,
        "page_size_pt": [round(float(first.width)), round(float(first.height))]
        if first
        else None,
        "size": human(src(path).stat().st_size),
    }


@cap("pdf.text")
def text(
    path: str,
    pages: str = "",
    password: str = "",
    max_chars: int = 6000,
    save: bool = False,
) -> dict[str, Any]:
    reader = _reader(path, password)
    selected = (
        _pages(pages, len(reader.pages)) if pages else list(range(len(reader.pages)))
    )
    chunks = []
    for index in selected:
        content = reader.pages[index].extract_text() or ""
        chunks.append(f"--- 第 {index + 1} 页 ---\n{content.strip()}")
    full = "\n".join(chunks)
    payload = clip_text(full, max(500, min(int(max_chars), 20000)))
    if save:
        target = dest("", f"{Path(str(path)).stem}.txt")
        target.write_text(full, encoding="utf-8")
        payload["file"] = ref(target)
    if not full.replace("-", "").strip() or len(full) < 20 * len(selected):
        payload["note"] = (
            "几乎没有文字，可能是扫描件（图片）；可以用 pdf.images 把里面的图片取出来再看"
        )
    return payload


@cap("pdf.merge")
def merge(paths: list[str], output: str = "") -> dict[str, Any]:
    from pypdf import PdfWriter

    writer = PdfWriter()
    total = 0
    for item in as_list(paths):
        reader = _reader(item)
        for page in reader.pages:
            writer.add_page(page)
            total += 1
    if total == 0:
        raise CapError("没有页面可合并")
    target = dest(output, "合并.pdf")
    with target.open("wb") as handle:
        writer.write(handle)
    return {"file": ref(target), "pages": total, "size": human(target.stat().st_size)}


@cap("pdf.split")
def split(path: str, every: int = 1, password: str = "") -> dict[str, Any]:
    from pypdf import PdfWriter

    reader = _reader(path, password)
    step = max(1, int(every))
    stem = Path(str(path)).stem
    files = []
    for start in range(0, len(reader.pages), step):
        writer = PdfWriter()
        for index in range(start, min(start + step, len(reader.pages))):
            writer.add_page(reader.pages[index])
        target = dest("", f"{stem}_{start + 1:03d}.pdf")
        with target.open("wb") as handle:
            writer.write(handle)
        files.append(ref(target))
    return {"files": files[:30], "count": len(files)}


@cap("pdf.extract_pages")
def extract_pages(
    path: str, pages: str, output: str = "", password: str = ""
) -> dict[str, Any]:
    from pypdf import PdfWriter

    reader = _reader(path, password)
    writer = PdfWriter()
    selected = _pages(pages, len(reader.pages))
    for index in selected:
        writer.add_page(reader.pages[index])
    target = dest(output, f"{Path(str(path)).stem}_选页.pdf")
    with target.open("wb") as handle:
        writer.write(handle)
    return {"file": ref(target), "pages": len(selected)}


@cap("pdf.rotate")
def rotate(
    path: str, degrees: int = 90, pages: str = "", output: str = ""
) -> dict[str, Any]:
    from pypdf import PdfWriter

    if int(degrees) % 90:
        raise CapError("degrees 要是 90 的倍数")
    reader = _reader(path)
    selected = (
        set(_pages(pages, len(reader.pages)))
        if pages
        else set(range(len(reader.pages)))
    )
    writer = PdfWriter()
    for index, page in enumerate(reader.pages):
        if index in selected:
            page.rotate(int(degrees))
        writer.add_page(page)
    target = dest(output, f"{Path(str(path)).stem}_旋转.pdf")
    with target.open("wb") as handle:
        writer.write(handle)
    return {"file": ref(target), "rotated": len(selected)}


@cap("pdf.encrypt")
def encrypt(path: str, password: str, output: str = "") -> dict[str, Any]:
    from pypdf import PdfWriter

    if len(str(password)) < 4:
        raise CapError("密码至少 4 位")
    reader = _reader(path)
    writer = PdfWriter(clone_from=reader)
    writer.encrypt(str(password), algorithm="AES-256")
    target = dest(output, f"{Path(str(path)).stem}_加密.pdf")
    with target.open("wb") as handle:
        writer.write(handle)
    return {"file": ref(target)}


@cap("pdf.decrypt")
def decrypt(path: str, password: str, output: str = "") -> dict[str, Any]:
    from pypdf import PdfWriter

    reader = _reader(path, password)
    writer = PdfWriter(clone_from=reader)
    target = dest(output, f"{Path(str(path)).stem}_无密码.pdf")
    with target.open("wb") as handle:
        writer.write(handle)
    return {"file": ref(target)}


@cap("pdf.images")
def images(path: str, limit: int = 30) -> dict[str, Any]:
    reader = _reader(path)
    folder = Path("out") / f"{Path(str(path)).stem}_图片"
    folder.mkdir(parents=True, exist_ok=True)
    saved = []
    for page_index, page in enumerate(reader.pages, start=1):
        for image in getattr(page, "images", []):
            target = folder / f"p{page_index}_{Path(image.name).name}"
            target.write_bytes(image.data)
            saved.append(ref(target))
            if len(saved) >= int(limit):
                return {"images": saved, "count": len(saved), "truncated": True}
    return {"images": saved, "count": len(saved)}


@cap("pdf.add_text_page")
def add_text_page(text: str, output: str = "", title: str = "") -> dict[str, Any]:
    """把一段文字排成 PDF（用中文字体，适合把结果做成可打印的文档）。"""

    import matplotlib as mpl

    mpl.use("Agg")
    import textwrap

    from matplotlib import pyplot as plt
    from matplotlib.backends.backend_pdf import PdfPages

    lines: list[str] = []
    for paragraph in str(text).splitlines() or [""]:
        lines.extend(textwrap.wrap(paragraph, 38) or [""])
    target = dest(output, f"{title or '文档'}.pdf")
    per_page = 42
    with PdfPages(target) as pdf:
        for start in range(0, max(len(lines), 1), per_page):
            fig = plt.figure(figsize=(8.27, 11.69))
            fig.text(0.08, 0.95, title if start == 0 else "", fontsize=16, va="top")
            fig.text(
                0.08,
                0.91,
                "\n".join(lines[start : start + per_page]),
                fontsize=11,
                va="top",
                linespacing=1.5,
            )
            pdf.savefig(fig)
            plt.close(fig)
    return {"file": ref(target), "pages": (len(lines) + per_page - 1) // per_page or 1}
