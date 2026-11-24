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
    WellKnownAttr, AttrType, TypedAttr,
)
from ._metadata import MetaBlock, infer_attr_type
from ._ext import ExtBlock
from ._aux import AuxBlock
from ._vc_data import VcDataBlock
from ._txn_data import TxnDataBlock
from .codec import (
    Store, lookup_vc_codec, lookup_txn_codec, CORE_VALUECHANGE, CORE_RECORD,
)

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
    """Hierarchy builder returned by :meth:`~trlog.TrlWriter.begin_hierarchy`.

    Use it as a context manager. Scopes, variables and streams are collected
    in memory and the hierarchy block is written when the ``with`` block exits
    (or when :meth:`flush` is called).
    """

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
        """Open a scope inside the currently open scope (or at the root).

        Args:
            scope_type: Kind of scope, for example ``ScopeType.ST_MODULE``.
            name: Instance name.
            component: Definition name, for example the module type.
            src_file: Source file that declares the scope.
            src_line: Line number in ``src_file``.
        """
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
        """Close the most recently opened scope.

        Raises:
            ZstFormatError: No scope is open.
        """
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
        """Add a variable (signal) to the currently open scope.

        Args:
            name: Variable name.
            sig_type_id: Signal type ID from :meth:`~trlog.TrlWriter.add_signal_type`.
            direction: Port direction.
            src_file: Source file that declares the variable.
            src_line: Line number in ``src_file``.
            driver_file: Source file of the code that drives the variable.
                Stored as the ``trlog.driver.file`` attribute.
            driver_line: Line number in ``driver_file``. Stored as the
                ``trlog.driver.line`` attribute.

        Returns:
            The variable ID, used with :meth:`_VcBlockContext.add_change`.
            IDs are unique across all hierarchies in the file.
        """
        name_id     = self._writer._strtab.intern(name)
        src_file_id = self._writer._strtab.intern(src_file) if src_file else 0
        # IDs are numbered writer-wide, so they stay unique across hierarchies.
        self._blk._next_var_id = self._writer._next_var_id
        var_id = self._blk.add_var(
            name_id, sig_type_id, direction,
            src_file_str_id=src_file_id,
            src_line=src_line,
        )
        self._writer._next_var_id = self._blk._next_var_id
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
        """Add a transaction stream instance to the currently open scope.

        Args:
            stream_type_id: Stream type ID from :meth:`~trlog.TrlWriter.add_stream_type`.
            name: Stream instance name.
            src_file: Source file that declares the stream. Stored as the
                ``trlog.src.file`` attribute.
            src_line: Line number in ``src_file``. Stored as the
                ``trlog.src.line`` attribute.

        Returns:
            The stream instance ID, used when writing transactions. IDs are
            unique across all hierarchies in the file.
        """
        name_id = self._writer._strtab.intern(name)
        self._blk._next_stream_id = self._writer._next_stream_id
        inst_id = self._blk.add_stream(stream_type_id, name_id)
        self._writer._next_stream_id = self._blk._next_stream_id
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
        """Attach a string attribute to the most recently declared node.

        The node is the last scope, variable or stream added; after
        :meth:`end_scope` it is the enclosing scope.

        Args:
            key: Attribute name.
            value: Attribute value. Stored as ``str(value)``.

        Raises:
            ZstFormatError: No scope, variable or stream has been declared.
        """
        key_id = self._writer._strtab.intern(key)
        val_id = self._writer._strtab.intern(str(value))
        self._blk.add_attr(key_id, val_id)

    def add_typed_attr(self, key: str, value: Any, attr_type: Optional[int] = None) -> None:
        """Attach a typed attribute to the most recently declared node.

        Args:
            key: Attribute name.
            value: ``bool``, ``int``, ``float``, ``str``, or a list of one of
                these.
            attr_type: An :class:`~trlog.AttrType` value. Inferred from
                ``value`` when omitted.

        Raises:
            ZstFormatError: No scope, variable or stream has been declared.
        """
        self._blk.add_typed_attr(
            self._writer._build_typed_attr(key, value, attr_type))

    def flush(self) -> None:
        """Write the hierarchy block. Later calls do nothing."""
        if not self._flushed:
            self._writer._flush_hierarchy(self._blk)
            self._flushed = True

    def __enter__(self) -> '_HierarchyContext':
        return self

    def __exit__(self, *args) -> None:
        self.flush()


