"""Query engine over a trace.

The scan runs on the pure-Python reader, or on the optional C engine
(``_cengine``, libtrl's ``trl_qscan_*``) when the native library loads.
Both give the same answers; Python is the reference and the fallback.

:class:`Trace` opens a file, builds a catalog of streams and transaction
types, and scans TXN_DATA blocks into joined :class:`Txn` records
(FULL/BEGIN/ATTR/END/LINK/META merged per txn id). The verb methods
(``find``, ``count``, ``show``, ``info``, ``stream_summary``, ``schema``)
return plain Python objects; rendering lives in ``_format``.

Design background: query_cli_design.md §7.1.
"""

from __future__ import annotations

import builtins
import fnmatch
import os
import struct
import time as _time
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from typing import Dict, Iterable, Iterator, List, Optional, Sequence, Tuple

from .._exceptions import ZstError
from .._reader import TRL_MAGIC, TrlReader
from .._types import (
    BLOCK_HEADER_SIZE, BlockType, FieldType, HStream, LinkType, Radix,
    TxnAttrRecord, TxnBegin, TxnEnd, TxnFull, TxnLink, TxnMeta,
)
from . import _cengine
from ._expr import FieldSpec, Filter, QueryError, SchemaView, compile_filter
from ._rows import FieldKey, Rows, group_order, key_columns
from ._values import MISSING, EnumVal, LogicVal, normalize_field, time_unit_name

MIXED_STREAM = 0xFFFFFFFF

KIND_NAMES = {
    FieldType.FT_U8: "u8", FieldType.FT_U16: "u16", FieldType.FT_U32: "u32",
    FieldType.FT_U64: "u64", FieldType.FT_I8: "i8", FieldType.FT_I16: "i16",
    FieldType.FT_I32: "i32", FieldType.FT_I64: "i64", FieldType.FT_F32: "f32",
    FieldType.FT_F64: "f64", FieldType.FT_BOOL: "bool", FieldType.FT_STRING: "str",
    FieldType.FT_BITVEC: "bitvec", FieldType.FT_BYTES: "bytes",
    FieldType.FT_TIME: "time", FieldType.FT_ENUM: "enum",
    FieldType.FT_LOGICVEC: "logicvec",
}

INT_KINDS = frozenset({"u8", "u16", "u32", "u64", "i8", "i16", "i32", "i64"})

RADIX_NAMES = {
    Radix.RX_HEX: "hex", Radix.RX_DEC: "dec", Radix.RX_OCT: "oct",
    Radix.RX_BIN: "bin", Radix.RX_UDEC: "udec", Radix.RX_SIGNED: "signed",
    Radix.RX_REAL: "real", Radix.RX_STRING: "string", Radix.RX_TIME: "time",
}

LINK_KIND_NAMES = {
    LinkType.LT_CAUSE_EFFECT: "cause", LinkType.LT_PARENT_CHILD: "parent_child",
    LinkType.LT_RELATED: "related", LinkType.LT_CUSTOM: "custom",
}
LINK_KINDS = tuple(LINK_KIND_NAMES.values())
"""Link kind names, as used by :meth:`Trace.links` and in :class:`Link`:
``("cause", "parent_child", "related", "custom")``."""


# ---------------------------------------------------------------------------
# Catalog records
# ---------------------------------------------------------------------------

@dataclass
class StreamInfo:
    """A stream instance from the trace's hierarchy.

    Attributes:
        id: Stream instance id.
        path: Dotted path from the scope tree, e.g. ``"top.env.axi0.mon"``.
            The hierarchy name is not part of it.
        decl_id: Id of the stream declaration (stream type).
        decl_name: Name of the stream declaration.
        kind: Kind string of the declaration, e.g. ``"axi"``; may be empty.
        hierarchy: Name of the hierarchy that declares the stream.
        metadata: Declaration metadata merged with the stream's own
            attributes.
    """

    id: int
    path: str
    decl_id: int
    decl_name: str
    kind: str
    hierarchy: str
    metadata: Dict[str, object] = field(default_factory=dict)


@dataclass
class FieldInfo:
    """One field of a transaction type.

    Attributes:
        name: Field name.
        kind: Value kind: ``u8``..``u64``, ``i8``..``i64``, ``f32``, ``f64``,
            ``bool``, ``str``, ``bitvec``, ``logicvec``, ``bytes``,
            ``time`` or ``enum``.
        radix: Display radix hint, e.g. ``"hex"`` or ``"dec"``.
        enum_name: For an enum field, the enum type's name.
        enum_labels: For an enum field, ``{integer value: label}``.
    """

    name: str
    kind: str
    radix: str
    enum_name: Optional[str] = None
    enum_labels: Optional[Dict[int, str]] = None


@dataclass
class TypeInfo:
    """A transaction type (schema).

    Attributes:
        id: Type id.
        name: Type name, e.g. ``"axi_wr"``.
        fields: The fields in declaration order.
    """

    id: int
    name: str
    fields: List[FieldInfo]

    def field(self, name: str) -> Optional[FieldInfo]:
        """Return the field called ``name``, or None."""
        for f in self.fields:
            if f.name == name:
                return f
        return None


@dataclass
class Link:
    """One link edge as seen from a transaction.

    Attributes:
        kind: One of :data:`LINK_KINDS`.
        dir: ``"out"`` when this transaction is the source, ``"in"`` when
            it is the target.
        other: Id of the transaction at the other end.
        label: Optional edge label.
    """

    kind: str
    dir: str        # "out" (this txn is the source) or "in"
    other: int      # txn id at the other end
    label: Optional[str] = None


class Txn:
    """One transaction, joined from its FULL/BEGIN/ATTR/END/LINK/META records.

    Attributes:
        id: Transaction id.
        stream_id: Stream instance id.
        stream: Stream path.
        type_id: Type id.
        type: Type name.
        start: Start time in ticks.
        end: End time in ticks, or None while the transaction is open.
        parent: Parent transaction id, or None.
        fields: ``{field name: value}``. Fields with no value are absent.
            Values are normalised (see :class:`EnumVal`, :class:`LogicVal`).
        meta: Per-transaction metadata ``{key: string}``.
        links: :class:`Link` records. Filled only when the scan read links
            (``want_links``, or the navigation methods).
        children: Child ids sorted by (start, id), or None when not computed.
            Filled by :meth:`Trace.graph` and the methods that use it.
    """

    __slots__ = ("id", "stream_id", "stream", "type_id", "type", "start", "end",
                 "parent", "fields", "meta", "links", "children")

    def __init__(self, id, stream_id, stream, type_id, type_name, start, end, parent):
        self.id = id
        self.stream_id = stream_id
        self.stream = stream
        self.type_id = type_id
        self.type = type_name
        self.start = start
        self.end = end                  # None while open
        self.parent = parent            # None when there is no parent
        self.fields: Dict[str, object] = {}
        self.meta: Dict[str, str] = {}
        self.links: List[Link] = []
        self.children: Optional[List[int]] = None   # filled by show()

    @property
    def ref(self) -> str:
        """The ``t:ID`` reference used by the CLI."""
        return f"t:{self.id}"

    @property
    def open(self) -> bool:
        """True when the transaction never ended."""
        return self.end is None

    @property
    def dur(self) -> Optional[int]:
        """``end - start`` in ticks, or None while open."""
        return None if self.end is None else self.end - self.start

    def __repr__(self) -> str:
        return (f"Txn(t:{self.id} {self.stream} {self.type} "
                f"{self.start}..{self.end} {self.fields})")


@dataclass
class ScanStats:
    """What a query read; the CLI's JSON ``scan`` section.

    Attributes:
        blocks_total: Transaction blocks in the file.
        blocks_read: Blocks the query read.
        ms: Scan time in milliseconds.
        index: True when the file's block index was used.
        qindex: True when a query index was used (not written yet).
        duplicate_ids: Transaction ids seen more than once.
        cached: True when the result came from the graph cached by an
            earlier call.
    """

    blocks_total: int = 0
    blocks_read: int = 0
    ms: float = 0.0
    index: bool = False
    qindex: bool = False        # query index (§7.4); not written yet
    duplicate_ids: int = 0
    cached: bool = False        # served from the graph cached by an earlier call

    def to_dict(self) -> dict:
        """The JSON ``scan`` object."""
        d = {"blocks_read": self.blocks_read,
             "blocks_skipped": self.blocks_total - self.blocks_read,
             "ms": round(self.ms, 1), "index": self.index, "qindex": self.qindex}
        if self.duplicate_ids:
            d["duplicate_ids"] = self.duplicate_ids
        if self.cached:
            d["cached"] = True
        return d


