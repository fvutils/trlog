"""Deterministic trace for the query CLI tests (query_cli_design.md §9).

``build(path)`` writes ``query_corpus.trl``: four streams under a two-level
hierarchy, parent/child txns, cause and related links, open txns, a txn
split into BEGIN/ATTR/END across blocks, mixed types per stream, enums,
bitvec/logicvec/bytes fields, and per-txn metadata. Times are in ps.

Layout (ids in brackets)::

    top.env.seqr      seq_item  [1] wr_burst  [2] rd_burst  [3] err_test  [4] hang (open)
    top.env.axi0.mon  axi_wr [10..13] (parent 1)   axi_rd [20..23] (parent 2)
    top.env.axi1.mon  axi_wr [30] SLVERR, axi_rd [31] DECERR split over 2 blocks,
                      axi_rd [32] open                   (parents 3, 3, 4)
    top.log           msg [100..103]; links 30->101 (cause), 31->102 (cause),
                      32->103 (related); also 3->30 (cause), so 3->30->101
                      is a two-hop cause chain
"""

from __future__ import annotations

from trlog._types import FieldDef, FieldType, LinkType, ScopeType, TxnAttr, Radix
from trlog._writer import TrlWriter

OKAY, EXOKAY, SLVERR, DECERR = 0, 1, 2, 3
INFO, WARN, ERROR = 0, 1, 2


def _a(*vals):
    return [TxnAttr(field_idx=i, value=v) for i, v in enumerate(vals)]


def _attr_rec(kinds, *vals):
    """Attrs for a TR_ATTR record, which carries each value's kind inline."""
    out = []
    for i, (ft, v) in enumerate(zip(kinds, vals)):
        if v is None:
            continue
        a = TxnAttr(field_idx=i, value=v)
        a._field_type = ft
        out.append(a)
    return out


