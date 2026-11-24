"""Write the small request/response trace used by the ``trlog match`` examples.

    python examples/query_bus.py bus.trl
    trlog match bus.trl --a "type=='req'" --b "type=='rsp'" --on tag

Times are in ns:

    top.bus.req  req: [1] tag 1 @0-1  [2] tag 2 @2-3  [3] tag 1 @4-5
                      [4] tag 3 @6-7 (addr 2^64-1)  [5] tag 4 @20-21
    top.bus.rsp  rsp: [11] tag 2 @8-9  [12] tag 1 @10-11  [13] tag 1 @12-14
                      [14] tag 5 @15-16  [15] tag 4 @19 (open)  [16] no tag @22-23
    top.bus.mon  req: [21] @0-10  [22] @2-4 (inside 21)  [23] @12-13

The test suite (tests/query/test_analysis.py) builds the same trace.
"""

import sys

from trlog import FieldDef, FieldType, Radix, ScopeType, TrlWriter, TxnAttr

BIG = (1 << 64) - 1


def _a(*vals):
    return [TxnAttr(field_idx=i, value=v) for i, v in enumerate(vals)]


def build_bus(path) -> None:
    with TrlWriter(str(path), timescale_exp=-9, compress=False) as w:
        def fd(name, ft):
            return FieldDef(name_str_id=w.intern(name), field_type=ft,
                            display_hint=Radix.RX_DEC, enum_type_id=0)
        t_req = w.add_txn_schema("req", [fd("tag", FieldType.FT_U8),
                                         fd("addr", FieldType.FT_U64)])
        t_rsp = w.add_txn_schema("rsp", [fd("tag", FieldType.FT_U8),
                                         fd("q", FieldType.FT_F64)])
        st = w.add_stream_type("bus", kind="bus")
        with w.begin_hierarchy(name="design") as h:
            h.begin_scope(ScopeType.ST_MODULE, "top")
            h.begin_scope(ScopeType.ST_MODULE, "bus")
            req = h.add_stream(st, "req")
            rsp = h.add_stream(st, "rsp")
            mon = h.add_stream(st, "mon")
            h.end_scope()
            h.end_scope()
        with w.begin_txn_block(0, stream_inst_id=req) as b:
            for i, (tag, s, addr) in enumerate([(1, 0, 0x10), (2, 2, 0x20), (1, 4, 0x30),
                                                (3, 6, BIG), (4, 20, 0x50)]):
                b.write_full(req, t_req, i + 1, s, s + 1, 0, _a(tag, addr))
        with w.begin_txn_block(0, stream_inst_id=rsp) as b:
            b.write_full(rsp, t_rsp, 11, 8, 9, 0, _a(2, 0.5))
            b.write_full(rsp, t_rsp, 12, 10, 11, 0, _a(1, 1.5))
            b.write_full(rsp, t_rsp, 13, 12, 14, 0, _a(1, 2.5))
            b.write_full(rsp, t_rsp, 14, 15, 16, 0, _a(5, 1.0))
            b.write_begin(rsp, t_rsp, 15, 19, 0)
            b.write_attr(15, _a(4))     # tag only; the response is still open
            b.write_full(rsp, t_rsp, 16, 22, 23, 0, [])
        with w.begin_txn_block(0, stream_inst_id=mon) as b:
            b.write_full(mon, t_req, 21, 0, 10, 0, _a(9, 0))
            b.write_full(mon, t_req, 22, 2, 4, 0, _a(9, 0))
            b.write_full(mon, t_req, 23, 12, 13, 0, _a(9, 0))


if __name__ == "__main__":
    if len(sys.argv) != 2:
        sys.exit("usage: python examples/query_bus.py OUT.trl")
    build_bus(sys.argv[1])
