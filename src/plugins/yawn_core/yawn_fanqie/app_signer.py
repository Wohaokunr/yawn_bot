"""Reading 7.2.1 iOS request signatures used by the Fanqie App protocol.

Protocol reference: ``https://github.com/ZreXoc/fanqie-rs`` commit
``906c6fd5744af0ef49e529102cdb64a250c067f7``.  Its ``Cargo.toml`` declares
the implementation MIT licensed.  This module is a pure-Python port pinned to
that source version; keep the reference and the golden-vector tests together
when updating it.

The query is deliberately sliced from the original URL without parsing.  Its
parameter order, empty fields, percent-escape spelling, and repeated keys are
part of the signature.  This module never logs request or device material.
"""

# The fixed protocol contains functions with several native input registers.
# ruff: noqa: PLR0913, PLR0917, PLR2004, TRY003

from __future__ import annotations

import base64
import hashlib
import secrets
import time
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass

from cryptography.hazmat.decrepit.ciphers.modes import OFB
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms

_U8_MASK = 0xFF
_U16_MASK = 0xFFFF
_U32_MASK = 0xFFFF_FFFF
_U64_MASK = 0xFFFF_FFFF_FFFF_FFFF
_I32_MAX = 0x7FFF_FFFF
_I32_MIN = -0x8000_0000
_I64_MAX = 0x7FFF_FFFF_FFFF_FFFF

_MAX_SIGNING_CLOCK_SKEW = 120
_GORGON_ENTROPY = 0x1EB0
_RUNTIME_PROPERTY = 0x0128
_GORGON_SDK_VERSION = 0x0409_0901

_SM3_IV = (
    0x7380_166F,
    0x4914_B2B9,
    0x1724_42D7,
    0xDA8A_0600,
    0xA96F_30BC,
    0x1631_38AA,
    0xE38D_EE4D,
    0xB0FB_0E4E,
)

_PACK_INITIAL_STATE = (
    0x5D27_E43D,
    0x8B32_83B3,
    0x01FD_0032,
    0x07A8_979A,
    0x55A6_881D,
    0x8EB2_1D59,
    0xA7D5_F6D5,
    0x259C_2840,
)
_PACK_SCHEDULE_SALT = bytes.fromhex("fa4561d7c275611a7fe36db35753be7b")
_PACK_PROFILE_REFERENCE_KHRONOS = 1_785_465_602
_PACK_ROUND_CONSTANTS = (
    0xED93EBFA,
    0xEAB73081,
    0xCC61EFF3,
    0x044E3FF8,
    0x42FF3FDB,
    0x8C28EF96,
    0x06B58E2D,
    0x0603AE0D,
    0x8CFADBFC,
    0x6961332C,
    0x30C371F3,
    0xF476D451,
    0x5594F7B5,
    0x2A13E85C,
    0xD1CF0495,
    0xDBC0A23D,
    0x01DFC28B,
    0x8372B436,
    0xEE1BDB3E,
    0x7133C03F,
    0x2280835A,
    0x9DB1A9B9,
    0x358CE2E3,
    0x8F234386,
    0x3A2C1AF3,
    0xB43B42DA,
    0x1489C5E8,
    0x1C6B20F7,
    0xA176DC48,
    0x6D3769B5,
    0xDBE1941C,
    0x580C1B19,
    0x5F38A998,
    0x08D05FB9,
    0x903850C2,
    0x13900C95,
    0x37FECAA2,
    0x5B06B81C,
    0x119EA213,
    0x6C8EECC1,
    0x742E19B8,
    0xA8F72E2E,
    0x8CDD34EB,
    0xCE069430,
    0xC0BDD6C5,
    0x9B68DA3F,
    0x2D33D23C,
    0x83FEEE13,
    0xAAB4435C,
    0xB14E8F49,
    0xAF383DBC,
    0x155D606D,
    0x895FF752,
    0x0C60A8A3,
    0xC2CEE1AA,
    0x577EC50F,
    0xC40D8D13,
    0xDED4B5F4,
    0x13BFC591,
    0xC28CF47A,
    0x2A1BDB9B,
    0x0A12E9C0,
    0x0E1F3E82,
    0xECDD6197,
)

_MEDUSA_SIGN_KEY = bytes.fromhex(
    "346b8093079e4282d0d0fc19e015226d29ed8e9dd62a0766830a823141224f42"
)
_MEDUSA_PACKET_PREFIX_MASKS = (
    0x0000_0005,
    0xCA4F_4B2D,
    0x430D_7549,
    0x2CAE_B53F,
    0x56CC_6D22,
)
_MEDUSA_BIT_LANES = (
    1 << 0,
    1 << 14,
    1 << 21,
    1 << 31,
    1 << 33,
    1 << 44,
    1 << 50,
    1 << 59,
)
_MEDUSA_BIT_LANE_MASK = 0
for _lane in _MEDUSA_BIT_LANES:
    _MEDUSA_BIT_LANE_MASK |= _lane