@dataclass
class Selection:
    """The resolved ``-s`` / ``-T`` / ``-t`` / EXPR selection of a query.

    Build one with :meth:`Trace.select`.

    Attributes:
        stream_globs: Stream globs as given.
        type_names: Type names or globs as given.
        window: Inclusive ``(lo, hi)`` time window in ticks; None for an
            open side.
        where: Filter expression text, or None.
        stream_ids: Selected stream ids, or None for all streams.
        type_ids: Selected type ids, or None for all types.
        filter: The compiled :class:`Filter`, or None.
    """

    stream_globs: Tuple[str, ...] = ()
    type_names: Tuple[str, ...] = ()
    window: Tuple[Optional[int], Optional[int]] = (None, None)
    where: Optional[str] = None
    stream_ids: Optional[frozenset] = None   # None = all streams
    type_ids: Optional[frozenset] = None     # None = all types
    filter: Optional[Filter] = None

    def query_dict(self) -> dict:
        """The selection as the CLI's JSON ``query`` object."""
        q = {}
        if self.where:
            q["where"] = self.where
        if self.stream_globs:
            q["streams"] = list(self.stream_globs)
        if self.type_names:
            q["types"] = list(self.type_names)
        if self.window != (None, None):
            q["time"] = list(self.window)
        return q


@dataclass
class FindResult:
    """Result of :meth:`Trace.find` and :meth:`Trace.active`.

    Attributes:
        selection: The selection that was run.
        txns: Every match, in the requested order (no paging).
        stats: What the scan read.
    """

    selection: Selection
    txns: List[Txn]              # every match, in the requested order
    stats: ScanStats

    @property
    def matched(self) -> int:
        """Number of matches."""
        return len(self.txns)

    def span(self) -> Optional[Tuple[int, int]]:
        """``(first start, last start-or-end)`` of the matches, or None."""
        if not self.txns:
            return None
        lo = min(t.start for t in self.txns)
        hi = max(t.start if t.end is None else t.end for t in self.txns)
        return (lo, hi)

    def by_stream(self) -> Dict[str, int]:
        """Match counts per stream path, largest first."""
        return dict(Counter(t.stream for t in self.txns).most_common())


@dataclass
class TreeResult:
    """Result of :meth:`Trace.tree`.

    Attributes:
        focus: The transaction asked for.
        root: The topmost ancestor reached (the focus when it has none).
        nodes: ``(depth, txn)`` in tree pre-order; depth is relative to the
            focus, negative for ancestors.
        truncated: True when ``limit`` cut the descendants.
        hidden_children: Descendants of the focus left out by depth or limit.
        dangling_parent: A parent id that is not in the trace, or None.
        stats: What the scan read.
    """

    focus: Txn
    root: Txn
    nodes: List[Tuple[int, Txn]]        # (depth relative to focus, txn), tree order
    truncated: bool                     # --limit cut the descendants
    hidden_children: int                # focus descendants cut by depth/limit
    dangling_parent: Optional[int]      # parent id that is not in the trace
    stats: ScanStats


@dataclass
class LinkResult:
    """Result of :meth:`Trace.links`.

    Attributes:
        focus: The transaction asked for.
        nodes: ``(distance, txn)``; distance is negative towards causes,
            positive towards effects, 0 for the focus.
        edges: ``(src, dst, kind, label)`` for edges between returned nodes,
            from the furthest cause down.
        missing: Link ends that are not in the trace.
        truncated: True when ``limit`` cut the walk.
        stats: What the scan read.
    """

    focus: Txn
    nodes: List[Tuple[int, Txn]]        # (signed distance, txn); up < 0 < down
    edges: List[Tuple[int, int, str, Optional[str]]]   # (src, dst, kind, label)
    missing: List[int]                  # link ends that are not in the trace
    truncated: bool
    stats: ScanStats


@dataclass
class AroundResult:
    """Result of :meth:`Trace.around`.

    Attributes:
        focus: The anchor transaction, or None for a time anchor.
        at: Anchor time in ticks (the anchor transaction's start).
        selection: The selection used for the neighbours.
        txns: Rows in (start, id) order; an anchor transaction is included.
        n_before: In ``n`` mode, the number of rows before the anchor; None
            in ``window`` mode.
        more_before: True when more rows exist before the first one.
        more_after: True when more rows exist after the last one.
        stats: What the scan read.
    """

    focus: Optional[Txn]                # anchor txn, or None for @TIME
    at: int                             # anchor time (the anchor txn's start)
    selection: Selection
    txns: List[Txn]                     # in (start, id) order, anchor included
    n_before: Optional[int]             # -n mode: rows before the anchor
    more_before: bool
    more_after: bool
    stats: ScanStats


@dataclass
class CountResult:
    """Result of :meth:`Trace.count`.

    Attributes:
        selection: The selection that was run.
        keys: The ``by`` keys.
        groups: ``(key values, count)``, largest count first, then by key.
            A missing key value is None. Without keys, one ``((), n)``
            entry, or none when nothing matched.
        matched: Total number of matches.
        stats: What the scan read.
    """

    selection: Selection
    keys: List[str]
    groups: List[Tuple[tuple, int]]     # (key values, count), largest first
    matched: int
    stats: ScanStats


# ---------------------------------------------------------------------------
# Trace
# ---------------------------------------------------------------------------

