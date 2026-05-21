"""TrlWriter — high-level writer for the TRLOG binary format."""

from __future__ import annotations

import io
import struct
import time as _time
from contextlib import contextmanager
from typing import Any, Dict, Iterator, List, Optional, Tuple, Union

from ._codec import encode_uvarint
from ._exceptions import ZstCompressionError
from ._hierarchy import HierarchyBlock
from ._index import IndexBlock
from ._string_table import StringTable, _make_block
from ._type_registry import TypeRegistry
from ._types import (
    BlockType, HierKind, SignalEncoding, VarDir, Radix, FieldType,
    FieldDef, ScopeType, TxnAttr, FLAG_COMPRESSED, BLOCK_HEADER_SIZE,
    WellKnownAttr,
)
from ._ext import ExtBlock
from ._vc_data import VcDataBlock
from ._txn_data import TxnDataBlock

# Magic bytes for BLK_FILE_HDR
TRL_MAGIC = b'TRL\x00\x00\x00\x01\x00'
TRL_VERSION_MAJOR = 2
TRL_VERSION_MINOR = 1

# Offset within the file where index_offset u64 lives (when creator/date str IDs = 0):
#   10 (block header) + 8 (magic) + 1+1+1 (version×2 + timescale) + 8 (timezero)
#   + 4 (flags) + 1+1 (creator+date str_ids each = 0 → 1 byte varint)
#   = 35
_INDEX_OFFSET_FIELD_POS = 35


def _encode_file_header(
    timescale_exp: int = -9,
    timezero: int = 0,
    creator_str_id: int = 0,
    date_str_id: int = 0,
    index_offset: int = 0,
) -> bytes:
    payload = bytearray()
    payload += TRL_MAGIC
    payload += struct.pack('BB', TRL_VERSION_MAJOR, TRL_VERSION_MINOR)
    payload += struct.pack('b', timescale_exp)           # signed byte
    payload += struct.pack('<q', timezero)               # i64 LE
    payload += struct.pack('<I', 0)                      # flags = 0
    payload += encode_uvarint(creator_str_id)
    payload += encode_uvarint(date_str_id)
    payload += struct.pack('<Q', index_offset)           # u64 LE
    payload += b'\x00' * 16                              # reserved
    return _make_block(BlockType.BLK_FILE_HDR, 0, bytes(payload))


class _HierarchyContext:
    """Returned by :meth:`TrlWriter.begin_hierarchy`; acts as a context manager."""

    def __init__(
        self,
        writer: 'TrlWriter',
        hier_id: int,
        kind: HierKind,
        name: str,
    ) -> None:
        self._writer = writer
        self._blk = HierarchyBlock(
            hier_id=hier_id, kind=kind,
            name_str_id=writer._strtab.intern(name),
        )
        self._flushed = False
        self._scope_stack: List[int] = []  # stack of scope_ids

    # Forward hierarchy builder methods
    def begin_scope(
        self,
        scope_type: ScopeType,
        name: str,
        component: str = "",
        src_file: str = "",
        src_line: int = 0,
    ) -> None:
        src_file_id = self._writer._strtab.intern(src_file) if src_file else 0
        self._blk.begin_scope(
            scope_type,
            self._writer._strtab.intern(name),
            self._writer._strtab.intern(component),
            src_file_str_id=src_file_id,
            src_line=src_line,
        )
        # Assign a new scope_id and push onto the stack
        self._writer._scope_id_counter += 1
        self._scope_stack.append(self._writer._scope_id_counter)

    def end_scope(self) -> None:
        self._blk.end_scope()
        if self._scope_stack:
            self._scope_stack.pop()

    def add_var(
        self,
        name: str,
        sig_type_id: int,
        direction: VarDir = VarDir.VD_IN,
        src_file: str = "",
        src_line: int = 0,
        driver_file: str = "",
        driver_line: int = 0,
    ) -> int:
        name_id     = self._writer._strtab.intern(name)
        src_file_id = self._writer._strtab.intern(src_file) if src_file else 0
        var_id = self._blk.add_var(
            name_id, sig_type_id, direction,
            src_file_str_id=src_file_id,
            src_line=src_line,
        )
        if sig_type_id in self._writer._sig_types:
            self._writer._var_types[var_id] = self._writer._sig_types[sig_type_id]
        # Record which scope this var belongs to
        if self._scope_stack:
            self._writer._var_scope[var_id] = self._scope_stack[-1]
        if driver_file:
            self._blk.add_attr(
                self._writer._strtab.intern(WellKnownAttr.DRIVER_FILE),
                self._writer._strtab.intern(driver_file),
            )
            self._blk.add_attr(
                self._writer._strtab.intern(WellKnownAttr.DRIVER_LINE),
                self._writer._strtab.intern(str(driver_line)),
            )
        return var_id

    def add_stream(
        self,
        stream_type_id: int,
        name: str,
        src_file: str = "",
        src_line: int = 0,
    ) -> int:
        name_id = self._writer._strtab.intern(name)
        inst_id = self._blk.add_stream(stream_type_id, name_id)
        if src_file:
            self._blk.add_attr(
                self._writer._strtab.intern(WellKnownAttr.SRC_FILE),
                self._writer._strtab.intern(src_file),
            )
            self._blk.add_attr(
                self._writer._strtab.intern(WellKnownAttr.SRC_LINE),
                self._writer._strtab.intern(str(src_line)),
            )
        return inst_id

    def add_attr(self, key: str, value: Any) -> None:
        key_id = self._writer._strtab.intern(key)
        val_id = self._writer._strtab.intern(str(value))
        self._blk.add_attr(key_id, val_id)

    def flush(self) -> None:
        if not self._flushed:
            self._writer._flush_hierarchy(self._blk)
            self._flushed = True

    def __enter__(self) -> '_HierarchyContext':
        return self

    def __exit__(self, *args) -> None:
        self.flush()