_TRANSFORM_IV = bytes.fromhex("c73a20452c238a5cc51b8d0525f2a31a")
_TRANSFORM_ROUND_KEYS = (
    bytes.fromhex("050a1d40ce6cae0df6bf9511ed9b03ad"),
    bytes.fromhex("209f13e8eef3bde5184c28f4f5d72b59"),
    bytes.fromhex("8b5dc72965ae7acc7de2523888357961"),
)
_TRANSFORM_SBOX = bytes.fromhex(
    "9d7a1f957fb98b86d7f5ebead1b28cda"
    "bd7469cd733477e8b10911b6242b8965"
    "17229a6214d8411e2e780bc2a0a45531"
    "81640401f7ab8d85ee255a7d484f560c"
    "7be468076a26db1ddf9750423a4560b7"
    "63b084d92d3dde75b52fbec3dce229fd"
    "b323c52ae0c7209c2c6b924bba51f438"
    "5f911b6fef57791c825d30bf1ac46637"
    "465e6ead27b4cb39ff1803c902701698"
    "a1dd138393d25ce6f1a6cca8c66c28ed"
    "bc08f9a305445854a9530dcac8fc67a5"
    "e36dd388cf5ba7f232439b8e80b84976"
    "10f80af30072069eceaeecd6d5e9e159"
    "7c87c0d0949f96c14d61f0bbfe52993e"
    "fa4ee7af3f19acf6478a3b0f350e1240"
    "3c8ffb7eaad471e5904a2133a215364c"
)
_TRANSFORM_SUBSTITUTE_PERMUTATION = (
    8,
    9,
    10,
    11,
    0,
    1,
    2,
    3,
    12,
    13,
    14,
    15,
    4,
    5,
    6,
    7,
)
_TRANSFORM_SHIFT_PERMUTATION = (
    0,
    9,
    14,
    15,
    4,
    13,
    2,
    7,
    8,
    1,
    6,
    3,
    12,
    5,
    10,
    11,
)

_MESSAGE_PROFILE_REFERENCE_MS = 1_785_411_604_000
_MESSAGE_PROFILE_WALL_OFFSET_MS = 894
_MESSAGE_ROOT_ID = bytes.fromhex("2d4b4fca49750d433fb5ae2c226dcc56")
_MESSAGE_ROOT_TOKEN = "AxDyxqn1mAqONJyY4CVna7kVs"
_MESSAGE_FIELD24_JSON = (
    '{"sts":32219,"kd":0,"fkd":514145325,"pd":-375617555,'
    '"lp":"2|520830913368|520830913368|2091399554238|53817867328",'
    '"fl":"0|0|0|0|0|0|0|0|0|0","dyn":"","do":0,"tk":true}'
)


@dataclass(frozen=True, slots=True)
class SignerDeviceProfile:
    """Device identifiers embedded by the pinned anonymous client profile."""

    device_id: str = "1378108152030395"
    helios_device_id: str = "442130837"
    aid: str = "1967"


@dataclass(frozen=True, slots=True)
class SignerNonces:
    """Four native callback values, exposed for deterministic verification."""

    helios: int
    medusa_root: int
    medusa_payload: int
    medusa_seed: int


DeviceProfileInput = SignerDeviceProfile | Mapping[str, str]
NonceInput = SignerNonces | Mapping[str, int] | Sequence[int]


def _u32(value: int) -> int:
    return value & _U32_MASK


def _u64(value: int) -> int:
    return value & _U64_MASK


def _ror32(value: int, rotation: int) -> int:
    rotation &= 31
    value &= _U32_MASK
    return ((value >> rotation) | (value << ((32 - rotation) & 31))) & _U32_MASK


def _ror64(value: int, rotation: int) -> int:
    rotation &= 63
    value &= _U64_MASK
    return ((value >> rotation) | (value << ((64 - rotation) & 63))) & _U64_MASK


def _ror8(value: int, rotation: int) -> int:
    rotation &= 7
    value &= _U8_MASK
    return ((value >> rotation) | (value << ((8 - rotation) & 7))) & _U8_MASK


def _as_i32(value: int) -> int:
    value &= _U32_MASK
    return value if value <= _I32_MAX else value - (_U32_MASK + 1)


def _require_i32(value: int, name: str) -> int:
    if not _I32_MIN <= value <= _I32_MAX:
        raise ValueError(f"{name} does not fit a signed 32-bit integer")
    return value


def _raw_query(url: str) -> bytes:
    query = url.partition("?")[2]
    return query.partition("#")[0].encode()


