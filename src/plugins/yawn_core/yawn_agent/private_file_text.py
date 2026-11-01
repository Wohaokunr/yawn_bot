# ruff: noqa: E501, PERF203
"""私聊发来的文档在 Bot 端直接抽文字：虚拟电脑不可用、入箱失败时也能“读到”文件。

只做零依赖的抽取：纯文本类（txt/md/csv/json/代码……，utf-8 或 gb18030）和 Office
Open XML（docx/pptx/xlsx，本质是 zip 里的 XML）。PDF、老 doc 等二进制格式不在这里，
交给虚拟电脑。抽出来的文字是不可信资料，只当内容看，不当指令。
"""

from __future__ import annotations

import html
import io
import re
import zipfile
from pathlib import PurePosixPath
from typing import Any

from .log import dbg, dbg_exc

MAX_SOURCE_BYTES = 5 * 1024 * 1024
MAX_XML_BYTES = 20 * 1024 * 1024
EXCERPT_CHARS = 4000
HISTORY_EXCERPT_CHARS = 1500

TEXT_SUFFIXES = frozenset(
    {
        ".txt", ".md", ".markdown", ".csv", ".tsv", ".json", ".jsonl", ".log", ".ini", ".cfg",
        ".conf", ".toml", ".yaml", ".yml", ".xml", ".html", ".htm", ".py", ".js", ".ts", ".java",
        ".c", ".h", ".cpp", ".cs", ".go", ".rs", ".php", ".rb", ".sh", ".bat", ".sql", ".srt",
        ".vtt", ".lrc", ".tex", ".rtf",
    }
)
OOXML_SUFFIXES = frozenset({".docx", ".pptx", ".xlsx"})

_TAG = re.compile(r"<[^>]+>")
_PARAGRAPH = re.compile(r"</(?:w:p|a:p)>|<w:br/>|<w:tab/>")
_SLIDE = re.compile(r"ppt/slides/slide(\d+)\.xml$")


def readable(name: str | None) -> bool:
    suffix = PurePosixPath(str(name or "")).suffix.lower()
    return suffix in TEXT_SUFFIXES or suffix in OOXML_SUFFIXES


def _decode(data: bytes) -> str | None:
    for encoding in ("utf-8-sig", "gb18030"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    return None


def _xml_text(raw: bytes) -> str:
    text = _PARAGRAPH.sub("\n", raw.decode("utf-8", errors="ignore"))
    text = html.unescape(_TAG.sub("", text))
    return "\n".join(line.strip() for line in text.splitlines() if line.strip())


def _member(archive: zipfile.ZipFile, name: str) -> bytes | None:
    try:
        info = archive.getinfo(name)
    except KeyError:
        return None
    if info.file_size > MAX_XML_BYTES:
        return None
    return archive.read(info)


def _ooxml(data: bytes, suffix: str) -> str | None:
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        if suffix == ".docx":
            raw = _member(archive, "word/document.xml")
            return _xml_text(raw) if raw else None
        if suffix == ".pptx":
            slides = sorted(
                (int(match.group(1)), name)
                for name in archive.namelist()
                if (match := _SLIDE.search(name))
            )
            parts = []
            for number, name in slides:
                raw = _member(archive, name)
                if raw:
                    parts.append(f"[第{number}页]\n{_xml_text(raw)}")
            return "\n".join(parts) or None
        raw = _member(archive, "xl/sharedStrings.xml")
        return _xml_text(raw.replace(b"</si>", b"</si>\n")) if raw else None


def extract_text(data: bytes, name: str | None) -> str | None:
    """抽出文档文字；不支持的格式、太大或解析失败返回 None。"""

    if not data or len(data) > MAX_SOURCE_BYTES:
        return None
    suffix = PurePosixPath(str(name or "")).suffix.lower()
    try:
        if suffix in OOXML_SUFFIXES:
            text = _ooxml(data, suffix)
        elif suffix in TEXT_SUFFIXES:
            text = _decode(data)
        else:
            return None
    except (zipfile.BadZipFile, OSError, ValueError):
        dbg(f"私聊文件抽文字失败 name 后缀={suffix}")
        return None
    text = (text or "").strip()
    return text or None


def excerpt(text: str, limit: int = EXCERPT_CHARS) -> str:
    if len(text) <= limit:
        return text
    return f"{text[:limit]}……（共 {len(text)} 字，只给了前 {limit} 字）"


async def read_files(bot: Any, media: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """虚拟电脑用不了时：直接取私聊文件的字节抽文字。返回给模型看的每项结果。"""

    from .sandbox_inbox import Attachment, InboxError, fetch_deadline, fetch_file

    rows: list[dict[str, Any]] = []
    for index, item in enumerate(media or [], start=1):
        if str(item.get("type") or "") != "file":
            continue
        ref = {key: value for key, value in item.items() if key != "type"}
        attachment = Attachment(key=f"m{index}", media_type="file", source="current", ref=ref, index=index)
        name = attachment.name
        row: dict[str, Any] = {"id": attachment.key, "type": "file", "name": name or "文件"}
        if not readable(name):
            rows.append({**row, "failed": "这种格式没法直接读文字，需要虚拟电脑"})
            continue
        declared = attachment.declared_size
        if declared and declared > MAX_SOURCE_BYTES:
            rows.append({**row, "failed": "文件太大，没法直接读文字"})
            continue
        try:
            import asyncio

            fetched = await asyncio.wait_for(
                fetch_file(bot, 0, attachment, max_bytes=MAX_SOURCE_BYTES),
                timeout=fetch_deadline(declared),
            )
        except InboxError as exc:
            rows.append({**row, "failed": str(exc)})
            continue
        except Exception:  # noqa: BLE001
            dbg_exc("私聊文件直接读取失败")
            rows.append({**row, "failed": "没能取到文件"})
            continue
        try:
            text = extract_text(fetched.data, name)
        finally:
            fetched.close()
        if text:
            rows.append({**row, "text_excerpt": excerpt(text)})
        else:
            rows.append({**row, "failed": "文件里没读出文字"})
    return rows


__all__ = [
    "EXCERPT_CHARS",
    "HISTORY_EXCERPT_CHARS",
    "excerpt",
    "extract_text",
    "read_files",
    "readable",
]
