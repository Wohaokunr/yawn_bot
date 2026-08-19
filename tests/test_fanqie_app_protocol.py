from __future__ import annotations

import base64
import gzip
import importlib
import json
import logging
import sys
from pathlib import Path
from typing import TYPE_CHECKING, Any

import httpx
import nonebot
import pytest
from cryptography.hazmat.primitives import padding
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

PROJECT_ROOT = Path(__file__).resolve().parents[1]
_DEVICE_ID = "1378108152030395"
_IID = "1378108152034491"
_ITEM_ID = "7592456826415743550"
_BOOK_ID = "7601032456199736382"
_META_KEY = b"UmlsZTU1WTgwMjM4"
_TWICE = 2
_REGISTER_PACKET_BYTES = 48

if TYPE_CHECKING:
    from types import ModuleType


@pytest.fixture(scope="module")
def app_protocol() -> ModuleType:
    if str(PROJECT_ROOT) not in sys.path:
        sys.path.insert(0, str(PROJECT_ROOT))
    try:
        nonebot.get_driver()
    except ValueError:
        nonebot.init()
    if nonebot.get_plugin("yawn_core") is None:
        nonebot.load_from_toml("pyproject.toml")
    return importlib.import_module("src.plugins.yawn_core.yawn_fanqie.app_protocol")


def _encrypt_packet(plaintext: bytes, key: bytes, iv: bytes) -> str:
    padder = padding.PKCS7(128).padder()
    padded = padder.update(plaintext) + padder.finalize()
    encryptor = Cipher(algorithms.AES(key), modes.CBC(iv)).encryptor()
    ciphertext = encryptor.update(padded) + encryptor.finalize()
    return base64.b64encode(iv + ciphertext).decode("ascii")


def _key_blob(key: bytes, marker: int) -> str:
    return _encrypt_packet(key, _META_KEY, bytes([marker]) * 16)


def _chapter_blob(text: str, key: bytes, marker: int) -> str:
    compressed = gzip.compress(text.encode())
    return _encrypt_packet(compressed, key, bytes([marker]) * 16)


def _stub_sign_request(
    _url: str, body: bytes | None = None, **_kwargs: object
) -> dict[str, str]:
    headers = {
        "X-Argus": "argus",
        "X-Gorgon": "gorgon",
        "X-Helios": "helios",
        "X-Khronos": "1",
        "X-Ladon": "ladon",
        "X-Medusa": "medusa",
    }
    if body is not None:
        headers["x-ss-stub"] = "stub"
    return headers


