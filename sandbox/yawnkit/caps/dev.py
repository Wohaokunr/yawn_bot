"""dev 包：编码解码、哈希、UUID/密码、JWT 解码、JSON 格式化与查询、进制、时间戳、正则测试、代码检查。"""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import re
import secrets
import string
import subprocess
import sys
import urllib.parse
import uuid as uuid_lib
from datetime import datetime, timedelta, timezone
from typing import Any

from . import CapError, cap, clip_text, dest, read_text, ref, src

BEIJING = timezone(timedelta(hours=8))


@cap("dev.encode")
def encode(text: str, kind: str = "base64") -> dict[str, Any]:
    raw = str(text).encode("utf-8")
    if kind == "base64":
        result = base64.b64encode(raw).decode()
    elif kind == "base64url":
        result = base64.urlsafe_b64encode(raw).decode()
    elif kind == "hex":
        result = raw.hex()
    elif kind == "url":
        result = urllib.parse.quote(str(text), safe="")
    elif kind == "unicode":
        result = str(text).encode("unicode_escape").decode()
    elif kind == "html":
        import html

        result = html.escape(str(text))
    else:
        raise CapError("kind 只能是 base64/base64url/hex/url/unicode/html")
    return clip_text(result)


@cap("dev.decode")
def decode(text: str, kind: str = "base64") -> dict[str, Any]:
    value = str(text).strip()
    try:
        if kind in {"base64", "base64url"}:
            padded = value + "=" * (-len(value) % 4)
            raw = (
                base64.urlsafe_b64decode(padded)
                if kind == "base64url" or "-" in value or "_" in value
                else base64.b64decode(padded)
            )
        elif kind == "hex":
            raw = bytes.fromhex(value)
        elif kind == "url":
            return clip_text(urllib.parse.unquote(value))
        elif kind == "unicode":
            return clip_text(value.encode().decode("unicode_escape"))
        elif kind == "html":
            import html

            return clip_text(html.unescape(value))
        else:
            raise CapError("kind 只能是 base64/base64url/hex/url/unicode/html")
    except (binascii.Error, ValueError) as exc:
        raise CapError(f"解不开：{exc}") from exc
    try:
        return clip_text(raw.decode("utf-8"))
    except UnicodeDecodeError:
        target = dest("", "decoded.bin")
        target.write_bytes(raw)
        return {"binary": True, "bytes": len(raw), "file": ref(target)}


@cap("dev.hash_text")
def hash_text(text: str, algorithm: str = "sha256") -> dict[str, Any]:
    if algorithm not in {"md5", "sha1", "sha256", "sha512", "blake2b", "sha3_256"}:
        raise CapError("algorithm 只能是 md5/sha1/sha256/sha512/blake2b/sha3_256")
    return {
        "algorithm": algorithm,
        "hash": hashlib.new(algorithm, str(text).encode()).hexdigest(),
    }


@cap("dev.uuid")
def uuid(count: int = 1, version: int = 4) -> dict[str, Any]:
    maker = uuid_lib.uuid1 if int(version) == 1 else uuid_lib.uuid4
    return {"uuids": [str(maker()) for _ in range(max(1, min(int(count), 50)))]}


@cap("dev.password")
def password(
    length: int = 16, count: int = 1, symbols: bool = True, avoid_ambiguous: bool = True
) -> dict[str, Any]:
    alphabet = (
        string.ascii_letters + string.digits + ("!@#$%^&*-_=+" if symbols else "")
    )
    if avoid_ambiguous:
        alphabet = "".join(ch for ch in alphabet if ch not in "O0Il1")
    size = max(6, min(int(length), 128))
    return {
        "passwords": [
            "".join(secrets.choice(alphabet) for _ in range(size))
            for _ in range(max(1, min(int(count), 20)))
        ]
    }


@cap("dev.jwt_decode")
def jwt_decode(token: str) -> dict[str, Any]:
    """只解码不验签：看 JWT 的 header/payload 和过期时间。"""

    parts = str(token).strip().split(".")
    if len(parts) < 2:
        raise CapError("不像 JWT（应是 xxx.yyy.zzz）")

    def part(text: str) -> Any:
        try:
            return json.loads(base64.urlsafe_b64decode(text + "=" * (-len(text) % 4)))
        except (binascii.Error, ValueError) as exc:
            raise CapError(f"解不开：{exc}") from exc

    payload = part(parts[1])
    result: dict[str, Any] = {
        "header": part(parts[0]),
        "payload": payload,
        "verified": False,
    }
    if isinstance(payload, dict) and isinstance(payload.get("exp"), (int, float)):
        expires = datetime.fromtimestamp(payload["exp"], BEIJING)
        result["expires"] = expires.strftime("%Y-%m-%d %H:%M:%S")
        result["expired"] = expires < datetime.now(BEIJING)
    return result


