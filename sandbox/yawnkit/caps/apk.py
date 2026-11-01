"""apk 包：安卓安装包分析（包名/版本/权限/组件/签名/图标/原生库/文件列表/解包）。纯 Python 解析二进制 AndroidManifest。"""

from __future__ import annotations

import hashlib
import re
import struct
import zipfile
from pathlib import Path
from typing import Any, ClassVar

from . import CapError, cap, clip_list, dest, human, ref, src

ANDROID_NS = "http://schemas.android.com/apk/res/android"
DANGEROUS = {
    "READ_CONTACTS",
    "WRITE_CONTACTS",
    "READ_SMS",
    "SEND_SMS",
    "RECEIVE_SMS",
    "READ_CALL_LOG",
    "WRITE_CALL_LOG",
    "CALL_PHONE",
    "READ_PHONE_STATE",
    "RECORD_AUDIO",
    "CAMERA",
    "ACCESS_FINE_LOCATION",
    "ACCESS_COARSE_LOCATION",
    "ACCESS_BACKGROUND_LOCATION",
    "READ_EXTERNAL_STORAGE",
    "WRITE_EXTERNAL_STORAGE",
    "READ_MEDIA_IMAGES",
    "READ_MEDIA_VIDEO",
    "READ_MEDIA_AUDIO",
    "BODY_SENSORS",
    "SYSTEM_ALERT_WINDOW",
    "REQUEST_INSTALL_PACKAGES",
    "BIND_ACCESSIBILITY_SERVICE",
    "BIND_DEVICE_ADMIN",
    "MANAGE_EXTERNAL_STORAGE",
    "QUERY_ALL_PACKAGES",
    "POST_NOTIFICATIONS",
}


def _open(path: str) -> zipfile.ZipFile:
    target = src(path, kind="APK")
    try:
        return zipfile.ZipFile(target)
    except zipfile.BadZipFile as exc:
        raise CapError("不是有效的 APK（APK 本质是 zip）") from exc


class _Axml:
    """最小可用的 Android 二进制 XML 解析器：只取元素名和属性。"""

    def __init__(self, data: bytes) -> None:
        self.data = data
        self.strings: list[str] = []
        self.resource_ids: list[int] = []
        self.elements: list[tuple[int, str, dict[str, Any]]] = []
        self._parse()

    def _string_pool(self, offset: int) -> None:
        _, header_size, _ = struct.unpack_from("<HHI", self.data, offset)
        count, _, flags, strings_start, _ = struct.unpack_from(
            "<IIIII", self.data, offset + 8
        )
        utf8 = bool(flags & 0x100)
        offsets = struct.unpack_from(f"<{count}I", self.data, offset + header_size)
        base = offset + strings_start
        for rel in offsets:
            pos = base + rel
            if utf8:
                pos += 2 if self.data[pos] & 0x80 else 1
                length = self.data[pos]
                if length & 0x80:
                    length = ((length & 0x7F) << 8) | self.data[pos + 1]
                    pos += 1
                pos += 1
                self.strings.append(
                    self.data[pos : pos + length].decode("utf-8", errors="replace")
                )
            else:
                length = struct.unpack_from("<H", self.data, pos)[0]
                pos += 2
                if length & 0x8000:
                    length = ((length & 0x7FFF) << 16) | struct.unpack_from(
                        "<H", self.data, pos
                    )[0]
                    pos += 2
                self.strings.append(
                    self.data[pos : pos + length * 2].decode(
                        "utf-16-le", errors="replace"
                    )
                )

    def _str(self, index: int) -> str:
        return self.strings[index] if 0 <= index < len(self.strings) else ""

    def _parse(self) -> None:
        if len(self.data) < 8 or struct.unpack_from("<H", self.data, 0)[0] != 0x0003:
            raise CapError("AndroidManifest.xml 不是二进制 XML，解析不了")
        offset = 8
        depth = 0
        while offset + 8 <= len(self.data):
            chunk_type, header_size, size = struct.unpack_from(
                "<HHI", self.data, offset
            )
            if size <= 0:
                break
            if chunk_type == 0x0001:
                self._string_pool(offset)
            elif chunk_type == 0x0180:
                count = (size - header_size) // 4
                self.resource_ids = list(
                    struct.unpack_from(f"<{count}I", self.data, offset + header_size)
                )
            elif chunk_type == 0x0102:
                ext = offset + 16
                _, name_idx, attr_start, attr_size, attr_count = struct.unpack_from(
                    "<iiHHH", self.data, ext
                )
                attrs: dict[str, Any] = {}
                for i in range(attr_count):
                    pos = ext + attr_start + i * attr_size
                    _ns, a_name, a_raw, _sz, _res0, a_type, a_data = struct.unpack_from(
                        "<iiiHBBI", self.data, pos
                    )
                    key = self._str(a_name) or self._attr_from_id(a_name)
                    if a_raw >= 0:
                        value: Any = self._str(a_raw)
                    elif a_type == 0x03:
                        value = self._str(a_data)
                    elif a_type == 0x12:
                        value = a_data != 0
                    elif a_type in {0x10, 0x11}:
                        value = a_data if a_data < 0x80000000 else a_data - 0x100000000
                    elif a_type == 0x01:
                        value = f"@0x{a_data:08x}"
                    else:
                        value = a_data
                    attrs[key] = value
                self.elements.append((depth, self._str(name_idx), attrs))
                depth += 1
            elif chunk_type == 0x0103:
                depth -= 1
            offset += size

    _KNOWN: ClassVar[dict[int, str]] = {
        0x01010003: "name",
        0x0101021B: "versionCode",
        0x0101021C: "versionName",
        0x0101020C: "minSdkVersion",
        0x01010270: "targetSdkVersion",
        0x01010001: "label",
        0x01010002: "icon",
        0x01010010: "exported",
    }

    def _attr_from_id(self, index: int) -> str:
        if 0 <= index < len(self.resource_ids):
            return self._KNOWN.get(
                self.resource_ids[index], f"0x{self.resource_ids[index]:08x}"
            )
        return ""


