"""Value Change Data Block — encode/decode BLK_VC_DATA.

The VcDataBlock collects signal changes over a time window and encodes them
using the per-signal-type wire format described in §9 of the spec.

Signal type info (encoding + bit_width) must be supplied via *sig_types*
(a dict mapping ``var_id → (SignalEncoding, bit_width)``).
"""

from __future__ import annotations

import hashlib
import struct
import zlib
from collections import defaultdict
from typing import Any, Dict, List, Optional, Tuple

from ._codec import encode_uvarint, decode_uvarint
from ._string_table import _make_block
from ._types import (
    BlockType, FLAG_COMPRESSED, FLAG_COMPRESS_ALG, FLAG_TIME_ZLIB, FLAG_WAVE_LZ4,
    FLAG_WAVE_XOR_DELTA, FLAG_WAVE_ZLIB, FLAG_SEEKABLE,
    BLOCK_HEADER_SIZE, SignalEncoding, VcChange,
)
from ._exceptions import ZstFormatError

# 9-state encoding codes
_9STATE_CODES = {0: 0, 1: 1, 'X': 2, 'Z': 3, 'H': 4, 'U': 5, 'W': 6, 'L': 7, '-': 8}
_9STATE_DECODE = {v: k for k, v in _9STATE_CODES.items()}