def _sm3_ff(round_number: int, x: int, y: int, z: int) -> int:
    if round_number < 16:
        return x ^ y ^ z
    return (x & y) | (x & z) | (y & z)


def _sm3_gg(round_number: int, x: int, y: int, z: int) -> int:
    if round_number < 16:
        return x ^ y ^ z
    return (x & y) | ((~x & _U32_MASK) & z)


def _sm3_p0(value: int) -> int:
    return value ^ _ror32(value, 23) ^ _ror32(value, 15)


def _sm3_p1(value: int) -> int:
    return value ^ _ror32(value, 17) ^ _ror32(value, 9)


def _sm3_digest(data: bytes) -> bytes:
    bit_length = _u64(len(data) * 8)
    message = bytearray(data)
    message.append(0x80)
    while len(message) % 64 != 56:
        message.append(0)
    message.extend(bit_length.to_bytes(8, "big"))

    state = list(_SM3_IV)
    for block_offset in range(0, len(message), 64):
        block = message[block_offset : block_offset + 64]
        schedule = [0] * 68
        for index in range(16):
            offset = index * 4
            schedule[index] = int.from_bytes(block[offset : offset + 4], "big")
        for index in range(16, 68):
            schedule[index] = _u32(
                _sm3_p1(
                    schedule[index - 16]
                    ^ schedule[index - 9]
                    ^ _ror32(schedule[index - 3], 17)
                )
                ^ _ror32(schedule[index - 13], 25)
                ^ schedule[index - 6]
            )

        a, b, c, d, e, f, g, h = state
        for round_number in range(64):
            constant = 0x79CC_4519 if round_number < 16 else 0x7A87_9D8A
            constant = _ror32(constant, -round_number)
            ss1 = _ror32(
                _u32(_ror32(a, 20) + e + constant),
                25,
            )
            ss2 = ss1 ^ _ror32(a, 20)
            tt1 = _u32(
                _sm3_ff(round_number, a, b, c)
                + d
                + ss2
                + (schedule[round_number] ^ schedule[round_number + 4])
            )
            tt2 = _u32(
                _sm3_gg(round_number, e, f, g) + h + ss1 + schedule[round_number]
            )
            d, c, b, a = c, _ror32(b, 23), a, tt1
            h, g, f, e = g, _ror32(f, 13), e, _sm3_p0(tt2)

        working = (a, b, c, d, e, f, g, h)
        state = [old ^ new for old, new in zip(state, working, strict=True)]

    return b"".join(value.to_bytes(4, "big") for value in state)


def _sm3_first_six(data: bytes) -> bytes:
    return _sm3_digest(data or bytes(16))[:6]


def _pack_selector(context: bytes) -> int:
    state = 0x2023_0928
    for offset in range(0, len(context), 2):
        first, second = context[offset : offset + 2]
        state = _u32((state >> 4) ^ (_u32(state << 6) ^ first) ^ state)
        bic = _u32(state << 12) & (~second & _U32_MASK)
        state = _u32(~(_u32(bic + second) ^ (state >> 7) ^ state))
    return state & 0x0F


def _small_sigma_zero(value: int) -> int:
    return _ror32(value, 7) ^ _ror32(value, 18) ^ (value >> 3)


def _small_sigma_one(value: int) -> int:
    return _ror32(value, 17) ^ _ror32(value, 19) ^ (value >> 10)


def _big_sigma_zero(value: int) -> int:
    return _ror32(value, 2) ^ _ror32(value, 13) ^ _ror32(value, 22)


def _big_sigma_one(value: int) -> int:
    return _ror32(value, 6) ^ _ror32(value, 11) ^ _ror32(value, 25)