class Trace:
    """An open trace plus the query verbs over it.

    Open one with :func:`open`. The catalog (streams and types) is read
    when the file opens; each verb method scans the blocks it needs.
    All times, in arguments and results, are integer ticks in the trace's
    time unit.

    Example::

        with trlog.query.open("run.trl") as q:
            r = q.find("resp != 'OKAY'", streams="top.*")
            for t in r.txns[:10]:
                print(t.ref, t.stream, t.fields)

    Args:
        path: Trace file.
        engine: ``"auto"``, ``"c"`` or ``"python"``; see :func:`open`.

    Raises:
        QueryError: ``file_error`` when the file is not a readable trace,
            ``bad_engine`` or ``engine_unavailable`` for ``engine``.

    Attributes:
        path: The trace path.
        timescale_exp: Time unit as a power of ten in seconds (-12 = ps).
        time_unit: Time unit name, e.g. ``"ps"``.
        streams: ``{stream id: StreamInfo}``.
        types: ``{type id: TypeInfo}``.
        engine: The engine in use: ``"c"`` or ``"python"``.
        engine_note: Why the C engine is not in use, when it is not.
        c_fallbacks: Scans the C engine handed back to Python, with the
            reason.
    """

    def __init__(self, path: str, engine: Optional[str] = None) -> None:
        self.path = str(path)
        _check_header(self.path)
        try:
            self._r = TrlReader(self.path)
        except (ZstError, EOFError, ValueError, OSError, MemoryError,
                struct.error) as e:
            raise QueryError("file_error", f"cannot read {self.path}: {e}",
                             path=self.path)
        self._strtab = self._r.string_table
        self.timescale_exp = self._r.timescale_exp
        self.time_unit = time_unit_name(self.timescale_exp)
        self.streams: Dict[int, StreamInfo] = {}
        self.stream_id_collisions: List[int] = []
        self.types: Dict[int, TypeInfo] = {}
        self._types_by_name: Dict[str, TypeInfo] = {}
        self._build_types()
        self._build_streams()
        self._streams_by_path = {s.path: s for s in self.streams.values()}
        self._graph_cache: Optional[Dict[int, Txn]] = None
        self._graph_stats: Optional[ScanStats] = None
        self._span: Optional[tuple] = None
        self._cscan = None
        #: Scans the C engine handed back to Python, with the reason.
        self.c_fallbacks: List[str] = []
        self._select_engine(engine)

    def _select_engine(self, engine: Optional[str]) -> None:
        """``engine``: "auto" (C when libtrl loads, else Python), "c"
        (required) or "python". Default: $TRLOG_ENGINE, else "auto"."""
        want = (engine or os.environ.get("TRLOG_ENGINE") or "auto").lower()
        if want not in ("auto", "c", "python"):
            raise QueryError("bad_engine", f"unknown engine '{want}'",
                             hint="use auto, c or python (TRLOG_ENGINE)")
        self.engine = "python"
        self.engine_note: Optional[str] = None
        if want == "python":
            return
        lib = _cengine.load()
        if lib is not None:
            try:
                self._cscan = _cengine.CScan(lib, self)
                self.engine = "c"
                return
            except OSError as e:
                self.engine_note = str(e)
        else:
            self.engine_note = _cengine.load_error()
        if want == "c":
            raise QueryError("engine_unavailable",
                             f"the C scan engine is not available: {self.engine_note}",
                             hint="build libtrl (cmake --build build) or set "
                                  "TRL_LIBRARY_PATH; TRLOG_ENGINE=auto falls back "
                                  "to Python")

    # -- lifecycle --------------------------------------------------------

    def close(self) -> None:
        """Release the file and the C engine state."""
        if self._cscan is not None:
            self._cscan.close()
            self._cscan = None
        self._r.close()

    def __enter__(self) -> "Trace":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    # -- catalog ----------------------------------------------------------

    def _str(self, sid: int) -> str:
        if not sid:
            return ""
        try:
            return self._strtab.lookup(sid)
        except (KeyError, IndexError):
            return f"<str#{sid}>"

    def _build_types(self) -> None:
        reg = self._r.type_registry
        for tid, entry in sorted(reg._txn_schemas.items()):
            fields = []
            for i, fd in enumerate(entry.fields):
                kind = KIND_NAMES.get(fd.field_type, f"kind{int(fd.field_type)}")
                fi = FieldInfo(name=self._str(fd.name_str_id) or f"f{i}", kind=kind,
                               radix=RADIX_NAMES.get(fd.display_hint, "hex"))
                if kind == "enum":
                    et = reg.get_enum_type(fd.enum_type_id)
                    if et is not None:
                        fi.enum_name = self._str(et.name_str_id)
                        fi.enum_labels = {v.integer_value: self._str(v.label_str_id)
                                          for v in et.values}
                    else:
                        fi.enum_labels = {}
                fields.append(fi)
            ti = TypeInfo(id=tid, name=self._str(entry.name_str_id) or f"type{tid}",
                          fields=fields)
            self.types[tid] = ti
            self._types_by_name.setdefault(ti.name, ti)

    def _node_attrs(self, node) -> Dict[str, object]:
        out: Dict[str, object] = {}
        for a in getattr(node, "attrs", ()):
            out[self._str(a.key_str_id)] = self._str(a.value_str_id)
        for a in getattr(node, "typed_attrs", ()):
            out[self._str(a.key_str_id)] = self._r._resolve_typed_attr(a)
        return out

    def _build_streams(self) -> None:
        reg = self._r.type_registry
        for hid, hier in self._r.hierarchies.items():
            hname = self._str(hier.header.name_str_id)

            def walk(scope, prefix):
                for hs in scope.streams:
                    self._add_stream(hs, prefix, hname, reg)
                for child in scope.children:
                    walk(child, prefix + [child.name])
            walk(self._r.scope_tree(hid), [])
            # Streams recorded in the flat map but not reachable from the tree
            for sid, hs in hier.streams.items():
                if sid not in self.streams:
                    self._add_stream(hs, [], hname, reg)

    def _add_stream(self, hs: HStream, prefix: List[str], hname: str, reg) -> None:
        if hs.stream_inst_id in self.streams:
            self.stream_id_collisions.append(hs.stream_inst_id)
            return
        decl = reg.get_stream_decl(hs.stream_type_id)
        meta = {}
        if decl is not None:
            meta.update(self._r.stream_metadata(hs.stream_type_id))
        meta.update(self._node_attrs(hs))
        path = ".".join([p for p in prefix if p] + [self._str(hs.name_str_id)])
        self.streams[hs.stream_inst_id] = StreamInfo(
            id=hs.stream_inst_id, path=path, decl_id=hs.stream_type_id,
            decl_name=self._str(decl.name_str_id) if decl else "",
            kind=self._str(decl.kind_str_id) if decl else "",
            hierarchy=hname, metadata=meta)

    def type_by_name(self, name: str) -> Optional[TypeInfo]:
        """Return the type called ``name`` (exact, no globs), or None."""
        return self._types_by_name.get(name)

    def stream_by_path(self, path: str) -> Optional[StreamInfo]:
        """Return the stream with this exact path, or None."""
        return self._streams_by_path.get(path)

    def schema_view(self, selected_types: Optional[Iterable[str]] = None) -> SchemaView:
        """The :class:`SchemaView` that expressions compile against.

        Args:
            selected_types: When given, unqualified field names resolve
                against these type names only (as with ``-T``).
        """
        types = {}
        for ti in self.types.values():
            types.setdefault(ti.name, {})
            for f in ti.fields:
                types[ti.name].setdefault(f.name, FieldSpec(f.kind, f.enum_labels))
        return SchemaView(timescale_exp=self.timescale_exp, types=types,
                          stream_paths=sorted(self._streams_by_path),
                          selected_types=set(selected_types) if selected_types else None)

    @property
    def has_index(self) -> bool:
        """True when the file has a block index."""
        return self._r._index is not None

    # -- selection --------------------------------------------------------

    def _match_stream_globs(self, globs: Sequence[str]) -> frozenset:
        ids = set()
        for g in globs:
            hit = [s.id for s in self.streams.values() if fnmatch.fnmatchcase(s.path, g)]
            if not hit:
                import difflib
                dym = difflib.get_close_matches(g, sorted(self._streams_by_path),
                                                n=3, cutoff=0.5)
                raise QueryError("unknown_stream", f"no stream matches '{g}'", name=g,
                                 did_you_mean=dym or None,
                                 hint="globs match the full dotted path; run "
                                      "`trlog streams TRACE` to list paths")
            ids.update(hit)
        return frozenset(ids)

    def _match_types(self, names: Sequence[str]) -> frozenset:
        ids = set()
        for n in names:
            hit = [t.id for t in self.types.values()
                   if fnmatch.fnmatchcase(t.name, n)]
            if not hit:
                import difflib
                dym = difflib.get_close_matches(n, sorted(self._types_by_name), n=3,
                                                cutoff=0.5)
                raise QueryError("unknown_type", f"no transaction type matches '{n}'",
                                 name=n, did_you_mean=dym or None,
                                 known_types=sorted(self._types_by_name))
            ids.update(hit)
        return frozenset(ids)

    def select(self, where: Optional[str] = None, streams=None, types=None,
               time: Tuple[Optional[int], Optional[int]] = (None, None)) -> Selection:
        """Resolve a selection for the verb methods' ``selection`` argument.

        Args:
            where: Filter expression, or None.
            streams: Stream path glob, or a list of globs.
            types: Type name or glob, or a list of them.
            time: Inclusive ``(lo, hi)`` window in ticks; None for an open
                side. Transactions overlapping it are selected.

        Returns:
            The resolved :class:`Selection`.

        Raises:
            QueryError: ``unknown_stream`` or ``unknown_type`` when a glob
                matches nothing, or an expression error.
        """
        sg = _as_tuple(streams)
        tn = _as_tuple(types)
        sel = Selection(stream_globs=sg, type_names=tn, window=tuple(time), where=where)
        if sg:
            sel.stream_ids = self._match_stream_globs(sg)
        if tn:
            sel.type_ids = self._match_types(tn)
        if where:
            names = {self.types[i].name for i in sel.type_ids} if sel.type_ids else None
            sel.filter = compile_filter(where, self.schema_view(names))
            self._apply_pushdown(sel)
        return sel

    def _apply_pushdown(self, sel: Selection) -> None:
        """Lift stream/type conjuncts of EXPR into the block selection (§7.2)."""
        for kind, arg in sel.filter.pushdown:
            if kind == "stream_eq":
                ids = frozenset(s.id for s in self.streams.values() if s.path == arg)
            elif kind == "stream_in":
                ids = frozenset(s.id for s in self.streams.values() if s.path in arg)
            elif kind == "stream_glob":
                ids = frozenset(s.id for s in self.streams.values()
                                if fnmatch.fnmatchcase(s.path, arg))
            elif kind == "type_eq":
                ids = frozenset(t.id for t in self.types.values() if t.name == arg)
                sel.type_ids = ids if sel.type_ids is None else sel.type_ids & ids
                continue
            elif kind == "type_in":
                ids = frozenset(t.id for t in self.types.values() if t.name in arg)
                sel.type_ids = ids if sel.type_ids is None else sel.type_ids & ids
                continue
            else:
                continue
            sel.stream_ids = ids if sel.stream_ids is None else sel.stream_ids & ids

    # -- scanning ---------------------------------------------------------

    def _block_plan(self, stream_ids: Optional[frozenset]):
        """List of (start, end, offset, sid) TXN blocks to visit, in file order,
        or None when the file has no usable index (linear scan)."""
        idx = self._r._index
        if idx is None or not idx._txn_entries:
            return None
        entries = sorted(idx._txn_entries, key=lambda e: e[2])
        if stream_ids is None:
            return entries
        return [e for e in entries if e[3] == MIXED_STREAM or e[3] in stream_ids]

    def _iter_records(self, stats: ScanStats, stream_ids, t_hi,
                      pending_open: Dict[int, int]) -> Iterator[list]:
        """Yield decoded record lists, one per visited TXN block."""
        r = self._r
        plan = self._block_plan(stream_ids)
        stats.index = r._index is not None
        if plan is None:
            r._file.seek(0)
            for btype, flags, payload, _off in r._iter_raw_blocks():
                if btype != BlockType.BLK_TXN_DATA:
                    continue
                stats.blocks_total += 1
                stats.blocks_read += 1
                recs: list = []
                r._txn_codec.decode_block(None, r._store, payload, flags, recs.append)
                yield recs
            return
        stats.blocks_total = len(r._index._txn_entries)
        for b_start, _b_end, off, sid in plan:
            # Early stop for an upper time bound: a block that starts after
            # t_hi can only matter for txns (of its stream) still open at t_hi.
            # Assumes a block's declared start is <= the times inside it.
            if t_hi is not None and b_start > t_hi:
                if sid == MIXED_STREAM:
                    if not any(pending_open.values()):
                        continue
                elif not pending_open.get(sid):
                    continue
            r._file.seek(off)
            btype, flags, payload, _ = r._read_one_block()
            if btype != BlockType.BLK_TXN_DATA:
                continue
            stats.blocks_read += 1
            recs = []
            r._txn_codec.decode_block(None, r._store, payload, flags, recs.append)
            yield recs

    def scan(self, stream_ids: Optional[frozenset] = None,
             type_ids: Optional[frozenset] = None,
             t_hi: Optional[int] = None,
             want_links: bool = False,
             want_meta: bool = True,
             max_txns: Optional[int] = None,
             stats: Optional[ScanStats] = None) -> Dict[int, Txn]:
        """Scan TXN blocks and return joined txns keyed by id, in the order
        they were first seen.

        ``stream_ids``/``type_ids`` restrict which txns are built (None = all).
        ``t_hi`` lets the scan skip blocks that start after it. With
        ``want_links`` every block is read, since a link record can live in
        any stream's block. ``max_txns``
        stops once that many txns have been started (for sampling).
        """
        stats = stats if stats is not None else ScanStats()
        t0 = _time.perf_counter()
        if self._cscan is not None:
            rows = self._c_run(stats, stream_ids, type_ids, want_links=want_links,
                               want_meta=want_meta, t_hi=t_hi, max_txns=max_txns)
            if rows is not None:
                stats.ms += (_time.perf_counter() - t0) * 1000.0
                return {t.id: t for t, _ in rows}
        txns: Dict[int, Txn] = {}
        pending_open: Dict[int, int] = defaultdict(int)
        types = self.types
        streams = self.streams

        def new_txn(rec, end) -> Optional[Txn]:
            if stream_ids is not None and rec.stream_inst_id not in stream_ids:
                return None
            if type_ids is not None and rec.txn_type_id not in type_ids:
                return None
            si = streams.get(rec.stream_inst_id)
            ti = types.get(rec.txn_type_id)
            t = Txn(rec.txn_id, rec.stream_inst_id,
                    si.path if si else f"<stream#{rec.stream_inst_id}>",
                    rec.txn_type_id, ti.name if ti else f"<type#{rec.txn_type_id}>",
                    rec.start_time, end, rec.parent_txn_id or None)
            if t.id in txns:
                stats.duplicate_ids += 1
            txns[t.id] = t
            return t

        def add_attrs(t: Txn, attrs) -> None:
            ti = types.get(t.type_id)
            for a in attrs:
                fi = ti.fields[a.field_idx] if ti and a.field_idx < len(ti.fields) else None
                if fi is None:
                    t.fields[f"f{a.field_idx}"] = a.value
                else:
                    t.fields[fi.name] = normalize_field(fi.kind, a.value, fi.enum_labels)

        # LINK records usually sit in the block of the *other* end's stream
        # (or later in time), so collecting links means visiting every block;
        # only the txns built stay restricted to the selection.
        block_ids = None if want_links else stream_ids
        block_t_hi = None if want_links else t_hi
        started = 0
        for recs in self._iter_records(stats, block_ids, block_t_hi, pending_open):
            for rec in recs:
                cls = type(rec)
                if cls is TxnFull:
                    t = new_txn(rec, rec.end_time)
                    if t is not None:
                        started += 1
                        add_attrs(t, rec.attrs)
                elif cls is TxnBegin:
                    t = new_txn(rec, None)
                    if t is not None:
                        started += 1
                        if t_hi is not None and t.start <= t_hi:
                            pending_open[t.stream_id] += 1
                elif cls is TxnAttrRecord:
                    t = txns.get(rec.txn_id)
                    if t is not None:
                        add_attrs(t, rec.attrs)
                elif cls is TxnEnd:
                    t = txns.get(rec.txn_id)
                    if t is not None and t.end is None:
                        t.end = rec.end_time
                        if t_hi is not None and t.start <= t_hi:
                            pending_open[t.stream_id] -= 1
                elif cls is TxnLink:
                    if not want_links:
                        continue
                    kind = LINK_KIND_NAMES.get(rec.link_type, "custom")
                    label = self._str(rec.label_str_id) or None
                    src = txns.get(rec.source_txn_id)
                    if src is not None:
                        src.links.append(Link(kind, "out", rec.target_txn_id, label))
                    tgt = txns.get(rec.target_txn_id)
                    if tgt is not None:
                        tgt.links.append(Link(kind, "in", rec.source_txn_id, label))
                elif cls is TxnMeta:
                    if not want_meta:
                        continue
                    t = txns.get(rec.txn_id)
                    if t is not None:
                        t.meta[self._str(rec.key_str_id)] = self._str(rec.value_str_id)
            if max_txns is not None and started >= max_txns:
                break
        stats.ms += (_time.perf_counter() - t0) * 1000.0
        return txns

    def _c_run(self, stats: ScanStats, stream_ids, type_ids, **kw):
        """Run the C engine; None means "use Python" (no index, or FALLBACK)."""
        idx = self._r._index
        plan = self._block_plan(None if kw["want_links"] else stream_ids)
        if plan is None:
            return None
        try:
            rows, counts, proj = self._cscan.run(plan, stream_ids, type_ids, **kw)
        except _cengine.Fallback as e:
            self.c_fallbacks.append(str(e))
            return None
        stats.index = True
        stats.blocks_total = len(idx._txn_entries)
        stats.blocks_read += counts[0]
        stats.duplicate_ids += counts[1]
        return (rows, proj) if kw.get("project") else rows

    def _rows(self, sel: Selection, values: Sequence = (),
              stats: Optional[ScanStats] = None, whole_scan: bool = False) -> Rows:
        """Columns of the rows ``_matches`` would return, with each of
        ``values`` (ValueExprs or FieldKeys) evaluated per row.

        On the C engine the rows C can decide come back as columns, with no
        ``Txn`` built; the rest come back as Txns and are evaluated here.
        ``whole_scan`` (info): every txn, read like ``scan(want_meta=False)``
        rather than through the graph cache.
        """
        stats = stats if stats is not None else ScanStats()
        flt = sel.filter
        exprs = list(values) + ([flt] if flt is not None else [])
        graphy = any(e.uses_parent or e.uses_children for e in exprs)
        if self._cscan is not None and not graphy and (
                whole_scan or self._graph_cache is None):
            t0 = _time.perf_counter()
            want_links = any(e.uses_links for e in exprs)
            prog = _cengine.compile_program(self, flt, values)
            lo, hi = sel.window
            res = self._c_run(stats, sel.stream_ids, sel.type_ids,
                              want_links=want_links, want_meta=not whole_scan,
                              t_hi=hi, window=None if whole_scan else (lo, hi),
                              program=prog, project=True)
            if res is not None:
                full, proj = res
                rows = Rows(self, len(values))
                rows.ids, rows.starts, rows.ends = proj["ids"], proj["starts"], proj["ends"]
                rows.stream_ids, rows.type_ids = proj["stream_ids"], proj["type_ids"]
                rows.parents = proj["parents"]
                if values:
                    rows.vals = proj["vals"]
                rows.add_txns((t for t, deferred in full
                               if not deferred or flt.match(t)), values)
                stats.ms += (_time.perf_counter() - t0) * 1000.0
                return rows
        if whole_scan:
            txns = list(self.scan(want_meta=False, stats=stats).values())
        else:
            from ._analysis import matched_txns
            txns = matched_txns(self, sel, values, stats)
        return Rows.from_txns(self, txns, values)

    # -- verbs ------------------------------------------------------------

    def _matches(self, sel: Selection, want_links: bool = False,
                 stats: Optional[ScanStats] = None) -> List[Txn]:
        stats = stats if stats is not None else ScanStats()
        lo, hi = sel.window
        flt = sel.filter
        if self._graph_cache is not None or (
                flt is not None and (flt.uses_parent or flt.uses_children)):
            # parent.X needs the parent and nchildren needs every child, either
            # of which may be outside the selection (§7.3): use the whole
            # graph. Once it is cached (batch, navigation) it is also the
            # cheapest source for any query.
            every = self.graph(stats)
            if flt is not None:
                flt.set_parent_lookup(every.get)
            pool: Iterable[Txn] = (
                t for t in every.values()
                if (sel.stream_ids is None or t.stream_id in sel.stream_ids)
                and (sel.type_ids is None or t.type_id in sel.type_ids))
        else:
            if flt is not None and flt.uses_links:
                want_links = True
            if self._cscan is not None:
                t0 = _time.perf_counter()
                prog = None
                if flt is not None:
                    prog = getattr(flt, "_cprog", None)
                    if prog is None:
                        prog = flt._cprog = _cengine.compile_program(self, flt)
                rows = self._c_run(stats, sel.stream_ids, sel.type_ids,
                                   want_links=want_links, want_meta=True, t_hi=hi,
                                   window=(lo, hi), program=prog)
                if rows is not None:
                    # Deferred rows: C could not decide; the filter is the judge.
                    out = [t for t, deferred in rows
                           if not deferred or flt.match(t)]
                    stats.ms += (_time.perf_counter() - t0) * 1000.0
                    return out
            pool = self.scan(sel.stream_ids, sel.type_ids, t_hi=hi,
                             want_links=want_links, stats=stats).values()
        out = []
        for t in pool:
            if hi is not None and t.start > hi:
                continue
            if lo is not None and t.end is not None and t.end < lo:
                continue
            if flt is not None and not flt.match(t):
                continue
            out.append(t)
        return out

    def find(self, where: Optional[str] = None, streams=None, types=None,
             time=(None, None), sort: Optional[str] = None,
             want_links: bool = False, selection: Optional[Selection] = None
             ) -> FindResult:
        """All transactions matching the selection.

        Args:
            where: Filter expression.
            streams: Stream path glob or list of globs.
            types: Type name or glob, or a list of them.
            time: Inclusive ``(lo, hi)`` window in ticks.
            sort: Comma-separated sort keys (default ``start,id``); prefix a
                key with ``-`` for descending. Missing values sort last.
            want_links: Also read link records into ``Txn.links``. This
                reads every block.
            selection: A :class:`Selection` to use instead of the four
                selection arguments.

        Returns:
            A :class:`FindResult` with every match.
        """
        sel = selection or self.select(where, streams, types, time)
        stats = ScanStats()
        txns = self._matches(sel, want_links=want_links, stats=stats)
        txns = sort_txns(txns, sort, self)
        return FindResult(sel, txns, stats)

    def count(self, where: Optional[str] = None, by: Sequence[str] = (),
              streams=None, types=None, time=(None, None),
              selection: Optional[Selection] = None) -> CountResult:
        """Count the matching transactions, optionally grouped.

        Args:
            where: Filter expression.
            by: Group keys: ``stream``, ``type``, ``open``, ``parent``,
                ``meta[KEY]`` or a field name.
            streams: Stream path glob or list of globs.
            types: Type name or glob, or a list of them.
            time: Inclusive ``(lo, hi)`` window in ticks.
            selection: A :class:`Selection` to use instead.

        Returns:
            A :class:`CountResult`.
        """
        sel = selection or self.select(where, streams, types, time)
        getters = [self._group_key(k, sel) for k in by]      # validates the keys
        stats = ScanStats()
        if any(k.startswith("meta[") for k in by):
            txns = self._matches(sel, stats=stats)
            n = len(txns)
            c = Counter(tuple(g(t) for g in getters) for t in txns)
        else:
            fields = [k for k in by if k not in ("stream", "type", "open", "parent")]
            rows = self._rows(sel, [FieldKey(k) for k in fields], stats)
            n = len(rows)
            cols = key_columns(rows, by, {k: i for i, k in enumerate(fields)})
            c = None
            if by:
                keys = list(zip(*cols))
                c = Counter(keys[i] for i in group_order(rows, cols))
        if not by:
            return CountResult(sel, [], [((), n)] if n else [], n, stats)
        groups = sorted(c.items(), key=lambda kv: (-kv[1], _sortable(kv[0])))
        return CountResult(sel, list(by), groups, n, stats)

    def _group_key(self, key: str, sel: Selection):
        if key == "stream":
            return lambda t: t.stream
        if key == "type":
            return lambda t: t.type
        if key == "open":
            return lambda t: t.end is None
        if key == "parent":
            return lambda t: t.parent
        if key.startswith("meta[") and key.endswith("]"):
            k = key[5:-1].strip("'\"")
            return lambda t: t.meta.get(k)
        sv = self.schema_view({self.types[i].name for i in sel.type_ids}
                              if sel.type_ids else None)
        if not sv.field_specs(key):
            import difflib
            cands = sorted({f for fs in sv.visible_types().values() for f in fs}
                           | {"stream", "type", "open", "parent"})
            dym = difflib.get_close_matches(key, cands, n=3, cutoff=0.6)
            raise QueryError("unknown_field", f"unknown group key '{key}'", name=key,
                             did_you_mean=dym or None,
                             hint="--by takes stream, type, open, parent, meta[KEY] "
                                  "or a field name")

        def get(t, k=key):
            v = t.fields.get(k, MISSING)
            if v is MISSING:
                return None
            if isinstance(v, EnumVal):
                return v.label if v.label is not None else int(v)
            return v
        return get

    def show(self, ids: Sequence[int]) -> Tuple[List[Txn], List[int], ScanStats]:
        """Full records (fields, meta, links, children) for transaction ids.

        Args:
            ids: Transaction ids.

        Returns:
            ``(found, missing, stats)``: the found transactions in request
            order, the ids that are not in the trace, and a
            :class:`ScanStats`.
        """
        stats = ScanStats()
        every = self.graph(stats)
        found, missing = [], []
        for i in ids:
            t = every.get(i)
            if t is None:
                missing.append(i)
            else:
                found.append(t)
        return found, missing, stats

    def graph(self, stats: Optional[ScanStats] = None) -> Dict[int, Txn]:
        """Every transaction in the trace, keyed by id, with meta, links
        (both directions) and ``children`` (ids sorted by start, id) filled in.

        Built by one full scan and cached on the Trace, so a ``batch`` run
        or a script that navigates repeatedly pays for it once. Treat the
        returned txns as read-only.
        """
        if self._graph_cache is None:
            gs = ScanStats()
            every = self.scan(want_links=True, stats=gs)
            kids: Dict[int, List[Txn]] = defaultdict(list)
            for t in every.values():
                if t.parent is not None:
                    kids[t.parent].append(t)
            for t in every.values():
                ch = kids.get(t.id)
                t.children = ([c.id for c in sorted(ch, key=lambda c: (c.start, c.id))]
                              if ch else [])
            self._graph_cache, self._graph_stats = every, gs
            if stats is not None:
                _merge_stats(stats, gs)
        elif stats is not None:
            stats.index = self._graph_stats.index
            stats.blocks_total = self._graph_stats.blocks_total
            stats.cached = True
        return self._graph_cache

    def _get(self, txn_id: int, stats: ScanStats) -> Tuple[Dict[int, Txn], Txn]:
        every = self.graph(stats)
        t = every.get(txn_id)
        if t is None:
            raise QueryError("not_found", f"t:{txn_id} is not in this trace",
                             ref=f"t:{txn_id}",
                             hint="refs come from find/show output; check the id")
        return every, t

    def tree(self, txn_id: int, up: Optional[int] = None, down: Optional[int] = 1,
             limit: int = 200) -> "TreeResult":
        """The parent/child structure around a transaction.

        Descendants are taken breadth first, so a cut drops the deepest
        levels rather than later siblings. Nodes come back in tree
        (pre-)order with a depth relative to the focus (ancestors < 0).

        Args:
            txn_id: The focus transaction id.
            up: Ancestor levels; None for all, up to the root.
            down: Descendant levels; None for all.
            limit: Maximum number of nodes.

        Returns:
            A :class:`TreeResult`.

        Raises:
            QueryError: ``not_found`` when ``txn_id`` is not in the trace.
        """
        stats = ScanStats()
        every, focus = self._get(txn_id, stats)
        chain: List[Txn] = []
        dangling = None
        seen = {focus.id}
        cur = focus
        while cur.parent is not None and (up is None or len(chain) < up):
            p = every.get(cur.parent)
            if p is None:
                dangling = cur.parent
                break
            if p.id in seen:        # malformed parent cycle
                break
            seen.add(p.id)
            chain.append(p)
            cur = p
        chain.reverse()
        # Breadth-first selection of descendants, within depth and limit.
        budget = max(0, limit - len(chain) - 1)
        included = {focus.id}
        frontier, depth, truncated = [focus], 0, False
        while frontier and (down is None or depth < down):
            nxt = []
            for n in frontier:
                for cid in n.children or ():
                    c = every.get(cid)
                    if c is None or c.id in included or c.id in seen:
                        continue
                    if budget == 0:
                        truncated = True
                        break
                    budget -= 1
                    included.add(c.id)
                    nxt.append(c)
                if truncated:
                    break
            if truncated:
                break
            frontier, depth = nxt, depth + 1
        nodes: List[Tuple[int, Txn]] = [(d - len(chain), t) for d, t in enumerate(chain)]

        def walk(t: Txn, d: int) -> None:
            nodes.append((d, t))
            for cid in t.children or ():
                if cid in included and cid != t.id:
                    walk(every[cid], d + 1)
        walk(focus, 0)
        # Descendants of the focus cut by depth or limit (an ancestor's other
        # children are off the path by design and not counted).
        hidden = sum(1 for d, t in nodes if d >= 0
                     for c in (t.children or ()) if c not in included)
        root = chain[0] if chain else focus
        return TreeResult(focus, root, nodes, truncated, hidden, dangling, stats)

    def links(self, txn_id: int, kinds: Optional[Sequence[str]] = None,
              up: Optional[int] = 1, down: Optional[int] = 1,
              limit: int = 200) -> "LinkResult":
        """Follow link edges from a transaction transitively.

        A node reachable both ways keeps the first distance found (up
        before down).

        Args:
            txn_id: The focus transaction id.
            kinds: Edge kinds to follow (see :data:`LINK_KINDS`); None for
                all.
            up: Levels against edge direction, from an effect to its
                causes; None for no limit, 0 for none.
            down: Levels along edge direction, towards effects; None for
                no limit, 0 for none.
            limit: Maximum number of nodes.

        Returns:
            A :class:`LinkResult`.

        Raises:
            QueryError: ``not_found`` for an unknown id,
                ``unknown_link_kind`` for a bad kind.
        """
        if kinds:
            bad = [k for k in kinds if k not in LINK_KINDS]
            if bad:
                raise QueryError("unknown_link_kind", f"unknown link kind '{bad[0]}'",
                                 name=bad[0], valid_values=list(LINK_KINDS))
        kset = set(kinds) if kinds else None
        stats = ScanStats()
        every, focus = self._get(txn_id, stats)
        dist: Dict[int, int] = {focus.id: 0}
        edges: Dict[Tuple[int, int, str, Optional[str]], None] = {}
        missing: List[int] = []
        truncated = False
        for direction, depth_max in (("in", up), ("out", down)):
            if depth_max == 0:
                continue
            sign = -1 if direction == "in" else 1
            frontier, depth = [focus], 0
            while frontier and (depth_max is None or depth < depth_max):
                nxt = []
                for n in frontier:
                    for l in n.links:
                        if l.dir != direction or (kset and l.kind not in kset):
                            continue
                        src, dst = (l.other, n.id) if direction == "in" else (n.id, l.other)
                        edges[(src, dst, l.kind, l.label)] = None
                        if l.other in dist:
                            continue
                        if len(dist) >= limit:
                            truncated = True
                            continue
                        dist[l.other] = sign * (depth + 1)
                        o = every.get(l.other)
                        if o is None:
                            missing.append(l.other)
                        else:
                            nxt.append(o)
                frontier, depth = nxt, depth + 1
        # Edges between two nodes we kept, from the furthest cause down.
        edge_list = sorted(((s_, d, k, lb) for (s_, d, k, lb) in edges
                            if s_ in dist and d in dist),
                           key=lambda e: (dist[e[0]], dist[e[1]], e[0], e[1], e[2]))
        nodes = sorted(((d, every[i]) for i, d in dist.items() if i in every),
                       key=lambda x: (x[0], x[1].start, x[1].id))
        return LinkResult(focus, nodes, edge_list, missing, truncated, stats)

    def active(self, at: int, where: Optional[str] = None, streams=None, types=None,
               sort: Optional[str] = None, selection: Optional[Selection] = None
               ) -> FindResult:
        """Transactions in progress at an instant.

        A transaction is active when ``start <= at`` and it is either open
        or ``end > at``; one that ends exactly at ``at`` is not active.

        Args:
            at: The instant, in ticks.
            where: Filter expression.
            streams: Stream path glob or list of globs.
            types: Type name or glob, or a list of them.
            sort: Sort keys, as for :meth:`find`.
            selection: A :class:`Selection` to use instead; its window is
                replaced.

        Returns:
            A :class:`FindResult`.
        """
        sel = selection or self.select(where, streams, types, (at, at))
        sel.window = (at, at)
        stats = ScanStats()
        txns = [t for t in self._matches(sel, stats=stats)
                if t.end is None or t.end > at]
        return FindResult(sel, sort_txns(txns, sort, self), stats)

    def around(self, anchor: Optional[int] = None, at: Optional[int] = None,
               n: int = 5, window: Optional[int] = None, same_stream: bool = False,
               where: Optional[str] = None, streams=None, types=None,
               selection: Optional[Selection] = None) -> "AroundResult":
        """What happened near a transaction or a time, across streams.

        Without ``window``: the ``n`` transactions that start before the
        anchor and the ``n`` that start after it, by (start, id). With
        ``window``: every transaction overlapping
        ``[T0 - window, T1 + window]``, where ``T0..T1`` is the anchor
        transaction's extent (or the instant ``at``).

        Args:
            anchor: Anchor transaction id. Give this or ``at``.
            at: Anchor time in ticks.
            n: Rows before and after the anchor.
            window: Extension of the anchor on each side, in ticks.
            same_stream: Keep only the anchor transaction's stream (needs
                ``anchor``).
            where: Filter expression for the neighbours.
            streams: Stream path glob or list of globs.
            types: Type name or glob, or a list of them.
            selection: A :class:`Selection` to use instead; its window is
                replaced.

        Returns:
            An :class:`AroundResult`.

        Raises:
            QueryError: ``not_found`` for an unknown anchor id, ``usage``
                when neither anchor is given or ``same_stream`` has no
                transaction anchor.
        """
        stats = ScanStats()
        focus = None
        if anchor is not None:
            _, focus = self._get(anchor, stats)
            t0 = focus.start
            t1 = focus.start if focus.end is None else focus.end
        elif at is not None:
            t0 = t1 = at
        else:
            raise QueryError("usage", "around needs a t:ID ref or an @TIME")
        sel = selection or self.select(where, streams, types)
        if same_stream:
            if focus is None:
                raise QueryError("usage", "--same-stream needs a t:ID anchor, "
                                 "not a time", hint="use -s STREAM with @TIME")
            ids = frozenset({focus.stream_id})
            sel.stream_ids = ids if sel.stream_ids is None else sel.stream_ids & ids
        if window is not None:
            sel.window = (max(0, t0 - window), t1 + window)
            txns = sort_txns(self._matches(sel, stats=stats), None, self)
            return AroundResult(focus, t0, sel, txns, None, False, False, stats)
        sel.window = (None, None)
        txns = sort_txns(self._matches(sel, stats=stats), None, self)
        if focus is not None:
            key = (focus.start, focus.id)
            pos = next((i for i, t in enumerate(txns) if (t.start, t.id) >= key),
                       len(txns))
            before = txns[max(0, pos - n):pos]
            k = pos + 1 if pos < len(txns) and txns[pos].id == focus.id else pos
            after = txns[k:k + n]
            more_after = k + n < len(txns)
            mid = [focus]
        else:
            pos = next((i for i, t in enumerate(txns) if t.start >= t0), len(txns))
            before = txns[max(0, pos - n):pos]
            after = txns[pos:pos + n]
            more_after = pos + n < len(txns)
            mid = []
        more_before = pos - n > 0
        return AroundResult(focus, t0, sel, before + mid + after, len(before),
                            more_before, more_after, stats)

    def stream_summary(self, globs=None) -> Tuple[List[dict], ScanStats]:
        """Per-stream counts, observed types, first/last time and open count.

        Args:
            globs: Stream path glob or list of globs; None for all.

        Returns:
            ``(rows, stats)``: one dict per stream, sorted by path, with
            ``stream``, ``decl``, ``kind``, ``types``, ``count``, ``open``,
            ``first``, ``last`` and ``metadata``.
        """
        gl = _as_tuple(globs)
        ids = self._match_stream_globs(gl) if gl else None
        stats = ScanStats()
        txns = self.scan(stream_ids=ids, want_meta=False, stats=stats)
        agg: Dict[int, dict] = {}
        for t in txns.values():
            a = agg.get(t.stream_id)
            if a is None:
                a = agg[t.stream_id] = {"count": 0, "open": 0, "first": t.start,
                                        "last": t.start, "types": Counter()}
            a["count"] += 1
            a["types"][t.type] += 1
            if t.end is None:
                a["open"] += 1
            a["first"] = min(a["first"], t.start)
            a["last"] = max(a["last"], t.start if t.end is None else t.end)
        rows = []
        for s in sorted(self.streams.values(), key=lambda s: s.path):
            if ids is not None and s.id not in ids:
                continue
            a = agg.get(s.id)
            rows.append({
                "stream": s.path, "decl": s.decl_name, "kind": s.kind or None,
                "types": [n for n, _ in a["types"].most_common()] if a else [],
                "count": a["count"] if a else 0,
                "open": a["open"] if a else 0,
                "first": a["first"] if a else None,
                "last": a["last"] if a else None,
                "metadata": s.metadata,
            })
        return rows, stats

    def schema(self, type_names=None, sample: int = 0) -> Tuple[List[dict], ScanStats]:
        """Field definitions per type.

        Args:
            type_names: Type name or glob, or a list of them; None for all.
            sample: When > 0, add ``observed`` value statistics per field
                from the first ``sample`` transactions of each type.

        Returns:
            ``(rows, stats)``: one dict per type with ``type``, ``id``,
            ``fields`` and, when sampling, ``sampled``.
        """
        tn = _as_tuple(type_names)
        tids = self._match_types(tn) if tn else frozenset(self.types)
        stats = ScanStats()
        samples: Dict[int, List[Txn]] = defaultdict(list)
        if sample > 0:
            txns = self.scan(type_ids=tids, want_meta=False,
                             max_txns=sample * len(tids), stats=stats)
            for t in txns.values():
                if len(samples[t.type_id]) < sample:
                    samples[t.type_id].append(t)
        rows = []
        for tid in sorted(tids):
            ti = self.types[tid]
            fields = []
            for f in ti.fields:
                fd = {"name": f.name, "kind": f.kind}
                if f.kind in INT_KINDS:
                    fd["radix"] = f.radix
                if f.kind == "enum":
                    fd["enum"] = f.enum_name
                    fd["values"] = {lbl: v for v, lbl in sorted(f.enum_labels.items())}
                if sample > 0:
                    fd["observed"] = _observe([t.fields.get(f.name, MISSING)
                                               for t in samples[tid]])
                fields.append(fd)
            row = {"type": ti.name, "id": tid, "fields": fields}
            if sample > 0:
                row["sampled"] = len(samples[tid])
            rows.append(row)
        return rows, stats

    def span(self) -> Optional[Tuple[int, int]]:
        """(first start, last start-or-end) over the whole trace; cached."""
        if self._span is None:
            every = (self._graph_cache if self._graph_cache is not None
                     else self.scan(want_meta=False))
            self._span = (min((t.start for t in every.values()), default=None),
                          max((t.start if t.end is None else t.end
                               for t in every.values()), default=None))
        return None if self._span[0] is None else self._span

    # -- analysis (phase Q3; implemented in _analysis.py) -------------------

    def stats(self, target: str = "dur", where: Optional[str] = None,
              by: Sequence[str] = (), streams=None, types=None, time=(None, None),
              selection: Optional[Selection] = None):
        """n, min, max, mean and p50/p90/p99 of a numeric value.

        Args:
            target: ``dur``, ``start``, ``end``, a field, or any numeric
                expression such as ``"addr & 0xfff"``.
            where: Filter expression.
            by: Group keys, as for :meth:`count`.
            streams: Stream path glob or list of globs.
            types: Type name or glob, or a list of them.
            time: Inclusive ``(lo, hi)`` window in ticks.
            selection: A :class:`Selection` to use instead.

        Returns:
            A :class:`~trlog.query._analysis.StatsResult`.

        Raises:
            QueryError: ``not_numeric`` when ``target`` is an enum, string
                or bytes value.
        """
        from . import _analysis
        sel = selection or self.select(where, streams, types, time)
        return _analysis.stats(self, target, sel, by)

    def hist(self, target: str = "dur", where: Optional[str] = None,
             bin: Optional[float] = None, max_bins: int = 20, streams=None,
             types=None, time=(None, None), selection: Optional[Selection] = None):
        """Histogram of a numeric value in bins of equal width.

        Args:
            target: Value to bin, as for :meth:`stats`.
            where: Filter expression.
            bin: Bin width (ticks for time values); None picks the smallest
                1/2/5 x 10^k width giving at most ``max_bins`` bins.
            max_bins: Bin limit when ``bin`` is None.
            streams: Stream path glob or list of globs.
            types: Type name or glob, or a list of them.
            time: Inclusive ``(lo, hi)`` window in ticks.
            selection: A :class:`Selection` to use instead.

        Returns:
            A :class:`~trlog.query._analysis.HistResult`.

        Raises:
            QueryError: ``not_numeric``, or ``too_many_bins`` beyond 10000
                bins.
        """
        from . import _analysis
        sel = selection or self.select(where, streams, types, time)
        return _analysis.hist(self, target, sel, bin, max_bins)

    def gaps(self, min: int = 0, where: Optional[str] = None, streams=None,
             types=None, time=(None, None), sort: str = "-dur",
             selection: Optional[Selection] = None):
        """Idle intervals on each selected stream.

        A gap is a stretch of the range where no selected transaction is in
        progress. The range is ``time``, with open sides taken from the
        trace span.

        Args:
            min: Keep only gaps longer than this, in ticks.
            where: Filter expression.
            streams: Stream path glob or list of globs.
            types: Type name or glob, or a list of them.
            time: Inclusive ``(lo, hi)`` range in ticks.
            sort: ``dur``, ``start``, ``end`` or ``stream``, with ``-`` for
                descending.
            selection: A :class:`Selection` to use instead.

        Returns:
            A :class:`~trlog.query._analysis.GapsResult`.
        """
        from . import _analysis
        sel = selection or self.select(where, streams, types, time)
        return _analysis.gaps(self, sel, min, sort)

    def match(self, a: Optional[str], b: Optional[str], on=None, order: str = "fifo",
              within: Optional[int] = None, streams=None, types=None,
              time=(None, None)):
        """Pair B transactions one-to-one with A transactions of equal key.

        Args:
            a: Expression selecting the A side (e.g. requests).
            b: Expression selecting the B side (e.g. responses).
            on: Key: ``"a.tag == b.tag && ..."``, bare field names
                (``"tag"`` or ``"tag,addr"``), a list of
                ``(a_expr, b_expr)`` pairs, or None to pair in order.
            order: ``"fifo"``: the oldest unmatched A that started at or
                before B. ``"any"``: the unmatched A whose start is nearest.
            within: Pair only when ``|b.start - a.start| <= within`` ticks.
            streams: Stream path glob or list of globs, for both sides.
            types: Type name or glob, or a list of them, for both sides.
            time: Inclusive ``(lo, hi)`` window in ticks, for both sides.

        Returns:
            A :class:`~trlog.query._analysis.MatchResult`.
        """
        from . import _analysis
        terms = _analysis.parse_on(on) if isinstance(on, str) or on is None else list(on)
        sel_a = self.select(a, streams, types, time)
        sel_b = self.select(b, streams, types, time)
        return _analysis.match(self, sel_a, sel_b, terms, order, within)

    def export(self, fmt: str, out: str, where: Optional[str] = None, streams=None,
               types=None, time=(None, None), force: bool = False,
               selection: Optional[Selection] = None) -> dict:
        """Write the selected transactions to SQLite or Parquet.

        Args:
            fmt: ``"sqlite"`` (one file) or ``"parquet"`` (a directory of
                ``<table>.parquet`` files; needs ``pyarrow``).
            out: Output file or directory.
            where: Filter expression.
            streams: Stream path glob or list of globs.
            types: Type name or glob, or a list of them.
            time: Inclusive ``(lo, hi)`` window in ticks.
            force: Replace ``out`` if it exists.
            selection: A :class:`Selection` to use instead.

        Returns:
            A dict with ``format``, ``out``, ``tables`` (row count per
            table) and ``views``.

        Raises:
            QueryError: ``exists`` when ``out`` exists without ``force``,
                ``missing_dependency`` for Parquet without ``pyarrow``.
        """
        from . import _export
        sel = selection or self.select(where, streams, types, time)
        return _export.export(self, fmt, out, sel, force)

    def info(self) -> dict:
        """File-level facts plus a full-scan transaction count and span.

        Returns:
            A dict with ``file``, ``version``, ``time_unit``,
            ``timescale_exp``, ``span``, ``txns``, ``open``, ``streams``,
            ``types``, ``signals``, ``hierarchies``, ``txn_blocks``,
            ``vc_blocks``, ``index``, ``qindex``, ``engine``, ``metadata``
            and ``warnings``.
        """
        r = self._r
        stats = ScanStats()
        rows = self._rows(Selection(), stats=stats, whole_scan=True)
        n_open = sum(1 for e in rows.ends if e is None)
        span = None
        if len(rows):
            span = [min(rows.starts),
                    max(s if e is None else e for s, e in zip(rows.starts, rows.ends))]
        idx = r._index
        vc_blocks = len(idx._vc_entries) if idx is not None else None
        n_vars = sum(len(h.vars) for h in r.hierarchies.values())
        return {
            "file": self.path,
            "version": f"{r._version_major}.{r._version_minor}",
            "time_unit": self.time_unit,
            "timescale_exp": self.timescale_exp,
            "span": span,
            "txns": len(rows),
            "open": n_open,
            "streams": len(self.streams),
            "types": len(self.types),
            "signals": n_vars,
            "hierarchies": [self._str(h.header.name_str_id)
                            for h in r.hierarchies.values()],
            "txn_blocks": stats.blocks_total,
            "vc_blocks": vc_blocks,
            "index": idx is not None,
            "qindex": False,
            "engine": self.engine,
            "metadata": r.trace_metadata(),
            "warnings": _catalog_warnings(self, stats),
            "_stats": stats,
        }


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _as_tuple(v) -> Tuple[str, ...]:
    if v is None:
        return ()
    if isinstance(v, str):
        v = [v]
    out = []
    for item in v:
        out.extend(p.strip() for p in item.split(",") if p.strip())
    return tuple(out)


