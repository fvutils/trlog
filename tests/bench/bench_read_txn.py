"""Benchmark: read 1 M TR_FULL records."""

import pytest
import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '../../python/src'))

from trlog import TrlWriter, TrlReader
from trlog._types import FieldType, FieldDef, TxnAttr


@pytest.fixture(scope="module")
def txn_file(tmp_path_factory):
    """Write a 1 M TR_FULL file once; return path."""
    p = tmp_path_factory.mktemp("bench") / "bench_txn.trl"
    attrs = [TxnAttr(field_idx=i, value=i * 100) for i in range(5)]
    with TrlWriter(str(p), compress=False) as w:
        fields = [FieldDef(name_str_id=0, field_type=FieldType.FT_U64)] * 5
        schema_id = w.add_txn_schema("TxnType", fields)
        with w.begin_txn_block(0) as txn:
            for i in range(1_000_000):
                txn.write_full(
                    stream_inst_id=1,
                    txn_type_id=schema_id,
                    txn_id=i,
                    start=i * 10,
                    end=i * 10 + 5,
                    parent=0,
                    attrs=attrs,
                )
    return p


def bench_read_1m_txn_full(benchmark, txn_file):
    """Read 1 M TR_FULL records sequentially; measure records/s."""

    def _read():
        total = 0
        with TrlReader(str(txn_file)) as r:
            for blk in r.iter_txn_blocks():
                total += len(blk)
        return total

    count = benchmark(_read)
    if benchmark.stats:
        rate = count / benchmark.stats["mean"]
        print(f"\n  {count:,} records, rate: {rate:,.0f} rec/s")