@cap("dev.json_format")
def json_format(
    text: str = "",
    path: str = "",
    indent: int = 2,
    minify: bool = False,
    sort_keys: bool = False,
) -> dict[str, Any]:
    source = read_text(src(path)) if path else str(text)
    try:
        data = json.loads(source)
    except json.JSONDecodeError as exc:
        raise CapError(
            f"JSON 不合法：第 {exc.lineno} 行第 {exc.colno} 列 {exc.msg}"
        ) from exc
    if minify:
        result = json.dumps(
            data, ensure_ascii=False, separators=(",", ":"), sort_keys=bool(sort_keys)
        )
    else:
        result = json.dumps(
            data,
            ensure_ascii=False,
            indent=max(0, min(int(indent), 8)),
            sort_keys=bool(sort_keys),
        )
    if path:
        target = dest("", f"{src(path).stem}_格式化.json")
        target.write_text(result, encoding="utf-8")
        return {"valid": True, "file": ref(target)}
    return {"valid": True, **clip_text(result)}


def _walk(data: Any, path: str) -> list[Any]:
    """极简路径：a.b[0].c、items[*].name。"""

    current = [data]
    for token in re.findall(r"[^.\[\]]+|\[\*?\d*\]", path):
        nxt: list[Any] = []
        for item in current:
            if token in {"[*]", "[]"}:
                if isinstance(item, list):
                    nxt.extend(item)
                elif isinstance(item, dict):
                    nxt.extend(item.values())
            elif token.startswith("["):
                index = int(token[1:-1])
                if isinstance(item, list) and -len(item) <= index < len(item):
                    nxt.append(item[index])
            elif isinstance(item, dict) and token in item:
                nxt.append(item[token])
        current = nxt
    return current


@cap("dev.json_query")
def json_query(query: str, text: str = "", path: str = "") -> dict[str, Any]:
    source = read_text(src(path)) if path else str(text)
    try:
        data = json.loads(source)
    except json.JSONDecodeError as exc:
        raise CapError(f"JSON 不合法：{exc.msg}") from exc
    found = _walk(data, str(query))
    rendered = json.dumps(
        found if len(found) != 1 else found[0],
        ensure_ascii=False,
        indent=2,
        default=str,
    )
    return {"matches": len(found), **clip_text(rendered)}


@cap("dev.base_convert")
def base_convert(value: str, from_base: int = 10) -> dict[str, Any]:
    try:
        number = int(str(value).strip().replace("_", ""), int(from_base))
    except ValueError as exc:
        raise CapError(f"不是 {from_base} 进制数：{value}") from exc
    return {
        "dec": str(number),
        "hex": hex(number),
        "oct": oct(number),
        "bin": bin(number),
        "base36": _base36(number),
    }


def _base36(number: int) -> str:
    digits = "0123456789abcdefghijklmnopqrstuvwxyz"
    sign = "-" if number < 0 else ""
    number = abs(number)
    out = ""
    while True:
        number, rest = divmod(number, 36)
        out = digits[rest] + out
        if not number:
            return sign + out


@cap("dev.timestamp")
def timestamp(value: str = "", fmt: str = "%Y-%m-%d %H:%M:%S") -> dict[str, Any]:
    """时间戳 ↔ 北京时间。value 留空给现在；数字当 Unix 秒/毫秒；否则按 ISO/常见格式解析。"""

    text = str(value).strip()
    if not text:
        moment = datetime.now(BEIJING)
    elif re.fullmatch(r"-?\d+(\.\d+)?", text):
        number = float(text)
        moment = datetime.fromtimestamp(
            number / 1000 if abs(number) > 1e11 else number, BEIJING
        )
    else:
        moment = None
        for pattern in (
            "%Y-%m-%d %H:%M:%S",
            "%Y-%m-%d %H:%M",
            "%Y-%m-%d",
            "%Y/%m/%d %H:%M:%S",
            "%Y/%m/%d",
            "%Y年%m月%d日 %H:%M",
            "%Y年%m月%d日",
        ):
            try:
                moment = datetime.strptime(text, pattern).replace(tzinfo=BEIJING)
                break
            except ValueError:
                continue
        if moment is None:
            try:
                moment = datetime.fromisoformat(text)
            except ValueError as exc:
                raise CapError("认不出这个时间") from exc
            if moment.tzinfo is None:
                moment = moment.replace(tzinfo=BEIJING)
    local = moment.astimezone(BEIJING)
    return {
        "beijing": local.strftime(fmt),
        "iso": local.isoformat(),
        "unix": int(local.timestamp()),
        "unix_ms": int(local.timestamp() * 1000),
        "weekday": "一二三四五六日"[local.weekday()],
    }