def _sortable(key: tuple) -> tuple:
    return tuple((0, "") if v is None else (1, str(v)) if isinstance(v, str)
                 else (1, f"{v:020d}") if isinstance(v, int) else (1, str(v))
                 for v in key)


_BUILTIN_SORT = {
    "start": lambda t: t.start,
    "end": lambda t: t.end,
    "dur": lambda t: None if t.end is None else t.end - t.start,
    "id": lambda t: t.id,
    "stream": lambda t: t.stream,
    "type": lambda t: t.type,
    "parent": lambda t: t.parent,
}


def sort_txns(txns: List[Txn], sort: Optional[str], trace: Trace) -> List[Txn]:
    """Stable multi-key sort. Missing values sort last in either direction."""
    keys = [k.strip() for k in (sort or "start,id").split(",") if k.strip()]
    out = list(txns)
    known_fields = {f.name for ti in trace.types.values() for f in ti.fields}
    for k in reversed(keys):
        desc = k.startswith("-")
        name = k.lstrip("-+")
        if name in _BUILTIN_SORT:
            get = _BUILTIN_SORT[name]
        elif name in known_fields:
            get = (lambda t, n=name: (lambda v: None if v is MISSING else v)(
                t.fields.get(n, MISSING)))
        else:
            import difflib
            dym = difflib.get_close_matches(name, sorted(set(_BUILTIN_SORT)
                                                         | known_fields), n=3)
            raise QueryError("unknown_field", f"unknown sort key '{name}'", name=name,
                             did_you_mean=dym or None,
                             hint="sort keys: start end dur id stream type parent "
                                  "or a field; prefix '-' for descending")
        present = [t for t in out if get(t) is not None]
        absent = [t for t in out if get(t) is None]
        try:
            present.sort(key=get, reverse=desc)
        except TypeError:
            present.sort(key=lambda t: str(get(t)), reverse=desc)
        out = present + absent
    return out


