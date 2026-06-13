"""Golden corpus — deterministic, representative ``.trl`` traces.

This module is the single source of truth for the Phase 0 parity baseline
(see ``docs/design/pluggable-codecs-implementation-plan.md`` §4 Phase 0).

Each entry is a small, fully deterministic trace built through the *high-level*
writer API so that it exercises the complete on-disk framing (file header,
hierarchy, string table, type registry, value-change / transaction data blocks,
ext blocks, and the trailing index). The corpus deliberately avoids anything
non-reproducible (wall-clock timestamps, randomness, dict-ordering hazards) so
the resulting bytes can be checked in as golden fixtures.

A builder is a callable ``build(W, path)`` where ``W`` is a writer class
(``trlog._writer.TrlWriter`` for the pure-Python reference, or the ctypes
writer for the native path) and ``path`` is a filesystem path string. Each
builder is tagged with the capabilities it needs so the cross-implementation
parity test can skip entries the limited native binding cannot yet express.

The fixtures are regenerated deliberately (``python -m tests.golden.generate``)
and reviewed in diff — never auto-refreshed by a test.
"""

from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Callable, List

from trlog._types import (
    SignalEncoding, ScopeType, FieldType, FieldDef, TxnAttr, VarDir,
)


# Capability tags. The pure-Python reference supports all of them; the native
# ctypes binding supports only a subset today (see tests/integration/
# test_cross_impl_parity.py).
CAP_VC = "vc"                 # value-change blocks
CAP_TXN = "txn"               # transaction blocks
CAP_EXT = "ext"               # ext blocks (write_ext)
CAP_REAL = "real"             # real-valued signals
CAP_STRING = "string"         # string-valued signals
CAP_4STATE = "4state"         # 4/9-state signals
CAP_COMPRESS = "compress"     # block / wave compression on
CAP_COLUMN = "column"         # column-layout txn
CAP_MULTI_HIER = "multihier"  # more than one hierarchy block


@dataclass
class CorpusEntry:
    name: str
    build: Callable[[type, str], None]
    caps: List[str] = field(default_factory=list)
    doc: str = ""


_ENTRIES: "OrderedDict[str, CorpusEntry]" = OrderedDict()


def _entry(name, caps=(), doc=""):
    def deco(fn):
        _ENTRIES[name] = CorpusEntry(name=name, build=fn, caps=list(caps), doc=doc)
        return fn
    return deco


# ---------------------------------------------------------------------------
# Builders
# ---------------------------------------------------------------------------

@_entry("empty", doc="Empty trace: header + index only.")
def _empty(W, path):
    with W(path, compress=False):
        pass


@_entry("vc_2state_1bit", caps=[CAP_VC], doc="A 1-bit 2-state clock toggling.")
def _vc_2state_1bit(W, path):
    with W(path, compress=False) as w:
        st = w.add_signal_type(SignalEncoding.SE_2STATE, 1)
        with w.begin_hierarchy() as h:
            h.begin_scope(ScopeType.ST_MODULE, "top")
            clk = h.add_var("clk", st)
            h.end_scope()
        with w.begin_vc_block(0) as vc:
            for i in range(64):
                vc.add_change(clk, i * 10, i % 2)


@_entry("vc_2state_32bit", caps=[CAP_VC], doc="A 32-bit 2-state counter bus.")
def _vc_2state_32bit(W, path):
    with W(path, compress=False) as w:
        st = w.add_signal_type(SignalEncoding.SE_2STATE, 32)
        with w.begin_hierarchy() as h:
            h.begin_scope(ScopeType.ST_MODULE, "top")
            bus = h.add_var("count", st)
            h.end_scope()
        with w.begin_vc_block(0) as vc:
            for i in range(50):
                vc.add_change(bus, i * 10, (i * 0x1234) & 0xFFFFFFFF)


