# ruff: noqa: PLR2004, E501
"""图片文件头解析：宽、高、帧数（是不是动图）。纯函数，不依赖任何图像库。

表情包库只收 PNG / JPG / GIF / WebP，四种格式的尺寸都在文件头里，读几十个字节
就够；动图帧数要扫一遍块结构（GIF 的图像描述符、WebP 的 ANMF、APNG 的 acTL），
有上限、遇到坏数据就停，绝不抛异常——解析不出来就是 0，调用方当“未知”处理。
"""

from __future__ import annotations

import struct
from dataclasses import dataclass

MAX_FRAMES_COUNTED = 2000


@dataclass(frozen=True, slots=True)
class ImageInfo:
    width: int = 0
    height: int = 0
    frames: int = 0
    kind: str = ""

    @property
    def animated(self) -> bool:
        return self.frames > 1

    @property
    def known(self) -> bool:
        return self.width > 0 and self.height > 0

    @property
    def aspect(self) -> float:
        return self.width / self.height if self.known else 0.0


def _png(data: bytes) -> ImageInfo:
    if len(data) < 24 or data[12:16] != b"IHDR":
        return ImageInfo(kind="png")
    width, height = struct.unpack(">II", data[16:24])
    frames = 1
    # APNG：acTL 块在 IDAT 之前，带帧数。
    offset = 8
    for _ in range(64):
        if offset + 8 > len(data):
            break
        (length,) = struct.unpack(">I", data[offset : offset + 4])
        chunk = data[offset + 4 : offset + 8]
        if chunk == b"acTL" and offset + 12 <= len(data):
            (count,) = struct.unpack(">I", data[offset + 8 : offset + 12])
            frames = max(1, min(count, MAX_FRAMES_COUNTED))
            break
        if chunk in {b"IDAT", b"IEND"}:
            break
        offset += 12 + length
    return ImageInfo(width, height, frames, "png")


_SOF_MARKERS = frozenset({0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF})


def _jpeg(data: bytes) -> ImageInfo:
    offset = 2
    size = len(data)
    for _ in range(512):
        while offset < size and data[offset] != 0xFF:
            offset += 1
        while offset < size and data[offset] == 0xFF:
            offset += 1
        if offset >= size:
            break
        marker = data[offset]
        offset += 1
        if marker in {0xD8, 0x01} or 0xD0 <= marker <= 0xD7:
            continue
        if offset + 2 > size:
            break
        (length,) = struct.unpack(">H", data[offset : offset + 2])
        if marker in _SOF_MARKERS:
            if offset + 7 > size:
                break
            height, width = struct.unpack(">HH", data[offset + 3 : offset + 7])
            return ImageInfo(width, height, 1, "jpeg")
        if length < 2:
            break
        offset += length
    return ImageInfo(kind="jpeg")


def _gif_skip_sub_blocks(data: bytes, offset: int) -> int:
    size = len(data)
    while offset < size:
        block = data[offset]
        offset += 1
        if block == 0:
            return offset
        offset += block
    return size


def _gif(data: bytes) -> ImageInfo:
    if len(data) < 13:
        return ImageInfo(kind="gif")
    width, height = struct.unpack("<HH", data[6:10])
    flags = data[10]
    offset = 13
    if flags & 0x80:
        offset += 3 * (2 << (flags & 0x07))
    frames = 0
    size = len(data)
    while offset < size and frames < MAX_FRAMES_COUNTED:
        marker = data[offset]
        offset += 1
        if marker == 0x2C:  # 图像描述符 = 一帧
            if offset + 9 > size:
                break
            frames += 1
            local = data[offset + 8]
            offset += 9
            if local & 0x80:
                offset += 3 * (2 << (local & 0x07))
            offset += 1  # LZW 最小码长
            offset = _gif_skip_sub_blocks(data, offset)
        elif marker == 0x21:  # 扩展块
            offset += 1
            offset = _gif_skip_sub_blocks(data, offset)
        else:  # 0x3B 结束，或坏数据
            break
    return ImageInfo(width, height, max(frames, 1), "gif")


def _webp(data: bytes) -> ImageInfo:  # noqa: C901
    width = height = 0
    frames = 1
    animated = False
    offset = 12
    size = len(data)
    counted = 0
    for _ in range(MAX_FRAMES_COUNTED + 64):
        if offset + 8 > size:
            break
        chunk = data[offset : offset + 4]
        (length,) = struct.unpack("<I", data[offset + 4 : offset + 8])
        body = offset + 8
        if chunk == b"VP8X" and body + 10 <= size:
            animated = bool(data[body] & 0x02)
            width = 1 + int.from_bytes(data[body + 4 : body + 7], "little")
            height = 1 + int.from_bytes(data[body + 7 : body + 10], "little")
            if not animated:
                break
        elif chunk == b"VP8 " and body + 10 <= size and not width:
            if data[body + 3 : body + 6] == b"\x9d\x01\x2a":
                width = struct.unpack("<H", data[body + 6 : body + 8])[0] & 0x3FFF
                height = struct.unpack("<H", data[body + 8 : body + 10])[0] & 0x3FFF
            break
        elif chunk == b"VP8L" and body + 5 <= size and not width:
            if data[body] == 0x2F:
                bits = int.from_bytes(data[body + 1 : body + 5], "little")
                width = (bits & 0x3FFF) + 1
                height = ((bits >> 14) & 0x3FFF) + 1
            break
        elif chunk == b"ANMF":
            counted += 1
        offset = body + length + (length & 1)
    if animated:
        frames = max(counted, 1)
    return ImageInfo(width, height, frames, "webp")


def image_info(data: bytes) -> ImageInfo:
    """读文件头；不是支持的图片或解析不出来时返回全 0（``known`` 为 False）。"""

    try:
        head = bytes(data[:16])
        if head.startswith(b"\x89PNG\r\n\x1a\n"):
            return _png(data)
        if head.startswith(b"\xff\xd8\xff"):
            return _jpeg(data)
        if head.startswith((b"GIF87a", b"GIF89a")):
            return _gif(data)
        if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
            return _webp(data)
    except (struct.error, IndexError, ValueError):
        return ImageInfo()
    return ImageInfo()


def shape_label(width: int, height: int) -> str:
    """方图 / 横图 / 竖图 / 长条；没有尺寸返回空串。"""

    if width <= 0 or height <= 0:
        return ""
    ratio = width / height
    if ratio >= 3 or ratio <= 1 / 3:
        return "长条"
    if ratio > 1.25:
        return "横图"
    if ratio < 0.8:
        return "竖图"
    return "方图"


def describe_dimensions(width: int, height: int, frames: int = 0) -> str:
    if width <= 0 or height <= 0:
        return ""
    text = f"{width}×{height}"
    return f"{text} · 动图 {frames} 帧" if frames > 1 else text


__all__ = ["MAX_FRAMES_COUNTED", "ImageInfo", "describe_dimensions", "image_info", "shape_label"]