def _merge_stats(dst: ScanStats, src: ScanStats) -> None:
    dst.blocks_total = max(dst.blocks_total, src.blocks_total)
    dst.blocks_read += src.blocks_read
    dst.ms += src.ms
    dst.index = dst.index or src.index
    dst.duplicate_ids += src.duplicate_ids


def _observe(values: list) -> dict:
    present = [v for v in values if v is not MISSING]
    obs: dict = {"present": len(present), "missing": len(values) - len(present)}
    nums = [v for v in present
            if isinstance(v, (int, float)) and not isinstance(v, (bool, EnumVal))]
    if nums:
        obs["min"] = min(nums)
        obs["max"] = max(nums)
    keyed = [v.label if isinstance(v, EnumVal) else v for v in present]
    hashable = [v for v in keyed
                if isinstance(v, (int, float, str, bool, bytes, LogicVal))]
    if hashable:
        obs["top"] = [[v, n] for v, n in Counter(hashable).most_common(5)]
        obs["distinct"] = len(set(hashable))
    return obs


def _catalog_warnings(trace: Trace, stats: ScanStats) -> List[str]:
    w = []
    if trace.stream_id_collisions:
        w.append("stream instance ids reused across hierarchies: "
                 f"{sorted(set(trace.stream_id_collisions))}; only the first "
                 "declaration of each is used")
    if stats.duplicate_ids:
        w.append(f"{stats.duplicate_ids} duplicate txn id(s); later records "
                 "replace earlier ones")
    if not trace.has_index:
        w.append("no index block (trace not closed cleanly?); queries scan the "
                 "whole file")
    return w