@cap("dev.regex_test")
def regex_test(pattern: str, text: str, flags: str = "") -> dict[str, Any]:
    flag_value = 0
    for letter, flag in (
        ("i", re.IGNORECASE),
        ("m", re.MULTILINE),
        ("s", re.DOTALL),
        ("x", re.VERBOSE),
    ):
        if letter in str(flags):
            flag_value |= flag
    try:
        compiled = re.compile(str(pattern), flag_value)
    except re.error as exc:
        raise CapError(f"正则写错了：{exc}") from exc
    matches = []
    for match in compiled.finditer(str(text)):
        matches.append(
            {
                "match": match.group(0),
                "span": list(match.span()),
                "groups": list(match.groups()),
                "named": match.groupdict(),
            }
        )
        if len(matches) >= 50:
            break
    return {"count": len(matches), "matches": matches}


@cap("dev.lint_python")
def lint_python(path: str = "", code: str = "") -> dict[str, Any]:
    """语法检查 + 编译 Python 代码，报出第一处语法错误的行列。"""

    source = read_text(src(path)) if path else str(code)
    try:
        compile(source, path or "<code>", "exec")
    except SyntaxError as exc:
        return {
            "ok": False,
            "line": exc.lineno,
            "column": exc.offset,
            "error": exc.msg,
            "text": (exc.text or "").rstrip(),
        }
    return {"ok": True, "lines": len(source.splitlines())}


@cap("dev.run_python")
def run_python(
    path: str, args: list[str] | None = None, timeout: int = 120
) -> dict[str, Any]:
    """运行工作区里的一个 .py 脚本（独立进程），返回退出码和输出末尾。"""

    target = src(path, kind="脚本")
    command = [sys.executable, str(target), *[str(a) for a in (args or [])]]
    try:
        done = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=max(1, min(int(timeout), 900)),
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise CapError("脚本超时") from exc
    return {
        "exit_code": done.returncode,
        "stdout": done.stdout[-3000:],
        "stderr": done.stderr[-2000:],
    }


@cap("dev.count_code")
def count_code(folder: str = ".") -> dict[str, Any]:
    """按语言统计代码行数（跳过 .git、node_modules、虚拟环境）。"""

    from pathlib import Path

    root = src(folder, kind="文件夹") if folder not in {"", "."} else Path(".")
    languages = {
        ".py": "Python",
        ".js": "JavaScript",
        ".ts": "TypeScript",
        ".tsx": "TSX",
        ".java": "Java",
        ".kt": "Kotlin",
        ".go": "Go",
        ".rs": "Rust",
        ".c": "C",
        ".h": "C",
        ".cpp": "C++",
        ".cs": "C#",
        ".php": "PHP",
        ".rb": "Ruby",
        ".swift": "Swift",
        ".html": "HTML",
        ".css": "CSS",
        ".vue": "Vue",
        ".sh": "Shell",
        ".sql": "SQL",
        ".md": "Markdown",
        ".json": "JSON",
        ".yaml": "YAML",
        ".yml": "YAML",
        ".xml": "XML",
        ".lua": "Lua",
        ".smali": "Smali",
    }
    skip = {".git", "node_modules", ".venv", "venv", "__pycache__", ".pylib"}
    totals: dict[str, dict[str, int]] = {}
    for item in root.rglob("*"):
        if (
            not item.is_file()
            or skip.intersection(item.parts)
            or item.suffix.lower() not in languages
        ):
            continue
        lang = languages[item.suffix.lower()]
        try:
            lines = item.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            continue
        row = totals.setdefault(lang, {"files": 0, "lines": 0, "blank": 0})
        row["files"] += 1
        row["lines"] += len(lines)
        row["blank"] += sum(1 for line in lines if not line.strip())
    ordered = dict(sorted(totals.items(), key=lambda kv: -kv[1]["lines"]))
    return {
        "languages": ordered,
        "total_lines": sum(r["lines"] for r in ordered.values()),
    }
