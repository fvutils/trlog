"""Transparent metadata: typed attribute codec + trace-global BLK_META block.

Implements the transparent-metadata channel of design §4.5 / impl-plan §2.3: a
small **closed** set of value types (`bool, i64, u64, f64, string` + homogeneous
arrays) that any consumer can read *without the codec*. The encoding is shared
by all three attachment scopes:

* **trace-global** — a dedicated :class:`MetaBlock` (``BLK_META``), skippable,
  keeping the file header fixed (decision 1);
* **stream-type** — carried in the V2 type-registry entry (``_type_registry``);
* **stream-instance** — the ``H_ATTR2`` hierarchy tag (``_hierarchy``).

At this layer string values are represented by their **string-table id** (a
uvarint on the wire); the writer interns them and the reader query API resolves
them back to text. This keeps the codec self-contained (no string-table
dependency) and mirrors how legacy ``H_ATTR`` already stores ``value_str_id``.
"""

from __future__ import annotations

import struct
import zlib
from typing import List, Tuple

from ._codec import encode_uvarint, decode_uvarint, encode_svarint, decode_svarint
from ._string_table import _make_block
from ._types import (
    AttrType, TypedAttr, BlockType, FLAG_COMPRESSED, FLAG_COMPRESS_ALG,
)
from ._exceptions import ZstFormatError


# ---------------------------------------------------------------------------
# Typed value <-> bytes
# ---------------------------------------------------------------------------

def _scalar_base(attr_type: int) -> int:
    return attr_type & ~int(AttrType.AT_ARRAY)


def encode_typed_value(attr_type: int, value) -> bytes:
    """Encode a value of the given :class:`AttrType`. For ``AT_STR`` (and arrays
    of it) ``value`` must already be a string-table id (int)."""
    base = _scalar_base(attr_type)
    if attr_type & int(AttrType.AT_ARRAY):
        out = bytearray()
        out += encode_uvarint(len(value))
        for v in value:
            out += encode_typed_value(base, v)
        return bytes(out)
    if base == AttrType.AT_BOOL:
        return b"\x01" if value else b"\x00"
    if base == AttrType.AT_I64:
        return encode_svarint(int(value))
    if base == AttrType.AT_U64:
        return encode_uvarint(int(value))
    if base == AttrType.AT_F64:
        return struct.pack("<d", float(value))
    if base == AttrType.AT_STR:
        return encode_uvarint(int(value))   # string-table id
    raise ValueError(f"unknown transparent attr type 0x{attr_type:02x}")


def decode_typed_value(attr_type: int, buf, offset: int) -> Tuple[object, int]:
    base = _scalar_base(attr_type)
    if attr_type & int(AttrType.AT_ARRAY):
        count, offset = decode_uvarint(buf, offset)
        vals = []
        for _ in range(count):
            v, offset = decode_typed_value(base, buf, offset)
            vals.append(v)
        return vals, offset
    if base == AttrType.AT_BOOL:
        return (buf[offset] != 0), offset + 1
    if base == AttrType.AT_I64:
        return decode_svarint(buf, offset)
    if base == AttrType.AT_U64:
        return decode_uvarint(buf, offset)
    if base == AttrType.AT_F64:
        return struct.unpack_from("<d", buf, offset)[0], offset + 8
    if base == AttrType.AT_STR:
        return decode_uvarint(buf, offset)   # string-table id
    raise ValueError(f"unknown transparent attr type 0x{attr_type:02x}")


def encode_typed_attr(attr: TypedAttr) -> bytes:
    out = bytearray()
    out += encode_uvarint(attr.key_str_id)
    out.append(int(attr.attr_type))
    out += encode_typed_value(attr.attr_type, attr.value)
    return bytes(out)


def decode_typed_attr(buf, offset: int) -> Tuple[TypedAttr, int]:
    key_id, offset = decode_uvarint(buf, offset)
    attr_type = buf[offset]; offset += 1
    value, offset = decode_typed_value(attr_type, buf, offset)
    return TypedAttr(key_str_id=key_id, attr_type=attr_type, value=value), offset


def encode_typed_attr_list(attrs: List[TypedAttr]) -> bytes:
    out = bytearray()
    out += encode_uvarint(len(attrs))
    for a in attrs:
        out += encode_typed_attr(a)
    return bytes(out)


def decode_typed_attr_list(buf, offset: int) -> Tuple[List[TypedAttr], int]:
    count, offset = decode_uvarint(buf, offset)
    attrs: List[TypedAttr] = []
    for _ in range(count):
        a, offset = decode_typed_attr(buf, offset)
        attrs.append(a)
    return attrs, offset


# ---------------------------------------------------------------------------
# Python-value <-> AttrType inference (writer convenience)
# ---------------------------------------------------------------------------

def infer_attr_type(value) -> int:
    """Infer an :class:`AttrType` from a Python value (scalar or homogeneous
    list). ``str`` values must be interned to ids by the caller *after* this."""
    if isinstance(value, (list, tuple)):
        if not value:
            return int(AttrType.AT_ARRAY | AttrType.AT_STR)  # empty → array-of-str
        return int(AttrType.AT_ARRAY) | infer_attr_type(value[0])
    if isinstance(value, bool):
        return int(AttrType.AT_BOOL)
    if isinstance(value, int):
        return int(AttrType.AT_I64) if value < 0 else int(AttrType.AT_U64)
    if isinstance(value, float):
        return int(AttrType.AT_F64)
    if isinstance(value, str):
        return int(AttrType.AT_STR)
    raise TypeError(f"unsupported transparent-metadata value type: {type(value).__name__}")


# ---------------------------------------------------------------------------
# Trace-global metadata block (BLK_META)
# ---------------------------------------------------------------------------

class MetaBlock:
    """Trace-global transparent metadata, serialized as a skippable ``BLK_META``
    block (impl-plan §2.3, decision 1)."""

    def __init__(self) -> None:
        self.attrs: List[TypedAttr] = []

    def add(self, attr: TypedAttr) -> None:
        self.attrs.append(attr)

    def __bool__(self) -> bool:
        return bool(self.attrs)

    def encode_block(self, compress: bool = False) -> bytes:
        payload = encode_typed_attr_list(self.attrs)
        flags = 0
        if compress and payload:
            payload = zlib.compress(payload)
            flags |= FLAG_COMPRESSED
        return _make_block(BlockType.BLK_META, flags, payload)

    def read_block(self, payload: bytes | memoryview, flags: int = 0) -> None:
        if flags & FLAG_COMPRESSED:
            if flags & FLAG_COMPRESS_ALG:
                import zstandard as zstd  # type: ignore
                payload = zstd.ZstdDecompressor().decompress(bytes(payload))
            else:
                payload = zlib.decompress(bytes(payload))
        attrs, _ = decode_typed_attr_list(bytes(payload), 0)
        self.attrs.extend(attrs)