def _check_header(path: str) -> None:
    """Reject non-trace files before the reader trusts their block lengths."""
    try:
        size = os.path.getsize(path)
        with builtins.open(path, "rb") as f:
            head = f.read(BLOCK_HEADER_SIZE + len(TRL_MAGIC))
    except FileNotFoundError:
        raise QueryError("file_error", f"no such file: {path}", path=path)
    except OSError as e:
        raise QueryError("file_error", f"cannot read {path}: {e}", path=path)
    ok = len(head) == BLOCK_HEADER_SIZE + len(TRL_MAGIC)
    if ok:
        btype, _flags, blen = struct.unpack_from("<BBQ", head)
        ok = (btype == BlockType.BLK_FILE_HDR and blen <= size
              and head[BLOCK_HEADER_SIZE:] == TRL_MAGIC)
    if not ok:
        raise QueryError("file_error", f"{path} is not a trlog trace (bad header)",
                         path=path)


def open(path: str, engine: Optional[str] = None) -> Trace:   # noqa: A001
    """Open a trace for querying.

    Args:
        path: Trace file (``.trl``).
        engine: ``"auto"`` (the C engine when libtrl loads, else pure
            Python), ``"c"`` (an error when the library is not available)
            or ``"python"``. None uses ``$TRLOG_ENGINE``, else ``"auto"``.
            Both engines give the same results.

    Returns:
        A :class:`Trace`; use it as a context manager or call
        :meth:`Trace.close`.

    Raises:
        QueryError: ``file_error`` when the file is not a readable trace.
    """
    return Trace(path, engine)