def build(path) -> None:
    with TrlWriter(str(path), timescale_exp=-12, compress=False) as w:
        w.add_trace_metadata("tool", "query_corpus")
        w.add_trace_metadata("seed", 1)
        resp = w.add_enum_type("axi_resp", [("OKAY", OKAY), ("EXOKAY", EXOKAY),
                                            ("SLVERR", SLVERR), ("DECERR", DECERR)])
        sev = w.add_enum_type("severity", [("INFO", INFO), ("WARN", WARN),
                                           ("ERROR", ERROR)])

        def fd(name, ft, radix=Radix.RX_HEX, enum=0):
            return FieldDef(name_str_id=w.intern(name), field_type=ft,
                            display_hint=radix, enum_type_id=enum)

        RD = [FieldType.FT_U64, FieldType.FT_U8, FieldType.FT_ENUM,
              FieldType.FT_BITVEC, FieldType.FT_LOGICVEC]
        SEQ = [FieldType.FT_STRING, FieldType.FT_I32]
        t_rd = w.add_txn_schema("axi_rd", [
            fd("addr", FieldType.FT_U64), fd("len", FieldType.FT_U8, Radix.RX_DEC),
            fd("resp", FieldType.FT_ENUM, enum=resp), fd("data", FieldType.FT_BITVEC),
            fd("bus", FieldType.FT_LOGICVEC)])
        t_wr = w.add_txn_schema("axi_wr", [
            fd("addr", FieldType.FT_U64), fd("len", FieldType.FT_U8, Radix.RX_DEC),
            fd("resp", FieldType.FT_ENUM, enum=resp), fd("strb", FieldType.FT_BYTES)])
        t_seq = w.add_txn_schema("seq_item", [
            fd("name", FieldType.FT_STRING), fd("prio", FieldType.FT_I32, Radix.RX_DEC)])
        t_msg = w.add_txn_schema("msg", [
            fd("text", FieldType.FT_STRING), fd("sev", FieldType.FT_ENUM, enum=sev)])

        st_axi = w.add_stream_type("axi_mon", kind="axi")
        w.add_stream_metadata(st_axi, "protocol", "AXI4")
        st_seq = w.add_stream_type("seq", kind="sequencer")
        st_log = w.add_stream_type("log", kind="log")

        with w.begin_hierarchy(name="design") as h:
            h.begin_scope(ScopeType.ST_MODULE, "top")
            h.begin_scope(ScopeType.ST_MODULE, "env")
            h.begin_scope(ScopeType.ST_MODULE, "axi0")
            axi0 = h.add_stream(st_axi, "mon")
            h.end_scope()
            h.begin_scope(ScopeType.ST_MODULE, "axi1")
            axi1 = h.add_stream(st_axi, "mon")
            h.end_scope()
            seqr = h.add_stream(st_seq, "seqr")
            h.end_scope()
            log = h.add_stream(st_log, "log")
            h.end_scope()

        # Sequencer: three closed items and one that never ends.
        with w.begin_txn_block(0, stream_inst_id=seqr) as b:
            b.write_full(seqr, t_seq, 1, 1000, 20000, 0, _a("wr_burst", 1),
                         origin_file="tb/seq_lib.sv", origin_line=42)
            b.write_full(seqr, t_seq, 2, 21000, 40000, 0, _a("rd_burst", 2))
            b.write_full(seqr, t_seq, 3, 41000, 60000, 0, _a("err_test", 5))
            b.write_begin(seqr, t_seq, 4, 61000, 0)
            b.write_attr(4, _attr_rec(SEQ, "hang", 0))

        # axi0: a write burst in one block, a read burst in the next.
        with w.begin_txn_block(0, stream_inst_id=axi0) as b:
            for i in range(4):
                b.write_full(axi0, t_wr, 10 + i, 2000 + 4000 * i, 5000 + 4000 * i, 1,
                             _a(0x4000 + 0x10 * i, i + 1, OKAY, b"\x0f" * (i + 1)))
        with w.begin_txn_block(21000, stream_inst_id=axi0) as b:
            for i in range(4):
                b.write_full(axi0, t_rd, 20 + i, 22000 + 4000 * i, 24500 + 4000 * i, 2,
                             _a(0x4000 + 0x10 * i, 4, OKAY, (32, 0xA0A00000 + i),
                                (8, 0xA0 + i, 0)))

        # axi1: error responses; txn 31 is split BEGIN | ATTR+END across blocks.
        with w.begin_txn_block(40000, stream_inst_id=axi1) as b:
            b.write_full(axi1, t_wr, 30, 42000, 45000, 3,
                         _a(0x8000, 1, SLVERR, b"\xff"))
            b.write_meta(30, "note", "injected error")
            # err_test (seqr) caused the SLVERR: with 30->101 a two-hop chain
            b.write_link(LinkType.LT_CAUSE_EFFECT, 3, 30, w.intern("stimulus"))
            b.write_begin(axi1, t_rd, 31, 47000, 3)
        with w.begin_txn_block(49000, stream_inst_id=axi1) as b:
            b.write_attr(31, _attr_rec(RD, 0x8004, 1, DECERR, (32, 0), (8, 0x0F, 0xF0)))
            b.write_end(31, 49500)
            b.write_begin(axi1, t_rd, 32, 62000, 4)
            b.write_attr(32, _attr_rec(RD, 0xC000))

        # Log messages, linked from the txns that caused them.
        with w.begin_txn_block(0, stream_inst_id=log) as b:
            b.write_full(log, t_msg, 100, 19000, 19000, 0, _a("write burst done", INFO))
        with w.begin_txn_block(45000, stream_inst_id=log) as b:
            b.write_full(log, t_msg, 101, 45500, 45500, 0,
                         _a("slave error at 0x8000", ERROR))
            b.write_link(LinkType.LT_CAUSE_EFFECT, 30, 101, w.intern("resp"))
            b.write_full(log, t_msg, 102, 50500, 50500, 0, _a("decode error", ERROR))
            b.write_link(LinkType.LT_CAUSE_EFFECT, 31, 102, w.intern("resp"))
        with w.begin_txn_block(70000, stream_inst_id=log) as b:
            b.write_full(log, t_msg, 103, 70000, 70000, 0,
                         _a("timeout waiting for rsp", WARN))
            b.write_link(LinkType.LT_RELATED, 32, 103)
