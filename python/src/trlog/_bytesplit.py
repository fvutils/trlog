"""Byte-plane split transform and the block format of ``core.bytesplit``.

The transform is the same as Parquet's BYTE_STREAM_SPLIT: given ``n`` values
of ``width`` bytes each, it stores all byte 0s together, then all byte 1s, and
so on. A byte compressor applied afterwards sees homogeneous planes; the
high-order bytes of slowly varying reals and wide counters are nearly
constant and compress to long runs. The transform is reversible.

:class:`BytesplitBlock` is the block accumulator and encoder used by
:class:`trlog.codec.CoreBytesplitCodec` (``org.fvutils.trlog.core.bytesplit``).
Its blocks set ``FLAG_STRUCT_CODEC`` and start the payload with the interned
codec id, so a reader dispatches them by id and a reader without the codec
skips them by the block length.
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
    """Split ``raw`` into ``width`` byte planes.

    Args:
        raw: ``n * width`` bytes: ``n`` values of ``width`` bytes each.
        width: Bytes per value. 0 or 1 returns ``raw`` unchanged.

    Returns:
        Plane 0 (byte 0 of every value), then plane 1, and so on.

    Raises:
        ValueError: If ``len(raw)`` is not a multiple of ``width``.
    """
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
    """Inverse of :func:`byte_split`.

    Raises:
        ValueError: If ``len(data)`` is not a multiple of ``width``.
    """
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
    """Accumulates value changes and encodes one ``core.bytesplit`` block.

    Supports ``SE_REAL`` (8-byte IEEE double) and ``SE_2STATE`` signals
    (``ceil(bit_width / 8)`` bytes, little-endian unsigned). Other encodings
    raise ``ValueError`` at encode time.

    Payload layout (after the 10-byte block header; block type
    ``BLK_VC_DATA``, flags ``FLAG_STRUCT_CODEC``)::

        uvarint codec_id_str      string-table id of the codec id
        uvarint internal_flags    bit 0: body is zlib-compressed
        body:
          uvarint start_time
          uvarint var_count
          var_count x:
            uvarint var_id
            u8      kind          0 = unsigned int, 1 = real
            uvarint width         bytes per value (the plane count)
            uvarint count
            count x uvarint       time deltas (first from 0, then from
                                  the previous change)
            count*width bytes     byte_split() of the little-endian values

    Args:
        start_time: Block start time.
        sig_types: Map of var id to ``(SignalEncoding, bit_width)``.
        compress: zlib-compress the body.
    """

    _FLAG_ZLIB = 0x01

    def __init__(self, start_time: int, sig_types: Dict[int, Tuple[int, int]],
                 compress: bool = True) -> None:
        self.start_time = start_time
        self.end_time = start_time
        self._sig_types = sig_types
        self._compress = compress
        self._changes: Dict[int, List[Tuple[int, object]]] = {}

    def add_change(self, var_id: int, time: int, value) -> None:
        """Append a change. Changes of one variable must be in time order."""
        self._changes.setdefault(var_id, []).append((time, value))
        if time > self.end_time:
            self.end_time = time

    def set_initial(self, var_id: int, value) -> None:
        """Record ``value`` at the block start time, before any other change."""
        self._changes.setdefault(var_id, []).insert(0, (self.start_time, value))

    def encode_block(self, codec_id_str: int) -> bytes:
        """Encode the accumulated changes.

        Args:
            codec_id_str: String-table id of the codec id string.

        Returns:
            The complete block, header included.
        """
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
        """Decode a ``core.bytesplit`` payload and emit its changes.

        Args:
            payload: The block payload without the 10-byte header. It still
                starts with the codec-id prefix, which is skipped, not checked.
            emit: Called once per change.
            vc_change_cls: Class used to build each change, called as
                ``vc_change_cls(var_id=..., time=..., value=...)``.
        """
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
