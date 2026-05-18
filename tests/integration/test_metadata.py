"""Integration tests for metadata facilities via the TrlWriter API.

Covers:
 - src_file / src_line on scopes and vars (H_SCOPE / H_VAR structured fields)
 - driver_file / driver_line on vars (H_ATTR with well-known keys)
 - src_file / src_line on stream instances (H_ATTR with well-known keys)
 - write_meta() on transactions (TR_META)
 - origin_file / origin_line / origin_component convenience params on
   write_full() and write_begin()
"""

import io
import pytest
from trlog._writer import TrlWriter
from trlog._reader import TrlReader
from trlog._types import (
    SignalEncoding, VarDir, ScopeType, HierKind, FieldType, FieldDef,
    TxnMeta, WellKnownAttr,
)


def make_writer():
    buf = io.BytesIO()
    w = TrlWriter(buf, timescale_exp=-9, compress=False)
    return w, buf


def open_reader(buf):
    buf.seek(0)
    return TrlReader(buf)


class TestHierarchySrcLocation:
    def test_scope_src_location(self):
        w, buf = make_writer()
        sig_t = w.add_signal_type(SignalEncoding.SE_2STATE, 1)
        with w.begin_hierarchy() as h:
            h.begin_scope(ScopeType.ST_MODULE, "top",
                          src_file="rtl/top.sv", src_line=10)
            h.add_var("clk", sig_t)
            h.end_scope()
        w.close()

        buf.seek(0)
        r = TrlReader(buf)
        hier = list(r.hierarchies.values())[0]
        scope = hier._root_children[0]
        file_str = r.string_table.lookup(scope.src_file_str_id)
        assert file_str == "rtl/top.sv"
        assert scope.src_line == 10

    def test_var_src_location(self):
        w, buf = make_writer()
        sig_t = w.add_signal_type(SignalEncoding.SE_2STATE, 1)
        with w.begin_hierarchy() as h:
            h.begin_scope(ScopeType.ST_MODULE, "top")
            h.add_var("data", sig_t, src_file="rtl/top.sv", src_line=25)
            h.end_scope()
        w.close()

        buf.seek(0)
        r = TrlReader(buf)
        hier = list(r.hierarchies.values())[0]
        var = list(hier.vars.values())[0]
        file_str = r.string_table.lookup(var.src_file_str_id)
        assert file_str == "rtl/top.sv"
        assert var.src_line == 25

    def test_var_driver_attrs(self):
        w, buf = make_writer()
        sig_t = w.add_signal_type(SignalEncoding.SE_2STATE, 1)
        with w.begin_hierarchy() as h:
            h.begin_scope(ScopeType.ST_MODULE, "top")
            h.add_var("q", sig_t, driver_file="rtl/ff.sv", driver_line=42)
            h.end_scope()
        w.close()

        buf.seek(0)
        r = TrlReader(buf)
        hier = list(r.hierarchies.values())[0]
        var = list(hier.vars.values())[0]
        assert len(var.attrs) == 2
        keys = {r.string_table.lookup(a.key_str_id): r.string_table.lookup(a.value_str_id)
                for a in var.attrs}
        assert keys[WellKnownAttr.DRIVER_FILE] == "rtl/ff.sv"
        assert keys[WellKnownAttr.DRIVER_LINE] == "42"

    def test_stream_src_attrs(self):
        w, buf = make_writer()
        stream_t = w.add_stream_type("AXI4")
        with w.begin_hierarchy() as h:
            h.begin_scope(ScopeType.ST_MODULE, "top")
            h.add_stream(stream_t, "axi_m",
                         src_file="tb/axi_agent.sv", src_line=7)
            h.end_scope()
        w.close()

        buf.seek(0)
        r = TrlReader(buf)
        hier = list(r.hierarchies.values())[0]
        stream = list(hier.streams.values())[0]
        assert len(stream.attrs) == 2
        keys = {r.string_table.lookup(a.key_str_id): r.string_table.lookup(a.value_str_id)
                for a in stream.attrs}
        assert keys[WellKnownAttr.SRC_FILE] == "tb/axi_agent.sv"
        assert keys[WellKnownAttr.SRC_LINE] == "7"


