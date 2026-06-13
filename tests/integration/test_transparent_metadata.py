"""Transparent (typed) metadata — Phase 2a (impl-plan §2.3 / §2.6).

Round-trips typed transparent metadata at all three scopes (trace-global,
stream-type, stream-instance) across every value type, verifies profiles and
the legacy-H_ATTR-as-STR compatibility, and that adding metadata does not
disturb files that don't use it.
"""

from __future__ import annotations

import io
import math

import pytest

from trlog._writer import TrlWriter
from trlog._reader import TrlReader
from trlog._types import SignalEncoding, ScopeType, AttrType, WellKnownAttr, TypedAttr
from trlog._metadata import (
    encode_typed_attr, decode_typed_attr, infer_attr_type, MetaBlock,
)


def _roundtrip(build):
    buf = io.BytesIO()
    w = TrlWriter(buf, compress=False)
    build(w)
    w.close()
    buf.seek(0)
    return TrlReader(buf)


# ---------------------------------------------------------------------------
# Low-level typed-attr codec
# ---------------------------------------------------------------------------

class TestTypedAttrCodec:
    @pytest.mark.parametrize("atype,value", [
        (AttrType.AT_BOOL, True),
        (AttrType.AT_BOOL, False),
        (AttrType.AT_U64, 0),
        (AttrType.AT_U64, 2**63 + 5),
        (AttrType.AT_I64, -42),
        (AttrType.AT_F64, 3.14159),
        (AttrType.AT_STR, 7),                          # str id at this layer
        (AttrType.AT_ARRAY | AttrType.AT_U64, [1, 2, 3]),
        (AttrType.AT_ARRAY | AttrType.AT_I64, [-1, 0, 1]),
        (AttrType.AT_ARRAY | AttrType.AT_STR, [3, 4, 5]),
        (AttrType.AT_ARRAY | AttrType.AT_BOOL, [True, False, True]),
    ])
    def test_scalar_and_array_roundtrip(self, atype, value):
        a = TypedAttr(key_str_id=11, attr_type=int(atype), value=value)
        enc = encode_typed_attr(a)
        got, off = decode_typed_attr(enc, 0)
        assert off == len(enc)
        assert got.key_str_id == 11
        assert got.attr_type == int(atype)
        assert got.value == value

    def test_f64_precision(self):
        a = TypedAttr(0, int(AttrType.AT_F64), math.pi)
        got, _ = decode_typed_attr(encode_typed_attr(a), 0)
        assert got.value == math.pi

    def test_infer_attr_type(self):
        assert infer_attr_type(True) == AttrType.AT_BOOL
        assert infer_attr_type(5) == AttrType.AT_U64
        assert infer_attr_type(-5) == AttrType.AT_I64
        assert infer_attr_type(1.5) == AttrType.AT_F64
        assert infer_attr_type("x") == AttrType.AT_STR
        assert infer_attr_type([1, 2]) == (AttrType.AT_ARRAY | AttrType.AT_U64)
        assert infer_attr_type(["a"]) == (AttrType.AT_ARRAY | AttrType.AT_STR)

    def test_meta_block_roundtrip(self):
        mb = MetaBlock()
        mb.add(TypedAttr(1, int(AttrType.AT_U64), 99))
        mb.add(TypedAttr(2, int(AttrType.AT_STR), 3))
        block = mb.encode_block()
        mb2 = MetaBlock()
        mb2.read_block(block[10:], flags=block[1])
        assert [(a.key_str_id, a.value) for a in mb2.attrs] == [(1, 99), (2, 3)]


# ---------------------------------------------------------------------------
# Trace-global scope
# ---------------------------------------------------------------------------