class _VcBlockContext:
    """Value-change block builder returned by :meth:`~trlog.TrlWriter.begin_vc_block`.

    Use it as a context manager; the block is encoded and written when the
    ``with`` block exits (or when :meth:`flush` is called).
    """

    def __init__(self, writer: 'TrlWriter', start_time: int,
                 codec: Optional[str] = None) -> None:
        self._writer = writer
        # Resolve the value-change codec for this block. The default path reuses
        # the trace-level core codec + state so its output stays byte-identical;
        # an explicitly selected codec (e.g. core.bytesplit) is opened on first
        # use, keeps its state for the rest of the trace, and produces a
        # self-identifying FLAG_STRUCT_CODEC block.
        if codec is None or codec == CORE_VALUECHANGE:
            self._codec = writer._vc_codec
            self._state = writer._vc_state
        else:
            self._codec, self._state = writer._open_block_codec(codec)
        self._blk = self._codec.new_block(writer._store, self._state, start_time)

    def add_change(self, var_id: int, time: int, value: Any) -> None:
        """Record a value change.

        Args:
            var_id: Variable ID from ``add_var``.
            time: Absolute time in trace units. Changes may be added out of
                time order within a block.
            value: New value. The accepted Python type depends on the
                variable's signal encoding (``int`` for 2-state signals,
                ``float`` for real, ``str`` for string, and so on).
        """
        self._blk.add_change(var_id, time, value)

    def set_initial(self, var_id: int, value: Any) -> None:
        """Set a variable's value at the start of the block.

        The reader reports this value only if the variable has no changes in
        the block. Without it, the first change is used as the initial value.
        """
        self._blk.set_initial(var_id, value)

    def flush(self) -> None:
        """Encode the block and write it to the file."""
        self._writer._flush_vc(self._blk, codec=self._codec, state=self._state)

    def __enter__(self) -> '_VcBlockContext':
        return self

    def __exit__(self, *args) -> None:
        self.flush()