@pytest.mark.asyncio
async def test_profile_bootstrap_is_atomic_and_strict(
    app_protocol: ModuleType,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    profile_path = tmp_path / "store" / "app-device-v1.json"
    replacements: list[tuple[Path, Path]] = []
    real_replace = app_protocol.os.replace

    def replace(source: str | Path, target: str | Path) -> None:
        source_path = Path(source)
        target_path = Path(target)
        replacements.append((source_path, target_path))
        real_replace(source_path, target_path)

    monkeypatch.setattr(app_protocol.os, "replace", replace)
    transport = httpx.MockTransport(
        lambda request: httpx.Response(500, request=request)
    )
    http_client = httpx.AsyncClient(transport=transport)
    client = app_protocol.FanqieAppClient(
        http_client,
        profile_path=profile_path,
    )

    assert len(replacements) == 1
    assert replacements[0][0].parent == replacements[0][1].parent
    assert replacements[0][1] == profile_path
    assert not list(profile_path.parent.glob("*.tmp"))
    raw = json.loads(profile_path.read_text(encoding="utf-8"))
    assert raw["schema_version"] == 1
    assert raw["reference_commit"].startswith("906c6fd")
    assert raw["device_id"] == _DEVICE_ID
    assert raw["iid"] == _IID
    assert app_protocol.AnonymousDeviceProfile.from_mapping(raw).device_id
    with pytest.raises(ValueError, match="schema"):
        app_protocol.AnonymousDeviceProfile.from_mapping({**raw, "extra": True})

    client._session = None
    raw["schema_version"] = 2
    profile_path.write_text(json.dumps(raw), encoding="utf-8")
    repaired = app_protocol.FanqieAppClient(
        http_client,
        profile_path=profile_path,
    )
    assert json.loads(profile_path.read_text(encoding="utf-8"))["schema_version"] == 1
    assert len(replacements) == _TWICE
    await client.aclose()
    await repaired.aclose()
    await http_client.aclose()


def test_aes_cbc_gzip_and_html_cleaning(app_protocol: ModuleType) -> None:
    key = b"chapter-key-1234"
    markup = (
        "<script>secret()</script><blk><b>第一段</b></blk>"
        "<blk>第二段 &amp; 更多</blk><style>.x{}</style>"
    )
    encrypted = _chapter_blob(markup, key, 7)

    assert app_protocol.decrypt_chapter_text(encrypted, key) == (
        "第一段\n第二段 & 更多"
    )
    assert app_protocol.unwrap_server_key(_key_blob(key, 8)) == key
    plaintext = app_protocol.build_register_plaintext(_DEVICE_ID)
    assert plaintext[:8] == int(_DEVICE_ID).to_bytes(8, "little")
    register_packet = app_protocol.encrypt_register_content(plaintext, bytes(range(16)))
    assert len(base64.b64decode(register_packet)) == _REGISTER_PACKET_BYTES


@pytest.mark.asyncio
async def test_official_host_and_redirect_are_enforced(
    app_protocol: ModuleType,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(app_protocol, "sign_request", _stub_sign_request)
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            302,
            headers={"Location": "https://example.test/collect"},
            request=request,
        )

    http_client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        follow_redirects=True,
    )
    client = app_protocol.FanqieAppClient(
        http_client,
        profile_path=tmp_path / "profile.json",
    )
    with pytest.raises(app_protocol.AppProtocolTransientError, match="redirect"):
        await client.fetch_chapter(_ITEM_ID, book_id=_BOOK_ID)

    assert len(requests) == 1
    assert requests[0].url.host == app_protocol.APP_HOST
    assert requests[0].url.path == app_protocol.REGISTER_KEY_PATH
    with pytest.raises(app_protocol.AppProtocolTransientError):
        app_protocol._validate_url("https://example.test/reading/crypt/registerkey")
    await http_client.aclose()


