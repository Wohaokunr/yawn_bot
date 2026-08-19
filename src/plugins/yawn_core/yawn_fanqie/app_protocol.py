"""Pinned anonymous Fanqie App protocol client.

The wire format is pinned to the MIT implementation at
``ZreXoc/fanqie-rs@906c6fd5744af0ef49e529102cdb64a250c067f7``.  The upstream
implementation does not contain a network device-registration endpoint.  The
"registration" performed here is therefore only initialization of its pinned
capture profile in localstore; it must not be described as a live account or
device registration.

Only the official HTTPS host and the ``registerkey``/single-chapter
``batch_full`` paths are reachable from this module.  Captured device fields
are persisted, but never logged.  Chapter keys are retained in memory only.
"""

# Public exception names are fixed by the provider contract.  Sanitized domain
# errors intentionally carry their stable messages at each protocol boundary.
# Atomic persistence explicitly requires os.replace rather than Path.replace.
# ruff: noqa: N818, TRY003, TRY004, PLR2004, PTH105, BLE001

from __future__ import annotations

import asyncio
import base64
import gzip
import json
import logging
import os
import re
import tempfile
from dataclasses import asdict, dataclass
from html.parser import HTMLParser
from pathlib import Path
from typing import TYPE_CHECKING, Any, ClassVar, Final
from urllib.parse import urlsplit

import httpx
from cryptography.hazmat.primitives import padding
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from nonebot_plugin_localstore import get_plugin_data_dir
from typing_extensions import Self

from .app_signer import sign_request

if TYPE_CHECKING:
    from collections.abc import Mapping

logger = logging.getLogger(__name__)

REFERENCE_REPOSITORY: Final = "https://github.com/ZreXoc/fanqie-rs"
REFERENCE_COMMIT: Final = "906c6fd5744af0ef49e529102cdb64a250c067f7"
APP_HOST: Final = "api5-normal-sinfonlinea.fqnovel.com"
APP_ORIGIN: Final = f"https://{APP_HOST}"
REGISTER_KEY_PATH: Final = "/reading/crypt/registerkey"
BATCH_FULL_PATH: Final = "/reading/reader/batch_full/v:version/"

_PROFILE_SCHEMA_VERSION: Final = 1
_PROFILE_VERSION: Final = "reading-ios-7.2.1.32-capture-v1"
_SAMPLE_DEVICE_ID: Final = "1378108152030395"
_SAMPLE_IID: Final = "1378108152034491"
_SAMPLE_CDID: Final = "48A9003E-1B38-424A-ABB8-7D04EC59BC43"
_SAMPLE_X_TT_DT: Final = (
    "AAA5VL3Q5H5RPMS46KJMFX7O2JRMAALF4WKT6OBKSIMOXUK2LJ6XX2VV3G76"
    "HWNBMMU42SJN7YYISVNHAFWVWSOMNH3NYVWB2PUWVUIBIPJPBNB5UBGB3THN5"
    "VNTV7ZZGGC4IM4HB5FUFLVDASKOC2I"
)
_PROFILE_FIELDS: Final = {
    "schema_version",
    "profile_version",
    "reference_repository",
    "reference_commit",
    "device_id",
    "iid",
    "cdid",
    "x_tt_dt",
}

_STATIC_META_KEY_1001: Final = b"UmlsZTU1WTgwMjM4"
_ORIGIN_KEY_DATA: Final = bytes(
    [
        0xAC,
        0x25,
        0xC6,
        0x7D,
        0xDD,
        0x8F,
        0x38,
        0xC1,
        0xB3,
        0x7A,
        0x23,
        0x48,
        0x82,
        0x8E,
        0x22,
        0x2E,
    ]
)
_SIGNATURE_HEADERS: Final = {
    "x-argus",
    "x-gorgon",
    "x-helios",
    "x-khronos",
    "x-ladon",
    "x-medusa",
    "x-ss-stub",
}
_REQUIRED_SIGNATURE_HEADERS: Final = _SIGNATURE_HEADERS - {"x-ss-stub"}
_ITEM_ID_RE: Final = re.compile(r"\d{6,32}")

