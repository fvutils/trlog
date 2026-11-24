"""Generate the query benchmark trace (query_impl_status.md, Q4).

    PYTHONPATH=python/src python benchmarks/query/bigtrace.py OUT.trl 200000

8 streams under ``top`` (m0..m7), one ``axi`` type (addr:u64, len:u8),
1000 txns per block, every txn complete (TR_FULL), 100 ticks apart (ps).
"""
import sys

from trlog._types import FieldDef, FieldType, ScopeType, TxnAttr
from trlog._writer import TrlWriter


def build(path: str, n: int) -> None:
    with TrlWriter(path, timescale_exp=-12) as w:
        ty = w.add_txn_schema("axi", [FieldDef(w.intern("addr"), FieldType.FT_U64),
                                      FieldDef(w.intern("len"), FieldType.FT_U8)])
        st = w.add_stream_type("mon")
        with w.begin_hierarchy() as h:
            h.begin_scope(ScopeType.ST_MODULE, "top")
            sids = [h.add_stream(st, f"m{i}") for i in range(8)]
            h.end_scope()
        per = 1000
        tid = 1
        for blk in range(n // per // len(sids)):
            for s in sids:
                t0 = blk * per * 100
                with w.begin_txn_block(t0, stream_inst_id=s) as b:
                    for i in range(per):
                        b.write_full(s, ty, tid, t0 + i * 100, t0 + i * 100 + 50, 0,
                                     [TxnAttr(0, (tid * 64) & 0xffffff),
                                      TxnAttr(1, tid % 16)])
                        tid += 1


if __name__ == "__main__":
    build(sys.argv[1], int(sys.argv[2]))