class _TxnBlockContext:
    """Transaction block builder returned by :meth:`~trlog.TrlWriter.begin_txn_block`.

    Use it as a context manager; the block is encoded and written when the
    ``with`` block exits (or when :meth:`flush` is called). Transaction IDs
    are chosen by the caller and should be unique within the trace.
    """

    def __init__(
        self,
        writer: 'TrlWriter',
        start_time: int,
        stream_inst_id: Optional[int] = None,
    ) -> None:
        self._writer = writer
        self._stream_inst_id = stream_inst_id
        # Dispatch block construction through the resolved record codec
        # (the re-homed built-in produces a byte-identical TxnDataBlock).
        self._blk = writer._txn_codec.new_block(
            writer._store, writer._txn_state, start_time)

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
        """Write a complete transaction.

        Args:
            stream_inst_id: Stream instance ID from ``add_stream``.
            txn_type_id: Schema ID from :meth:`~trlog.TrlWriter.add_txn_schema`.
            txn_id: Transaction ID.
            start: Start time.
            end: End time.
            parent: Parent transaction ID, or 0 for none.
            attrs: List of :class:`~trlog.TxnAttr` (field index and value).
                Values are encoded with the schema's field types.
            origin_file: Source file that produced the transaction. Stored as
                ``trlog.origin.file``/``trlog.origin.line`` metadata.
            origin_line: Line number in ``origin_file``.
            origin_component: Component that produced the transaction. Stored
                as ``trlog.origin.component`` metadata.
        """
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
        """Open a transaction whose end is written later with :meth:`write_end`.

        Args:
            stream_inst_id: Stream instance ID from ``add_stream``.
            txn_type_id: Schema ID from :meth:`~trlog.TrlWriter.add_txn_schema`.
            txn_id: Transaction ID.
            start: Start time.
            parent: Parent transaction ID, or 0 for none.
            origin_file: See :meth:`write_full`.
            origin_line: See :meth:`write_full`.
            origin_component: See :meth:`write_full`.
        """
        self._blk.write_begin(stream_inst_id, txn_type_id, txn_id, start, parent)
        self._writer._open_txn_types[txn_id] = txn_type_id
        self._write_origin_meta(txn_id, origin_file, origin_line, origin_component)

    def write_attr(self, txn_id, attrs):
        """Add attribute values to an open transaction.

        Values are encoded with the field types of the schema given to
        :meth:`write_begin`, which may have been called in an earlier block.

        Args:
            txn_id: ID of a transaction opened with :meth:`write_begin`.
            attrs: List of :class:`~trlog.TxnAttr`.
        """
        txn_type_id = self._writer._open_txn_types.get(txn_id)
        schema = self._writer._txn_schemas.get(txn_type_id, [])
        typed = []
        for attr in attrs:
            # An explicit _field_type (set by extensions) wins over the schema.
            if not hasattr(attr, "_field_type") and attr.field_idx < len(schema):
                a = TxnAttr(field_idx=attr.field_idx, value=attr.value)
                a._field_type = schema[attr.field_idx]
                attr = a
            typed.append(attr)
        self._blk.write_attr(txn_id, typed)

    def write_end(self, txn_id, end_time):
        """Close a transaction opened with :meth:`write_begin`."""
        self._blk.write_end(txn_id, end_time)
        self._writer._open_txn_types.pop(txn_id, None)

    def write_link(self, link_type, src, tgt, label_id=0):
        """Record a link between two transactions.

        Args:
            link_type: A :class:`~trlog.LinkType` value.
            src: Source transaction ID.
            tgt: Target transaction ID.
            label_id: Optional label as a string ID from
                :meth:`~trlog.TrlWriter.intern`; 0 for none.
        """
        self._blk.write_link(link_type, src, tgt, label_id)

    def write_meta(self, txn_id: int, key: str, value: str) -> None:
        """Attach a string key/value pair to a transaction."""
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
        """Encode the block and write it to the file."""
        self._writer._flush_txn(self._blk, stream_inst_id=self._stream_inst_id)

    def __enter__(self) -> '_TxnBlockContext':
        return self

    def __exit__(self, *args) -> None:
        self.flush()