_COMMON_QUERY: Final = (
    ("device_id", _SAMPLE_DEVICE_ID),
    ("os_version", "26.5"),
    ("version_name", "7.2.1.32"),
    ("device_model", "iPad13,8"),
    ("iid", _SAMPLE_IID),
    ("app_name", "novelapp"),
    ("key_register_ts", "0"),
    ("compliance_status", "0"),
    ("book_id", "7539126266163629118"),
    ("ac", "wifi"),
    ("novel_text_type", "0"),
    ("ssmix", "a"),
    ("version_code", "721"),
    ("channel", "App%20Store"),
    (
        "active_schema_params",
        "%7B%22material_id%22:%22unknown%22,%22schema%22:%22unknown%22,"
        "%22gd_label%22:%22unknown%22,%22unit_id_rule%22:%22unknown%22%7D",
    ),
    ("req_type", "0"),
    ("need_personal_recommend", "1"),
    ("update_version_code", "72132"),
    ("device_brand", "ipad"),
    ("device_platform", "ipad"),
    ("device_type", "iPad%20Pro%205"),
    ("item_ids", "7592456826415743550"),
    ("aid", "1967"),
    ("cdid", _SAMPLE_CDID),
    ("resolution", "3840*2160"),
)
_REGISTER_KEY_ORDER: Final = (
    "version_code",
    "need_personal_recommend",
    "app_name",
    "device_id",
    "channel",
    "aid",
    "version_name",
    "resolution",
    "update_version_code",
    "active_schema_params",
    "cdid",
    "ac",
    "os_version",
    "device_model",
    "ssmix",
    "compliance_status",
    "device_platform",
    "iid",
    "device_type",
    "device_brand",
)
_FIXED_HEADERS: Final = {
    "accept": "*/*",
    "connection": "keep-alive",
    "x-xs-from-web": "0",
    "x-vc-bdturing-sdk-version": "4.0.2",
    "content-type": "application/json; encoding=utf-8",
    "lc": "101",
    "user-agent": "Reading 7.2.1 rv:7.2.1.32 (iPad; iOS 26.5; zh_CN) Cronet",
    "sdk-version": "2",
    "x-ss-dp": "1967",
    "accept-encoding": "gzip, deflate, br",
}


class _RedactHTTPXAppQuery(logging.Filter):
    """Remove captured identifiers from httpx's built-in request log."""

    _fanqie_app_query_filter: ClassVar[bool] = True

    def filter(self, record: logging.LogRecord) -> bool:
        arguments = record.args
        if not isinstance(arguments, tuple) or len(arguments) < 2:
            return True
        url = arguments[1]
        if isinstance(url, httpx.URL) and url.host == APP_HOST:
            redacted = list(arguments)
            redacted[1] = url.copy_with(query=None)
            record.args = tuple(redacted)
        return True


_httpx_logger = logging.getLogger("httpx")
if not any(
    getattr(active, "_fanqie_app_query_filter", False)
    for active in _httpx_logger.filters
):
    _httpx_logger.addFilter(_RedactHTTPXAppQuery())


class FanqieAppError(RuntimeError):
    """Base error for the pinned App protocol."""


class AppChapterUnavailable(FanqieAppError):
    """The protocol succeeded but the requested chapter has no usable text."""


class AppProtocolTransientError(FanqieAppError):
    """A network, service-response, signing, or decryption failure."""


class _DeviceProfileRejected(AppProtocolTransientError):
    """The service explicitly rejected the pinned anonymous profile."""


class _CryptoError(ValueError):
    """Internal sanitized cryptographic format error."""


@dataclass(frozen=True, slots=True)
class AnonymousDeviceProfile:
    """Versioned fields initialized from the pinned anonymous capture."""

    schema_version: int
    profile_version: str
    reference_repository: str
    reference_commit: str
    device_id: str
    iid: str
    cdid: str
    x_tt_dt: str

    @classmethod
    def from_mapping(cls, raw: Mapping[str, object]) -> AnonymousDeviceProfile:
        if set(raw) != _PROFILE_FIELDS:
            raise ValueError("anonymous profile has an invalid schema")
        if (
            isinstance(raw.get("schema_version"), bool)
            or raw.get("schema_version") != _PROFILE_SCHEMA_VERSION
        ):
            raise ValueError("anonymous profile has an unsupported schema version")
        string_fields = _PROFILE_FIELDS - {"schema_version"}
        if any(not isinstance(raw.get(field), str) for field in string_fields):
            raise ValueError("anonymous profile contains a non-string field")
        profile = cls(
            schema_version=_PROFILE_SCHEMA_VERSION,
            profile_version=str(raw["profile_version"]),
            reference_repository=str(raw["reference_repository"]),
            reference_commit=str(raw["reference_commit"]),
            device_id=str(raw["device_id"]),
            iid=str(raw["iid"]),
            cdid=str(raw["cdid"]),
            x_tt_dt=str(raw["x_tt_dt"]),
        )
        if profile != _captured_profile():
            raise ValueError("anonymous profile does not match the pinned capture")
        return profile