class VcDataBlock:
    """Collects value changes and encodes them as a ``BLK_VC_DATA`` block.

    Parameters
    ----------
    start_time:
        Inclusive start of this block's time range.
    sig_types:
        Mapping ``var_id → (SignalEncoding, bit_width)``.  Required for
        encoding.  May be omitted if only *read_block* will be called.
    compress:
        Whether to compress the block payload.
    """

    def __init__(
        self,
        start_time: int,
        sig_types: Optional[Dict[int, Tuple[SignalEncoding, int]]] = None,
        compress: bool = True,
        use_zstd: bool = False,
        compress_time_table: bool = False,
        compress_waves: bool = True,
        compress_waves_alg: str = "lz4",
        seekable: bool = False,
    ) -> None:
        self.start_time = start_time
        self.end_time   = start_time
        self._sig_types  = sig_types or {}
        self._compress   = compress
        self._use_zstd   = use_zstd
        self._compress_time_table = compress_time_table
        self._compress_waves = compress_waves
        self._compress_waves_alg = compress_waves_alg
        self._seekable = seekable
        # {var_id: [(time, value), ...]} — unsorted while accumulating
        self._changes: Dict[int, List[Tuple[int, Any]]] = defaultdict(list)
        # initial values: {var_id: value} set explicitly or from first change
        self._init_values: Dict[int, Any] = {}

    # ------------------------------------------------------------------
    # Builder API
    # ------------------------------------------------------------------

    def set_initial(self, var_id: int, value: Any) -> None:
        """Set the initial value for *var_id* at block start."""
        self._init_values[var_id] = value

    def add_change(self, var_id: int, time: int, value: Any) -> None:
        """Record a value change.  *time* must be >= *start_time*."""
        if time > self.end_time:
            self.end_time = time
        self._changes[var_id].append((time, value))
        # If no explicit initial is set, the first change serves as the initial
        if var_id not in self._init_values:
            self._init_values[var_id] = value

    # ------------------------------------------------------------------
    # Block serialisation
    # ------------------------------------------------------------------

    def encode_block(self) -> bytes:
        """Serialise to a complete ``BLK_VC_DATA`` block (with header)."""
        # FLAG_WAVE_XOR_DELTA is an encoding flag; always set.
        # FLAG_TIME_ZLIB / FLAG_WAVE_LZ4 are sub-section compression flags valid
        # only when the block is not whole-block compressed.
        sub_flags = FLAG_WAVE_XOR_DELTA
        if not self._compress:
            if self._compress_time_table:
                sub_flags |= FLAG_TIME_ZLIB
            if self._compress_waves:
                if self._compress_waves_alg == "zlib":
                    sub_flags |= FLAG_WAVE_ZLIB
                else:
                    sub_flags |= FLAG_WAVE_LZ4
            if self._seekable:
                sub_flags |= FLAG_SEEKABLE
        payload = self._encode_payload(sub_flags)
        flags = sub_flags
        if self._compress:
            if self._use_zstd:
                try:
                    import zstandard as zstd  # type: ignore
                    payload = zstd.ZstdCompressor().compress(payload)
                    flags |= FLAG_COMPRESSED | FLAG_COMPRESS_ALG
                except ImportError:
                    raise RuntimeError("zstandard is not installed")
            else:
                payload = zlib.compress(payload)
                flags |= FLAG_COMPRESSED
        return _make_block(BlockType.BLK_VC_DATA, flags, payload)

    def read_block(
        self,
        payload: bytes | memoryview,
        flags: int = 0,
        sig_types: Optional[Dict[int, Tuple[SignalEncoding, int]]] = None,
    ) -> List[VcChange]:
        """Decode a ``BLK_VC_DATA`` payload.

        Returns a flat list of :class:`VcChange` objects sorted by time.
        """
        if sig_types is not None:
            self._sig_types = sig_types

        if flags & FLAG_COMPRESSED:
            if flags & FLAG_COMPRESS_ALG:
                import zstandard as zstd  # type: ignore
                payload = zstd.ZstdDecompressor().decompress(payload)
            else:
                payload = zlib.decompress(payload)
            # Strip sub-section compression flags after whole-block decompression.
            # FLAG_WAVE_XOR_DELTA is an encoding flag and survives.
            flags &= ~(FLAG_COMPRESSED | FLAG_COMPRESS_ALG | FLAG_TIME_ZLIB | FLAG_WAVE_LZ4 | FLAG_WAVE_ZLIB | FLAG_SEEKABLE)

        return self._decode_payload(bytes(payload), flags)

    # ------------------------------------------------------------------
    # Internal encode
    # ------------------------------------------------------------------

    def _encode_payload(self, flags: int = 0) -> bytes:
        compress_time  = bool(flags & FLAG_TIME_ZLIB)
        compress_waves = bool(flags & (FLAG_WAVE_LZ4 | FLAG_WAVE_ZLIB))
        waves_alg = "zlib" if (flags & FLAG_WAVE_ZLIB) else "lz4"
        seekable = bool(flags & FLAG_SEEKABLE)

        # Build time table: sorted distinct timestamps
        all_times: List[int] = sorted({
            t for changes in self._changes.values() for t, _ in changes
        })
        time_to_idx: Dict[int, int] = {t: i for i, t in enumerate(all_times)}

        # Vars sorted ascending
        var_ids = sorted(set(self._changes) | set(self._init_values))

        # Build per-var raw wave bytes; deduplicate on raw content
        raw_wave: Dict[int, bytes] = {}
        for var_id in var_ids:
            changes = sorted(self._changes.get(var_id, []), key=lambda x: x[0])
            enc, bw = self._sig_types.get(var_id, (SignalEncoding.SE_2STATE, 1))
            raw_wave[var_id] = _encode_wave(changes, enc, bw, time_to_idx)

        # Dedup by raw content; build stored (possibly compressed) entries
        hash_to_stored: Dict[str, bytes] = {}   # raw_hash → stored bytes in wave_data
        hash_to_offset: Dict[str, int] = {}
        wave_data_parts: List[bytes] = []
        wave_offset = 0
        var_positions: Dict[int, int] = {}

        for var_id in var_ids:
            raw = raw_wave[var_id]
            if not raw:
                var_positions[var_id] = 0
                continue
            h = hashlib.md5(raw).hexdigest()
            if h in hash_to_offset:
                var_positions[var_id] = hash_to_offset[h]
            else:
                if compress_waves:
                    stored = _compress_wave_entry(raw, alg=waves_alg)
                else:
                    stored = raw
                hash_to_stored[h] = stored
                hash_to_offset[h] = wave_offset
                var_positions[var_id] = wave_offset
                wave_data_parts.append(stored)
                wave_offset += len(stored)

        wave_data = b''.join(wave_data_parts)

        # Serialise
        out = bytearray()
        # start_time / end_time
        out += struct.pack('<QQ', self.start_time, self.end_time)

        # Time table
        out += encode_uvarint(len(all_times))
        if compress_time and all_times:
            delta_bytes = bytearray()
            prev = self.start_time
            for t in all_times:
                delta_bytes += encode_uvarint(t - prev)
                prev = t
            compressed_deltas = zlib.compress(bytes(delta_bytes))
            out += encode_uvarint(len(compressed_deltas))
            out += compressed_deltas
        else:
            prev = self.start_time
            for t in all_times:
                out += encode_uvarint(t - prev)
                prev = t

        # Initial values
        out += encode_uvarint(len(var_ids))
        for var_id in var_ids:
            out += encode_uvarint(var_id)
            enc, bw = self._sig_types.get(var_id, (SignalEncoding.SE_2STATE, 1))
            init_val = self._init_values.get(var_id, _default_value(enc))
            out += _encode_value(enc, bw, init_val)

        # Position table
        pos_table_offset = len(out)
        use_4byte = wave_offset < 2**32
        out.append(4 if use_4byte else 8)
        pos_fmt = '<I' if use_4byte else '<Q'
        for var_id in var_ids:
            out += struct.pack(pos_fmt, var_positions[var_id])

        # Wave data
        out += encode_uvarint(len(wave_data))
        out += wave_data

        # Seekability footer: u64 giving byte offset of position table within payload
        if seekable:
            out += struct.pack('<Q', pos_table_offset)

        return bytes(out)

    def _decode_payload(self, data: bytes, flags: int = 0) -> List[VcChange]:
        decompress_time = bool(flags & FLAG_TIME_ZLIB)
        wave_lz4        = bool(flags & FLAG_WAVE_LZ4)
        wave_zlib       = bool(flags & FLAG_WAVE_ZLIB)
        wave_xor_delta  = bool(flags & FLAG_WAVE_XOR_DELTA)
        seekable        = bool(flags & FLAG_SEEKABLE)

        # If FLAG_SEEKABLE, the last 8 bytes are the position-table offset.
        # We strip them before parsing the rest of the payload.
        if seekable:
            data = data[:-8]

        offset = 0
        start_time, end_time = struct.unpack_from('<QQ', data, offset)
        offset += 16
        self.start_time = start_time
        self.end_time   = end_time

        # Time table
        time_count, offset = decode_uvarint(data, offset)
        times: List[int] = []
        if decompress_time and time_count > 0:
            compressed_len, offset = decode_uvarint(data, offset)
            delta_bytes = zlib.decompress(data[offset:offset + compressed_len])
            offset += compressed_len
            prev = start_time
            doff = 0
            for _ in range(time_count):
                delta, doff = decode_uvarint(delta_bytes, doff)
                prev += delta
                times.append(prev)
        else:
            prev = start_time
            for _ in range(time_count):
                delta, offset = decode_uvarint(data, offset)
                prev += delta
                times.append(prev)

        # Initial values
        init_count, offset = decode_uvarint(data, offset)
        var_ids: List[int] = []
        init_values: Dict[int, Any] = {}
        for _ in range(init_count):
            var_id, offset = decode_uvarint(data, offset)
            var_ids.append(var_id)
            enc, bw = self._sig_types.get(var_id, (SignalEncoding.SE_2STATE, 1))
            val, offset = _decode_value(enc, bw, data, offset)
            init_values[var_id] = val

        # Position table
        pos_entry_size = data[offset]; offset += 1
        pos_fmt = '<I' if pos_entry_size == 4 else '<Q'
        pos_size = 4 if pos_entry_size == 4 else 8
        positions: List[int] = []
        for _ in range(init_count):
            pos, = struct.unpack_from(pos_fmt, data, offset)
            positions.append(pos)
            offset += pos_size

        # Wave data
        wave_len, offset = decode_uvarint(data, offset)
        wave_data = data[offset:offset + wave_len]

        # Reconstruct changes
        changes: List[VcChange] = []
        for i, var_id in enumerate(var_ids):
            enc, bw = self._sig_types.get(var_id, (SignalEncoding.SE_2STATE, 1))
            pos = positions[i]
            if pos == 0 and not wave_data:
                changes.append(VcChange(var_id=var_id, time=start_time,
                                        value=init_values[var_id]))
                continue
            var_changes = _decode_wave(wave_data, pos, enc, bw, times,
                                       wave_lz4=wave_lz4,
                                       wave_xor_delta=wave_xor_delta,
                                       wave_zlib=wave_zlib)
            if var_changes:
                for tidx, val in var_changes:
                    changes.append(VcChange(var_id=var_id, time=times[tidx], value=val))
            else:
                changes.append(VcChange(var_id=var_id, time=start_time,
                                        value=init_values[var_id]))

        changes.sort(key=lambda c: (c.time, c.var_id))
        return changes