def _manifest(path: str) -> _Axml:
    with _open(path) as archive:
        try:
            data = archive.read("AndroidManifest.xml")
        except KeyError as exc:
            raise CapError("APK 里没有 AndroidManifest.xml") from exc
    return _Axml(data)


def _first(elements: list[tuple[int, str, dict[str, Any]]], tag: str) -> dict[str, Any]:
    return next((attrs for _, name, attrs in elements if name == tag), {})


@cap("apk.info")
def info(path: str) -> dict[str, Any]:
    manifest = _manifest(path)
    root = _first(manifest.elements, "manifest")
    sdk = _first(manifest.elements, "uses-sdk")
    app = _first(manifest.elements, "application")
    with _open(path) as archive:
        names = archive.namelist()
    abis = sorted(
        {n.split("/")[1] for n in names if n.startswith("lib/") and n.count("/") >= 2}
    )
    permissions = [
        a.get("name", "") for _, t, a in manifest.elements if t == "uses-permission"
    ]
    return {
        "package": root.get("package"),
        "version_name": root.get("versionName"),
        "version_code": root.get("versionCode"),
        "min_sdk": sdk.get("minSdkVersion"),
        "target_sdk": sdk.get("targetSdkVersion"),
        "label": app.get("label"),
        "permissions": len(permissions),
        "dangerous_permissions": [
            p.rsplit(".", 1)[-1]
            for p in permissions
            if p.rsplit(".", 1)[-1] in DANGEROUS
        ],
        "abis": abis,
        "dex_files": len([n for n in names if re.fullmatch(r"classes\d*\.dex", n)]),
        "files": len(names),
        "size": human(src(path).stat().st_size),
        "split_or_bundle": any(n.endswith(".apk") for n in names),
    }


@cap("apk.permissions")
def permissions(path: str) -> dict[str, Any]:
    manifest = _manifest(path)
    rows = []
    for _, tag, attrs in manifest.elements:
        if tag in {"uses-permission", "uses-permission-sdk-23"}:
            name = str(attrs.get("name", ""))
            short = name.rsplit(".", 1)[-1]
            rows.append({"name": name, "dangerous": short in DANGEROUS})
    return {"permissions": rows, "dangerous": sum(1 for r in rows if r["dangerous"])}


@cap("apk.components")
def components(path: str) -> dict[str, Any]:
    manifest = _manifest(path)
    result: dict[str, list[dict[str, Any]]] = {
        "activity": [],
        "service": [],
        "receiver": [],
        "provider": [],
    }
    launcher = None
    current = None
    for _, tag, attrs in manifest.elements:
        if tag in result or tag == "activity-alias":
            key = "activity" if tag == "activity-alias" else tag
            current = {"name": attrs.get("name"), "exported": attrs.get("exported")}
            result[key].append(current)
        elif (
            tag == "category"
            and attrs.get("name") == "android.intent.category.LAUNCHER"
            and current
        ):
            launcher = current.get("name")
    return {"launcher": launcher, **{k: clip_list(v, 40) for k, v in result.items()}}


@cap("apk.files")
def files(path: str, pattern: str = "", limit: int = 100) -> dict[str, Any]:
    with _open(path) as archive:
        rows = [
            {"name": item.filename, "size": human(item.file_size)}
            for item in archive.infolist()
            if not item.is_dir()
            and (not pattern or re.search(str(pattern), item.filename))
        ]
    rows.sort(key=lambda r: r["name"])
    return clip_list(rows, max(1, min(int(limit), 500)))


@cap("apk.largest")
def largest(path: str, top: int = 20) -> dict[str, Any]:
    """哪些文件最占空间（瘦身/排查用）。"""

    with _open(path) as archive:
        items = sorted(archive.infolist(), key=lambda i: -i.file_size)[
            : max(1, min(int(top), 100))
        ]
        total = sum(i.file_size for i in archive.infolist())
    return {
        "total_uncompressed": human(total),
        "largest": [
            {
                "name": i.filename,
                "size": human(i.file_size),
                "compressed": human(i.compress_size),
            }
            for i in items
        ],
    }