@dataclass(frozen=True, slots=True)
class _SessionKey:
    key: bytes
    key_version: int
    key_register_ts: int


def _captured_profile() -> AnonymousDeviceProfile:
    return AnonymousDeviceProfile(
        schema_version=_PROFILE_SCHEMA_VERSION,
        profile_version=_PROFILE_VERSION,
        reference_repository=REFERENCE_REPOSITORY,
        reference_commit=REFERENCE_COMMIT,
        device_id=_SAMPLE_DEVICE_ID,
        iid=_SAMPLE_IID,
        cdid=_SAMPLE_CDID,
        x_tt_dt=_SAMPLE_X_TT_DT,
    )


def _default_profile_path() -> Path:
    return Path(get_plugin_data_dir()) / "fanqie" / "app-device-v1.json"


def _write_profile_atomic(path: Path, profile: AnonymousDeviceProfile) -> None:
    """Write JSON through a flushed, fsynced sibling before atomic replace."""

    path.parent.mkdir(parents=True, exist_ok=True)
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            newline="\n",
            prefix=f".{path.name}.",
            suffix=".tmp",
            dir=path.parent,
            delete=False,
        ) as stream:
            temporary = Path(stream.name)
            json.dump(asdict(profile), stream, ensure_ascii=True, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        temporary = None
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def _read_profile(path: Path) -> AnonymousDeviceProfile:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError("anonymous profile is not valid JSON") from exc
    if not isinstance(raw, dict):
        raise ValueError("anonymous profile root must be an object")
    return AnonymousDeviceProfile.from_mapping(raw)


def _initialize_capture_profile(path: Path) -> AnonymousDeviceProfile:
    """Initialize localstore from the pinned capture; no network call occurs."""

    profile = _captured_profile()
    _write_profile_atomic(path, profile)
    return profile


def _load_or_initialize_profile(path: Path) -> AnonymousDeviceProfile:
    if path.is_file():
        try:
            return _read_profile(path)
        except ValueError:
            path.unlink(missing_ok=True)
    return _initialize_capture_profile(path)


def _pkcs7_encrypt(plaintext: bytes, key: bytes, iv: bytes) -> bytes:
    if len(key) != 16 or len(iv) != 16:
        raise _CryptoError("AES key and IV must be 16 bytes")
    padder = padding.PKCS7(128).padder()
    padded = padder.update(plaintext) + padder.finalize()
    encryptor = Cipher(algorithms.AES(key), modes.CBC(iv)).encryptor()
    return encryptor.update(padded) + encryptor.finalize()


def _pkcs7_decrypt(ciphertext: bytes, key: bytes, iv: bytes) -> bytes:
    if len(key) != 16 or len(iv) != 16 or not ciphertext or len(ciphertext) % 16:
        raise _CryptoError("invalid AES-CBC packet")
    try:
        decryptor = Cipher(algorithms.AES(key), modes.CBC(iv)).decryptor()
        padded = decryptor.update(ciphertext) + decryptor.finalize()
        unpadder = padding.PKCS7(128).unpadder()
        return unpadder.update(padded) + unpadder.finalize()
    except ValueError:
        raise _CryptoError("invalid AES-CBC padding") from None


def build_register_plaintext(device_id: str) -> bytes:
    """Encode the captured decimal device ID as little-endian u64 plus zeros."""

    try:
        value = int(device_id, 10)
    except ValueError:
        raise _CryptoError("device ID is not decimal") from None
    if not 0 <= value <= (1 << 64) - 1:
        raise _CryptoError("device ID is outside u64")
    return value.to_bytes(8, "little") + bytes(8)


def encrypt_register_content(plaintext: bytes, iv: bytes) -> str:
    """Create the Base64 ``IV || AES-CBC/PKCS#7`` registerkey content."""

    if len(plaintext) != 16:
        raise _CryptoError("registerkey plaintext must be 16 bytes")
    packet = iv + _pkcs7_encrypt(plaintext, _ORIGIN_KEY_DATA, iv)
    return base64.b64encode(packet).decode("ascii")


def unwrap_server_key(key_blob_base64: str) -> bytes:
    """Unwrap a registerkey ``keyBlob`` using the two pinned meta-key forms."""

    try:
        raw = base64.b64decode(key_blob_base64.strip(), validate=True)
    except (ValueError, TypeError):
        raise _CryptoError("registerkey key blob is not valid Base64") from None
    if len(raw) < 32 or (len(raw) - 16) % 16:
        raise _CryptoError("registerkey key blob has an invalid length")
    iv, ciphertext = raw[:16], raw[16:]
    for meta_key in (_STATIC_META_KEY_1001, _ORIGIN_KEY_DATA):
        try:
            plaintext = _pkcs7_decrypt(ciphertext, meta_key, iv)
        except _CryptoError:
            continue
        if len(plaintext) == 16:
            return plaintext
    raise _CryptoError("registerkey key blob could not be unwrapped")


def decrypt_chapter_bytes(chapter_base64: str, aes_key: bytes) -> bytes:
    """Decode a chapter packet, then AES-CBC/PKCS#7 and gzip-decompress it."""

    try:
        raw = base64.b64decode(chapter_base64.strip(), validate=True)
    except (ValueError, TypeError):
        raise _CryptoError("chapter ciphertext is not valid Base64") from None
    if len(raw) < 32 or (len(raw) - 16) % 16:
        raise _CryptoError("chapter ciphertext has an invalid length")
    plaintext = _pkcs7_decrypt(raw[16:], aes_key, raw[:16])
    if not plaintext.startswith(b"\x1f\x8b"):
        raise _CryptoError("decrypted chapter is not gzip data")
    try:
        return gzip.decompress(plaintext)
    except (OSError, EOFError):
        raise _CryptoError("chapter gzip data is invalid") from None


class _ChapterHTMLParser(HTMLParser):
    _LINE_TAGS: ClassVar[set[str]] = {
        "article",
        "blk",
        "br",
        "div",
        "h1",
        "h2",
        "h3",
        "h4",
        "h5",
        "h6",
        "li",
        "p",
        "section",
    }

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._ignored_depth = 0
        self._block_depth = 0
        self._block: list[str] = []
        self.blocks: list[str] = []
        self.fallback: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        del attrs
        tag = tag.lower()
        if tag in {"script", "style"}:
            self._ignored_depth += 1
            return
        if self._ignored_depth:
            return
        if tag == "blk":
            if self._block_depth == 0:
                self._block = []
            self._block_depth += 1
        elif tag == "br" and self._block_depth:
            self._block.append("\n")
        if tag in self._LINE_TAGS:
            self.fallback.append("\n")

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.handle_starttag(tag, attrs)
        if tag.lower() not in {"br", "script", "style"}:
            self.handle_endtag(tag)

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if tag in {"script", "style"}:
            if self._ignored_depth:
                self._ignored_depth -= 1
            return
        if self._ignored_depth:
            return
        if tag == "blk" and self._block_depth:
            self._block_depth -= 1
            if self._block_depth == 0:
                self.blocks.append("".join(self._block))
                self._block = []
        if tag in self._LINE_TAGS:
            self.fallback.append("\n")

    def handle_data(self, data: str) -> None:
        if self._ignored_depth:
            return
        self.fallback.append(data)
        if self._block_depth:
            self._block.append(data)


def _normalize_text(value: str) -> str:
    lines = [re.sub(r"[ \t\f\v]+", " ", line).strip() for line in value.splitlines()]
    output: list[str] = []
    for line in lines:
        if line or (output and output[-1]):
            output.append(line)
    return "\n".join(output).strip()


def extract_text(markup: str) -> str:
    """Extract ``blk`` text first, otherwise produce clean structured text."""

    parser = _ChapterHTMLParser()
    try:
        parser.feed(markup)
        parser.close()
    except ValueError:
        raise _CryptoError("chapter markup is invalid") from None
    blocks = [_normalize_text(value) for value in parser.blocks]
    blocks = [value for value in blocks if value]
    if blocks:
        return "\n".join(blocks)
    return _normalize_text("".join(parser.fallback))


def decrypt_chapter_text(chapter_base64: str, aes_key: bytes) -> str:
    """Decrypt, decompress, UTF-8 decode, and clean a chapter."""

    raw = decrypt_chapter_bytes(chapter_base64, aes_key)
    return extract_text(raw.decode("utf-8", errors="replace"))


def _query_values(profile: AnonymousDeviceProfile) -> dict[str, str]:
    values = dict(_COMMON_QUERY)
    values.update(
        device_id=profile.device_id,
        iid=profile.iid,
        cdid=profile.cdid,
    )
    return values


def _register_key_url(profile: AnonymousDeviceProfile) -> str:
    values = _query_values(profile)
    query = "&".join(f"{key}={values[key]}" for key in _REGISTER_KEY_ORDER)
    return f"{APP_ORIGIN}{REGISTER_KEY_PATH}?{query}"


def _batch_full_url(
    profile: AnonymousDeviceProfile,
    item_id: str,
    key_register_ts: int,
    book_id: str,
) -> str:
    values = _query_values(profile)
    values.update(
        key_register_ts=str(key_register_ts),
        item_ids=item_id,
        novel_text_type="0",
        req_type="0",
    )
    if book_id:
        values["book_id"] = book_id
    parts = [
        f"{key}={values[key]}"
        for key, _value in _COMMON_QUERY
        if key != "book_id" or book_id
    ]
    return f"{APP_ORIGIN}{BATCH_FULL_PATH}?{'&'.join(parts)}"


def _validate_url(url: str) -> None:
    parsed = urlsplit(url)
    if (
        parsed.scheme != "https"
        or parsed.hostname != APP_HOST
        or parsed.port not in {None, 443}
        or parsed.username is not None
        or parsed.password is not None
        or parsed.fragment
        or parsed.path not in {REGISTER_KEY_PATH, BATCH_FULL_PATH}
    ):
        raise AppProtocolTransientError("App protocol endpoint was rejected")


def _integer(value: object) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.lstrip("-").isdigit():
        return int(value)
    return None


def _response_message(payload: Mapping[str, Any]) -> str:
    candidates: list[object] = [
        payload.get("message"),
        payload.get("msg"),
        payload.get("status_msg"),
    ]
    data = payload.get("data")
    if isinstance(data, dict):
        candidates.extend([data.get("message"), data.get("msg")])
    return " ".join(value for value in candidates if isinstance(value, str)).lower()


def _explicit_device_rejection(payload: Mapping[str, Any]) -> bool:
    containers: list[Mapping[str, Any]] = [payload]
    data = payload.get("data")
    if isinstance(data, dict):
        containers.append(data)
    for container in containers:
        if (
            container.get("device_invalid") is True
            or container.get("need_device_register") is True
        ):
            return True
        status = container.get("device_status")
        if isinstance(status, str) and status.lower() in {
            "expired",
            "invalid",
            "unregistered",
        }:
            return True
    message = _response_message(payload)
    english = "device" in message and any(
        marker in message
        for marker in ("expired", "invalid", "not registered", "unregistered")
    )
    chinese = "设备" in message and any(
        marker in message for marker in ("失效", "无效", "未注册", "异常")
    )
    return _integer(payload.get("code")) not in {None, 0} and (english or chinese)


def _looks_like_base64_cipher(content: str) -> bool:
    return len(content) >= 32 and all(
        character.isalnum() or character in "+/=" for character in content[:80]
    )


class FanqieAppClient:
    """Async client for pinned ``registerkey`` and single-item ``batch_full``."""

    def __init__(
        self,
        client: httpx.AsyncClient | None = None,
        *,
        profile_path: Path | None = None,
        timeout: float = 30.0,
    ) -> None:
        if timeout <= 0:
            raise ValueError("timeout must be positive")
        self._profile_path = profile_path or _default_profile_path()
        try:
            self._profile = _load_or_initialize_profile(self._profile_path)
        except (OSError, ValueError):
            raise FanqieAppError(
                "App protocol profile could not be initialized"
            ) from None
        self._timeout = timeout
        self._client = client or httpx.AsyncClient(
            timeout=timeout,
            follow_redirects=False,
        )
        self._owns_client = client is None
        self._session: _SessionKey | None = None
        self._lock = asyncio.Lock()
        self._closed = False

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *_exc: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        self._session = None
        self._closed = True
        if self._owns_client:
            await self._client.aclose()

    def _headers(self, url: str, body: bytes | None) -> dict[str, str]:
        try:
            signed = sign_request(url, body)
        except Exception:
            raise AppProtocolTransientError("App protocol signing failed") from None
        if not isinstance(signed, dict):
            raise AppProtocolTransientError(
                "App protocol signer returned invalid headers"
            )
        normalized = {str(key).lower(): value for key, value in signed.items()}
        if not set(normalized) >= _REQUIRED_SIGNATURE_HEADERS:
            raise AppProtocolTransientError("App protocol signature set is incomplete")
        if body is not None and "x-ss-stub" not in normalized:
            raise AppProtocolTransientError("App protocol body signature is missing")
        if set(normalized) - _SIGNATURE_HEADERS or any(
            not isinstance(value, str) or "\r" in value or "\n" in value
            for value in normalized.values()
        ):
            raise AppProtocolTransientError(
                "App protocol signer returned invalid headers"
            )
        headers = dict(_FIXED_HEADERS)
        headers["x-tt-dt"] = self._profile.x_tt_dt
        headers.update(normalized)
        return headers

    async def _send(
        self,
        method: str,
        url: str,
        headers: Mapping[str, str],
        body: bytes | None,
    ) -> httpx.Response:
        try:
            request = self._client.build_request(
                method,
                url,
                headers=headers,
                content=body,
                timeout=self._timeout,
            )
            if str(request.url) != url:
                raise AppProtocolTransientError(
                    "App protocol request URL was normalized"
                )
            return await self._client.send(request, follow_redirects=False)
        except httpx.TimeoutException:
            raise AppProtocolTransientError("App protocol request timed out") from None
        except httpx.RequestError:
            raise AppProtocolTransientError(
                "App protocol network request failed"
            ) from None

    async def _request_json(
        self,
        operation: str,
        method: str,
        url: str,
        body: bytes | None = None,
    ) -> dict[str, Any]:
        _validate_url(url)
        if self._closed:
            raise AppProtocolTransientError("App protocol client is closed")
        headers = self._headers(url, body)
        response = await self._send(method, url, headers, body)
        _validate_url(str(response.url))
        logger.debug(
            "fanqie app response: operation=%s status=%d bytes=%d",
            operation,
            response.status_code,
            len(response.content),
        )
        if response.is_redirect:
            raise AppProtocolTransientError(
                "App protocol redirect was rejected"
            ) from None
        if response.status_code >= httpx.codes.INTERNAL_SERVER_ERROR:
            raise AppProtocolTransientError(
                f"App protocol returned HTTP {response.status_code}"
            ) from None
        try:
            raw_payload = response.json()
        except (ValueError, UnicodeError):
            raw_payload = None
        payload = raw_payload if isinstance(raw_payload, dict) else None
        if payload is not None and _explicit_device_rejection(payload):
            raise _DeviceProfileRejected("App protocol device profile was rejected")
        if response.status_code >= 400:
            raise AppProtocolTransientError(
                f"App protocol returned HTTP {response.status_code}"
            ) from None
        if payload is None:
            raise AppProtocolTransientError(
                "App protocol returned invalid JSON"
            ) from None
        return payload

    async def _register_key(self) -> _SessionKey:
        try:
            plaintext = build_register_plaintext(self._profile.device_id)
            iv = os.urandom(16)
            content = encrypt_register_content(plaintext, iv)
        except _CryptoError:
            raise AppProtocolTransientError(
                "App protocol registerkey encryption failed"
            ) from None
        body = (f'{{\n  "content" : "{content}",\n  "keyver" : 1001\n}}').encode(
            "ascii"
        )
        payload = await self._request_json(
            "registerkey",
            "POST",
            _register_key_url(self._profile),
            body,
        )
        if _integer(payload.get("code")) != 0:
            raise AppProtocolTransientError("App protocol registerkey was rejected")
        data = payload.get("data")
        if not isinstance(data, dict):
            raise AppProtocolTransientError(
                "App protocol registerkey response is incomplete"
            )
        key_blob = data.get("key")
        if not isinstance(key_blob, str) or not key_blob:
            raise AppProtocolTransientError(
                "App protocol registerkey response is incomplete"
            )
        try:
            key = unwrap_server_key(key_blob)
        except _CryptoError:
            raise AppProtocolTransientError(
                "App protocol registerkey decryption failed"
            ) from None
        key_version = _integer(data.get("keyver")) or 0
        key_register_ts = _integer(
            data.get("key_register_ts", data.get("keyRegisterTs"))
        )
        return _SessionKey(
            key=key,
            key_version=key_version,
            key_register_ts=key_register_ts or 0,
        )

    async def _batch_full(
        self,
        item_id: str,
        session: _SessionKey,
        book_id: str,
    ) -> dict[str, Any]:
        payload = await self._request_json(
            "batch_full",
            "GET",
            _batch_full_url(
                self._profile,
                item_id,
                session.key_register_ts,
                book_id,
            ),
        )
        code = _integer(payload.get("code"))
        if code not in {None, 0}:
            raise AppProtocolTransientError("App protocol batch_full was rejected")
        data = payload.get("data")
        if not isinstance(data, dict):
            raise AppChapterUnavailable("App protocol chapter data is missing")
        chapter = data.get(item_id)
        if not isinstance(chapter, dict):
            raise AppChapterUnavailable("App protocol chapter is missing")
        return chapter

    @staticmethod
    def _needs_key_refresh(chapter: Mapping[str, Any], session: _SessionKey) -> bool:
        key_version = _integer(chapter.get("key_version"))
        crypt_status = _integer(chapter.get("crypt_status")) or 0
        return (
            crypt_status != 0
            and key_version not in {None, 0}
            and key_version != session.key_version
        )

    @staticmethod
    def _chapter_text(chapter: Mapping[str, Any], session: _SessionKey) -> str:
        content = chapter.get("content")
        crypt_status = _integer(chapter.get("crypt_status")) or 0
        if not isinstance(content, str) or not content or content == "Invalid":
            raise AppChapterUnavailable("App protocol chapter is locked or empty")
        if crypt_status == 0 and not _looks_like_base64_cipher(content):
            text = extract_text(content)
        else:
            try:
                text = decrypt_chapter_text(content, session.key)
            except _CryptoError:
                raise AppProtocolTransientError(
                    "App protocol chapter decryption failed"
                ) from None
        if not text.strip():
            raise AppChapterUnavailable("App protocol chapter text is empty")
        return text

    def _reset_rejected_profile(self) -> None:
        self._session = None
        try:
            self._profile_path.unlink(missing_ok=True)
            self._profile = _initialize_capture_profile(self._profile_path)
        except OSError:
            raise AppProtocolTransientError(
                "App protocol profile could not be reset"
            ) from None

    async def _fetch_once(self, item_id: str, book_id: str) -> str:
        if self._session is None:
            self._session = await self._register_key()
        session = self._session
        chapter = await self._batch_full(item_id, session, book_id)
        if self._needs_key_refresh(chapter, session):
            fresh = await self._register_key()
            self._session = fresh
            chapter = await self._batch_full(item_id, fresh, book_id)
            if self._needs_key_refresh(chapter, fresh):
                raise AppProtocolTransientError(
                    "App protocol key version remained inconsistent"
                )
            session = fresh
        return self._chapter_text(chapter, session)

    async def fetch_chapter(self, item_id: str, *, book_id: str) -> str:
        """Fetch one chapter, with at most one device reset and one key refresh."""

        if _ITEM_ID_RE.fullmatch(item_id) is None:
            raise ValueError("invalid chapter ID")
        if _ITEM_ID_RE.fullmatch(book_id) is None:
            raise ValueError("invalid book ID")
        async with self._lock:
            try:
                return await self._fetch_once(item_id, book_id)
            except _DeviceProfileRejected:
                self._reset_rejected_profile()
                try:
                    return await self._fetch_once(item_id, book_id)
                except _DeviceProfileRejected:
                    raise AppProtocolTransientError(
                        "App protocol device profile remained invalid"
                    ) from None


__all__ = [
    "APP_HOST",
    "BATCH_FULL_PATH",
    "REFERENCE_COMMIT",
    "REFERENCE_REPOSITORY",
    "REGISTER_KEY_PATH",
    "AnonymousDeviceProfile",
    "AppChapterUnavailable",
    "AppProtocolTransientError",
    "FanqieAppClient",
    "FanqieAppError",
    "build_register_plaintext",
    "decrypt_chapter_bytes",
    "decrypt_chapter_text",
    "encrypt_register_content",
    "extract_text",
    "unwrap_server_key",
]