# ---------------------------------------------------------------------------
# Value encode/decode helpers (per-SignalEncoding, used in init table)
# ---------------------------------------------------------------------------

def _default_value(enc: SignalEncoding) -> Any:
    if enc in (SignalEncoding.SE_2STATE, SignalEncoding.SE_4STATE,
               SignalEncoding.SE_9STATE, SignalEncoding.SE_BITVEC):
        return 0
    if enc in (SignalEncoding.SE_REAL, SignalEncoding.SE_ANALOG):
        return 0.0
    return ""   # SE_STRING


def _encode_value(enc: SignalEncoding, bit_width: int, value: Any) -> bytes:
    """Encode a single initial-value entry."""
    if enc in (SignalEncoding.SE_2STATE, SignalEncoding.SE_BITVEC):
        nbytes = max(1, (bit_width + 7) // 8)
        return encode_uvarint(bit_width) + int(value).to_bytes(nbytes, 'big')
    if enc == SignalEncoding.SE_4STATE:
        # store as 2-bits-per-bit packed
        nbytes = max(1, (bit_width * 2 + 7) // 8)
        return encode_uvarint(bit_width) + _pack_4state(value, bit_width)
    if enc == SignalEncoding.SE_9STATE:
        nibble_bytes = max(1, (bit_width + 1) // 2)
        return encode_uvarint(bit_width) + _pack_9state(value, bit_width)
    if enc in (SignalEncoding.SE_REAL, SignalEncoding.SE_ANALOG):
        return struct.pack('<d', float(value))
    if enc == SignalEncoding.SE_STRING:
        raw = (value or "").encode('utf-8')
        return encode_uvarint(len(raw)) + raw
    return b''


def _decode_value(enc: SignalEncoding, bit_width: int, data: bytes, offset: int):
    """Decode a single initial-value entry.  Returns (value, new_offset)."""
    if enc in (SignalEncoding.SE_2STATE, SignalEncoding.SE_BITVEC):
        bw, offset = decode_uvarint(data, offset)
        nbytes = max(1, (bw + 7) // 8)
        val = int.from_bytes(data[offset:offset + nbytes], 'big')
        return val, offset + nbytes
    if enc == SignalEncoding.SE_4STATE:
        bw, offset = decode_uvarint(data, offset)
        nbytes = max(1, (bw * 2 + 7) // 8)
        val = _unpack_4state(data[offset:offset + nbytes], bw)
        return val, offset + nbytes
    if enc == SignalEncoding.SE_9STATE:
        bw, offset = decode_uvarint(data, offset)
        nbytes = max(1, (bw + 1) // 2)
        val = _unpack_9state(data[offset:offset + nbytes], bw)
        return val, offset + nbytes
    if enc in (SignalEncoding.SE_REAL, SignalEncoding.SE_ANALOG):
        val, = struct.unpack_from('<d', data, offset)
        return val, offset + 8
    if enc == SignalEncoding.SE_STRING:
        length, offset = decode_uvarint(data, offset)
        val = data[offset:offset + length].decode('utf-8')
        return val, offset + length
    return 0, offset


# ---------------------------------------------------------------------------
# Wave data encode/decode
# ---------------------------------------------------------------------------

def _encode_wave(
    changes: List[Tuple[int, Any]],
    enc: SignalEncoding,
    bit_width: int,
    time_to_idx: Dict[int, int],
) -> bytes:
    """Encode the change stream for one variable.

    SE_2STATE multi-bit signals use XOR-delta + RLE encoding:

        rle_group_count : uvarint
        for each group:
            (tidx_delta << 1) | 1 : uvarint   -- time-index delta from prev+1
            xor_delta             : uvarint   -- prev XOR current value
            repeat - 1            : uvarint   -- 0 = single; N = N+1 repeats,
                                                 each advancing by tidx_delta

    All other encoding types use count:uvarint followed by
    (time_index_delta, value) entries.
    """
    out = bytearray()
    if not changes:
        out += encode_uvarint(0)
        return bytes(out)

    prev_tidx = -1

    # -------------------------------------------------------------------
    # XOR-delta + RLE encoding for SE_2STATE multi-bit
    # -------------------------------------------------------------------
    if enc == SignalEncoding.SE_2STATE and bit_width > 1:
        prev_val = 0
        pairs: List[Tuple[int, int]] = []
        for time, value in changes:
            tidx   = time_to_idx[time]
            tdelta = tidx - (prev_tidx + 1)
            prev_tidx = tidx
            curr = int(value)
            pairs.append((tdelta, curr ^ prev_val))
            prev_val = curr
        # Collapse into RLE groups
        groups: List[Tuple[int, int, int]] = []   # (tidx_delta, xor_delta, count)
        run_td, run_xd = pairs[0]
        run_count = 1
        for td, xd in pairs[1:]:
            if td == run_td and xd == run_xd:
                run_count += 1
            else:
                groups.append((run_td, run_xd, run_count))
                run_td, run_xd = td, xd
                run_count = 1
        groups.append((run_td, run_xd, run_count))
        out += encode_uvarint(len(groups))
        for td, xd, count in groups:
            out += encode_uvarint((td << 1) | 1)
            out += encode_uvarint(xd)
            out += encode_uvarint(count - 1)
        return bytes(out)

    # -------------------------------------------------------------------
    # All other encoding types
    # -------------------------------------------------------------------
    out += encode_uvarint(len(changes))
    for time, value in changes:
        tidx = time_to_idx[time]
        tidx_delta = tidx - (prev_tidx + 1)
        prev_tidx = tidx

        if enc == SignalEncoding.SE_2STATE and bit_width == 1:
            v = int(value) & 1
            out += encode_uvarint((tidx_delta << 2) | (v << 1) | 1)
        elif enc == SignalEncoding.SE_4STATE:
            if bit_width == 1:
                # 4-state 1-bit: bit0=1 (validity), bits 2:1 = state_code (0-3), bits 3+ = delta
                # state: 0=bin0, 1=bin1, 2=X, 3=Z
                state = _4state_code(value)
                out += encode_uvarint((tidx_delta << 3) | (state << 1) | 1)
            else:
                # N-bit 4-state
                is_binary = _is_binary_4state(value, bit_width)
                out += encode_uvarint((tidx_delta << 1) | (1 if is_binary else 0))
                if is_binary:
                    nbytes = max(1, (bit_width + 7) // 8)
                    out += int(value).to_bytes(nbytes, 'big')
                else:
                    out += _pack_4state(value, bit_width)
        elif enc == SignalEncoding.SE_9STATE:
            is_binary = isinstance(value, int) and value >= 0
            out += encode_uvarint((tidx_delta << 1) | (1 if is_binary else 0))
            if is_binary:
                nbytes = max(1, (bit_width + 7) // 8)
                out += int(value).to_bytes(nbytes, 'big')
            else:
                out += _pack_9state(value, bit_width)
        elif enc in (SignalEncoding.SE_REAL, SignalEncoding.SE_ANALOG):
            out += encode_uvarint(tidx_delta)
            out += struct.pack('<d', float(value))
        elif enc == SignalEncoding.SE_STRING:
            out += encode_uvarint(tidx_delta)
            raw = (value or "").encode('utf-8')
            out += encode_uvarint(len(raw))
            out += raw
        else:
            # SE_BITVEC — same as N-bit 2-state
            nbytes = max(1, (bit_width + 7) // 8)
            out += encode_uvarint((tidx_delta << 1) | 1)
            out += int(value).to_bytes(nbytes, 'big')

    return bytes(out)


def _compress_wave_entry(raw: bytes, alg: str = "lz4") -> bytes:
    """Return a stored wave entry: compressed if smaller, else raw.

    With FLAG_WAVE_LZ4 or FLAG_WAVE_ZLIB set, each entry in wave_data starts
    with a flag byte:
      0x00  raw wave bytes follow
      0x01  orig_size:uvarint + compressed_len:uvarint + compressed bytes follow
    """
    if alg == "zlib":
        compressed = zlib.compress(raw)
    else:
        try:
            import lz4.block as _lz4
        except ImportError:
            return b'\x00' + raw
        compressed = _lz4.compress(raw, store_size=False)

    if len(compressed) + 2 < len(raw):   # +2 accounts for flag + rough overhead
        header = b'\x01' + encode_uvarint(len(raw)) + encode_uvarint(len(compressed))
        return header + compressed
    return b'\x00' + raw


def _decode_wave(
    wave_data: bytes,
    pos: int,
    enc: SignalEncoding,
    bit_width: int,
    times: List[int],
    wave_lz4: bool = False,
    wave_xor_delta: bool = False,
    wave_zlib: bool = False,
) -> List[Tuple[int, Any]]:
    """Decode the change stream at *pos* within *wave_data*."""
    offset = pos
    if wave_lz4 or wave_zlib:
        flag = wave_data[offset]; offset += 1
        if flag == 0x01:
            orig_size, offset = decode_uvarint(wave_data, offset)
            compressed_len, offset = decode_uvarint(wave_data, offset)
            if wave_zlib:
                raw = zlib.decompress(wave_data[offset:offset + compressed_len])
            else:
                import lz4.block as _lz4
                raw = _lz4.decompress(
                    wave_data[offset:offset + compressed_len],
                    uncompressed_size=orig_size,
                )
            if len(raw) != orig_size:
                raise ZstFormatError(
                    f"Wave decompression: expected {orig_size} bytes, got {len(raw)}"
                )
            return _decode_wave(raw, 0, enc, bit_width, times,
                                wave_lz4=False, wave_xor_delta=wave_xor_delta,
                                wave_zlib=False)
        # flag == 0x00: raw wave follows at current offset

    # -------------------------------------------------------------------
    # XOR-delta + RLE decode (SE_2STATE multi-bit)
    # -------------------------------------------------------------------
    if wave_xor_delta and enc == SignalEncoding.SE_2STATE and bit_width > 1:
        group_count, offset = decode_uvarint(wave_data, offset)
        result: List[Tuple[int, Any]] = []
        prev_tidx = -1
        prev_val  = 0
        for _ in range(group_count):
            header,     offset = decode_uvarint(wave_data, offset)
            tidx_delta         = header >> 1
            xor_delta,  offset = decode_uvarint(wave_data, offset)
            repeat_m1,  offset = decode_uvarint(wave_data, offset)
            count = repeat_m1 + 1
            for _ in range(count):
                tidx = prev_tidx + 1 + tidx_delta
                prev_tidx = tidx
                prev_val ^= xor_delta
                result.append((tidx, prev_val))
        return result

    # -------------------------------------------------------------------
    # All other encoding types
    # -------------------------------------------------------------------
    count, offset = decode_uvarint(wave_data, offset)
    result = []
    prev_tidx = -1

    for _ in range(count):
        if enc == SignalEncoding.SE_2STATE and bit_width == 1:
            header, offset = decode_uvarint(wave_data, offset)
            value = (header >> 1) & 1
            tidx_delta = header >> 2
            tidx = prev_tidx + 1 + tidx_delta
            prev_tidx = tidx
            result.append((tidx, value))
        elif enc == SignalEncoding.SE_2STATE:
            header, offset = decode_uvarint(wave_data, offset)
            tidx_delta = header >> 1
            tidx = prev_tidx + 1 + tidx_delta
            prev_tidx = tidx
            nbytes = max(1, (bit_width + 7) // 8)
            val = int.from_bytes(wave_data[offset:offset + nbytes], 'big')
            offset += nbytes
            result.append((tidx, val))
        elif enc == SignalEncoding.SE_4STATE:
            if bit_width == 1:
                # bit0=1, bits 2:1 = state_code, bits 3+ = tidx_delta
                header, offset = decode_uvarint(wave_data, offset)
                state = (header >> 1) & 0x3
                tidx_delta = header >> 3
                value = _4state_decode(state)
                tidx = prev_tidx + 1 + tidx_delta
                prev_tidx = tidx
                result.append((tidx, value))
            else:
                header, offset = decode_uvarint(wave_data, offset)
                all_binary = header & 1
                tidx_delta = header >> 1
                tidx = prev_tidx + 1 + tidx_delta
                prev_tidx = tidx
                if all_binary:
                    nbytes = max(1, (bit_width + 7) // 8)
                    val = int.from_bytes(wave_data[offset:offset + nbytes], 'big')
                    offset += nbytes
                else:
                    nbytes = max(1, (bit_width * 2 + 7) // 8)
                    val = _unpack_4state(wave_data[offset:offset + nbytes], bit_width)
                    offset += nbytes
                result.append((tidx, val))

        elif enc == SignalEncoding.SE_9STATE:
            header, offset = decode_uvarint(wave_data, offset)
            all_binary = header & 1
            tidx_delta = header >> 1
            tidx = prev_tidx + 1 + tidx_delta
            prev_tidx = tidx
            if all_binary:
                nbytes = max(1, (bit_width + 7) // 8)
                val = int.from_bytes(wave_data[offset:offset + nbytes], 'big')
                offset += nbytes
            else:
                nbytes = max(1, (bit_width + 1) // 2)
                val = _unpack_9state(wave_data[offset:offset + nbytes], bit_width)
                offset += nbytes
            result.append((tidx, val))

        elif enc in (SignalEncoding.SE_REAL, SignalEncoding.SE_ANALOG):
            tidx_delta, offset = decode_uvarint(wave_data, offset)
            tidx = prev_tidx + 1 + tidx_delta
            prev_tidx = tidx
            val, = struct.unpack_from('<d', wave_data, offset)
            offset += 8
            result.append((tidx, val))

        elif enc == SignalEncoding.SE_STRING:
            tidx_delta, offset = decode_uvarint(wave_data, offset)
            tidx = prev_tidx + 1 + tidx_delta
            prev_tidx = tidx
            length, offset = decode_uvarint(wave_data, offset)
            val = wave_data[offset:offset + length].decode('utf-8')
            offset += length
            result.append((tidx, val))

        else:
            # SE_BITVEC
            header, offset = decode_uvarint(wave_data, offset)
            tidx_delta = header >> 1
            tidx = prev_tidx + 1 + tidx_delta
            prev_tidx = tidx
            nbytes = max(1, (bit_width + 7) // 8)
            val = int.from_bytes(wave_data[offset:offset + nbytes], 'big')
            offset += nbytes
            result.append((tidx, val))

    return result


# ---------------------------------------------------------------------------
# 4-state / 9-state helpers
# ---------------------------------------------------------------------------

def _4state_code(value) -> int:
    """Map a 4-state Python value to wire code 0-3."""
    if isinstance(value, int):
        return value & 1
    s = str(value).upper()
    return {'0': 0, '1': 1, 'X': 2, 'Z': 3}.get(s, 0)


def _4state_decode(code: int):
    return {0: 0, 1: 1, 2: 'X', 3: 'Z'}.get(code, 0)


def _is_binary_4state(value, bit_width: int) -> bool:
    """True if all bits in *value* are 0 or 1 (no X/Z)."""
    return isinstance(value, int)


def _pack_4state(value, bit_width: int) -> bytes:
    """Pack a 4-state value into 2-bits-per-bit format."""
    nbytes = max(1, (bit_width * 2 + 7) // 8)
    result = bytearray(nbytes)
    for i in range(bit_width):
        bit_pos = bit_width - 1 - i
        if isinstance(value, int):
            bit_val = (value >> bit_pos) & 1
        else:
            bit_val = 2  # X
        byte_idx = (i * 2) // 8
        bit_shift = 6 - ((i * 2) % 8)
        result[byte_idx] |= (bit_val & 3) << bit_shift
    return bytes(result)


def _unpack_4state(data: bytes, bit_width: int):
    """Unpack a 2-bits-per-bit 4-state value."""
    # Check if all bits are binary (0 or 1)
    all_binary = True
    int_val = 0
    for i in range(bit_width):
        byte_idx = (i * 2) // 8
        bit_shift = 6 - ((i * 2) % 8)
        code = (data[byte_idx] >> bit_shift) & 3
        if code > 1:
            all_binary = False
        int_val = (int_val << 1) | (code & 1)
    if all_binary:
        return int_val
    # Return a list of per-bit states
    states = []
    for i in range(bit_width):
        byte_idx = (i * 2) // 8
        bit_shift = 6 - ((i * 2) % 8)
        code = (data[byte_idx] >> bit_shift) & 3
        states.append(_4state_decode(code))
    return states


def _pack_9state(value, bit_width: int) -> bytes:
    """Pack a 9-state value into 4-bits-per-bit (nibble) format."""
    nbytes = max(1, (bit_width + 1) // 2)
    result = bytearray(nbytes)
    for i in range(bit_width):
        if isinstance(value, int):
            bit_val = (value >> (bit_width - 1 - i)) & 1
            code = bit_val
        elif isinstance(value, (list, tuple)):
            v = value[i] if i < len(value) else 0
            code = _9STATE_CODES.get(v, 0)
        else:
            code = 0
        byte_idx = i // 2
        if i % 2 == 0:
            result[byte_idx] |= (code & 0xF) << 4
        else:
            result[byte_idx] |= (code & 0xF)
    return bytes(result)


def _unpack_9state(data: bytes, bit_width: int):
    """Unpack a 4-bits-per-bit 9-state value."""
    codes = []
    all_binary = True
    int_val = 0
    for i in range(bit_width):
        byte_idx = i // 2
        if i % 2 == 0:
            code = (data[byte_idx] >> 4) & 0xF
        else:
            code = data[byte_idx] & 0xF
        codes.append(code)
        if code > 1:
            all_binary = False
        int_val = (int_val << 1) | (code & 1)
    if all_binary:
        return int_val
    return [_9STATE_DECODE.get(c, c) for c in codes]