class TestTraceMetadata:
    def test_all_value_types(self):
        def build(w):
            w.add_trace_metadata("tool", "sim")
            w.add_trace_metadata("cores", 8)
            w.add_trace_metadata("offset", -3)
            w.add_trace_metadata("vdd", 1.8)
            w.add_trace_metadata("strict", True)
            w.add_trace_metadata("ints", [10, 20, 30])
            w.add_trace_metadata("names", ["a", "b", "c"])
        r = _roundtrip(build)
        tm = r.trace_metadata()
        assert tm == {
            "tool": "sim", "cores": 8, "offset": -3, "vdd": 1.8,
            "strict": True, "ints": [10, 20, 30], "names": ["a", "b", "c"],
        }

    def test_absent_metadata_returns_empty(self):
        r = _roundtrip(lambda w: None)
        assert r.trace_metadata() == {}

    def test_explicit_type_override(self):
        def build(w):
            # force a non-negative int to i64
            w.add_trace_metadata("v", 5, attr_type=int(AttrType.AT_I64))
        r = _roundtrip(build)
        assert r.trace_metadata()["v"] == 5


# ---------------------------------------------------------------------------
# Stream-type scope + profiles
# ---------------------------------------------------------------------------

class TestStreamMetadata:
    def test_stream_metadata_and_profiles(self):
        ids = {}
        def build(w):
            t = w.add_stream_type("AXI4")
            ids["t"] = t
            w.add_stream_metadata(t, "isa", "rv64")
            w.add_stream_metadata(t, "xlen", 64)
            w.add_stream_metadata(t, "le", True)
            w.add_stream_profile(t, "org.fvutils.trlog.profile.exec-trace")
            w.add_stream_profile(t, "com.acme.profile.x")
        r = _roundtrip(build)
        t = ids["t"]
        sm = r.stream_metadata(t)
        assert sm["isa"] == "rv64" and sm["xlen"] == 64 and sm["le"] is True
        assert r.profiles(t) == [
            "org.fvutils.trlog.profile.exec-trace", "com.acme.profile.x"]

    def test_profiles_empty_when_none(self):
        ids = {}
        def build(w):
            ids["t"] = w.add_stream_type("BUS")
        r = _roundtrip(build)
        assert r.profiles(ids["t"]) == []


# ---------------------------------------------------------------------------
# Instance scope (H_ATTR2) + legacy H_ATTR coexistence
# ---------------------------------------------------------------------------

class TestInstanceTypedAttrs:
    def test_var_typed_attrs(self):
        def build(w):
            st = w.add_signal_type(SignalEncoding.SE_2STATE, 1)
            with w.begin_hierarchy() as h:
                h.begin_scope(ScopeType.ST_MODULE, "top")
                h.add_var("clk", st, driver_file="rtl/ff.sv", driver_line=1)
                h.add_typed_attr("freq_mhz", 100)
                h.add_typed_attr("rails", ["vdd", "vss"])
                h.end_scope()
        r = _roundtrip(build)
        hier = list(r.hierarchies.values())[0]
        var = list(hier.vars.values())[0]
        # legacy string H_ATTR still present (driver_file/line)
        assert len(var.attrs) == 2
        # typed attrs decoded
        typed = {r.string_table.lookup(a.key_str_id): r._resolve_typed_attr(a)
                 for a in var.typed_attrs}
        assert typed["freq_mhz"] == 100
        assert typed["rails"] == ["vdd", "vss"]


# ---------------------------------------------------------------------------
# Non-regression: metadata is fully opt-in
# ---------------------------------------------------------------------------

def test_metadata_is_opt_in_no_meta_block():
    """A trace with no transparent metadata writes no BLK_META and no V2 entry."""
    from trlog._types import BlockType
    buf = io.BytesIO()
    with TrlWriter(buf, compress=False) as w:
        st = w.add_signal_type(SignalEncoding.SE_2STATE, 1)
        with w.begin_hierarchy() as h:
            h.begin_scope(ScopeType.ST_MODULE, "top")
            h.add_var("clk", st)
            h.end_scope()
    raw = buf.getvalue()
    # No BLK_META (0x04) block-type byte should appear as a block header start.
    # (We check the reader reports empty metadata, which is the contract.)
    buf.seek(0)
    with TrlReader(buf) as r:
        assert r.trace_metadata() == {}
        assert r._meta is None
