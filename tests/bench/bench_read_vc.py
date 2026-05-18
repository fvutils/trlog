"""Benchmark: read 1 M clock toggles (1-bit 2-state)."""

import pytest
import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '../../python/src'))

from trlog import TrlWriter, TrlReader
from trlog._types import SignalEncoding, ScopeType


@pytest.fixture(scope="module")
def vc_file(tmp_path_factory):
    """Write a 1 M toggle file once; return (path, var_id)."""
    p = tmp_path_factory.mktemp("bench") / "bench_vc.trl"
    with TrlWriter(str(p), compress=False) as w:
        sig_t = w.add_signal_type(SignalEncoding.SE_2STATE, 1)
        with w.begin_hierarchy() as h:
            h.begin_scope(ScopeType.ST_MODULE, "top")
            v = h.add_var("clk", sig_t)
            h.end_scope()
        with w.begin_vc_block(0) as vc:
            for i in range(1_000_000):
                vc.add_change(v, i * 10, i % 2)
    return p, v


def bench_read_1m_toggles(benchmark, vc_file):
    """Read 1 M clock toggles; measure MB/s."""
    path, v = vc_file

    def _read():
        with TrlReader(str(path)) as r:
            changes = r.read_signal(v)
        return len(changes)

    count = benchmark(_read)
    size = path.stat().st_size
    mb = size / (1024 * 1024)
    if benchmark.stats:
        rate = mb / benchmark.stats["mean"]
        print(f"\n  {count:,} changes, {mb:.2f} MB, rate: {rate:.1f} MB/s")
