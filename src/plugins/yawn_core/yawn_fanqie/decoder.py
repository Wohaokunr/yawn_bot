"""番茄页面状态、HTML 正文和公开字体映射解码工具。

字体 glyph 到字符的常量表来自公开的 fanqienovel 字体解码研究，仅保留
算法所需的数据映射；本插件不包含第三方下载器或其网络/任务代码。详见
``docs/fanqie-notice.md``。
"""

# 这些函数保留页面解析失败的中文诊断；不屏蔽未定义名称和导入错误。
# ruff: noqa: TRY003, TRY004

from __future__ import annotations

import json
import re
from hashlib import sha256
from html import unescape
from html.parser import HTMLParser
from typing import Any

from .font_signatures import FONT_SIGNATURE_MAP

_READER_PUA_START = 58344
_READER_CHARSET = (
    "D\u5728\u4e3b\u7279\u5bb6\u519b\u7136\u8868\u573a4\u8981\u53eav\u548c?6\u522b\u8fd8g\u73b0\u513f\u5c81??"
    "\u6b64\u8c61\u67083\u51fa\u6218\u5de5\u76f8o\u7537\u76f4\u5931\u4e16F\u90fd\u5e73\u6587\u4ec0VO\u5c06\u771fT\u90a3"
    "\u5f53?\u4f1a\u7acb\u4e9bu\u662f\u5341\u5f20\u5b66\u6c14\u5927\u7231\u4e24\u547d\u5168\u540e\u4e1c\u6027\u901a\u88ab1\u5b83\u4e50"
    "\u63a5\u800c\u611f\u8f66\u5c71\u516c\u4e86\u5e38\u4ee5\u4f55\u53ef\u8bdd\u5148pi\u53eb\u8f7bM\u58ebw\u7740\u53d8\u5c14\u5feb"
    "l\u4e2a\u8bf4\u5c11\u8272\u91cc\u5b89\u82b1\u8fdc7\u96be\u5e08\u653et\u62a5\u8ba4\u9762\u9053S?\u514b\u5730\u5ea6I"
    "\u597d\u673aU\u6c11\u5199\u628a\u4e07\u540c\u6c34\u65b0\u6ca1\u4e66\u7535\u5403\u50cf\u65af5\u4e3ay\u767d\u51e0\u65e5\u6559\u770b"
    "\u4f46\u7b2c\u52a0\u5019\u4f5c\u4e0a\u62c9\u4f4f\u6709\u6cd5r\u4e8b\u5e94\u4f4d\u5229\u4f60\u58f0\u8eab\u56fd\u95ee\u9a6c\u5973\u4ed6Y"
    "\u6bd4\u7236xAHNsX\u8fb9\u7f8e\u5bf9\u6240\u91d1\u6d3b\u56de\u610f\u5230z\u4ecej\u77e5\u53c8\u5185\u56e0"
    "\u70b9Q\u4e09\u5b9a8Rb\u6b63\u6216\u592b\u5411\u5fb7\u542c\u66f4?\u5f97\u544a\u5e76\u672cq\u8fc7\u8bb0L\u8ba9"
    "\u6253f\u4eba\u5c31\u8005\u53bb\u539f\u6ee1\u4f53\u505a\u7ecfK\u8d70\u5982\u5b69cG\u7ed9\u4f7f\u7269?\u6700\u7b11\u90e8"
    "?\u5458\u7b49\u53d7k\u884c\u4e00\u6761\u679c\u52a8\u5149\u95e8\u5934\u89c1\u5f80\u81ea\u89e3\u6210\u5904\u5929\u80fd\u4e8e\u540d\u5176"
    "\u53d1\u603b\u6bcd\u7684\u6b7b\u624b\u5165\u8def\u8fdb\u5fc3\u6765h\u65f6\u529b\u591a\u5f00\u5df2\u8bb8d\u81f3\u7531\u5f88\u754cn"
    "\u5c0f\u4e0eZ\u60f3\u4ee3\u4e48\u5206\u751f\u53e3\u518d\u5988\u671b\u6b21\u897f\u98ce\u79cd\u5e26J?\u5b9e\u60c5\u624d\u8fd9?"
    "E\u6211\u795e\u683c\u957f\u89c9\u95f4\u5e74\u773c\u65e0\u4e0d\u4eb2\u5173\u7ed30\u53cb\u4fe1\u4e0b\u5374\u91cd\u5df1\u80012\u97f3"
    "\u5b57m\u5462\u660e\u4e4b\u524d\u9ad8PB\u76ee\u592ae9\u8d77\u7a1c\u5979\u4e5fW\u7528\u65b9\u5b50\u82f1\u6bcf\u7406"
    "\u4fbf\u56db\u6570\u671f\u4e2dC\u5916\u6837a\u6d77\u4eec\u4efb"
)