def _pack_branch_twelve(context: bytes) -> bytes:
    schedule = [0] * 128
    for index in range(13):
        offset = index * 4
        schedule[index] = int.from_bytes(context[offset : offset + 4], "big")
    khronos = int.from_bytes(context[48:], "little")
    salt_rotation = _u32(khronos + 1) & 7
    word_rotation = _u32(khronos - _PACK_PROFILE_REFERENCE_KHRONOS) & 31
    rotated_salt = bytes(_ror8(byte, salt_rotation) for byte in _PACK_SCHEDULE_SALT)
    schedule[13] = int.from_bytes(rotated_salt[:4], "big")
    schedule[15] = 0x0000_01A0
    for index in range(16, len(schedule)):
        schedule[index] = _u32(
            schedule[index - 16]
            + _small_sigma_zero(schedule[index - 15])
            + schedule[index - 7]
            + _small_sigma_one(schedule[index - 2])
        )

    initial = tuple(_ror32(word, word_rotation) for word in _PACK_INITIAL_STATE)
    a, b, c, d, e, f, g, h = initial
    for round_number in range(98):
        choose = (h & (d ^ f)) ^ f
        majority = (b & c) | (a & (b | c))
        first = _u32(
            _big_sigma_one(h)
            + e
            + _ror32(
                _PACK_ROUND_CONSTANTS[(round_number + 55) & 63],
                word_rotation,
            )
            + schedule[(round_number + 119) & 127]
            + choose
        )
        next_f = _u32(first + _big_sigma_zero(b) + majority)
        next_h = _u32(first + g)
        a, b, c, d, e, f, g, h = h, a, b, c, d, next_f, f, next_h

    working = (a, b, c, d, e, f, g, h)
    state = tuple(_u32(value + initial[index]) for index, value in enumerate(working))
    folded = bytearray()
    for index in range(4):
        folded.extend((state[index] ^ state[index + 4]).to_bytes(4, "big"))

    checksum = 0x2022_0420
    for index, byte in enumerate(folded[:12]):
        if index & 1 == 0:
            checksum = _u32(checksum ^ _u32(checksum << 7) ^ byte ^ (checksum >> 3))
        else:
            checksum = _u32(
                ~((_u32(checksum << 11) | byte) ^ (checksum >> 5) ^ checksum)
            )
    suffix = _u32((checksum ^ 0x0100_0000) | 0x04)
    return bytes(folded) + suffix.to_bytes(4, "little")


def _pack_context(query: bytes, body: bytes | None, khronos: int) -> bytes:
    context = bytearray(52)
    context[:32] = _sm3_digest(query)
    if body is not None:
        context[32:48] = hashlib.md5(body).digest()
    context[48:] = khronos.to_bytes(4, "little")
    return bytes(context)


def _select_khronos(
    query: bytes,
    body: bytes | None,
    current_khronos: int,
) -> tuple[int, bytes]:
    for distance in range(_MAX_SIGNING_CLOCK_SKEW + 1):
        for candidate in (
            _u32(current_khronos + distance),
            _u32(current_khronos - distance),
        ):
            if candidate & 3 != 2:
                continue
            context = _pack_context(query, body, candidate)
            if _pack_selector(context) == 12:
                return candidate, context
    raise ValueError("no normalized Medusa branch within 120 seconds")


class _ProtobufWriter:
    def __init__(self) -> None:
        self._output = bytearray()

    def finish(self) -> bytes:
        return bytes(self._output)

    def raw_varint(self, value: int) -> None:
        value &= _U64_MASK
        while value >= 0x80:
            self._output.append((value & 0x7F) | 0x80)
            value >>= 7
        self._output.append(value)

    def tag(self, number: int, wire_type: int) -> None:
        self.raw_varint((number << 3) | wire_type)

    def varint(self, number: int, value: int) -> None:
        self.tag(number, 0)
        self.raw_varint(value)

    def bytes(self, number: int, value: bytes) -> None:
        self.tag(number, 2)
        self.raw_varint(len(value))
        self._output.extend(value)

    def string(self, number: int, value: str) -> None:
        self.bytes(number, value.encode())

    def message(self, number: int, value: bytes) -> None:
        self.bytes(number, value)


def _zigzag32(value: int) -> int:
    return ((value << 1) ^ (value >> 31)) & _U32_MASK


def _zigzag64(value: int) -> int:
    return ((value << 1) ^ (value >> 63)) & _U64_MASK


def _summary_message() -> bytes:
    out = _ProtobufWriter()
    out.varint(1, _zigzag32(314))
    out.varint(2, _zigzag32(5))
    out.varint(3, _zigzag32(694_367))
    out.varint(5, _zigzag32(884_933_294))
    return out.finish()


def _device_message(wall_clock_ms: int, profile: SignerDeviceProfile) -> bytes:
    out = _ProtobufWriter()
    out.varint(1, _zigzag32(1))
    out.varint(2, _zigzag32(2))
    out.string(3, profile.aid)
    out.string(4, profile.device_id)
    out.string(5, "AcPwBVngdSOFTah-ug5gK3JFX")
    out.string(6, "7.2.1")
    out.varint(7, _zigzag32(100))
    out.varint(8, _zigzag32(100))
    out.varint(10, _zigzag32(1))
    out.string(11, "wifi")
    out.string(12, "Asia/Shanghai,8")
    out.string(13, "zh-Hans-CN")
    out.varint(14, _zigzag32(12))
    out.string(15, "3840,2160")
    out.string(22, "26.5")
    out.varint(23, _zigzag32(100))
    out.varint(25, _zigzag64(1_780_280_707_782))
    out.varint(26, _zigzag64(1_785_411_514_385))
    out.varint(27, _zigzag64(1_784_082_506_552))
    out.varint(28, _zigzag64(wall_clock_ms + _MESSAGE_PROFILE_WALL_OFFSET_MS))
    out.varint(29, _zigzag32(-999_999))
    out.string(30, "Mac16,11")
    out.string(31, "arm64")
    out.string(37, "25.5.0")
    out.varint(40, _zigzag64(1_785_411_514_071))
    return out.finish()


