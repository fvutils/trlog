"""Values written by the C library read back in full (issues #1 #2).

2-state values wider than 64 bits and multi-bit 4-state values used to be
truncated by the C writer. Each signal here is written by both the C writer
(through ctypes) and the pure-Python writer; both must read back the same.
"""

from __future__ import annotations

import pytest

from trlog import _native
from trlog._reader import TrlReader
from trlog._writer import TrlWriter
from trlog._types import ScopeType, SignalEncoding

pytestmark = pytest.mark.skipif(
    _native._LIB is None,
    reason="libtrl.so not built; build c/ and/or set TRL_LIBRARY_PATH",
)

W72_A = (0xAB << 64) | 0x0123456789ABCDEF
W72_B = (0xFF << 64) | 0xFFFFFFFFFFFFFFFF
W128 = (0xDEADBEEF << 96) | (0x1 << 64) | 0x2

SIGNALS = [
    # name, encoding, width, [(time, value), ...]
    ("w72", SignalEncoding.SE_2STATE, 72, [(0, W72_A), (10, W72_B), (20, 5), (30, W72_A)]),
    ("w128", SignalEncoding.SE_2STATE, 128, [(0, 0xDEADBEEF), (10, W128)]),
    ("l8", SignalEncoding.SE_4STATE, 8, [(0, 0xA5), (10, 0x3C), (20, "X"), (30, 0x81)]),
    ("l3", SignalEncoding.SE_4STATE, 3, [(0, 5), (10, "X"), (20, 2)]),
]


def _write(W, path):
    ids = {}
    with W(str(path), compress=False) as w:
        types = {(enc, bw): w.add_signal_type(enc, bw) for _, enc, bw, _ in SIGNALS}
        with w.begin_hierarchy() as h:
            h.begin_scope(ScopeType.ST_MODULE, "top")
            for name, enc, bw, _ in SIGNALS:
                ids[name] = h.add_var(name, types[(enc, bw)])
            h.end_scope()
        with w.begin_vc_block(0) as vc:
            changes = sorted((t, name, v) for name, _, _, chg in SIGNALS for t, v in chg)
            for t, name, v in changes:
                vc.add_change(ids[name], t, v)
    return ids


def _read(path, ids):
    r = TrlReader(str(path))
    return {name: [(c.time, c.value) for c in r.read_signal(vid)] for name, vid in ids.items()}


@pytest.mark.parametrize("name", [s[0] for s in SIGNALS])
def test_c_written_values_read_back_in_full(name, tmp_path):
    c_ids = _write(_native._CtypesWriter, tmp_path / "c.trl")
    py_ids = _write(TrlWriter, tmp_path / "py.trl")
    got = _read(tmp_path / "c.trl", c_ids)[name]
    want = _read(tmp_path / "py.trl", py_ids)[name]
    assert got == want
    # Binary values must survive exactly (not cut to 64 bits).
    sig = next(s for s in SIGNALS if s[0] == name)
    for (t, v), (gt, gv) in zip(sig[3], got):
        assert gt == t
        if isinstance(v, int):
            assert gv == v