@_entry("vc_4state", caps=[CAP_VC, CAP_4STATE], doc="1-bit 4-state with X/Z.")
def _vc_4state(W, path):
    with W(path, compress=False) as w:
        st = w.add_signal_type(SignalEncoding.SE_4STATE, 1)
        with w.begin_hierarchy() as h:
            h.begin_scope(ScopeType.ST_MODULE, "top")
            sig = h.add_var("d", st)
            h.end_scope()
        with w.begin_vc_block(0) as vc:
            for i, v in enumerate([0, 1, "X", "Z", 1, 0, "X", 1]):
                vc.add_change(sig, i * 10, v)


@_entry("vc_9state", caps=[CAP_VC, CAP_4STATE], doc="1-bit 9-state, all states.")
def _vc_9state(W, path):
    with W(path, compress=False) as w:
        st = w.add_signal_type(SignalEncoding.SE_9STATE, 1)
        with w.begin_hierarchy() as h:
            h.begin_scope(ScopeType.ST_MODULE, "top")
            sig = h.add_var("net", st)
            h.end_scope()
        with w.begin_vc_block(0) as vc:
            for i, v in enumerate([0, 1, "X", "Z", "H", "U", "W", "L", "-"]):
                vc.add_change(sig, i * 10, v)


@_entry("vc_real", caps=[CAP_VC, CAP_REAL], doc="Real-valued signal.")
def _vc_real(W, path):
    with W(path, compress=False) as w:
        st = w.add_signal_type(SignalEncoding.SE_REAL, 0)
        with w.begin_hierarchy() as h:
            h.begin_scope(ScopeType.ST_MODULE, "top")
            sig = h.add_var("v", st)
            h.end_scope()
        with w.begin_vc_block(0) as vc:
            for i in range(20):
                vc.add_change(sig, i * 10, float(i) * 1.5)


@_entry("vc_string", caps=[CAP_VC, CAP_STRING], doc="String-valued signal (UTF-8).")
def _vc_string(W, path):
    with W(path, compress=False) as w:
        st = w.add_signal_type(SignalEncoding.SE_STRING, 0)
        with w.begin_hierarchy() as h:
            h.begin_scope(ScopeType.ST_MODULE, "top")
            sig = h.add_var("state", st)
            h.end_scope()
        with w.begin_vc_block(0) as vc:
            for i, v in enumerate(["idle", "run", "stop", "日本語"]):
                vc.add_change(sig, i * 10, v)


@_entry("vc_compressed", caps=[CAP_VC, CAP_COMPRESS],
        doc="2-state trace with block + wave compression enabled.")
def _vc_compressed(W, path):
    with W(path, compress=True) as w:
        st = w.add_signal_type(SignalEncoding.SE_2STATE, 1)
        with w.begin_hierarchy() as h:
            h.begin_scope(ScopeType.ST_MODULE, "top")
            clk = h.add_var("clk", st)
            h.end_scope()
        with w.begin_vc_block(0) as vc:
            for i in range(256):
                vc.add_change(clk, i * 5, i % 2)


@_entry("hierarchy", caps=[CAP_VC], doc="Nested scopes with several vars.")
def _hierarchy(W, path):
    with W(path, compress=False) as w:
        st1 = w.add_signal_type(SignalEncoding.SE_2STATE, 1)
        st8 = w.add_signal_type(SignalEncoding.SE_2STATE, 8)
        with w.begin_hierarchy() as h:
            h.begin_scope(ScopeType.ST_MODULE, "top")
            clk = h.add_var("clk", st1)
            h.begin_scope(ScopeType.ST_MODULE, "sub")
            rst = h.add_var("rst", st1)
            data = h.add_var("data", st8)
            h.end_scope()
            h.end_scope()
        with w.begin_vc_block(0) as vc:
            for i in range(16):
                vc.add_change(clk, i * 10, i % 2)
                vc.add_change(rst, i * 10, 1 if i < 2 else 0)
                vc.add_change(data, i * 10, (i * 7) & 0xFF)