class _VcBlockContext:
    """Returned by :meth:`TrlWriter.begin_vc_block`; acts as a context manager."""

    def __init__(self, writer: 'TrlWriter', start_time: int) -> None:
        self._writer = writer
        # Use var_id → (enc, bw) so VcDataBlock can resolve types for any declared variable.
        # Fall back to _sig_types (keyed by sig_type_id) for compatibility with callers that
        # pass var_ids equal to their sig_type_ids (fallback when one type per var).
        sig_types = dict(writer._sig_types)   # sig_type_id → (enc, bw) as baseline
        sig_types.update(writer._var_types)   # var_id → (enc, bw) overrides / adds
        self._blk = VcDataBlock(
            start_time=start_time,
            sig_types=sig_types,
            compress=writer._compress,
            compress_time_table=writer._compress_time_table,
            compress_waves=writer._compress_waves,
            compress_waves_alg=writer._compress_waves_alg,
            seekable=writer._seekable,
        )

    def add_change(self, var_id: int, time: int, value: Any) -> None:
        self._blk.add_change(var_id, time, value)

    def set_initial(self, var_id: int, value: Any) -> None:
        self._blk.set_initial(var_id, value)

    def flush(self) -> None:
        self._writer._flush_vc(self._blk)

    def __enter__(self) -> '_VcBlockContext':
        return self

    def __exit__(self, *args) -> None:
        self.flush()


class _TxnBlockContext:
    """Returned by :meth:`TrlWriter.begin_txn_block`; acts as a context manager."""

    def __init__(
        self,
        writer: 'TrlWriter',
        start_time: int,
        stream_inst_id: Optional[int] = None,
    ) -> None:
        self._writer = writer
        self._stream_inst_id = stream_inst_id
        self._blk = TxnDataBlock(start_time=start_time, compress=writer._compress,
                                  column_layout=writer._column_layout)
        self._blk._delta_encode = writer._delta_encode_txn
        for txn_type_id, fts in writer._txn_schemas.items():
            self._blk.set_schema(txn_type_id, fts)

    def write_full(
        self,
        stream_inst_id,
        txn_type_id,
        txn_id,
        start,
        end,
        parent,
        attrs=None,
        origin_file: str = "",
        origin_line: int = 0,
        origin_component: str = "",
    ):
        self._blk.write_full(stream_inst_id, txn_type_id, txn_id, start, end, parent,
                              attrs or [])
        self._write_origin_meta(txn_id, origin_file, origin_line, origin_component)

    def write_begin(
        self,
        stream_inst_id,
        txn_type_id,
        txn_id,
        start,
        parent=0,
        origin_file: str = "",
        origin_line: int = 0,
        origin_component: str = "",
    ):
        self._blk.write_begin(stream_inst_id, txn_type_id, txn_id, start, parent)
        self._write_origin_meta(txn_id, origin_file, origin_line, origin_component)

    def write_attr(self, txn_id, attrs):
        self._blk.write_attr(txn_id, attrs)

    def write_end(self, txn_id, end_time):
        self._blk.write_end(txn_id, end_time)

    def write_link(self, link_type, src, tgt, label_id=0):
        self._blk.write_link(link_type, src, tgt, label_id)

    def write_meta(self, txn_id: int, key: str, value: str) -> None:
        """Attach a string key/value metadata pair to a transaction (TR_META)."""
        key_id = self._writer._strtab.intern(key)
        val_id = self._writer._strtab.intern(value)
        self._blk.write_meta(txn_id, key_id, val_id)

    def _write_origin_meta(
        self,
        txn_id: int,
        origin_file: str,
        origin_line: int,
        origin_component: str,
    ) -> None:
        """Emit TR_META records for origin info when non-empty."""
        if origin_file:
            self.write_meta(txn_id, WellKnownAttr.ORIGIN_FILE, origin_file)
            self.write_meta(txn_id, WellKnownAttr.ORIGIN_LINE, str(origin_line))
        if origin_component:
            self.write_meta(txn_id, WellKnownAttr.ORIGIN_COMPONENT, origin_component)

    def flush(self) -> None:
        self._writer._flush_txn(self._blk, stream_inst_id=self._stream_inst_id)

    def __enter__(self) -> '_TxnBlockContext':
        return self

    def __exit__(self, *args) -> None:
        self.flush()