class TrlWriter:
    """Write a TRLOG trace file.

    Register types, build hierarchies, then write value-change and
    transaction blocks. Each block is written to the file as soon as its
    builder is flushed. :meth:`close` (called on leaving a ``with`` block)
    writes the string table, type registry and index; a file that was not
    closed cannot be read.

    Args:
        path_or_file: Path of the file to create, or a writable binary file
            object. A file object is not closed by the writer and should be
            seekable so the index location can be recorded in the header.
        timescale_exp: Time unit as a power of ten seconds (-9 is ns).
        timezero: Time offset stored in the file header.
        compress: Compress blocks (with ``compressor``).
        compressor: ``"zlib"`` or ``"zstd"``, for ``compress``. ``"zstd"``
            needs the ``zstandard`` package and raises ImportError without it.
        compress_time_table: Compress the time table of value-change blocks
            (only when ``compress`` is false).
        compress_waves: Compress the wave data of value-change blocks (only
            when ``compress`` is false).
        compress_waves_alg: ``"zlib"`` or ``"lz4"``, for ``compress_waves``.
        seekable: Write per-signal offsets in value-change blocks (only when
            ``compress`` is false).
        scope_grouped: Split each value-change block into one block per scope.
        column_layout: Transaction block layout: ``"row"``, ``"column"``,
            ``"auto"`` or ``"adaptive"``.

    Example::

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
        compress_waves_alg: str = "zlib",
        seekable: bool = False,
        scope_grouped: bool = False,
        column_layout: str = "row",
    ) -> None:
        if compressor not in ("zlib", "zstd"):
            raise ValueError(f"unknown compressor {compressor!r}; use 'zlib' or 'zstd'")
        if compressor == "zstd":
            import zstandard  # noqa: F401  (fail here, not at the first flush)
        self._compress = compress
        self._compressor = compressor
        self._use_zstd = compress and compressor == "zstd"
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
        self._meta = MetaBlock()   # trace-global transparent metadata (§2.3)
        # Per-(owner,key) ordinal counter for opaque keyed (aux) blocks (§2.4)
        self._aux_ordinals: Dict[Tuple[int, int], int] = {}
        # Derived (virtual) signals (§5.2): next id + var_id → input var ids
        self._next_derived_id: int = 0
        self._derived_inputs: Dict[int, list] = {}

        # Codec SPI seam (design §4.3). The built-in VC/TXN encoders are reached
        # through the registry like any other codec; the default path resolves
        # to the re-homed core codecs and emits byte-identical output.
        self._store = Store(writer=self)
        self._vc_codec = lookup_vc_codec(CORE_VALUECHANGE)
        self._txn_codec = lookup_txn_codec(CORE_RECORD)
        self._vc_state = self._vc_codec.open_writer(self._store, 0, b"")
        # Codecs selected per block (begin_vc_block(codec=...)), in first-use
        # order: codec_id -> (codec, state). Finalized and closed at close().
        self._block_codecs: Dict[str, tuple] = {}
        self._txn_state = self._txn_codec.open_writer(self._store, 0, b"")

        # type metadata for context managers
        self._sig_types: Dict[int, Tuple[SignalEncoding, int]] = {}  # sig_type_id → (enc, bw)
        self._var_types: Dict[int, Tuple[SignalEncoding, int]] = {}  # var_id       → (enc, bw)
        self._txn_schemas: Dict[int, List[FieldType]] = {}
        self._open_txn_types: Dict[int, int] = {}  # txn_id → txn_type_id (begun, not ended)
        self._hier_offsets: List[int] = []  # file offsets of written hierarchy blocks
        # Scope tracking for scope-grouped blocks
        self._var_scope: Dict[int, int] = {}   # var_id → scope_id
        self._scope_id_counter: int = 0
        # Next variable / stream instance ID, shared by all hierarchies.
        self._next_var_id: int = 1
        self._next_stream_id: int = 1

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
        """Add a string to the string table and return its string ID.

        Interning the same string again returns the same ID.
        """
        return self._strtab.intern(s)

    def add_signal_type(
        self,
        encoding: SignalEncoding,
        bit_width: int,
        radix: Radix = Radix.RX_HEX,
    ) -> int:
        """Register a signal type.

        Args:
            encoding: Value encoding, for example ``SignalEncoding.SE_2STATE``.
            bit_width: Width in bits.
            radix: Display hint for viewers.

        Returns:
            The signal type ID, passed to ``add_var``.
        """
        sig_id = self._typereg.add_signal_type(encoding, bit_width, radix)
        self._sig_types[sig_id] = (encoding, bit_width)
        return sig_id

    def add_txn_schema(self, name: str, fields: List[FieldDef]) -> int:
        """Register a transaction schema.

        Args:
            name: Schema (transaction type) name.
            fields: Ordered list of :class:`~trlog.FieldDef`. A field's
                position is its field index in :class:`~trlog.TxnAttr`.

        Returns:
            The transaction type ID.
        """
        name_id = self._strtab.intern(name)
        txn_id = self._typereg.add_txn_schema(name_id, fields)
        self._txn_schemas[txn_id] = [f.field_type for f in fields]
        return txn_id

    def add_stream_type(self, name: str, kind: str = "") -> int:
        """Register a stream type.

        Args:
            name: Stream type name.
            kind: Optional category string, used by
                :meth:`~trlog.TrlReader.find_streams`.

        Returns:
            The stream type ID, passed to ``add_stream`` and to the metadata
            and dependency methods.
        """
        name_id = self._strtab.intern(name)
        kind_id = self._strtab.intern(kind) if kind else 0
        return self._typereg.add_stream_decl(name_id, kind_id)

    def set_stream_codec(
        self,
        stream_type_id: int,
        codec_id: str,
        version: int = 0,
        params: bytes = b"",
    ) -> None:
        """Record the codec a stream type's data uses.

        The codec ID, version and parameters are stored with the stream type
        and read back with :meth:`~trlog.TrlReader.stream_codec`. They are
        declarative: each data block still names the codec that encoded it,
        and readers decode blocks by that ID.

        Args:
            stream_type_id: Stream type ID from :meth:`add_stream_type`.
            codec_id: Reverse-DNS codec ID (for example ``"com.example.bus"``).
            version: Codec format version.
            params: Codec parameters, opaque to trlog.

        Raises:
            KeyError: ``stream_type_id`` is not a registered stream type.
            ValueError: ``codec_id`` is empty.
        """
        if not codec_id:
            raise ValueError("codec_id must be a non-empty string")
        if self._typereg.get_stream_decl(stream_type_id) is None:
            raise KeyError(stream_type_id)
        self._typereg.set_stream_codec(
            stream_type_id, self._strtab.intern(codec_id), version, bytes(params))

    # ------------------------------------------------------------------
    # Transparent metadata (impl-plan §2.3 / §2.6) — readable codec-free
    # ------------------------------------------------------------------

    def _build_typed_attr(self, key: str, value, attr_type: Optional[int] = None) -> TypedAttr:
        """Build a :class:`TypedAttr`, interning the key and any string values."""
        key_id = self._strtab.intern(key)
        if attr_type is None:
            attr_type = infer_attr_type(value)
        base = attr_type & ~int(AttrType.AT_ARRAY)
        if attr_type & int(AttrType.AT_ARRAY):
            if base == int(AttrType.AT_STR):
                value = [self._strtab.intern(v) for v in value]
            else:
                value = list(value)
        elif base == int(AttrType.AT_STR):
            value = self._strtab.intern(value)
        return TypedAttr(key_str_id=key_id, attr_type=int(attr_type), value=value)

    def add_trace_metadata(self, key: str, value, attr_type: Optional[int] = None) -> None:
        """Attach a typed key/value pair to the whole trace.

        Args:
            key: Metadata key.
            value: ``bool``, ``int``, ``float``, ``str``, or a list of one of
                these.
            attr_type: An :class:`~trlog.AttrType` value. Inferred from
                ``value`` when omitted.
        """
        self._meta.add(self._build_typed_attr(key, value, attr_type))

    def add_stream_metadata(
        self, stream_type_id: int, key: str, value, attr_type: Optional[int] = None
    ) -> None:
        """Attach a typed key/value pair to a stream type.

        Args:
            stream_type_id: Stream type ID from :meth:`add_stream_type`.
            key: Metadata key.
            value: Value, as for :meth:`add_trace_metadata`.
            attr_type: An :class:`~trlog.AttrType` value. Inferred from
                ``value`` when omitted.
        """
        self._typereg.add_stream_metadata(
            stream_type_id, self._build_typed_attr(key, value, attr_type))

    def add_derived_signal(
        self,
        name: str,
        expr,
        inputs,
        bit_width: int,
        materialized: bool = False,
        input_changes=None,
    ) -> int:
        """Define a signal computed from other signals.

        Only the expression is stored. :meth:`~trlog.TrlReader.read_derived`
        evaluates it at every time at which an input changes.

        Args:
            name: Signal name.
            expr: Expression built from :class:`~trlog.Input` and
                :class:`~trlog.Const`, or bytecode from
                :func:`~trlog.compile_expr`.
            inputs: IDs that ``Input(0)``, ``Input(1)``, ... refer to:
                variable IDs or IDs of other derived signals.
            bit_width: Result width; results are masked to it.
            materialized: Also evaluate the expression now and write the
                result as an ordinary value-change block.
            input_changes: Required when ``materialized`` is true: a mapping
                from each input ID to its ``[(time, value), ...]`` list.

        Returns:
            The derived signal ID (``0x40000000`` and up).

        Raises:
            DependencyError: The definition makes a derived signal depend on
                itself.
            ValueError: ``materialized`` is true and ``input_changes`` is
                missing.
        """
        from ._derived import (
            Expr, compile_expr, DerivedDef, FLAG_MATERIALIZED, evaluate_changes,
            DERIVED_AUX_OWNER, DERIVED_VAR_BASE,
        )
        from ._deps import DependencyGraph

        bytecode = compile_expr(expr) if isinstance(expr, Expr) else bytes(expr)
        inputs = list(inputs)
        var_id = DERIVED_VAR_BASE + self._next_derived_id
        self._next_derived_id += 1

        # Cycle detection over the derived-var dependency graph (reject at write).
        candidate = dict(self._derived_inputs)
        candidate[var_id] = inputs
        DependencyGraph.from_catalog(candidate).validate_acyclic()
        self._derived_inputs[var_id] = inputs

        flags = FLAG_MATERIALIZED if materialized else 0
        defn = DerivedDef(bit_width=bit_width, input_var_ids=inputs,
                          bytecode=bytecode, flags=flags)
        # Derived signals are integer bit-vectors; register the type so a
        # materialized block (and read_signal on it) resolves.
        self._var_types[var_id] = (SignalEncoding.SE_2STATE, bit_width)

        # Persist the definition in an aux block keyed by the derived var id.
        name_id = self._strtab.intern(name)
        self.write_aux(DERIVED_AUX_OWNER, var_id,
                       encode_uvarint(name_id) + defn.encode(), compress=False)

        if materialized:
            if input_changes is None:
                raise ValueError("materialized=True requires input_changes")
            changes = evaluate_changes(
                defn, [list(input_changes[i]) for i in inputs])
            start = changes[0][0] if changes else 0
            with self.begin_vc_block(start) as vc:
                for t, v in changes:
                    vc.add_change(var_id, t, v)
        return var_id

    def declare_dependencies(self, stream_type_id: int, input_stream_ids) -> None:
        """Declare that a stream type is computed from other stream types.

        Replaces any earlier declaration for ``stream_type_id``.

        Args:
            stream_type_id: The dependent stream type.
            input_stream_ids: Stream types it depends on.

        Raises:
            DependencyError: The declaration would create a cycle. Nothing is
                recorded in that case.
        """
        from ._deps import DependencyGraph
        # Validate a *candidate* graph that includes the new edge before
        # committing, so a rejected (cyclic) edge is never persisted.
        catalog = self._typereg.dependency_catalog()
        catalog[stream_type_id] = list(input_stream_ids)
        DependencyGraph.from_catalog(catalog).validate_acyclic()  # raises on cycle
        self._typereg.set_stream_dependencies(stream_type_id, input_stream_ids)

    def add_stream_profile(self, stream_type_id: int, profile_id: str) -> None:
        """Add a reverse-DNS profile ID (for example ``"org.example.apb"``)
        to a stream type.

        Profiles are stored as a list of strings under the ``trlog.profile``
        metadata key and read with :meth:`~trlog.TrlReader.profiles`.
        """
        entry = self._typereg.get_stream_decl(stream_type_id)
        key_id = self._strtab.intern(WellKnownAttr.PROFILE)
        pid = self._strtab.intern(profile_id)
        for attr in entry.metadata:
            if attr.key_str_id == key_id and (attr.attr_type & int(AttrType.AT_ARRAY)):
                attr.value.append(pid)
                return
        self._typereg.add_stream_metadata(
            stream_type_id,
            TypedAttr(key_str_id=key_id,
                      attr_type=int(AttrType.AT_ARRAY | AttrType.AT_STR),
                      value=[pid]))

    def add_enum_type(self, name: str, values: list) -> int:
        """Register an enum type for ``FT_ENUM`` transaction fields.

        Args:
            name: Enum type name.
            values: List of ``(label, integer)`` pairs.

        Returns:
            The enum type ID, set as ``FieldDef.enum_type_id``.
        """
        from ._types import EnumValue
        name_id = self._strtab.intern(name)
        enum_vals = [EnumValue(integer_value=n, label_str_id=self._strtab.intern(v))
                     for v, n in values]
        enum_id = self._typereg.add_enum_type(name_id, enum_vals)
        return enum_id

    # ------------------------------------------------------------------
    # Flush helpers
    # ------------------------------------------------------------------

    def flush_string_table(self) -> None:
        """Write the strings interned so far as a string-table block.

        Not needed in normal use: :meth:`close` writes the string table.
        """
        data = self._strtab.encode_block(compress=self._compress, use_zstd=self._use_zstd)
        if data:
            self._file.write(data)

    def flush_type_registry(self) -> None:
        """Write the type registry as a block.

        Not needed in normal use: :meth:`close` writes the type registry.
        """
        data = self._typereg.encode_block(compress=self._compress, use_zstd=self._use_zstd)
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
        """Start a hierarchy.

        Args:
            hier_id: Hierarchy ID; distinct for each hierarchy in the file.
            kind: What the hierarchy describes (design, software, ...).
            name: Hierarchy label.

        Returns:
            A hierarchy builder to use as a context manager.
        """
        return _HierarchyContext(self, hier_id=hier_id, kind=kind, name=name)

    def begin_vc_block(self, start_time: int, codec: Optional[str] = None) -> _VcBlockContext:
        """Start a value-change block.

        Args:
            start_time: Block start time; no later than the block's first
                change.
            codec: Optional value-change codec ID (for example
                ``trlog.CORE_BYTESPLIT``). The default is the core codec.

        Returns:
            A value-change block builder to use as a context manager.

        Raises:
            ValueError: ``codec`` is not registered.
        """
        return _VcBlockContext(self, start_time, codec=codec)

    def begin_txn_block(
        self,
        start_time: int,
        stream_inst_id: Optional[int] = None,
    ) -> _TxnBlockContext:
        """Start a transaction block.

        Args:
            start_time: Block start time.
            stream_inst_id: When every record in the block belongs to one
                stream, its stream instance ID. The index then lets readers
                find the stream's blocks directly.

        Returns:
            A transaction block builder to use as a context manager.
        """
        return _TxnBlockContext(self, start_time, stream_inst_id=stream_inst_id)

    def write_ext(self, ext_type: bytes, ext_version: int = 0, payload: bytes = b"") -> None:
        """Write an application-defined extension block immediately.

        Args:
            ext_type: 4-byte type tag, for example ``b"COVG"``.
            ext_version: Version of the payload layout (0-65535).
            payload: Block contents.

        Raises:
            ValueError: ``ext_type`` is not exactly 4 bytes.
        """
        blk = ExtBlock(ext_type=ext_type, ext_version=ext_version, payload=payload)
        self._file.write(blk.encode_block())

    def write_aux(
        self,
        owner_stream_id: int,
        key: int,
        payload: bytes,
        compress: Optional[bool] = None,
    ) -> int:
        """Write a keyed binary blob that is not tied to time.

        The blob is indexed by ``(owner_stream_id, key)`` and read back with
        :meth:`~trlog.TrlReader.read_aux`. Writing the same pair again adds another
        blob with the next ordinal.

        Args:
            owner_stream_id: Owner ID, normally a stream type ID.
            key: Integer key.
            payload: Bytes to store; not interpreted by trlog.
            compress: Compress the blob. Defaults to the writer's
                ``compress`` setting.

        Returns:
            The blob's ordinal for this ``(owner_stream_id, key)``, from 0.
        """
        if compress is None:
            compress = self._compress
        ordinal = self._aux_ordinals.get((owner_stream_id, key), 0)
        self._aux_ordinals[(owner_stream_id, key)] = ordinal + 1
        offset = self._file.tell()
        block = AuxBlock.encode(payload, compress=compress,
                               use_zstd=compress and self._use_zstd)
        self._file.write(block)
        self._index.add_aux_entry(owner_stream_id, key, ordinal, offset, len(block))
        return ordinal

    # ------------------------------------------------------------------
    # Internal flush helpers
    # ------------------------------------------------------------------

    def _flush_hierarchy(self, blk: HierarchyBlock) -> None:
        offset = self._file.tell()
        self._hier_offsets.append(offset)
        encoded = blk.encode_block(compress=self._compress, use_zstd=self._use_zstd)
        self._file.write(encoded)

    def _open_block_codec(self, codec_id: str) -> tuple:
        """Return ``(codec, state)`` for a per-block codec, opening it once."""
        entry = self._block_codecs.get(codec_id)
        if entry is None:
            codec = lookup_vc_codec(codec_id)
            if codec is None:
                raise ValueError(f"unknown value-change codec {codec_id!r}")
            entry = (codec, codec.open_writer(self._store, 0, b""))
            self._block_codecs[codec_id] = entry
        return entry

    def _flush_vc(self, blk: VcDataBlock, codec=None, state=None) -> None:
        codec = codec if codec is not None else self._vc_codec
        state = state if state is not None else self._vc_state
        # Scope-grouped splitting is only defined for the default core codec.
        if not self._scope_grouped or codec is not self._vc_codec:
            offset = self._file.tell()
            # Route the encode through the codec (the core codec returns the
            # block's own bytes + (start,end); the core does the indexing).
            encoded, start, end = codec.encode_block(state, self._store)
            self._index.add_vc_entry(start, end, offset)
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
                use_zstd=self._use_zstd,
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
        encoded, start, end = self._txn_codec.encode_block(self._txn_state, self._store)
        sid = stream_inst_id if stream_inst_id is not None else 0xFFFFFFFF
        self._index.add_txn_entry(start, end, offset, stream_inst_id=sid)
        self._file.write(encoded)

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def close(self) -> None:
        """Finish the file.

        Writes trace metadata, the string table, the type registry and the
        index, and closes the file if the writer opened it. Later calls do
        nothing.
        """
        if self._closed:
            return
        self._closed = True

        # Codec trace-finalize (design §4.7): runs after the last encode_block
        # and before the index/footer, so codecs can serialize trace-wide
        # accumulated metadata. Per design, finalize runs in **reverse
        # dependency order** (a depended-on stream finalizes after every stream
        # that feeds it). The catalog is validated acyclic here so that order is
        # well-defined; the built-in VC/TXN codecs are trace singletons with no
        # inter-stream deps, so the order is trivial today and becomes
        # load-bearing with per-stream codecs (Phase 4/5).
        from ._deps import DependencyGraph
        DependencyGraph.from_catalog(
            self._typereg.dependency_catalog()).validate_acyclic()
        for codec, state in self._block_codecs.values():
            codec.finalize(state, self._store)
        self._vc_codec.finalize(self._vc_state, self._store)
        self._txn_codec.finalize(self._txn_state, self._store)
        for codec, state in self._block_codecs.values():
            codec.close(state)
        self._vc_codec.close(self._vc_state)
        self._txn_codec.close(self._txn_state)

        # Trace-global transparent metadata block (BLK_META). Written only when
        # any trace metadata was added, so legacy files stay byte-identical.
        meta_offset = 0
        if self._meta:
            meta_offset = self._file.tell()
            self._file.write(self._meta.encode_block(compress=self._compress,
                                                     use_zstd=self._use_zstd))
        self._index.meta_offset = meta_offset

        # Flush string table and type registry, recording their file offsets
        strtab_offset = self._file.tell()
        strtab_data = self._strtab.encode_block(compress=self._compress, use_zstd=self._use_zstd)
        if strtab_data:
            self._file.write(strtab_data)
        else:
            strtab_offset = 0

        typereg_offset = self._file.tell()
        typereg_data = self._typereg.encode_block(compress=self._compress, use_zstd=self._use_zstd)
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