@_entry("txn_row", caps=[CAP_TXN], doc="Transaction stream, row layout.")
def _txn_row(W, path):
    with W(path, compress=False) as w:
        with w.begin_txn_block(0) as txn:
            for i in range(40):
                txn.write_full(
                    stream_inst_id=1, txn_type_id=1, txn_id=i,
                    start=i * 10, end=i * 10 + 5, parent=0,
                )


@_entry("txn_schema_attrs", caps=[CAP_TXN], doc="Transactions with a schema + attrs.")
def _txn_schema_attrs(W, path):
    with W(path, compress=False) as w:
        fields = [
            FieldDef(name_str_id=w.intern("addr"), field_type=FieldType.FT_U32),
            FieldDef(name_str_id=w.intern("data"), field_type=FieldType.FT_U64),
        ]
        t = w.add_txn_schema("Access", fields)
        with w.begin_txn_block(0) as txn:
            for i in range(32):
                txn.write_full(
                    stream_inst_id=1, txn_type_id=t, txn_id=i,
                    start=i * 10, end=i * 10 + 5, parent=0,
                    attrs=[TxnAttr(field_idx=0, value=i * 4),
                           TxnAttr(field_idx=1, value=i * 0x100)],
                )


@_entry("txn_column", caps=[CAP_TXN, CAP_COLUMN, CAP_COMPRESS],
        doc="Transaction stream, column layout + compression.")
def _txn_column(W, path):
    with W(path, compress=True, column_layout="column") as w:
        fields = [FieldDef(name_str_id=w.intern("v"), field_type=FieldType.FT_U32)]
        t = w.add_txn_schema("ColType", fields)
        with w.begin_txn_block(0) as txn:
            for i in range(100):
                txn.write_full(
                    stream_inst_id=1, txn_type_id=t, txn_id=i,
                    start=i * 10, end=i * 10 + 5, parent=0,
                    attrs=[TxnAttr(field_idx=0, value=i * 100)],
                )


@_entry("transparent_meta", caps=[CAP_VC],
        doc="Typed transparent metadata: trace-global, stream-type, profiles, "
            "and instance-scope H_ATTR2.")
def _transparent_meta(W, path):
    with W(path, compress=False) as w:
        st = w.add_signal_type(SignalEncoding.SE_2STATE, 1)
        stype = w.add_stream_type("AXI4")
        w.add_trace_metadata("trlog.tool", "trlog-test")
        w.add_trace_metadata("trlog.cores", 4)
        w.add_trace_metadata("vdd", 0.9)
        w.add_trace_metadata("opt", True)
        w.add_trace_metadata("tags", ["alpha", "beta"])
        w.add_stream_metadata(stype, "isa", "rv64gc")
        w.add_stream_metadata(stype, "xlen", 64)
        w.add_stream_profile(stype, "org.fvutils.trlog.profile.exec-trace")
        with w.begin_hierarchy() as h:
            h.begin_scope(ScopeType.ST_MODULE, "top")
            clk = h.add_var("clk", st)
            h.add_typed_attr("freq_mhz", 100)
            h.end_scope()
        with w.begin_vc_block(0) as vc:
            for i in range(8):
                vc.add_change(clk, i * 10, i % 2)


@_entry("bytesplit_real", caps=[CAP_VC, CAP_REAL, CAP_COMPRESS],
        doc="REAL signal encoded with the core.bytesplit structural codec "
            "(FLAG_STRUCT_CODEC, transform-then-compress).")
def _bytesplit_real(W, path):
    from trlog.codec import CORE_BYTESPLIT
    with W(path, compress=True) as w:
        rt = w.add_signal_type(SignalEncoding.SE_REAL, 0)
        with w.begin_hierarchy() as h:
            h.begin_scope(ScopeType.ST_MODULE, "top")
            v = h.add_var("analog", rt)
            h.end_scope()
        with w.begin_vc_block(0, codec=CORE_BYTESPLIT) as vc:
            for i in range(64):
                vc.add_change(v, i * 10, 1000.0 + i * 0.25)


