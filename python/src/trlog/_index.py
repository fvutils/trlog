"""Index Block -- time-to-offset seek index for TRLOG files."""

from __future__ import annotations

import struct
from typing import List, Optional, Tuple

from ._codec import encode_uvarint, decode_uvarint
from ._string_table import _make_block
from ._types import BlockType, BLOCK_HEADER_SIZE


class IndexBlock:
    """Build and encode/decode a ``BLK_INDEX`` block.

    Stores sorted ``(start_time, end_time, file_offset)`` entries for both VC
    and Txn data blocks, plus per-variable first/last change times, and (in
    the extended format) per-stream ``stream_inst_id`` for each txn block
    entry to enable stream-level seek.  Also stores (in the extended format)
    the file offsets of the string-table, type-registry, and hierarchy blocks
    so the reader can open a file without a linear scan.
    """

    def __init__(self) -> None:
        self._vc_entries: List[Tuple[int, int, int]] = []   # (start, end, offset)
        # (start, end, offset, stream_inst_id) -- 4th element for per-stream index
        self._txn_entries: List[Tuple[int, int, int, int]] = []
        self._var_ranges: List[Tuple[int, int, int]] = []   # (var_id, first, last)
        # Metadata-block offsets (0 = absent)
        self.strtab_offset:    int = 0
        self.typereg_offset:   int = 0
        self.hier_offsets: List[int] = []

    def add_vc_entry(self, start_time: int, end_time: int, file_offset: int) -> None:
        self._vc_entries.append((start_time, end_time, file_offset))

    def add_txn_entry(
        self,
        start_time: int,
        end_time: int,
        file_offset: int,
        stream_inst_id: int = 0xFFFFFFFF,
    ) -> None:
        self._txn_entries.append((start_time, end_time, file_offset, stream_inst_id))

    def add_var_range(self, var_id: int, first_change: int, last_change: int) -> None:
        self._var_ranges.append((var_id, first_change, last_change))

    def encode_block(self) -> bytes:
        payload = self._encode_payload()
        return _make_block(BlockType.BLK_INDEX, 0, payload)

    def read_block(self, payload: bytes | memoryview, flags: int = 0) -> None:
        self._decode_payload(bytes(payload))

    def _encode_payload(self) -> bytes:
        out = bytearray()
        out += encode_uvarint(len(self._vc_entries))
        for s, e, off in self._vc_entries:
            out += struct.pack('<QQQ', s, e, off)
        out += encode_uvarint(len(self._txn_entries))
        for s, e, off, _sid in self._txn_entries:
            out += struct.pack('<QQQ', s, e, off)
        out += encode_uvarint(len(self._var_ranges))
        for var_id, first, last in self._var_ranges:
            out += encode_uvarint(var_id)
            out += struct.pack('<QQ', first, last)
        # Extended metadata-offset section (new files).  A leading sentinel byte
        # of 0x01 marks its presence so old decoders can detect and skip it.
        out += b'\x01'
        out += struct.pack('<QQ', self.strtab_offset, self.typereg_offset)
        out += encode_uvarint(len(self.hier_offsets))
        for ho in self.hier_offsets:
            out += struct.pack('<Q', ho)
        # Per-stream txn index section (sentinel 0x02).  Only written when at
        # least one entry carries a real stream_inst_id (not the 0xFFFFFFFF
        # default), so legacy files stay byte-identical.
        has_stream_ids = any(sid != 0xFFFFFFFF for _, _, _, sid in self._txn_entries)
        if has_stream_ids:
            out += b'\x02'
            out += encode_uvarint(len(self._txn_entries))
            for s, e, off, sid in self._txn_entries:
                out += encode_uvarint(sid)
                out += struct.pack('<QQQ', s, e, off)
        return bytes(out)

    def _decode_payload(self, data: bytes) -> None:
        offset = 0
        count, offset = decode_uvarint(data, offset)
        self._vc_entries = []
        for _ in range(count):
            s, e, off = struct.unpack_from('<QQQ', data, offset)
            offset += 24
            self._vc_entries.append((s, e, off))
        count, offset = decode_uvarint(data, offset)
        self._txn_entries = []
        for _ in range(count):
            s, e, off = struct.unpack_from('<QQQ', data, offset)
            offset += 24
            self._txn_entries.append((s, e, off, 0xFFFFFFFF))
        count, offset = decode_uvarint(data, offset)
        self._var_ranges = []
        for _ in range(count):
            var_id, offset = decode_uvarint(data, offset)
            first, last = struct.unpack_from('<QQ', data, offset)
            offset += 16
            self._var_ranges.append((var_id, first, last))
        # Extended section: present only when the next byte is 0x01
        self.strtab_offset  = 0
        self.typereg_offset = 0
        self.hier_offsets   = []
        if offset < len(data) and data[offset] == 0x01:
            offset += 1
            self.strtab_offset, self.typereg_offset = struct.unpack_from('<QQ', data, offset)
            offset += 16
            hier_count, offset = decode_uvarint(data, offset)
            for _ in range(hier_count):
                ho, = struct.unpack_from('<Q', data, offset)
                offset += 8
                self.hier_offsets.append(ho)
        # Per-stream txn index section (sentinel 0x02)
        if offset < len(data) and data[offset] == 0x02:
            offset += 1
            stream_count, offset = decode_uvarint(data, offset)
            stream_entries: List[Tuple[int, int, int, int]] = []
            for _ in range(stream_count):
                sid, offset = decode_uvarint(data, offset)
                s, e, off = struct.unpack_from('<QQQ', data, offset)
                offset += 24
                stream_entries.append((s, e, off, sid))
            # Overlay: replace the basic txn entries with the richer tuples.
            # The basic section is kept in the file for backward compat but
            # the in-memory representation now carries stream_inst_id.
            if stream_entries:
                self._txn_entries = stream_entries

    def find_vc_blocks(self, start: int, end: int) -> List[int]:
        """Return file offsets of VC blocks whose time range overlaps [start, end]."""
        return [off for s, e, off in self._vc_entries if e >= start and s <= end]

    def find_txn_blocks(self, start: int, end: int) -> List[int]:
        """Return file offsets of Txn blocks whose time range overlaps [start, end].

        This method ignores ``stream_inst_id``; use
        :meth:`find_txn_blocks_for_stream` to filter by stream.
        """
        return [off for s, e, off, _sid in self._txn_entries if e >= start and s <= end]

    def find_txn_blocks_for_stream(
        self,
        stream_inst_id: int,
        start: Optional[int] = None,
        end: Optional[int] = None,
    ) -> List[int]:
        """Return file offsets of Txn blocks for *stream_inst_id*.

        When *start* / *end* are given the result is further restricted to
        blocks whose time range overlaps ``[start, end]``.  When no entries
        carry per-stream information (all ``0xFFFFFFFF``), every entry is
        returned (the caller must fall back to record-level filtering).
        """
        all_unknown = all(sid == 0xFFFFFFFF for _, _, _, sid in self._txn_entries)
        result: List[int] = []
        for s, e, off, sid in self._txn_entries:
            if not all_unknown and sid != stream_inst_id:
                continue
            if start is not None and e < start:
                continue
            if end is not None and s > end:
                continue
            result.append(off)
        return result

    def seek_vc(self, time: int) -> int | None:
        """Return the file offset of the first VC block with start_time >= *time*."""
        for s, e, off in sorted(self._vc_entries):
            if s >= time:
                return off
        return None
