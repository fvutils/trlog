"""Stateless varint and field encode/decode functions.

All functions are pure (no I/O).  They operate on ``bytes``, ``bytearray``,
or ``memoryview`` objects.
"""

from __future__ import annotations

import struct
from typing import Any, Tuple, Union

from ._types import FieldType

Buffer = Union[bytes, bytearray, memoryview]


# ---------------------------------------------------------------------------
# Unsigned LEB-128 varint
# ---------------------------------------------------------------------------

def encode_uvarint(n: int) -> bytes:
    """Encode a non-negative integer as an unsigned LEB-128 varint."""
    if n < 0:
        raise ValueError(f"encode_uvarint requires n >= 0, got {n}")
    out = bytearray()
    while True:
        byte = n & 0x7F
        n >>= 7
        if n:
            out.append(byte | 0x80)
        else:
            out.append(byte)
            break
    return bytes(out)


def decode_uvarint(buf: Buffer, offset: int = 0) -> Tuple[int, int]:
    """Decode an unsigned LEB-128 varint from *buf* starting at *offset*.

    Returns ``(value, new_offset)``.  Raises ``ValueError`` on truncated input.
    """
    result = 0
    shift = 0
    while True:
        if offset >= len(buf):
            raise ValueError("Truncated uvarint: buffer ended unexpectedly")
        byte = buf[offset]
        offset += 1
        result |= (byte & 0x7F) << shift
        if not (byte & 0x80):
            return result, offset
        shift += 7
        if shift >= 64:
            raise ValueError("uvarint too large (> 64 bits)")


# ---------------------------------------------------------------------------
# Signed zigzag-encoded varint
# ---------------------------------------------------------------------------

def encode_svarint(n: int) -> bytes:
    """Encode a signed integer as a zigzag-encoded unsigned LEB-128 varint."""
    encoded = (n << 1) ^ (n >> 63)
    return encode_uvarint(encoded & 0xFFFFFFFFFFFFFFFF)


def decode_svarint(buf: Buffer, offset: int = 0) -> Tuple[int, int]:
    """Decode a zigzag-encoded signed LEB-128 varint.

    Returns ``(value, new_offset)``.
    """
    n, offset = decode_uvarint(buf, offset)
    value = (n >> 1) ^ -(n & 1)
    return value, offset


# ---------------------------------------------------------------------------
# Field encode/decode per FieldType
# ---------------------------------------------------------------------------

_FMT = {
    FieldType.FT_U8:  ('<B', 1),
    FieldType.FT_U16: ('<H', 2),
    FieldType.FT_U32: ('<I', 4),
    FieldType.FT_U64: ('<Q', 8),
    FieldType.FT_I8:  ('<b', 1),
    FieldType.FT_I16: ('<h', 2),
    FieldType.FT_I32: ('<i', 4),
    FieldType.FT_I64: ('<q', 8),
    FieldType.FT_F32: ('<f', 4),
    FieldType.FT_F64: ('<d', 8),
    FieldType.FT_BOOL: ('<B', 1),
}


def encode_field(ft: FieldType, value: Any) -> bytes:
    """Encode *value* as field type *ft* and return the wire bytes."""
    if ft in _FMT:
        fmt, _ = _FMT[ft]
        if ft == FieldType.FT_BOOL:
            value = 1 if value else 0
        return struct.pack(fmt, value)

    if ft == FieldType.FT_STRING:
        encoded = value.encode('utf-8') if isinstance(value, str) else bytes(value)
        return encode_uvarint(len(encoded)) + encoded

    if ft == FieldType.FT_BITVEC:
        # value is (bit_width, int_value) tuple
        bit_width, int_value = value
        nbytes = (bit_width + 7) // 8
        raw = int_value.to_bytes(nbytes, 'big')
        return encode_uvarint(bit_width) + raw

    if ft == FieldType.FT_BYTES:
        raw = bytes(value)
        return encode_uvarint(len(raw)) + raw

    if ft == FieldType.FT_TIME:
        return struct.pack('<Q', value)

    if ft == FieldType.FT_ENUM:
        return encode_uvarint(value)

    raise ValueError(f"Unknown FieldType: {ft!r}")


def decode_field(ft: FieldType, buf: Buffer, offset: int = 0) -> Tuple[Any, int]:
    """Decode a field of type *ft* from *buf* at *offset*.

    Returns ``(value, new_offset)``.
    """
    if ft in _FMT:
        fmt, size = _FMT[ft]
        value, = struct.unpack_from(fmt, buf, offset)
        if ft == FieldType.FT_BOOL:
            value = bool(value)
        return value, offset + size

    if ft == FieldType.FT_STRING:
        length, offset = decode_uvarint(buf, offset)
        raw = bytes(buf[offset:offset + length])
        return raw.decode('utf-8'), offset + length

    if ft == FieldType.FT_BITVEC:
        bit_width, offset = decode_uvarint(buf, offset)
        nbytes = (bit_width + 7) // 8
        raw = bytes(buf[offset:offset + nbytes])
        int_value = int.from_bytes(raw, 'big')
        return (bit_width, int_value), offset + nbytes

    if ft == FieldType.FT_BYTES:
        length, offset = decode_uvarint(buf, offset)
        raw = bytes(buf[offset:offset + length])
        return raw, offset + length

    if ft == FieldType.FT_TIME:
        value, = struct.unpack_from('<Q', buf, offset)
        return value, offset + 8

    if ft == FieldType.FT_ENUM:
        return decode_uvarint(buf, offset)

    raise ValueError(f"Unknown FieldType: {ft!r}")
