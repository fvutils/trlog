"""TrlReader — high-level reader for the TRLOG binary format."""

from __future__ import annotations

import struct
from dataclasses import dataclass, field
from typing import Any, Dict, Iterator, List, Optional, Tuple, Union

from ._codec import decode_uvarint
from ._exceptions import ZstMagicError, ZstVersionError, ZstFormatError, ZstNoIndexError
from ._hierarchy import HierarchyBlock
from ._index import IndexBlock
from ._string_table import StringTable
from ._type_registry import TypeRegistry
from ._types import (
    BlockType, SignalEncoding, BLOCK_HEADER_SIZE, FLAG_COMPRESSED, FLAG_COMPRESS_ALG,
    FLAG_STRUCT_CODEC, VcChange, HStream, AttrType, WellKnownAttr,
    HScope, HVar, ScopeType,
)
from .codec import lookup_vc_codec as _lookup_vc_codec
from ._metadata import MetaBlock
from ._ext import ExtBlock
from ._aux import AuxBlock
from ._vc_data import VcDataBlock
from ._txn_data import TxnDataBlock
from .codec import (
    Store, lookup_vc_codec, lookup_txn_codec, CORE_VALUECHANGE, CORE_RECORD,
)

TRL_MAGIC = b'TRL\x00\x00\x00\x01\x00'


@dataclass
class ScopeNode:
    """One scope of a hierarchy, as returned by :meth:`TrlReader.scope_tree`
    and :meth:`TrlReader.scopes`.

    ``vars`` and ``streams`` are the :class:`HVar` and :class:`HStream`
    declared directly in this scope; resolve their ``name_str_id`` with
    ``reader.string_table.lookup``. ``path`` is the dotted scope path from the
    hierarchy root. The root of a hierarchy has ``scope_type`` None, an empty
    name and path, and ``node`` None; otherwise ``node`` is the raw
    :class:`HScope`.
    """
    name:       str
    scope_type: Optional[ScopeType]
    path:       str
    hier_id:    int
    children:   List["ScopeNode"] = field(default_factory=list)
    vars:       List[HVar] = field(default_factory=list)
    streams:    List[HStream] = field(default_factory=list)
    node:       Optional[HScope] = None