class TestTxnOriginMeta:
    def test_write_meta_direct(self):
        w, buf = make_writer()
        schema_id = w.add_txn_schema("req", [])
        stream_t = w.add_stream_type("BUS")
        with w.begin_hierarchy() as h:
            h.begin_scope(ScopeType.ST_MODULE, "top")
            stream_inst = h.add_stream(stream_t, "bus")
            h.end_scope()
        with w.begin_txn_block(0) as tb:
            tb.write_full(stream_inst, schema_id, txn_id=1,
                          start=0, end=100, parent=0)
            tb.write_meta(1, WellKnownAttr.ORIGIN_FILE, "seq/my_seq.sv")
            tb.write_meta(1, WellKnownAttr.ORIGIN_LINE, "88")
        w.close()

        buf.seek(0)
        r = TrlReader(buf)
        metas = [rec for block in r.iter_txn_blocks()
                 for rec in block if isinstance(rec, TxnMeta)]
        assert len(metas) == 2
        keys = {r.string_table.lookup(m.key_str_id): r.string_table.lookup(m.value_str_id)
                for m in metas}
        assert keys[WellKnownAttr.ORIGIN_FILE] == "seq/my_seq.sv"
        assert keys[WellKnownAttr.ORIGIN_LINE] == "88"

    def test_write_full_origin_convenience(self):
        w, buf = make_writer()
        schema_id = w.add_txn_schema("req", [])
        stream_t = w.add_stream_type("BUS")
        with w.begin_hierarchy() as h:
            h.begin_scope(ScopeType.ST_MODULE, "top")
            stream_inst = h.add_stream(stream_t, "bus")
            h.end_scope()
        with w.begin_txn_block(0) as tb:
            tb.write_full(stream_inst, schema_id, txn_id=2,
                          start=0, end=50, parent=0,
                          origin_file="seq/my_seq.sv", origin_line=12,
                          origin_component="my_seq")
        w.close()

        buf.seek(0)
        r = TrlReader(buf)
        metas = [rec for block in r.iter_txn_blocks()
                 for rec in block if isinstance(rec, TxnMeta)]
        keys = {r.string_table.lookup(m.key_str_id): r.string_table.lookup(m.value_str_id)
                for m in metas}
        assert keys[WellKnownAttr.ORIGIN_FILE] == "seq/my_seq.sv"
        assert keys[WellKnownAttr.ORIGIN_LINE] == "12"
        assert keys[WellKnownAttr.ORIGIN_COMPONENT] == "my_seq"

    def test_write_begin_origin_convenience(self):
        w, buf = make_writer()
        schema_id = w.add_txn_schema("req", [])
        stream_t = w.add_stream_type("BUS")
        with w.begin_hierarchy() as h:
            h.begin_scope(ScopeType.ST_MODULE, "top")
            stream_inst = h.add_stream(stream_t, "bus")
            h.end_scope()
        with w.begin_txn_block(0) as tb:
            tb.write_begin(stream_inst, schema_id, txn_id=3, start=0,
                           origin_file="seq/rand_seq.sv", origin_line=5,
                           origin_component="rand_seq")
            tb.write_end(3, end_time=200)
        w.close()

        buf.seek(0)
        r = TrlReader(buf)
        metas = [rec for block in r.iter_txn_blocks()
                 for rec in block if isinstance(rec, TxnMeta)]
        keys = {r.string_table.lookup(m.key_str_id): r.string_table.lookup(m.value_str_id)
                for m in metas}
        assert keys[WellKnownAttr.ORIGIN_FILE] == "seq/rand_seq.sv"
        assert keys[WellKnownAttr.ORIGIN_COMPONENT] == "rand_seq"

    def test_no_origin_emits_no_meta(self):
        w, buf = make_writer()
        schema_id = w.add_txn_schema("req", [])
        stream_t = w.add_stream_type("BUS")
        with w.begin_hierarchy() as h:
            h.begin_scope(ScopeType.ST_MODULE, "top")
            stream_inst = h.add_stream(stream_t, "bus")
            h.end_scope()
        with w.begin_txn_block(0) as tb:
            tb.write_full(stream_inst, schema_id, txn_id=4,
                          start=0, end=10, parent=0)
        w.close()

        buf.seek(0)
        r = TrlReader(buf)
        metas = [rec for block in r.iter_txn_blocks()
                 for rec in block if isinstance(rec, TxnMeta)]
        assert len(metas) == 0
