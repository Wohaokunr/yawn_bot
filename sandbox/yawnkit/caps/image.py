"""image 包：图片信息、缩放裁剪旋转、格式转换压缩、拼图水印、二维码、GIF。"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from . import CapError, as_list, cap, dest, human, ref, src

_FORMATS = {
    "png": "PNG",
    "jpg": "JPEG",
    "jpeg": "JPEG",
    "webp": "WEBP",
    "bmp": "BMP",
    "gif": "GIF",
    "tiff": "TIFF",
    "ico": "ICO",
    "pdf": "PDF",
}


def _open(path: str) -> Any:
    from PIL import Image, UnidentifiedImageError

    target = src(path, kind="图片")
    try:
        image = Image.open(target)
        image.load()
    except (UnidentifiedImageError, OSError) as exc:
        raise CapError(f"打不开这张图片：{exc}") from exc
    return image


def _save(
    image: Any, output: str, default: str, *, quality: int = 90
) -> dict[str, Any]:
    target = dest(output, default)
    fmt = _FORMATS.get(target.suffix.lower().lstrip("."), "PNG")
    if fmt == "JPEG" and image.mode not in {"RGB", "L"}:
        image = image.convert("RGB")
    kwargs: dict[str, Any] = {}
    if fmt in {"JPEG", "WEBP"}:
        kwargs["quality"] = max(10, min(int(quality), 100))
    image.save(target, fmt, **kwargs)
    return {
        "file": ref(target),
        "size": human(target.stat().st_size),
        "pixels": f"{image.width}x{image.height}",
    }


def _name(path: str, suffix: str, ext: str | None = None) -> str:
    p = Path(str(path).removeprefix("sandbox:"))
    return f"{p.stem}_{suffix}{ext or p.suffix or '.png'}"


@cap("image.info")
def info(path: str) -> dict[str, Any]:
    image = _open(path)
    target = src(path)
    return {
        "format": image.format,
        "mode": image.mode,
        "width": image.width,
        "height": image.height,
        "frames": getattr(image, "n_frames", 1),
        "size": human(target.stat().st_size),
        "has_alpha": image.mode in {"RGBA", "LA", "PA"},
    }


@cap("image.exif")
def exif(path: str) -> dict[str, Any]:
    from PIL import ExifTags

    image = _open(path)
    data = image.getexif()
    rows = {}
    for key, value in data.items():
        name = ExifTags.TAGS.get(key, str(key))
        text = (
            value.decode(errors="replace") if isinstance(value, bytes) else str(value)
        )
        rows[name] = text[:120]
    gps = data.get_ifd(0x8825) if hasattr(data, "get_ifd") else {}
    return {
        "exif": rows,
        "has_gps": bool(gps),
        "note": "含 GPS 的照片发出去会暴露拍摄位置" if gps else None,
    }


@cap("image.strip_exif")
def strip_exif(path: str, output: str = "") -> dict[str, Any]:
    from PIL import Image

    image = _open(path)
    clean = Image.new(image.mode, image.size)
    clean.putdata(list(image.getdata()))
    return _save(clean, output, _name(path, "clean"))


@cap("image.resize")
def resize(
    path: str, width: int = 0, height: int = 0, scale: float = 0, output: str = ""
) -> dict[str, Any]:
    image = _open(path)
    if scale:
        size = (
            max(1, int(image.width * float(scale))),
            max(1, int(image.height * float(scale))),
        )
    elif width and height:
        size = (int(width), int(height))
    elif width:
        size = (int(width), max(1, round(image.height * int(width) / image.width)))
    elif height:
        size = (max(1, round(image.width * int(height) / image.height)), int(height))
    else:
        raise CapError("给 width、height 或 scale 之一")
    if max(size) > 20000:
        raise CapError("目标尺寸太大")
    return _save(image.resize(size), output, _name(path, f"{size[0]}x{size[1]}"))


@cap("image.crop")
def crop(
    path: str, left: int, top: int, right: int, bottom: int, output: str = ""
) -> dict[str, Any]:
    image = _open(path)
    box = (int(left), int(top), int(right), int(bottom))
    if not (
        0 <= box[0] < box[2] <= image.width and 0 <= box[1] < box[3] <= image.height
    ):
        raise CapError(f"裁剪框超出图片范围（图片 {image.width}x{image.height}）")
    return _save(image.crop(box), output, _name(path, "crop"))


@cap("image.rotate")
def rotate(path: str, degrees: float = 90, output: str = "") -> dict[str, Any]:
    image = _open(path)
    return _save(
        image.rotate(-float(degrees), expand=True),
        output,
        _name(path, f"rot{int(degrees)}"),
    )


@cap("image.flip")
def flip(path: str, direction: str = "horizontal", output: str = "") -> dict[str, Any]:
    from PIL import ImageOps

    image = _open(path)
    flipped = (
        ImageOps.mirror(image) if direction == "horizontal" else ImageOps.flip(image)
    )
    return _save(flipped, output, _name(path, "flip"))


@cap("image.convert")
def convert(
    path: str, to: str = "png", output: str = "", quality: int = 90
) -> dict[str, Any]:
    fmt = str(to).lower().lstrip(".")
    if fmt not in _FORMATS:
        raise CapError(f"to 只能是 {'/'.join(_FORMATS)}")
    image = _open(path)
    return _save(image, output, _name(path, "conv", f".{fmt}"), quality=quality)


@cap("image.compress")
def compress(path: str, max_kb: int = 500, output: str = "") -> dict[str, Any]:
    image = _open(path)
    if image.mode not in {"RGB", "L"}:
        image = image.convert("RGB")
    target = dest(output, _name(path, "small", ".jpg"))
    quality = 90
    current = image
    for _ in range(20):
        current.save(target, "JPEG", quality=quality, optimize=True)
        if target.stat().st_size <= int(max_kb) * 1024:
            break
        if quality > 40:
            quality -= 10
        else:
            current = current.resize(
                (max(1, int(current.width * 0.85)), max(1, int(current.height * 0.85)))
            )
    return {
        "file": ref(target),
        "size": human(target.stat().st_size),
        "quality": quality,
        "pixels": f"{current.width}x{current.height}",
    }


@cap("image.grayscale")
def grayscale(path: str, output: str = "") -> dict[str, Any]:
    return _save(_open(path).convert("L"), output, _name(path, "gray"))


@cap("image.filter")
def apply_filter(
    path: str, effect: str = "blur", strength: float = 2, output: str = ""
) -> dict[str, Any]:
    from PIL import ImageEnhance, ImageFilter, ImageOps

    image = _open(path).convert("RGB")
    value = float(strength)
    if effect == "blur":
        result = image.filter(ImageFilter.GaussianBlur(value))
    elif effect == "sharpen":
        result = ImageEnhance.Sharpness(image).enhance(1 + value)
    elif effect == "brightness":
        result = ImageEnhance.Brightness(image).enhance(value)
    elif effect == "contrast":
        result = ImageEnhance.Contrast(image).enhance(value)
    elif effect == "saturation":
        result = ImageEnhance.Color(image).enhance(value)
    elif effect == "invert":
        result = ImageOps.invert(image)
    elif effect == "edges":
        result = image.filter(ImageFilter.FIND_EDGES)
    elif effect == "sepia":
        gray = ImageOps.grayscale(image)
        result = ImageOps.colorize(gray, "#3b2a1a", "#f3e2c6")
    elif effect == "pixelate":
        size = max(2, int(value * 4))
        small = image.resize(
            (max(1, image.width // size), max(1, image.height // size))
        )
        result = small.resize(image.size, resample=0)
    else:
        raise CapError(
            "effect 只能是 blur/sharpen/brightness/contrast/saturation/invert/edges/sepia/pixelate"
        )
    return _save(result, output, _name(path, effect))


@cap("image.border")
def border(
    path: str, size: int = 20, color: str = "white", output: str = ""
) -> dict[str, Any]:
    from PIL import ImageOps

    image = _open(path).convert("RGB")
    return _save(
        ImageOps.expand(image, border=int(size), fill=color),
        output,
        _name(path, "border"),
    )


@cap("image.watermark")
def watermark(
    path: str,
    text: str,
    position: str = "bottom-right",
    opacity: float = 0.5,
    output: str = "",
) -> dict[str, Any]:
    from PIL import Image, ImageDraw, ImageFont

    image = _open(path).convert("RGBA")
    layer = Image.new("RGBA", image.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(layer)
    font_size = max(14, image.width // 25)
    font: Any
    try:
        font = ImageFont.truetype(
            "/usr/share/fonts/truetype/wqy/wqy-microhei.ttc", font_size
        )
    except OSError:
        font = ImageFont.load_default()
    box = draw.textbbox((0, 0), str(text), font=font)
    w, h = box[2] - box[0], box[3] - box[1]
    margin = font_size // 2
    spots = {
        "bottom-right": (image.width - w - margin, image.height - h - margin),
        "bottom-left": (margin, image.height - h - margin),
        "top-left": (margin, margin),
        "top-right": (image.width - w - margin, margin),
        "center": ((image.width - w) // 2, (image.height - h) // 2),
    }
    if position not in spots:
        raise CapError(
            "position 只能是 bottom-right/bottom-left/top-left/top-right/center"
        )
    alpha = int(255 * max(0.05, min(float(opacity), 1)))
    draw.text(
        spots[position],
        str(text),
        font=font,
        fill=(255, 255, 255, alpha),
        stroke_width=2,
        stroke_fill=(0, 0, 0, alpha),
    )
    return _save(Image.alpha_composite(image, layer), output, _name(path, "wm", ".png"))


@cap("image.collage")
def collage(
    paths: list[str], direction: str = "horizontal", gap: int = 10, output: str = ""
) -> dict[str, Any]:
    from PIL import Image

    images = [_open(p).convert("RGB") for p in as_list(paths)]
    if len(images) < 2:
        raise CapError("至少两张图")
    if direction == "horizontal":
        height = min(i.height for i in images)
        images = [
            i.resize((max(1, i.width * height // i.height), height)) for i in images
        ]
        canvas = Image.new(
            "RGB",
            (sum(i.width for i in images) + gap * (len(images) - 1), height),
            "white",
        )
        x = 0
        for item in images:
            canvas.paste(item, (x, 0))
            x += item.width + gap
    else:
        width = min(i.width for i in images)
        images = [
            i.resize((width, max(1, i.height * width // i.width))) for i in images
        ]
        canvas = Image.new(
            "RGB",
            (width, sum(i.height for i in images) + gap * (len(images) - 1)),
            "white",
        )
        y = 0
        for item in images:
            canvas.paste(item, (0, y))
            y += item.height + gap
    return _save(canvas, output, "拼图.jpg")


@cap("image.grid")
def grid(
    paths: list[str], columns: int = 3, cell: int = 300, output: str = ""
) -> dict[str, Any]:
    from PIL import Image, ImageOps

    images = [_open(p).convert("RGB") for p in as_list(paths)][:64]
    if not images:
        raise CapError("没有图片")
    columns = max(1, min(int(columns), 10))
    rows = (len(images) + columns - 1) // columns
    canvas = Image.new("RGB", (columns * cell, rows * cell), "white")
    for index, item in enumerate(images):
        thumb = ImageOps.fit(item, (cell, cell))
        canvas.paste(thumb, ((index % columns) * cell, (index // columns) * cell))
    return _save(canvas, output, "九宫格.jpg")


@cap("image.palette")
def palette(path: str, colors: int = 6) -> dict[str, Any]:
    image = _open(path).convert("RGB")
    image.thumbnail((200, 200))
    quantized = image.quantize(colors=max(2, min(int(colors), 16)))
    pal = quantized.getpalette() or []
    counts = sorted(quantized.getcolors() or [], reverse=True)
    rows = []
    total = sum(count for count, _ in counts) or 1
    for count, index in counts:
        r, g, b = pal[index * 3 : index * 3 + 3]
        rows.append(
            {"hex": f"#{r:02x}{g:02x}{b:02x}", "share": f"{count * 100 / total:.1f}%"}
        )
    return {"colors": rows}


@cap("image.to_pdf")
def to_pdf(paths: list[str], output: str = "") -> dict[str, Any]:
    images = [_open(p).convert("RGB") for p in as_list(paths)]
    if not images:
        raise CapError("没有图片")
    target = dest(output, "图片合集.pdf")
    images[0].save(
        target, "PDF", save_all=True, append_images=images[1:], resolution=150
    )
    return {
        "file": ref(target),
        "pages": len(images),
        "size": human(target.stat().st_size),
    }


@cap("image.qrcode")
def make_qrcode(text: str, output: str = "", size: int = 10) -> dict[str, Any]:
    import qrcode

    if not str(text).strip():
        raise CapError("text 不能为空")
    image = qrcode.make(str(text), box_size=max(2, min(int(size), 30)))
    target = dest(output, "二维码.png")
    image.save(target)
    return {"file": ref(target)}


@cap("image.gif_frames")
def gif_frames(path: str, every: int = 1, limit: int = 60) -> dict[str, Any]:
    from PIL import ImageSequence

    image = _open(path)
    folder = Path("out") / f"{Path(str(path)).stem}_frames"
    folder.mkdir(parents=True, exist_ok=True)
    saved = []
    for index, frame in enumerate(ImageSequence.Iterator(image)):
        if index % max(1, int(every)):
            continue
        target = folder / f"frame_{index:04d}.png"
        frame.convert("RGBA").save(target)
        saved.append(ref(target))
        if len(saved) >= int(limit):
            break
    return {"frames": saved[:10], "count": len(saved), "folder": ref(folder)}


@cap("image.make_gif")
def make_gif(
    paths: list[str], duration_ms: int = 200, loop: bool = True, output: str = ""
) -> dict[str, Any]:
    frames = [_open(p).convert("RGB") for p in as_list(paths)]
    if len(frames) < 2:
        raise CapError("至少两帧")
    size = frames[0].size
    frames = [f.resize(size) for f in frames]
    target = dest(output, "动图.gif")
    frames[0].save(
        target,
        save_all=True,
        append_images=frames[1:],
        duration=int(duration_ms),
        loop=0 if loop else 1,
    )
    return {
        "file": ref(target),
        "frames": len(frames),
        "size": human(target.stat().st_size),
    }


@cap("image.ascii")
def ascii_art(path: str, width: int = 60) -> dict[str, Any]:
    chars = "@%#*+=-:. "
    image = _open(path).convert("L")
    width = max(20, min(int(width), 120))
    height = max(1, int(image.height * width / image.width * 0.5))
    small = image.resize((width, height))
    pixels = list(small.getdata())
    lines = [
        "".join(
            chars[p * (len(chars) - 1) // 255]
            for p in pixels[row * width : (row + 1) * width]
        )
        for row in range(height)
    ]
    return {"text": "\n".join(lines)}