class TrlReader:
    """Read a TRLOG trace file.

    Opening the reader loads the string table, type registry, hierarchies,
    trace metadata and index. Value-change and transaction data is read on
    demand.

    Args:
        path_or_file: Path of the file, or a readable, seekable binary file
            object. A file object is not closed by the reader.

    Raises:
        ZstMagicError: The file is not a TRLOG file.
        ZstVersionError: The file's major version is not supported.
        ZstFormatError: The file does not start with a file header block.

    Example::

        with TrlReader("trace.trl") as r:
            for blk in r.iter_vc_blocks():
                for change in blk:
                    ...
    """

    def __init__(self, path_or_file) -> None:
        if isinstance(path_or_file, (str, bytes)):
            self._file = open(path_or_file, 'rb')
            self._owns_file = True
        else:
            self._file = path_or_file
            self._owns_file = False

        self._strtab = StringTable()
        self._typereg = TypeRegistry()
        self._hierarchies: Dict[int, HierarchyBlock] = {}
        self._meta: Optional[MetaBlock] = None   # trace-global transparent metadata
        self._derived_cache: Optional[Dict[int, dict]] = None
        self._index: Optional[IndexBlock] = None
        self._timescale_exp: int = -9
        self._timezero: int = 0
        self._version_major: int = 0
        self._version_minor: int = 0

        # Sig type map: var_id → (SignalEncoding, bit_width) populated from type registry
        self._sig_types: Dict[int, tuple] = {}

        # Codec SPI seam (design §4.3). Block decode is dispatched through the
        # resolved codec; legacy blocks (no per-stream codec recorded) resolve
        # to the re-homed core codecs.
        self._store = Store(reader=self)
        self._vc_codec = lookup_vc_codec(CORE_VALUECHANGE)
        self._txn_codec = lookup_txn_codec(CORE_RECORD)

        self._scan_file()

    # ------------------------------------------------------------------
    # Properties
    # ------------------------------------------------------------------

    @property
    def string_table(self) -> StringTable:
        """The string table. ``string_table.lookup(str_id)`` returns the text
        for a string ID."""
        return self._strtab

    @property
    def type_registry(self) -> TypeRegistry:
        """The type registry, with ``get_signal_type()``,
        ``get_txn_schema()``, ``get_enum_type()`` and ``get_stream_decl()``
        lookups by ID."""
        return self._typereg

    @property
    def hierarchies(self) -> Dict[int, HierarchyBlock]:
        """Hierarchies by ``hier_id``. Each has ``header``, ``vars``
        (variable ID to ``HVar``) and ``streams`` (stream instance ID to
        ``HStream``)."""
        return self._hierarchies

    def scope_tree(self, hier_id: int) -> ScopeNode:
        """Return the root :class:`ScopeNode` of hierarchy ``hier_id``.

        Raises KeyError if there is no such hierarchy.
        """
        hier = self._hierarchies[hier_id]
        root = ScopeNode(name="", scope_type=None, path="", hier_id=hier_id)
        self._fill_scope(root, hier._root_children)
        return root

    def scopes(self, hier_id: Optional[int] = None) -> Iterator[ScopeNode]:
        """Yield every scope depth-first, in declaration order.

        Walks hierarchy ``hier_id``, or all hierarchies in ID order when it
        is None. Hierarchy roots are not yielded (see :meth:`scope_tree`).
        """
        ids = sorted(self._hierarchies) if hier_id is None else [hier_id]

        def walk(node: ScopeNode):
            for child in node.children:
                yield child
                yield from walk(child)

        for hid in ids:
            yield from walk(self.scope_tree(hid))

    def _fill_scope(self, parent: ScopeNode, children: list) -> None:
        for node in children:
            if isinstance(node, HScope):
                name = self._strtab.lookup(node.name_str_id) if node.name_str_id else ""
                path = f"{parent.path}.{name}" if parent.path else name
                child = ScopeNode(name=name, scope_type=node.scope_type, path=path,
                                  hier_id=parent.hier_id, node=node)
                self._fill_scope(child, node.children)
                parent.children.append(child)
            elif isinstance(node, HVar):
                parent.vars.append(node)
            elif isinstance(node, HStream):
                parent.streams.append(node)

    @property
    def timescale_exp(self) -> int:
        """Time unit as a power of ten seconds (-9 is ns)."""
        return self._timescale_exp

    @property
    def timezero(self) -> int:
        """Time offset from the file header."""
        return self._timezero

    @property
    def version_major(self) -> int:
        """Major format version of the file."""
        return self._version_major

    # ------------------------------------------------------------------
    # Iterators
    # ------------------------------------------------------------------

    def iter_vc_blocks(self) -> Iterator[List[VcChange]]:
        """Iterate over value-change blocks in file order.

        Yields:
            For each block, a list of :class:`~trlog.VcChange` for all
            variables in the block, sorted by time. Blocks written with a
            codec that is not registered are skipped.
        """
        self._file.seek(0)
        for block_type, flags, payload, _offset in self._iter_raw_blocks():
            if block_type == BlockType.BLK_VC_DATA:
                changes = self._decode_vc_block(payload, flags)
                if changes is not None:
                    yield changes

    def _decode_vc_block(self, payload, flags) -> Optional[List[VcChange]]:
        """Decode one ``BLK_VC_DATA`` payload with the block's codec; ``None``
        if the block's codec is not registered."""
        changes: List[VcChange] = []
        if flags & FLAG_STRUCT_CODEC:
            # Self-identifying structural-codec block: resolve its codec
            # id from the interned prefix and dispatch; a block whose
            # codec is unavailable is skipped (never errors) — §2.4/§4.2.
            codec = self._resolve_block_codec(payload)
            if codec is None:
                return None
            codec.decode_block(None, self._store, payload, flags,
                               changes.append)
        else:
            self._vc_codec.decode_block(None, self._store, payload, flags,
                                        changes.append)
        return changes

    def iter_txn_blocks(
        self, stream_inst_id: Optional[int] = None
    ) -> Iterator[list]:
        """Iterate over transaction blocks in file order.

        Args:
            stream_inst_id: When given, drop records that belong to other
                streams. Records without a stream ID (``TxnAttrRecord``,
                ``TxnEnd``, ``TxnLink``, ``TxnMeta``) are kept. If blocks were
                written with ``stream_inst_id``, only that stream's blocks are
                read.

        Yields:
            For each block with matching records, a list of records
            (``TxnFull``, ``TxnBegin``, ``TxnAttrRecord``, ``TxnEnd``,
            ``TxnLink``, ``TxnMeta``).
        """
        schemas = self._build_txn_schemas()

        # Fast path: per-stream index available and a specific stream requested
        if (stream_inst_id is not None
                and self._index is not None
                and any(sid != 0xFFFFFFFF
                        for _, _, _, sid in self._index._txn_entries)):
            offsets = self._index.find_txn_blocks_for_stream(stream_inst_id)
            for off in offsets:
                self._file.seek(off)
                blk_type, flags, payload, _ = self._read_one_block()
                if blk_type != BlockType.BLK_TXN_DATA:
                    continue
                records = []
                self._txn_codec.decode_block(None, self._store, payload, flags,
                                             records.append)
                # The block *should* be single-stream, but if it was written
                # as mixed we still need to filter at record level.
                filtered = [
                    r for r in records
                    if not hasattr(r, "stream_inst_id")
                    or r.stream_inst_id == stream_inst_id
                ]
                if filtered:
                    yield filtered
            return

        # Slow path: linear scan
        self._file.seek(0)
        for block_type, flags, payload, _offset in self._iter_raw_blocks():
            if block_type == BlockType.BLK_TXN_DATA:
                records = []
                self._txn_codec.decode_block(None, self._store, payload, flags,
                                             records.append)
                if stream_inst_id is not None:
                    records = [
                        r for r in records
                        if not hasattr(r, "stream_inst_id")
                        or r.stream_inst_id == stream_inst_id
                    ]
                if records:
                    yield records

    def iter_ext_blocks(self, ext_type: Optional[bytes] = None) -> Iterator[ExtBlock]:
        """Iterate over extension blocks in file order.

        Args:
            ext_type: If given, only blocks with this 4-byte tag are yielded.

        Yields:
            :class:`~trlog.ExtBlock` objects.
        """
        self._file.seek(0)
        for block_type, flags, payload, _offset in self._iter_raw_blocks():
            if block_type == BlockType.BLK_EXT:
                blk = ExtBlock.read_block(payload)
                if ext_type is None or blk.ext_type == bytes(ext_type):
                    yield blk

    def seek_time(self, time: int) -> int:
        """Return the file offset of the first value-change block whose start
        time is at or after ``time``.

        Raises:
            ZstNoIndexError: The file has no index, or there is no such block.
        """
        if self._index is None:
            raise ZstNoIndexError("No index in file; cannot seek by time")
        off = self._index.seek_vc(time)
        if off is None:
            raise ZstNoIndexError(f"No VC block found at or after time {time}")
        return off

    def read_signal(
        self, var_id: int, start: Optional[int] = None, end: Optional[int] = None
    ) -> List[VcChange]:
        """Return the value changes of one variable.

        Derived signals are not evaluated here; use :meth:`read_derived`.

        Args:
            var_id: Variable ID.
            start: Earliest time to include. When given, the index is used to
                read only the blocks in range.
            end: Latest time to include.

        Returns:
            A list of :class:`~trlog.VcChange`, sorted by time.
        """
        changes: List[VcChange] = []

        if self._index is not None and start is not None:
            end_t = end if end is not None else (1 << 63)
            offsets = self._index.find_vc_blocks(start, end_t)
            for off in offsets:
                self._file.seek(off)
                blk_type, flags, payload, _ = self._read_one_block()
                if blk_type != BlockType.BLK_VC_DATA:
                    continue
                for c in self._decode_vc_block(payload, flags) or ():
                    if c.var_id == var_id:
                        if start is None or c.time >= start:
                            if end is None or c.time <= end:
                                changes.append(c)
        else:
            for block_changes in self.iter_vc_blocks():
                for c in block_changes:
                    if c.var_id == var_id:
                        if start is None or c.time >= start:
                            if end is None or c.time <= end:
                                changes.append(c)
        return changes

    def read_stream(
        self,
        stream_inst_id: int,
        start: Optional[int] = None,
        end: Optional[int] = None,
    ) -> list:
        """Return the transactions of one stream.

        Args:
            stream_inst_id: Stream instance ID.
            start: Earliest start time to include.
            end: Latest start time to include.

        Returns:
            The stream's ``TxnFull`` and ``TxnBegin`` records whose start time
            is in range. Attribute, end, link and metadata records are not
            included; use :meth:`iter_txn_blocks` for those.
        """
        records = []
        for block_recs in self.iter_txn_blocks(stream_inst_id=stream_inst_id):
            for r in block_recs:
                t = getattr(r, 'start_time', None)
                if t is None:
                    continue
                if start is not None and t < start:
                    continue
                if end is not None and t > end:
                    continue
                records.append(r)
        return records

    def find_streams(self, kind: Optional[str] = None) -> Dict[int, HStream]:
        """Return stream instances, optionally filtered by stream kind.

        Args:
            kind: If given, only streams whose stream type was registered
                with this ``kind`` are returned.

        Returns:
            A dictionary from stream instance ID to ``HStream``, across all
            hierarchies.
        """
        result: Dict[int, HStream] = {}
        # Build stream_type_id -> kind string lookup
        kind_by_type: Dict[int, str] = {}
        for sid, decl in self._typereg._stream_decls.items():
            if decl.kind_str_id:
                try:
                    resolved = self._strtab.lookup(decl.kind_str_id)
                    kind_by_type[sid] = resolved
                except KeyError:
                    pass
        for hier in self._hierarchies.values():
            for inst_id, hstream in hier.streams.items():
                if kind is None:
                    result[inst_id] = hstream
                else:
                    stream_kind = kind_by_type.get(hstream.stream_type_id, "")
                    if stream_kind == kind:
                        result[inst_id] = hstream
        return result

    # ------------------------------------------------------------------
    # Transparent-metadata query API (impl-plan §2.3 / §2.6) — codec-free
    # ------------------------------------------------------------------

    def _resolve_typed_attr(self, attr):
        """Resolve a TypedAttr's wire form to Python values (string ids → text)."""
        base = attr.attr_type & ~int(AttrType.AT_ARRAY)
        if attr.attr_type & int(AttrType.AT_ARRAY):
            if base == int(AttrType.AT_STR):
                return [self._strtab.lookup(v) for v in attr.value]
            return list(attr.value)
        if base == int(AttrType.AT_STR):
            return self._strtab.lookup(attr.value)
        return attr.value

    def _attrs_to_dict(self, attrs) -> Dict[str, object]:
        out: Dict[str, object] = {}
        for attr in attrs:
            key = self._strtab.lookup(attr.key_str_id)
            out[key] = self._resolve_typed_attr(attr)   # last-wins on conflict
        return out

    def trace_metadata(self) -> Dict[str, object]:
        """Return the trace metadata as ``{key: value}``, with string values
        resolved."""
        if self._meta is None:
            return {}
        return self._attrs_to_dict(self._meta.attrs)

    def stream_metadata(self, stream_type_id: int) -> Dict[str, object]:
        """Return a stream type's metadata as ``{key: value}``, with string
        values resolved. Empty if the stream type does not exist."""
        decl = self._typereg.get_stream_decl(stream_type_id)
        if decl is None:
            return {}
        return self._attrs_to_dict(decl.metadata)

    def stream_codec(self, stream_type_id: int) -> Optional[Tuple[str, int, bytes]]:
        """Return a stream type's codec as ``(codec_id, version, params)``,
        as set with :meth:`~trlog.TrlWriter.set_stream_codec`. ``None`` if the
        stream type has no codec or does not exist.

        This is informational: blocks are decoded by the codec ID each block
        carries."""
        decl = self._typereg.get_stream_decl(stream_type_id)
        if decl is None or not decl.codec_id_str:
            return None
        return (self._strtab.lookup(decl.codec_id_str), decl.codec_version,
                bytes(decl.params))

    def profiles(self, stream_type_id: int) -> List[str]:
        """Return the profile IDs added to a stream type with
        :meth:`~trlog.TrlWriter.add_stream_profile`."""
        meta = self.stream_metadata(stream_type_id)
        val = meta.get(WellKnownAttr.PROFILE, [])
        if isinstance(val, list):
            return list(val)
        return [val]

    # ------------------------------------------------------------------
    # Opaque keyed (aux) metadata (impl-plan §2.4)
    # ------------------------------------------------------------------

    def aux_entries(self, owner_stream_id: Optional[int] = None):
        """List aux blobs from the index without reading them.

        Args:
            owner_stream_id: If given, only this owner's entries.

        Returns:
            A list of ``(owner_stream_id, key, ordinal, offset, length)``
            tuples. Empty if the file has no index.
        """
        if self._index is None:
            return []
        return self._index.aux_entries(owner_stream_id)

    def aux_keys(self, owner_stream_id: int):
        """Return the distinct aux keys of an owner, in first-seen order."""
        seen = []
        for own, key, _ord, _off, _len in self.aux_entries(owner_stream_id):
            if key not in seen:
                seen.append(key)
        return seen

    def read_aux(self, owner_stream_id: int, key: int) -> List[bytes]:
        """Return the aux blobs stored under ``(owner_stream_id, key)``, in
        ordinal order, as ``bytes``."""
        if self._index is None:
            return []
        out: List[bytes] = []
        for _ord, off, _len in self._index.find_aux(owner_stream_id, key):
            self._file.seek(off)
            _btype, flags, payload, _ = self._read_one_block()
            out.append(AuxBlock.decode(payload, flags=flags))
        return out

    # ------------------------------------------------------------------
    # Structural codecs on value-change blocks (impl-plan §2.2, §4.2)
    # ------------------------------------------------------------------

    def _block_codec_id(self, payload) -> Optional[str]:
        """Resolve the codec-id string of a FLAG_STRUCT_CODEC block from its
        interned prefix (``uvarint codec_id_str``), without decoding it."""
        try:
            cid, _ = decode_uvarint(payload, 0)
            return self._strtab.lookup(cid)
        except (KeyError, IndexError, ValueError):
            return None

    def _resolve_block_codec(self, payload):
        cid = self._block_codec_id(payload)
        return _lookup_vc_codec(cid) if cid is not None else None

    def vc_block_report(self):
        """Enumerate every value-change block as
        ``{codec, available, start, end}`` *without decoding payloads* — the
        skip-unknown discovery surface (§4.2). ``codec`` is ``None`` for the
        default core path; ``available`` is False when a structural codec is not
        registered, and the reader skips such blocks rather than erroring."""
        # Map block file offset -> (start, end) from the index, if present.
        ranges = {}
        if self._index is not None:
            for s, e, off in self._index._vc_entries:
                ranges[off] = (s, e)
        report = []
        self._file.seek(0)
        for block_type, flags, payload, offset in self._iter_raw_blocks():
            if block_type != BlockType.BLK_VC_DATA:
                continue
            s, e = ranges.get(offset, (None, None))
            if flags & FLAG_STRUCT_CODEC:
                cid = self._block_codec_id(payload)
                report.append({
                    "codec": cid,
                    "available": _lookup_vc_codec(cid) is not None,
                    "start": s, "end": e,
                })
            else:
                report.append({"codec": None, "available": True,
                               "start": s, "end": e})
        return report

    def missing_codecs(self):
        """Sorted distinct codec ids referenced by blocks but not registered."""
        return sorted({r["codec"] for r in self.vc_block_report()
                       if r["codec"] is not None and not r["available"]})

    # ------------------------------------------------------------------
    # Stream dependency resolution (impl-plan §2.5 / design §4.4)
    # ------------------------------------------------------------------

    def dependency_catalog(self) -> Dict[int, List[int]]:
        """Return the declared stream dependencies as
        ``{stream_type_id: [input_stream_type_id, ...]}``."""
        return self._typereg.dependency_catalog()

    def dependencies(self, stream_type_id: int) -> List[int]:
        """Return the stream types that ``stream_type_id`` depends on."""
        decl = self._typereg.get_stream_decl(stream_type_id)
        return list(decl.input_streams) if decl is not None else []

    def dependency_graph(self):
        """Return a :class:`~trlog.DependencyGraph` built from
        :meth:`dependency_catalog`."""
        from ._deps import DependencyGraph
        return DependencyGraph.from_catalog(self.dependency_catalog())

    def dependency_order(self) -> List[int]:
        """Return the stream types in dependency order, inputs first.

        Raises:
            DependencyError: The stored dependencies contain a cycle.
        """
        return self.dependency_graph().topo_order()

    def materializer(self, compute):
        """Return a :class:`~trlog.Materializer` over the stream dependencies.

        Args:
            compute: Called as ``compute(stream_id, inputs)``, where
                ``inputs`` maps each input stream ID to its computed result.
                It is called at most once per stream, when the stream or a
                stream that depends on it is first requested.
        """
        from ._deps import Materializer
        return Materializer(self.dependency_graph(), compute)

    # ------------------------------------------------------------------
    # Derived (virtual) signals (impl-plan §4 / design §5.2)
    # ------------------------------------------------------------------

    def derived_signals(self) -> Dict[int, dict]:
        """Return the derived signals in the trace.

        Returns:
            ``{derived_id: info}``, where ``info`` is a dictionary with
            ``name``, ``bit_width``, ``inputs`` (input IDs), ``materialized``
            (whether values were also stored) and ``def`` (the decoded
            definition).
        """
        if self._derived_cache is not None:
            return self._derived_cache
        from ._derived import DerivedDef, DERIVED_AUX_OWNER
        out: Dict[int, dict] = {}
        for own, key, _ordn, _off, _ln in self.aux_entries(DERIVED_AUX_OWNER):
            payloads = self.read_aux(own, key)
            if not payloads:
                continue
            payload = payloads[0]
            name_id, o = decode_uvarint(payload, 0)
            defn = DerivedDef.decode(payload[o:])
            out[key] = {
                "name": self._strtab.lookup(name_id),
                "bit_width": defn.bit_width,
                "inputs": list(defn.input_var_ids),
                "materialized": defn.materialized,
                "def": defn,
            }
        self._derived_cache = out
        return out

    def read_derived(self, var_id: int) -> List[VcChange]:
        """Evaluate a derived signal.

        Inputs that are derived signals are evaluated first; each input is
        evaluated once per call. A change is reported at each input change
        time where the computed value differs from the previous value.

        Args:
            var_id: Derived signal ID from
                :meth:`~trlog.TrlWriter.add_derived_signal`.

        Returns:
            A list of :class:`~trlog.VcChange`, sorted by time.

        Raises:
            KeyError: No derived signal has this ID.
            DependencyError: The stored definitions contain a cycle.
        """
        from ._derived import evaluate_changes
        from ._deps import DependencyGraph, Materializer
        defs = self.derived_signals()
        if var_id not in defs:
            raise KeyError(f"no derived signal with var id {var_id}")
        catalog = {vid: info["inputs"] for vid, info in defs.items()}
        graph = DependencyGraph.from_catalog(catalog)

        def compute(node, inputs):
            if node in defs:
                defn = defs[node]["def"]
                return evaluate_changes(defn, [inputs[i] for i in defn.input_var_ids])
            # base signal: read its raw changes
            return [(c.time, c.value) for c in self.read_signal(node)]

        changes = Materializer(graph, compute).materialize(var_id)
        return [VcChange(var_id=var_id, time=t, value=v) for t, v in changes]

    # ------------------------------------------------------------------
    # Internal scan
    # ------------------------------------------------------------------

    def _scan_file(self) -> None:
        """Read through the file and populate metadata."""
        # --- Phase 1: read file header and index via direct seeks -----------
        self._file.seek(0)
        blk_type, flags, payload, _ = self._read_one_block()
        if blk_type != BlockType.BLK_FILE_HDR:
            raise ZstFormatError("First block is not BLK_FILE_HDR")
        self._parse_file_header(payload)
        hdr_off = 8 + 2 + 1 + 8 + 4
        cr_id, hdr_off = decode_uvarint(payload, hdr_off)
        dt_id, hdr_off = decode_uvarint(payload, hdr_off)
        index_offset, = struct.unpack_from('<Q', payload, hdr_off)

        # Read index first (it contains metadata-block offsets in new files)
        if index_offset:
            self._file.seek(index_offset)
            try:
                blk_type, flags, idx_payload, _ = self._read_one_block()
                if blk_type == BlockType.BLK_INDEX:
                    self._index = IndexBlock()
                    self._index.read_block(idx_payload, flags=flags)
            except (struct.error, EOFError):
                pass

        # --- Phase 2: resolve metadata blocks --------------------------------
        if (self._index is not None
                and self._index.strtab_offset
                and self._index.typereg_offset):
            # Fast path: index tells us exactly where everything is
            for ho in self._index.hier_offsets:
                self._file.seek(ho)
                _, hflags, hpayload, _ = self._read_one_block()
                blk = HierarchyBlock()
                blk.read_block(hpayload, flags=hflags)
                self._hierarchies[blk.hier_id] = blk

            self._file.seek(self._index.strtab_offset)
            _, sflags, spayload, _ = self._read_one_block()
            self._strtab.read_block(spayload, flags=sflags)

            self._file.seek(self._index.typereg_offset)
            _, tflags, tpayload, _ = self._read_one_block()
            self._typereg.read_block(tpayload, flags=tflags)

            if self._index.meta_offset:
                self._file.seek(self._index.meta_offset)
                _, mflags, mpayload, _ = self._read_one_block()
                self._meta = MetaBlock()
                self._meta.read_block(mpayload, flags=mflags)
        else:
            # Slow path: linear scan, but skip data-block payloads entirely
            self._file.seek(0)
            skip = {BlockType.BLK_VC_DATA, BlockType.BLK_TXN_DATA, BlockType.BLK_EXT,
                    BlockType.BLK_AUX, BlockType.BLK_INDEX}
            for block_type, flags, payload, _ in self._iter_raw_blocks(skip_types=skip):
                if payload is None:
                    continue
                if block_type == BlockType.BLK_STR_TABLE:
                    self._strtab.read_block(payload, flags=flags)
                elif block_type == BlockType.BLK_TYPE_REG:
                    self._typereg.read_block(payload, flags=flags)
                elif block_type == BlockType.BLK_HIERARCHY:
                    blk = HierarchyBlock()
                    blk.read_block(payload, flags=flags)
                    self._hierarchies[blk.hier_id] = blk
                elif block_type == BlockType.BLK_META:
                    self._meta = MetaBlock()
                    self._meta.read_block(payload, flags=flags)

    def _parse_file_header(self, payload: bytes) -> None:
        off = 0
        magic = payload[off:off+8]; off += 8
        if magic != TRL_MAGIC:
            raise ZstMagicError(f"Bad magic: {magic!r}")
        self._version_major = payload[off]; off += 1
        self._version_minor = payload[off]; off += 1
        if self._version_major not in (1, 2):
            raise ZstVersionError(
                f"Unsupported version_major={self._version_major}"
            )
        self._timescale_exp = struct.unpack_from('b', payload, off)[0]; off += 1
        self._timezero = struct.unpack_from('<q', payload, off)[0]; off += 8

    def _build_sig_types(self) -> Dict[int, tuple]:
        """Build var_id → (SignalEncoding, bit_width) from type registry."""
        result = {}
        # Baseline: sig_type_id → (enc, bw) so VcDataBlock can fall back to it
        for sig_id, entry in self._typereg._signal_types.items():
            result[sig_id] = (entry.encoding, entry.bit_width)
        # Overlay: var_id → (enc, bw) resolved via hierarchy's var→sig_type mapping
        for hier in self._hierarchies.values():
            for var_id, hvar in hier.vars.items():
                sig = self._typereg._signal_types.get(hvar.sig_type_id)
                if sig is not None:
                    result[var_id] = (sig.encoding, sig.bit_width)
        return result

    def _build_txn_schemas(self) -> Dict[int, list]:
        result = {}
        for txn_id, entry in self._typereg._txn_schemas.items():
            result[txn_id] = [f.field_type for f in entry.fields]
        return result

    def _iter_raw_blocks(self, skip_types=frozenset()):
        """Yield (block_type, flags, payload_bytes, file_offset) for all blocks.

        For block types in *skip_types* the payload is not read into memory;
        ``None`` is yielded in its place and the file position is advanced past
        the block.
        """
        while True:
            file_offset = self._file.tell()
            header = self._file.read(BLOCK_HEADER_SIZE)
            if not header:
                break
            if len(header) < BLOCK_HEADER_SIZE:
                break
            block_type_byte, flags, block_length = struct.unpack('<BBQ', header)
            payload_length = block_length - BLOCK_HEADER_SIZE
            if payload_length < 0:
                break
            try:
                block_type = BlockType(block_type_byte)
            except ValueError:
                self._file.seek(payload_length, 1)
                continue
            if block_type in skip_types:
                self._file.seek(payload_length, 1)
                yield block_type, flags, None, file_offset
            else:
                payload = self._file.read(payload_length)
                if len(payload) < payload_length:
                    break
                yield block_type, flags, payload, file_offset

    def _read_one_block(self):
        """Read a single block at the current file position."""
        file_offset = self._file.tell()
        header = self._file.read(BLOCK_HEADER_SIZE)
        if len(header) < BLOCK_HEADER_SIZE:
            raise EOFError("Truncated block header")
        block_type_byte, flags, block_length = struct.unpack('<BBQ', header)
        payload_length = block_length - BLOCK_HEADER_SIZE
        payload = self._file.read(payload_length)
        block_type = BlockType(block_type_byte)
        return block_type, flags, payload, file_offset

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def close(self) -> None:
        """Close the file if the reader opened it."""
        if self._owns_file:
            self._file.close()

    def __enter__(self) -> 'TrlReader':
        return self

    def __exit__(self, *args) -> None:
        self.close()