@_entry("derived", caps=[CAP_VC],
        doc="Derived (virtual) signals: identity alias + c = a & b, stored as "
            "expressions (aux defs), not data.")
def _derived(W, path):
    from trlog._derived import Input
    with W(path, compress=False) as w:
        bit = w.add_signal_type(SignalEncoding.SE_2STATE, 1)
        with w.begin_hierarchy() as h:
            h.begin_scope(ScopeType.ST_MODULE, "top")
            a = h.add_var("a", bit)
            b = h.add_var("b", bit)
            h.end_scope()
        with w.begin_vc_block(0) as vc:
            vc.add_change(a, 0, 0); vc.add_change(a, 10, 1); vc.add_change(a, 30, 0)
            vc.add_change(b, 0, 0); vc.add_change(b, 20, 1)
        w.add_derived_signal("c_and", Input(0) & Input(1), inputs=[a, b], bit_width=1)
        w.add_derived_signal("d_alias", Input(0), inputs=[a], bit_width=1)


@_entry("callreturn", caps=[CAP_TXN, CAP_EXT],
        doc="ext.callreturn external codec: signatures (aux) + call events "
            "(ext block w/ back-references) + dependency catalog + raw fallback.")
def _callreturn(W, path):
    from trlog.ext.callreturn import CallReturnWriter, Signature, ArgType
    with W(path, compress=False) as w:
        calls = w.add_stream_type("calls")
        sig_s = w.add_stream_type("sig")
        cr = CallReturnWriter(w, calls, sig_s, [
            Signature(1, "add", [ArgType.INT, ArgType.INT], ArgType.INT),
            Signature(2, "log", [ArgType.STR], ArgType.NONE),
        ], window=16)
        cr.enter(1, [3, 4], time=10)
        cr.enter(2, ["hi"], time=12)
        cr.exit(time=13)
        cr.enter(1, [3, 4], time=15)   # back-reference
        cr.exit(ret=7, time=16)
        cr.exit(ret=7, time=20)
        cr.enter_raw(99, [42], start=30, end=40)
        cr.close()


@_entry("ext", caps=[CAP_EXT], doc="Application-specific ext block.")
def _ext(W, path):
    with W(path, compress=False) as w:
        w.write_ext(ext_type=b"TEST", ext_version=1, payload=b"hello-ext-payload")


@_entry("multi_hier", caps=[CAP_VC, CAP_MULTI_HIER], doc="Two hierarchy blocks.")
def _multi_hier(W, path):
    # NB: each HierarchyBlock restarts its var-id counter at 1, so vars in
    # separate hierarchies share an id space. The corpus only drives the first
    # hierarchy's var to avoid that collision; the second exercises multi-block
    # hierarchy framing (a second hier offset in the index).
    with W(path, compress=False) as w:
        st = w.add_signal_type(SignalEncoding.SE_2STATE, 1)
        with w.begin_hierarchy(hier_id=1, name="design") as h:
            h.begin_scope(ScopeType.ST_MODULE, "dut")
            a = h.add_var("a", st)
            h.end_scope()
        with w.begin_hierarchy(hier_id=2, name="tb") as h:
            h.begin_scope(ScopeType.ST_MODULE, "env")
            h.add_var("b", st)
            h.end_scope()
        with w.begin_vc_block(0) as vc:
            for i in range(8):
                vc.add_change(a, i * 10, i % 2)


# ---------------------------------------------------------------------------
# Public accessors
# ---------------------------------------------------------------------------

def entries() -> "List[CorpusEntry]":
    """Return all corpus entries in stable, declaration order."""
    return list(_ENTRIES.values())


def names() -> "List[str]":
    return list(_ENTRIES.keys())


def get(name: str) -> CorpusEntry:
    return _ENTRIES[name]


def build_bytes(entry: CorpusEntry, writer_cls) -> bytes:
    """Build a corpus entry into an in-memory buffer and return its bytes."""
    import io
    buf = io.BytesIO()
    entry.build(writer_cls, buf)
    return buf.getvalue()