class TrlWriter:
    """Write a TRLOG trace file.

    Usage::

        with TrlWriter("out.trl", timescale_exp=-9) as w:
            sig_t = w.add_signal_type(SignalEncoding.SE_2STATE, 1)
            with w.begin_hierarchy() as h:
                h.begin_scope(ScopeType.ST_MODULE, "top")
                clk = h.add_var("clk", sig_t)
                h.end_scope()
            with w.begin_vc_block(0) as vc:
                vc.add_change(clk, 5, 1)
                vc.add_change(clk, 10, 0)
    """

    def __init__(
        self,
        path_or_file,
        timescale_exp: int = -9,
        timezero: int = 0,
        compress: bool = True,
        compressor: str = "zlib",
        compress_time_table: bool = False,
        compress_waves: bool = True,
        compress_waves_alg: str = "lz4",
        seekable: bool = False,
        scope_grouped: bool = False,
        column_layout: str = "auto",
    ) -> None:
        self._compress = compress
        self._compressor = compressor
        self._compress_time_table = compress_time_table
        self._compress_waves = compress_waves
        self._compress_waves_alg = compress_waves_alg
        self._seekable = seekable
        self._scope_grouped = scope_grouped
        self._delta_encode_txn = True
        self._column_layout = column_layout
        self._strtab = StringTable()
        self._typereg = TypeRegistry()
        self._index = IndexBlock()

        # type metadata for context managers
        self._sig_types: Dict[int, Tuple[SignalEncoding, int]] = {}  # sig_type_id → (enc, bw)
        self._var_types: Dict[int, Tuple[SignalEncoding, int]] = {}  # var_id       → (enc, bw)
        self._txn_schemas: Dict[int, List[FieldType]] = {}
        self._hier_offsets: List[int] = []  # file offsets of written hierarchy blocks
        # Scope tracking for scope-grouped blocks
        self._var_scope: Dict[int, int] = {}   # var_id → scope_id
        self._scope_id_counter: int = 0

        if isinstance(path_or_file, (str, bytes)):
            self._file = open(path_or_file, 'wb')
            self._owns_file = True
        else:
            self._file = path_or_file
            self._owns_file = False

        self._timescale_exp = timescale_exp
        self._timezero = timezero
        self._closed = False

        hdr = _encode_file_header(timescale_exp=timescale_exp, timezero=timezero)
        self._file.write(hdr)

    # ------------------------------------------------------------------
    # Metadata API
    # ------------------------------------------------------------------

    def intern(self, s: str) -> int:
        return self._strtab.intern(s)

    def add_signal_type(
        self,
        encoding: SignalEncoding,
        bit_width: int,
        radix: Radix = Radix.RX_HEX,
    ) -> int:
        """Register a signal type and return its ID."""
        sig_id = self._typereg.add_signal_type(encoding, bit_width, radix)
        self._sig_types[sig_id] = (encoding, bit_width)
        return sig_id

    def add_txn_schema(self, name: str, fields: List[FieldDef]) -> int:
        """Register a transaction schema and return its ID."""
        name_id = self._strtab.intern(name)
        txn_id = self._typereg.add_txn_schema(name_id, fields)
        self._txn_schemas[txn_id] = [f.field_type for f in fields]
        return txn_id

    def add_stream_type(self, name: str, kind: str = "") -> int:
        """Register a stream declaration and return its type ID."""
        name_id = self._strtab.intern(name)
        kind_id = self._strtab.intern(kind) if kind else 0
        return self._typereg.add_stream_decl(name_id, kind_id)

    def add_enum_type(self, name: str, values: list) -> int:
        from ._types import EnumValue
        name_id = self._strtab.intern(name)
        enum_vals = [EnumValue(name_id=self._strtab.intern(v), numeric_value=n)
                     for v, n in values]
        enum_id = self._typereg.add_enum_type(name_id, enum_vals)
        return enum_id

    # ------------------------------------------------------------------
    # Flush helpers
    # ------------------------------------------------------------------

    def flush_string_table(self) -> None:
        """Flush pending string table entries to the file."""
        data = self._strtab.encode_block(compress=self._compress)
        if data:
            self._file.write(data)

    def flush_type_registry(self) -> None:
        """Flush type registry to the file."""
        data = self._typereg.encode_block(compress=self._compress)
        if data:
            self._file.write(data)

    # ------------------------------------------------------------------
    # Block context managers
    # ------------------------------------------------------------------

    def begin_hierarchy(
        self,
        hier_id: int = 1,
        kind: HierKind = HierKind.HK_DESIGN,
        name: str = "design",
    ) -> _HierarchyContext:
        return _HierarchyContext(self, hier_id=hier_id, kind=kind, name=name)

    def begin_vc_block(self, start_time: int) -> _VcBlockContext:
        return _VcBlockContext(self, start_time)

    def begin_txn_block(
        self,
        start_time: int,
        stream_inst_id: Optional[int] = None,
    ) -> _TxnBlockContext:
        return _TxnBlockContext(self, start_time, stream_inst_id=stream_inst_id)

    def write_ext(self, ext_type: bytes, ext_version: int = 0, payload: bytes = b"") -> None:
        """Write an application-specific BLK_EXT block immediately."""
        blk = ExtBlock(ext_type=ext_type, ext_version=ext_version, payload=payload)
        self._file.write(blk.encode_block())

    # ------------------------------------------------------------------
    # Internal flush helpers
    # ------------------------------------------------------------------

    def _flush_hierarchy(self, blk: HierarchyBlock) -> None:
        offset = self._file.tell()
        self._hier_offsets.append(offset)
        encoded = blk.encode_block(compress=self._compress)
        self._file.write(encoded)

    def _flush_vc(self, blk: VcDataBlock) -> None:
        if not self._scope_grouped:
            offset = self._file.tell()
            encoded = blk.encode_block()
            self._index.add_vc_entry(blk.start_time, blk.end_time, offset)
            self._file.write(encoded)
            return

        # Scope-grouped: split changes by scope, write one VcDataBlock per group.
        # Each group uses per-signal compression (no whole-block), with seekability.
        scope_changes: Dict[int, Dict[int, list]] = {}  # scope_id → {var_id: [(t,v)...]}
        for var_id, changes in blk._changes.items():
            scope_id = self._var_scope.get(var_id, 0)
            scope_changes.setdefault(scope_id, {})
            scope_changes[scope_id][var_id] = changes

        # Also include vars with initial values but no changes
        for var_id in blk._init_values:
            scope_id = self._var_scope.get(var_id, 0)
            scope_changes.setdefault(scope_id, {})
            if var_id not in scope_changes[scope_id]:
                scope_changes[scope_id][var_id] = []

        for scope_id in sorted(scope_changes):
            scope_blk = VcDataBlock(
                start_time=blk.start_time,
                sig_types=blk._sig_types,
                compress=True,
                compress_waves=False,
            )
            for var_id, changes in scope_changes[scope_id].items():
                if var_id in blk._init_values:
                    scope_blk.set_initial(var_id, blk._init_values[var_id])
                for t, v in changes:
                    scope_blk.add_change(var_id, t, v)

            offset = self._file.tell()
            encoded = scope_blk.encode_block()
            self._index.add_vc_entry(scope_blk.start_time, scope_blk.end_time, offset)
            self._file.write(encoded)

    def _flush_txn(
        self, blk: TxnDataBlock, stream_inst_id: Optional[int] = None
    ) -> None:
        offset = self._file.tell()
        encoded = blk.encode_block()
        sid = stream_inst_id if stream_inst_id is not None else 0xFFFFFFFF
        self._index.add_txn_entry(blk.start_time, blk.end_time, offset,
                                   stream_inst_id=sid)
        self._file.write(encoded)

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True

        # Flush string table and type registry, recording their file offsets
        strtab_offset = self._file.tell()
        strtab_data = self._strtab.encode_block(compress=self._compress)
        if strtab_data:
            self._file.write(strtab_data)
        else:
            strtab_offset = 0

        typereg_offset = self._file.tell()
        typereg_data = self._typereg.encode_block(compress=self._compress)
        if typereg_data:
            self._file.write(typereg_data)
        else:
            typereg_offset = 0

        # Populate metadata offsets in index for O(1) reader open
        self._index.strtab_offset  = strtab_offset
        self._index.typereg_offset = typereg_offset
        self._index.hier_offsets   = list(self._hier_offsets)

        # Write index block and patch index_offset in file header
        index_file_offset = self._file.tell()
        self._file.write(self._index.encode_block())

        # Patch index_offset field if the file supports seeking
        try:
            self._file.seek(_INDEX_OFFSET_FIELD_POS)
            self._file.write(struct.pack('<Q', index_file_offset))
        except (OSError, AttributeError):
            pass  # non-seekable file — leave index_offset = 0

        if self._owns_file:
            self._file.close()

    def __enter__(self) -> 'TrlWriter':
        return self

    def __exit__(self, *args) -> None:
        self.close()
