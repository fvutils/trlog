"""Benchmark: write 1 M clock toggles (1-bit 2-state)."""

import pytest
import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '../../python/src'))

from trlog import TrlWriter
from trlog._types import SignalEncoding, ScopeType


@pytest.fixture
def vc_file(tmp_path):
    """Return path to a pre-written 1M toggle file."""
    p = tmp_path / "bench_vc.trl"
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


def bench_write_1m_toggles(benchmark, tmp_path):
    """Write 1 M clock toggles; measure MB/s."""

    def _write():
        p = tmp_path / "b.trl"
        with TrlWriter(str(p), compress=False) as w:
            sig_t = w.add_signal_type(SignalEncoding.SE_2STATE, 1)
            with w.begin_hierarchy() as h:
                h.begin_scope(ScopeType.ST_MODULE, "top")
                h.add_var("clk", sig_t)
                h.end_scope()
            with w.begin_vc_block(0) as vc:
                for i in range(1_000_000):
                    vc.add_change(sig_t, i * 10, i % 2)
        return p.stat().st_size

    size = benchmark(_write)
    mb = size / (1024 * 1024)
    if benchmark.stats:
        rate = mb / benchmark.stats["mean"]
        print(f"\n  File size: {size:,} bytes ({mb:.2f} MB), rate: {rate:.1f} MB/s")