def _process_message() -> bytes:
    out = _ProtobufWriter()
    out.varint(1, _zigzag64(1_785_411_520))
    out.varint(2, 3)
    out.varint(4, _zigzag32(200))
    return out.finish()


def _environment_message(wall_clock_ms: int, profile: SignerDeviceProfile) -> bytes:
    delta_ms = wall_clock_ms - _MESSAGE_PROFILE_REFERENCE_MS
    elapsed = _require_i32(90 + delta_ms // 1_000, "profile elapsed time")
    out = _ProtobufWriter()
    out.varint(1, _zigzag32(elapsed))
    out.varint(2, _zigzag32(694_367))
    out.varint(3, _zigzag32(694_367))
    out.varint(5, _zigzag32(694_367))
    out.varint(7, _zigzag32(46_617))
    out.string(8, "App Store")
    out.string(11, "655351128")
    out.message(12, _device_message(wall_clock_ms, profile))
    out.message(13, _process_message())
    out.varint(15, _zigzag32(47_577))
    out.varint(16, 46)
    out.varint(17, _zigzag32(494))
    out.varint(22, _zigzag32(694_367))
    out.varint(23, _zigzag32(694_367))
    return out.finish()


def _build_medusa_message(
    query: bytes,
    khronos: int,
    wall_clock_ms: int,
    root_nonce: int,
    pack: bytes,
    profile: SignerDeviceProfile,
) -> bytes:
    timestamp_zigzag = _zigzag32(_as_i32(khronos))
    out = _ProtobufWriter()
    out.bytes(1, _MESSAGE_ROOT_ID)
    out.varint(2, 10)
    out.varint(3, _zigzag32(_as_i32(root_nonce)))
    out.string(4, profile.aid)
    out.string(5, profile.device_id)
    out.string(6, profile.helios_device_id)
    out.string(7, "7.2.1")
    out.string(8, "v04.09.09-ml-iOS")
    out.varint(9, _zigzag32(67_700_993))
    out.bytes(10, bytes((0x28, 0x01, 0, 0, 0, 0, 0, 0)))
    out.varint(11, 1)
    out.varint(12, timestamp_zigzag)
    out.bytes(13, pack)
    out.bytes(14, _sm3_first_six(query))
    out.message(15, _summary_message())
    out.string(16, _MESSAGE_ROOT_TOKEN)
    out.varint(17, timestamp_zigzag)
    out.string(20, "none")
    out.varint(21, _zigzag32(369))
    out.message(23, _environment_message(wall_clock_ms, profile))
    out.string(24, _MESSAGE_FIELD24_JSON)
    return out.finish()


def _payload_key(nonce: int) -> bytes:
    material = _MEDUSA_SIGN_KEY + nonce.to_bytes(4, "little") + _MEDUSA_SIGN_KEY
    return _sm3_digest(material)


def _encode_payload(payload: bytes, key: bytes) -> bytes:
    padding = 16 - len(payload) % 16
    output = bytearray(payload + bytes((padding,)) * padding)
    round_keys = [
        int.from_bytes(key[offset : offset + 8], "little")
        for offset in range(24, -1, -8)
    ]
    for offset in range(0, len(output), 16):
        left = int.from_bytes(output[offset : offset + 8], "little")
        right = int.from_bytes(output[offset + 8 : offset + 16], "little")
        for round_key in round_keys:
            round_function = (_ror64(right, 60) & _ror64(right, 59)) ^ _ror64(right, 61)
            left, right = right, _u64(left ^ round_function ^ round_key)
        output[offset : offset + 8] = left.to_bytes(8, "little")
        output[offset + 8 : offset + 16] = right.to_bytes(8, "little")
    return bytes(output)


def _tail_hash(tail: bytes) -> int:
    state = 0
    for index, byte in enumerate(tail):
        if index & 1 == 0:
            mixed = _u32(state << 7) ^ byte
            mixed ^= state >> 3
            state = _u32(state ^ mixed)
        else:
            mixed = _u32(state << 11) | byte
            mixed ^= state >> 5
            mixed ^= state
            state = _u32(~mixed)
    return state


def _build_second_buffer(
    payload: bytes,
    runtime_property: int,
    payload_nonce: int,
    seed_nonce: int,
    seed_profile: int,
) -> bytearray:
    encoded = _encode_payload(payload, _payload_key(payload_nonce))
    reverse_source = runtime_property.to_bytes(8, "little") + encoded
    nonce_bytes = payload_nonce.to_bytes(4, "little")
    tail = nonce_bytes[2:]
    reverse_key = _tail_hash(tail).to_bytes(4, "big")
    intermediate = bytes(
        byte ^ reverse_key[index & 3]
        for index, byte in enumerate(reversed(reverse_source))
    )
    output = bytearray((0x4E,))
    output.extend(seed_nonce.to_bytes(4, "little"))
    output.append(0x01)
    output.extend(seed_profile.to_bytes(2, "little"))
    output.append(0x18)
    output.extend(intermediate)
    output.extend(tail)
    return output


def _permute_byte(value: int) -> int:
    return (
        (value & 1)
        | (((value >> 7) & 1) << 1)
        | (((value >> 1) & 1) << 2)
        | (((value >> 2) & 1) << 3)
        | (((value >> 3) & 1) << 4)
        | (((value >> 5) & 1) << 5)
        | (((value >> 6) & 1) << 6)
        | (((value >> 4) & 1) << 7)
    )


def _unpermute_byte(value: int) -> int:
    return (
        (value & 1)
        | (((value >> 2) & 1) << 1)
        | (((value >> 3) & 1) << 2)
        | (((value >> 4) & 1) << 3)
        | (((value >> 7) & 1) << 4)
        | (((value >> 5) & 1) << 5)
        | (((value >> 6) & 1) << 6)
        | (((value >> 1) & 1) << 7)
    )


def _scatter_byte(value: int) -> int:
    compact = _permute_byte(value)
    word = 0
    for bit, lane in enumerate(_MEDUSA_BIT_LANES):
        if compact & (1 << bit):
            word |= lane
    return word


def _compact_word(word: int) -> int:
    compact = 0
    for bit, lane in enumerate(_MEDUSA_BIT_LANES):
        if word & lane:
            compact |= 1 << bit
    return _unpermute_byte(compact)


def _extract_transform_input(second_buffer: bytearray) -> bytes:
    if len(second_buffer) < 240:
        raise ValueError("Medusa second buffer is too short")
    return bytes(
        _compact_word(
            int.from_bytes(second_buffer[index * 8 : index * 8 + 8], "little")
        )
        for index in range(30)
    )


def _patch_transform_output(second_buffer: bytearray, transformed: bytes) -> None:
    if len(second_buffer) < 240:
        raise ValueError("Medusa second buffer is too short")
    for index, byte in enumerate(transformed):
        offset = index * 8
        word = int.from_bytes(second_buffer[offset : offset + 8], "little")
        word = (word & (~_MEDUSA_BIT_LANE_MASK & _U64_MASK)) | _scatter_byte(byte)
        second_buffer[offset : offset + 8] = word.to_bytes(8, "little")


def _add_vm_round_key(state: bytearray, key: bytes) -> None:
    order = (1, 0, 2, 3)
    for group in range(4):
        for offset, key_offset in enumerate(order):
            state[group * 4 + offset] ^= key[group * 4 + key_offset]


def _add_final_round_key(state: bytearray) -> None:
    order = (0, 3, 1, 2)
    key = _TRANSFORM_ROUND_KEYS[1]
    for group in range(4):
        for offset, key_offset in enumerate(order):
            state[group * 4 + offset] ^= key[group * 4 + key_offset]


def _substitute_and_permute(state: bytearray) -> bytearray:
    return bytearray(
        _TRANSFORM_SBOX[state[index]] for index in _TRANSFORM_SUBSTITUTE_PERMUTATION
    )


def _shift_transform(state: bytearray) -> bytearray:
    return bytearray(state[index] for index in _TRANSFORM_SHIFT_PERMUTATION)


def _xtime(value: int) -> int:
    return ((value << 1) & _U8_MASK) ^ (0x1B if value & 0x80 else 0)


def _mix_column(column: Iterable[int]) -> tuple[int, int, int, int]:
    a, b, c, d = column
    return (
        _xtime(a) ^ _xtime(b) ^ b ^ c ^ d,
        a ^ _xtime(b) ^ _xtime(c) ^ c ^ d,
        a ^ b ^ _xtime(c) ^ _xtime(d) ^ d,
        _xtime(a) ^ a ^ b ^ c ^ _xtime(d),
    )


def _mix_transform(state: bytearray) -> bytearray:
    arranged = bytearray(state)
    for group in range(4):
        first = group * 4
        arranged[first], arranged[first + 1] = arranged[first + 1], arranged[first]
    mixed = bytearray(16)
    for column in range(4):
        result = _mix_column(arranged[column + row * 4] for row in range(4))
        for row in range(4):
            mixed[row * 4 + column] = result[row]
    output_order = (2, 1, 3, 0)
    return bytearray(
        mixed[(index // 4) * 4 + output_order[index & 3]] for index in range(16)
    )


def _encrypt_transform_block(block: bytes, chain: bytes) -> bytes:
    state = bytearray(left ^ right for left, right in zip(block, chain, strict=True))
    _add_vm_round_key(state, _TRANSFORM_ROUND_KEYS[0])
    state = _substitute_and_permute(state)
    state = _shift_transform(state)
    state = _mix_transform(state)
    _add_vm_round_key(state, _TRANSFORM_ROUND_KEYS[1])
    state = _substitute_and_permute(state)
    state = _shift_transform(state)
    _add_vm_round_key(state, _TRANSFORM_ROUND_KEYS[2])
    _add_final_round_key(state)
    return bytes(state)


def _apply_final_transform(transform_input: bytes) -> bytes:
    padded = transform_input + bytes((2, 2))
    first = _encrypt_transform_block(padded[:16], _TRANSFORM_IV)
    second = _encrypt_transform_block(padded[16:], first)
    return first + second


def _seed_profile_word(query: bytes, pack: bytes) -> int:
    return 0x1000 | ((_sm3_digest(query)[0] & 0x3F) << 6) | (pack[0] & 0x3F)


def _medusa_sign(
    message: bytes,
    khronos: int,
    payload_nonce: int,
    seed_nonce: int,
    seed_profile: int,
) -> str:
    second_buffer = _build_second_buffer(
        message,
        _RUNTIME_PROPERTY,
        payload_nonce,
        seed_nonce,
        seed_profile,
    )
    transformed = _apply_final_transform(_extract_transform_input(second_buffer))
    _patch_transform_output(second_buffer, transformed[:30])

    packet = bytearray()
    for mask in _MEDUSA_PACKET_PREFIX_MASKS:
        packet.extend((khronos ^ mask).to_bytes(4, "little"))
    packet.extend(payload_nonce.to_bytes(4, "little")[:2])
    packet.extend((0x01, 0x02))
    packet.extend(transformed[30:])
    packet.extend(second_buffer)
    return base64.b64encode(packet).decode("ascii")


def _reverse_bits(value: int) -> int:
    value = ((value & 0xF0) >> 4) | ((value & 0x0F) << 4)
    value = ((value & 0xCC) >> 2) | ((value & 0x33) << 2)
    return ((value & 0xAA) >> 1) | ((value & 0x55) << 1)


def _gorgon_sign(
    query: bytes,
    body: bytes | None,
    khronos: int,
) -> str:
    query_digest = hashlib.md5(query).digest()
    payload = bytearray(20)
    payload[:4] = query_digest[:4]
    if body is not None:
        payload[4:8] = hashlib.md5(body).digest()[:4]
    payload[12:16] = _GORGON_SDK_VERSION.to_bytes(4, "little")
    payload[16:20] = khronos.to_bytes(4, "big")

    entropy_bytes = _GORGON_ENTROPY.to_bytes(2, "little")
    property_bytes = _RUNTIME_PROPERTY.to_bytes(2, "little")
    key = (
        0x05,
        property_bytes[0],
        0x50,
        entropy_bytes[1],
        0x47,
        0x1E,
        property_bytes[1],
        entropy_bytes[0],
    )
    keybox = list(range(256))
    accumulator = 0
    for index in range(256):
        accumulator = (keybox[index] + accumulator + key[index & 7]) & _U8_MASK
        selected = keybox[accumulator]
        keybox[index] = selected
        keybox[accumulator] = selected

    index = 0
    accumulator = 0
    for offset in range(len(payload)):
        index = (index + 1) & _U8_MASK
        accumulator = (keybox[index] + accumulator) & _U8_MASK
        selected = keybox[accumulator]
        keybox[index] = selected
        keybox[accumulator] = selected
        payload[offset] ^= keybox[(keybox[index] + selected) & _U8_MASK]

    for offset in range(len(payload)):
        value = ((payload[offset] << 4) | (payload[offset] >> 4)) & _U8_MASK
        if offset + 1 < len(payload):
            value ^= payload[offset + 1]
        elif offset > 0:
            value ^= payload[0]
        payload[offset] = (~(_reverse_bits(value) ^ len(payload))) & _U8_MASK

    prefix = bytes(
        (
            0x84,
            0x04,
            entropy_bytes[0],
            entropy_bytes[1],
            property_bytes[0],
            property_bytes[1],
        )
    )
    return (prefix + payload).hex()


def _helios_sign(
    khronos: int,
    profile: SignerDeviceProfile,
    nonce: int,
) -> str:
    material = nonce.to_bytes(4, "little") + profile.aid.encode()
    key_material = hashlib.md5(material).hexdigest().encode("ascii")
    plaintext = f"{khronos}-{profile.helios_device_id}-{profile.aid}".encode()
    padding = 16 - len(plaintext) % 16
    padded = plaintext + bytes((padding,)) * padding
    encryptor = Cipher(
        algorithms.AES(key_material[:16]),
        OFB(key_material[16:]),
    ).encryptor()
    ciphertext = encryptor.update(padded) + encryptor.finalize()
    packet = nonce.to_bytes(4, "little") + ciphertext
    return base64.b64encode(packet).decode("ascii")


def _normalize_profile(profile: DeviceProfileInput | None) -> SignerDeviceProfile:
    if profile is None:
        normalized = SignerDeviceProfile()
    elif isinstance(profile, SignerDeviceProfile):
        normalized = profile
    else:
        defaults = SignerDeviceProfile()
        normalized = SignerDeviceProfile(
            device_id=profile.get("device_id", defaults.device_id),
            helios_device_id=profile.get(
                "helios_device_id",
                defaults.helios_device_id,
            ),
            aid=profile.get("aid", defaults.aid),
        )
    if not all(
        isinstance(value, str) and value
        for value in (
            normalized.device_id,
            normalized.helios_device_id,
            normalized.aid,
        )
    ):
        raise ValueError("signer device profile values must be non-empty strings")
    return normalized


def _normalize_nonces(nonces: NonceInput | None) -> SignerNonces:
    if nonces is None:
        normalized = SignerNonces(
            helios=secrets.randbits(31),
            medusa_root=secrets.randbits(31),
            medusa_payload=secrets.randbits(31),
            medusa_seed=secrets.randbits(31),
        )
    elif isinstance(nonces, SignerNonces):
        normalized = nonces
    elif isinstance(nonces, Mapping):
        try:
            normalized = SignerNonces(
                helios=nonces["helios"],
                medusa_root=nonces["medusa_root"],
                medusa_payload=nonces["medusa_payload"],
                medusa_seed=nonces["medusa_seed"],
            )
        except KeyError as exc:
            raise ValueError(f"missing signer nonce: {exc.args[0]}") from exc
    else:
        if len(nonces) != 4:
            raise ValueError("signer nonces must contain exactly four values")
        normalized = SignerNonces(*nonces)

    for value in (
        normalized.helios,
        normalized.medusa_root,
        normalized.medusa_payload,
        normalized.medusa_seed,
    ):
        if isinstance(value, bool) or not isinstance(value, int):
            raise TypeError("signer nonces must be integers")
        if not 0 <= value <= _U32_MASK:
            raise ValueError("signer nonces must fit unsigned 32-bit integers")
    return normalized


def sign_request(
    url: str,
    body: bytes | None = None,
    *,
    now_ms: int | None = None,
    nonces: NonceInput | None = None,
    device_profile: DeviceProfileInput | None = None,
) -> dict[str, str]:
    """Return the pinned client's six signature headers for one exact URL.

    ``now_ms`` and ``nonces`` exist for oracle comparison and deterministic
    tests.  Production callers should leave both unset.
    """

    if not isinstance(url, str):
        raise TypeError("url must be a string")
    if body is not None and not isinstance(body, bytes):
        raise TypeError("body must be bytes or None")
    if now_ms is None:
        wall_clock_ms = time.time_ns() // 1_000_000
    else:
        if isinstance(now_ms, bool) or not isinstance(now_ms, int):
            raise TypeError("now_ms must be an integer or None")
        wall_clock_ms = now_ms
    if not 0 <= wall_clock_ms <= _U64_MASK:
        raise ValueError("now_ms must fit an unsigned 64-bit integer")
    current_khronos = wall_clock_ms // 1_000
    if current_khronos > _U32_MASK:
        raise ValueError("now_ms seconds must fit an unsigned 32-bit integer")
    if wall_clock_ms > _I64_MAX:
        raise ValueError("now_ms must fit the signed Medusa profile clock")

    profile = _normalize_profile(device_profile)
    fixed_nonces = _normalize_nonces(nonces)
    query = _raw_query(url)
    khronos, context = _select_khronos(query, body, current_khronos)
    pack = _pack_branch_twelve(context)
    signing_wall_clock_ms = khronos * 1_000
    message = _build_medusa_message(
        query,
        khronos,
        signing_wall_clock_ms,
        fixed_nonces.medusa_root,
        pack,
        profile,
    )

    headers = {
        "X-Argus": base64.b64encode(khronos.to_bytes(4, "little")).decode("ascii"),
        "X-Gorgon": _gorgon_sign(query, body, khronos),
        "X-Helios": _helios_sign(khronos, profile, fixed_nonces.helios),
        "X-Khronos": str(khronos),
        "X-Ladon": "AAAAAA==",
        "X-Medusa": _medusa_sign(
            message,
            khronos,
            fixed_nonces.medusa_payload,
            fixed_nonces.medusa_seed,
            _seed_profile_word(query, pack),
        ),
    }
    if body is not None:
        headers["x-ss-stub"] = hashlib.md5(body).hexdigest().upper()
    return headers


__all__ = ["SignerDeviceProfile", "SignerNonces", "sign_request"]