_INITIAL_STATE_RE = re.compile(r"window\.__INITIAL_STATE__\s*=\s*", re.DOTALL)
_TAG_RE = re.compile(r"<[^>]+>")
_PUA_RE = re.compile(r"[\ue000-\uf8ff\U000f0000-\U000ffffd]")


def extract_initial_state(page: str) -> dict[str, Any]:
    """提取页面内的 ``window.__INITIAL_STATE__`` JSON。"""

    match = _INITIAL_STATE_RE.search(page)
    if match is None:
        raise ValueError("页面缺少 __INITIAL_STATE__")
    decoder = json.JSONDecoder()
    try:
        state, _ = decoder.raw_decode(page[match.end() :].lstrip())
    except json.JSONDecodeError as exc:
        raise ValueError("__INITIAL_STATE__ 不是有效 JSON") from exc
    if not isinstance(state, dict):
        raise ValueError("__INITIAL_STATE__ 顶层不是对象")
    return state


def html_to_text(content: str) -> str:
    """把章节 HTML 清理成保留段落换行的纯文本。"""

    text = re.sub(r"<\s*br\s*/?\s*>", "\n", content, flags=re.IGNORECASE)
    text = re.sub(
        r"</?\s*(?:p|div|section|article|li|h[1-6])\b[^>]*>",
        "\n",
        text,
        flags=re.IGNORECASE,
    )
    text = _TAG_RE.sub("", text)
    text = unescape(text).replace("\xa0", " ")
    lines = [re.sub(r"[ \t]+", " ", line).strip() for line in text.splitlines()]
    output: list[str] = []
    for line in lines:
        if line or (output and output[-1]):
            output.append(line)
    return "\n".join(output).strip()


class _ReaderContentParser(HTMLParser):
    """提取公开阅读页 ``muye-reader-content`` 容器中的段落。"""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._depth = 0
        self._container_depth: int | None = None
        self._paragraph_depth = 0
        self._current: list[str] = []
        self.paragraphs: list[str] = []

    @property
    def _in_container(self) -> bool:
        return self._container_depth is not None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.lower()
        if self._container_depth is None and tag == "div":
            classes = next(
                (value or "" for key, value in attrs if key.lower() == "class"),
                "",
            )
            if "muye-reader-content" in classes.split():
                self._container_depth = self._depth
        if tag == "br":
            if self._in_container and self._paragraph_depth:
                self._current.append("\n")
            return
        self._depth += 1
        if self._in_container and tag == "p":
            self._paragraph_depth += 1

    def handle_startendtag(
        self, tag: str, attrs: list[tuple[str, str | None]]
    ) -> None:
        self.handle_starttag(tag, attrs)
        if tag.lower() != "br":
            self.handle_endtag(tag)

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if self._in_container and tag == "p" and self._paragraph_depth:
            self._paragraph_depth -= 1
            if self._paragraph_depth == 0 and self._current:
                self.paragraphs.append("".join(self._current))
                self._current = []
        if self._depth:
            self._depth -= 1
        if self._container_depth is not None and self._depth == self._container_depth:
            self._container_depth = None
            self._paragraph_depth = 0
            self._current = []

    def handle_data(self, data: str) -> None:
        if self._in_container and self._paragraph_depth:
            self._current.append(data)


def extract_reader_content(page: str) -> str:
    """从阅读页 DOM 备用提取章节段落，不推断或修改访问权限。"""

    parser = _ReaderContentParser()
    parser.feed(page)
    parser.close()
    paragraphs = [
        paragraph.strip()
        for paragraph in parser.paragraphs
        if paragraph.strip()
    ]
    return html_to_text("\n\n".join(paragraphs))


def decrypt_pua(text: str, mapping: dict[str, str] | None = None) -> str:
    """用字体生成的 PUA 映射替换正文中的字符。"""

    active = mapping or {}
    return "".join(active.get(char, char) for char in text)


def decrypt_reader_pua(text: str) -> str:
    """Decode the fixed Reader PUA permutation without affecting Search fonts."""

    output: list[str] = []
    for char in text:
        offset = ord(char) - _READER_PUA_START
        if 0 <= offset < len(_READER_CHARSET):
            decoded = _READER_CHARSET[offset]
            output.append(char if decoded == "?" else decoded)
        else:
            output.append(char)
    return "".join(output)


def contains_pua(text: str) -> bool:
    """判断文本是否仍含有待解码的私用区字符。"""

    return _PUA_RE.search(text) is not None


def font_glyph_signature_to_text(font: Any, glyph_name: str) -> str | None:
    """按字形轮廓解码被番茄重新编号的公开字体。"""

    try:
        from fontTools.pens.recordingPen import RecordingPen

        pen = RecordingPen()
        font.getGlyphSet()[glyph_name].draw(pen)
        signature = sha256(
            repr(tuple(pen.value)).encode("utf-8")
        ).hexdigest()
    except (AttributeError, KeyError, TypeError, ValueError):
        return None
    return FONT_SIGNATURE_MAP.get(signature)
