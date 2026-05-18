"""Benchmark: write 1 M TR_FULL records with 5 attributes each."""

import pytest
import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '../../python/src'))

from trlog import TrlWriter
from trlog._types import SignalEncoding, ScopeType, FieldType, FieldDef, TxnAttr


def _make_attrs():
    return [
        TxnAttr(field_idx=0, value=i)
        for i in range(5)
    ]


def bench_write_1m_txn_full(benchmark, tmp_path):
    """Write 1 M TR_FULL records with 5 u64 attrs; measure records/s."""

    def _write():
        p = tmp_path / "b.trl"
        attrs = _make_attrs()
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
        return p.stat().st_size

    size = benchmark(_write)
    count = 1_000_000
    if benchmark.stats:
        rate = count / benchmark.stats["mean"]
        print(f"\n  File size: {size:,} bytes, rate: {rate:,.0f} rec/s")
