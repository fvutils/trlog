"""Byte-plane split transform + the core.bytesplit value-change codec.

The transform is Parquet's BYTE_STREAM_SPLIT (design §5.4): given ``n`` values of
``width`` bytes each, store all byte-0s together, then all byte-1s, … so the
downstream byte compressor sees homogeneous planes (the high-order bytes of
slowly-varying reals/wide vectors are nearly constant → long runs). It is a pure,
reversible transform that composes *under* byte compression — the
transform-then-compress layering this phase validates.

``CoreBytesplitCodec`` packages it as a self-contained value-change codec
(``org.fvutils.trlog.core.bytesplit``). Its block owns its own layout, applies
the split to fixed-width values, then zlib-compresses. The block is
**self-identifying**: with ``FLAG_STRUCT_CODEC`` set, the payload begins with the
interned codec-id string id, so the reader resolves and dispatches — and a reader
lacking the codec enumerates the block and skips it by length (§2.4/§2.8).
"""

from __future__ import annotations

import struct
import zlib
from typing import Dict, List, Tuple

from ._codec import encode_uvarint, decode_uvarint
from ._string_table import _make_block
from ._types import (
    BlockType, SignalEncoding, FLAG_STRUCT_CODEC,
)


# ---------------------------------------------------------------------------
# Pure transform
# ---------------------------------------------------------------------------

def byte_split(raw: bytes, width: int) -> bytes:
    """Scatter ``raw`` (n*width bytes) into ``width`` contiguous byte planes."""
    if width <= 1:
        return bytes(raw)
    if len(raw) % width != 0:
        raise ValueError(f"byte_split: length {len(raw)} not a multiple of width {width}")
    n = len(raw) // width
    out = bytearray(len(raw))
    for plane in range(width):
        base = plane * n
        src = plane
        for i in range(n):
            out[base + i] = raw[src]
            src += width
    return bytes(out)


def byte_unsplit(data: bytes, width: int) -> bytes:
    """Inverse of :func:`byte_split`."""
    if width <= 1:
        return bytes(data)
    if len(data) % width != 0:
        raise ValueError(f"byte_unsplit: length {len(data)} not a multiple of width {width}")
    n = len(data) // width
    out = bytearray(len(data))
    for plane in range(width):
        base = plane * n
        dst = plane
        for i in range(n):
            out[dst] = data[base + i]
            dst += width
    return bytes(out)


# ---------------------------------------------------------------------------
# Value <-> fixed-width little-endian bytes
# ---------------------------------------------------------------------------

_KIND_UINT = 0
_KIND_REAL = 1


def _value_width(encoding: int, bit_width: int) -> Tuple[int, int]:
    """Return ``(kind, byte_width)`` for a signal type, or raise if bytesplit
    cannot represent it (it only handles fixed-width numeric values)."""
    if encoding == SignalEncoding.SE_REAL:
        return _KIND_REAL, 8
    if encoding in (SignalEncoding.SE_2STATE,):
        bw = max(1, bit_width)
        return _KIND_UINT, (bw + 7) // 8
    raise ValueError(f"bytesplit does not support encoding {encoding!r}")


def _encode_value(kind: int, width: int, value) -> bytes:
    if kind == _KIND_REAL:
        return struct.pack("<d", float(value))
    return int(value).to_bytes(width, "little")


def _decode_value(kind: int, width: int, raw: bytes):
    if kind == _KIND_REAL:
        return struct.unpack("<d", raw)[0]
    return int.from_bytes(raw, "little")


# ---------------------------------------------------------------------------
# Self-contained bytesplit value-change block
# ---------------------------------------------------------------------------

class BytesplitBlock:
    """Accumulates fixed-width value-changes and encodes them with byte-plane
    split + zlib. One ``width`` (plane count) per variable is recorded as the
    codec param (design §5.4: "params record plane count")."""

    _FLAG_ZLIB = 0x01

    def __init__(self, start_time: int, sig_types: Dict[int, Tuple[int, int]],
                 compress: bool = True) -> None:
        self.start_time = start_time
        self.end_time = start_time
        self._sig_types = sig_types
        self._compress = compress
        self._changes: Dict[int, List[Tuple[int, object]]] = {}

    def add_change(self, var_id: int, time: int, value) -> None:
        self._changes.setdefault(var_id, []).append((time, value))
        if time > self.end_time:
            self.end_time = time

    def set_initial(self, var_id: int, value) -> None:
        self._changes.setdefault(var_id, []).insert(0, (self.start_time, value))

    def encode_block(self, codec_id_str: int) -> bytes:
        body = bytearray()
        body += encode_uvarint(self.start_time)
        body += encode_uvarint(len(self._changes))
        for var_id in sorted(self._changes):
            changes = self._changes[var_id]
            enc, bw = self._sig_types[var_id]
            kind, width = _value_width(int(enc), bw)
            body += encode_uvarint(var_id)
            body.append(kind)
            body += encode_uvarint(width)            # plane count param
            body += encode_uvarint(len(changes))
            prev_t = 0
            raw = bytearray()
            for t, v in changes:
                body += encode_uvarint(t - prev_t)   # time deltas (monotonic)
                prev_t = t
                raw += _encode_value(kind, width, v)
            body += byte_split(bytes(raw), width)

        internal_flags = 0
        payload_body = bytes(body)
        if self._compress and payload_body:
            payload_body = zlib.compress(payload_body)
            internal_flags |= self._FLAG_ZLIB

        payload = bytearray()
        payload += encode_uvarint(codec_id_str)      # self-identifying prefix
        payload += encode_uvarint(internal_flags)
        payload += payload_body
        return _make_block(BlockType.BLK_VC_DATA, FLAG_STRUCT_CODEC, bytes(payload))

    @staticmethod
    def decode_changes(payload: bytes, emit, vc_change_cls):
        """Parse a bytesplit VC payload (after the 10-byte block header) and push
        each reconstructed change to ``emit``. ``payload`` still carries the
        leading codec-id prefix; the caller has already matched it."""
        offset = 0
        _codec_id_str, offset = decode_uvarint(payload, offset)
        internal_flags, offset = decode_uvarint(payload, offset)
        body = payload[offset:]
        if internal_flags & BytesplitBlock._FLAG_ZLIB:
            body = zlib.decompress(bytes(body))
        body = bytes(body)
        o = 0
        start_time, o = decode_uvarint(body, o)
        n_vars, o = decode_uvarint(body, o)
        for _ in range(n_vars):
            var_id, o = decode_uvarint(body, o)
            kind = body[o]; o += 1
            width, o = decode_uvarint(body, o)
            count, o = decode_uvarint(body, o)
            times = []
            prev_t = 0
            for _ in range(count):
                d, o = decode_uvarint(body, o)
                prev_t += d
                times.append(prev_t)
            split = body[o:o + count * width]; o += count * width
            raw = byte_unsplit(split, width)
            for i, t in enumerate(times):
                v = _decode_value(kind, width, raw[i * width:(i + 1) * width])
                emit(vc_change_cls(var_id=var_id, time=t, value=v))