@pytest.mark.asyncio
async def test_device_rejection_resets_and_retries_only_once(
    app_protocol: ModuleType,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(app_protocol, "sign_request", _stub_sign_request)
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(
            401,
            json={"code": 1001, "message": "device not registered"},
            request=request,
        )

    http_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    client = app_protocol.FanqieAppClient(
        http_client,
        profile_path=tmp_path / "profile.json",
    )
    resets = 0
    real_initialize = app_protocol._initialize_capture_profile

    def initialize(path: Path) -> Any:
        nonlocal resets
        resets += 1
        return real_initialize(path)

    monkeypatch.setattr(app_protocol, "_initialize_capture_profile", initialize)
    with pytest.raises(
        app_protocol.AppProtocolTransientError,
        match="remained invalid",
    ):
        await client.fetch_chapter(_ITEM_ID, book_id=_BOOK_ID)

    assert calls == _TWICE
    assert resets == 1
    await http_client.aclose()


@pytest.mark.asyncio
async def test_key_version_refreshes_registerkey_and_batch_once(
    app_protocol: ModuleType,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(app_protocol, "sign_request", _stub_sign_request)
    first_key = b"first-key-123456"
    second_key = b"second-key-12345"
    register_calls = 0
    batch_calls = 0
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal batch_calls, register_calls
        requests.append(request)
        if request.url.path == app_protocol.REGISTER_KEY_PATH:
            register_calls += 1
            key = first_key if register_calls == 1 else second_key
            return httpx.Response(
                200,
                json={
                    "code": 0,
                    "data": {
                        "key": _key_blob(key, register_calls),
                        "keyver": register_calls,
                        "key_register_ts": register_calls * 11,
                    },
                },
                request=request,
            )
        batch_calls += 1
        key = first_key if batch_calls == 1 else second_key
        return httpx.Response(
            200,
            json={
                "code": 0,
                "data": {
                    _ITEM_ID: {
                        "content": _chapter_blob(
                            "<blk>第一段</blk><blk>第二段</blk>", key, 10 + batch_calls
                        ),
                        "crypt_status": 1,
                        "key_version": 2,
                    }
                },
            },
            request=request,
        )

    http_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    client = app_protocol.FanqieAppClient(
        http_client,
        profile_path=tmp_path / "profile.json",
    )
    assert await client.fetch_chapter(_ITEM_ID, book_id=_BOOK_ID) == "第一段\n第二段"

    assert register_calls == _TWICE
    assert batch_calls == _TWICE
    batch_requests = [
        request
        for request in requests
        if request.url.path == app_protocol.BATCH_FULL_PATH
    ]
    assert [request.url.params["key_register_ts"] for request in batch_requests] == [
        "11",
        "22",
    ]
    assert all(request.url.params["book_id"] == _BOOK_ID for request in batch_requests)
    assert all(request.url.host == app_protocol.APP_HOST for request in requests)
    await http_client.aclose()


@pytest.mark.asyncio
async def test_failures_do_not_rotate_profile_or_leak_sensitive_logs(
    app_protocol: ModuleType,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    monkeypatch.setattr(app_protocol, "sign_request", _stub_sign_request)
    profile_path = tmp_path / "profile.json"

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            503,
            json={
                "device_invalid": True,
                "message": f"sensitive {_DEVICE_ID} {_ITEM_ID}",
            },
            request=request,
        )

    http_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    client = app_protocol.FanqieAppClient(
        http_client,
        profile_path=profile_path,
    )
    resets = 0
    real_initialize = app_protocol._initialize_capture_profile

    def initialize(path: Path) -> Any:
        nonlocal resets
        resets += 1
        return real_initialize(path)

    monkeypatch.setattr(app_protocol, "_initialize_capture_profile", initialize)
    caplog.set_level(logging.DEBUG, logger=app_protocol.__name__)
    caplog.set_level(logging.INFO, logger="httpx")
    with pytest.raises(app_protocol.AppProtocolTransientError, match="HTTP 503"):
        await client.fetch_chapter(_ITEM_ID, book_id=_BOOK_ID)

    captured_logs = "\n".join(record.getMessage() for record in caplog.records)
    profile = json.loads(profile_path.read_text(encoding="utf-8"))
    assert resets == 0
    assert profile["device_id"] == _DEVICE_ID
    assert _DEVICE_ID not in captured_logs
    assert _IID not in captured_logs
    assert _ITEM_ID not in captured_logs
    assert profile["x_tt_dt"] not in captured_logs
    await client.aclose()
    await http_client.aclose()


@pytest.mark.asyncio
async def test_decryption_failure_does_not_rotate_profile(
    app_protocol: ModuleType,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(app_protocol, "sign_request", _stub_sign_request)
    key = b"chapter-key-1234"

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == app_protocol.REGISTER_KEY_PATH:
            return httpx.Response(
                200,
                json={
                    "code": 0,
                    "data": {
                        "key": _key_blob(key, 1),
                        "keyver": 1,
                        "key_register_ts": 11,
                    },
                },
                request=request,
            )
        return httpx.Response(
            200,
            json={
                "code": 0,
                "data": {
                    _ITEM_ID: {
                        "content": base64.b64encode(bytes(32)).decode(),
                        "crypt_status": 1,
                        "key_version": 1,
                    }
                },
            },
            request=request,
        )

    http_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    client = app_protocol.FanqieAppClient(
        http_client,
        profile_path=tmp_path / "profile.json",
    )
    resets = 0
    real_initialize = app_protocol._initialize_capture_profile

    def initialize(path: Path) -> Any:
        nonlocal resets
        resets += 1
        return real_initialize(path)

    monkeypatch.setattr(app_protocol, "_initialize_capture_profile", initialize)
    with pytest.raises(
        app_protocol.AppProtocolTransientError,
        match="decryption failed",
    ):
        await client.fetch_chapter(_ITEM_ID, book_id=_BOOK_ID)

    assert resets == 0
    await client.aclose()
    await http_client.aclose()