@cap("apk.signature")
def signature(path: str) -> dict[str, Any]:
    """v1 签名（META-INF 证书）的指纹；v2/v3 签名块只报告是否存在。"""

    target = src(path, kind="APK")
    with _open(path) as archive:
        certs = [
            n
            for n in archive.namelist()
            if n.upper().startswith("META-INF/")
            and n.upper().endswith((".RSA", ".DSA", ".EC"))
        ]
        prints = []
        for name in certs:
            blob = archive.read(name)
            prints.append(
                {
                    "file": name,
                    "sha256_of_block": hashlib.sha256(blob).hexdigest(),
                    "sha1_of_block": hashlib.sha1(blob).hexdigest(),
                }
            )
    tail = target.read_bytes()[-min(target.stat().st_size, 4 * 1024 * 1024) :]
    return {
        "v1_certificates": prints,
        "v2_or_v3_block": b"APK Sig Block 42" in tail,
        "note": "指纹是整个 PKCS#7 块的哈希，可用来比较两个 APK 是不是同一签名；要证书主体信息可以在 run_code 里用 cryptography 解析",
    }


@cap("apk.icon")
def icon(path: str, output: str = "") -> dict[str, Any]:
    """取出分辨率最高的 PNG/WebP 启动图标。"""

    with _open(path) as archive:
        candidates = [
            i
            for i in archive.infolist()
            if re.search(
                r"(mipmap|drawable)[^/]*/(ic_launcher|icon|app_icon)[^/]*\.(png|webp)$",
                i.filename,
            )
        ]
        if not candidates:
            candidates = [
                i
                for i in archive.infolist()
                if re.search(r"(mipmap|drawable)[^/]*/[^/]*\.(png|webp)$", i.filename)
                and "launcher" in i.filename
            ]
        if not candidates:
            raise CapError("没找到图标（可能是自适应图标 XML，或资源名被混淆）")
        best = max(candidates, key=lambda i: i.file_size)
        target = dest(
            output, f"{Path(str(path)).stem}_icon{Path(best.filename).suffix}"
        )
        target.write_bytes(archive.read(best))
    return {"file": ref(target), "from": best.filename}


@cap("apk.native_libs")
def native_libs(path: str) -> dict[str, Any]:
    with _open(path) as archive:
        rows: dict[str, list[str]] = {}
        for name in archive.namelist():
            parts = name.split("/")
            if len(parts) == 3 and parts[0] == "lib" and parts[2].endswith(".so"):
                rows.setdefault(parts[1], []).append(parts[2])
    engines = {
        "Flutter": "libflutter.so",
        "Unity": "libunity.so",
        "React Native": "libreactnativejni.so",
        "Cocos": "libcocos2dcpp.so",
        "Unreal": "libUE4.so",
        "Xamarin": "libmonodroid.so",
    }
    found = sorted(
        {
            engine
            for engine, lib in engines.items()
            for libs in rows.values()
            if lib in libs
        }
    )
    return {
        "abis": {abi: sorted(libs)[:40] for abi, libs in rows.items()},
        "frameworks": found,
    }


@cap("apk.extract")
def extract(
    path: str, pattern: str = "", output: str = "", limit: int = 2000
) -> dict[str, Any]:
    """把 APK 里的文件（可按正则筛）解到文件夹，便于继续用 files/image 包查看。"""

    folder = (
        Path(str(output).removeprefix("sandbox:"))
        if output
        else Path("out") / f"{Path(str(path)).stem}_解包"
    )
    if folder.is_absolute() or ".." in folder.parts:
        raise CapError("输出路径要用工作区里的相对路径")
    count = 0
    with _open(path) as archive:
        for item in archive.infolist():
            if item.is_dir() or (
                pattern and not re.search(str(pattern), item.filename)
            ):
                continue
            name = Path(item.filename)
            if name.is_absolute() or ".." in name.parts:
                continue
            target = folder / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(archive.read(item))
            count += 1
            if count >= int(limit):
                break
    return {"folder": ref(folder), "files": count}


@cap("apk.strings")
def strings(
    path: str, pattern: str = r"https?://[\x21-\x7e]+", limit: int = 100
) -> dict[str, Any]:
    """在 dex 里搜字符串（默认找网址），看这个应用会连哪些服务器。"""

    try:
        compiled = re.compile(str(pattern).encode())
    except re.error as exc:
        raise CapError(f"正则写错了：{exc}") from exc
    found: dict[str, None] = {}
    with _open(path) as archive:
        for name in archive.namelist():
            if not re.fullmatch(r"classes\d*\.dex", name):
                continue
            for match in compiled.finditer(archive.read(name)):
                text = match.group(0).decode("utf-8", errors="replace").rstrip(".,;:)'\"]>")
                found[text] = None
                if len(found) >= int(limit):
                    break
    return clip_list(list(found), int(limit))
