"""Variable and stream IDs are unique across hierarchy blocks (issue #7).

Each hierarchy block used to number its variables and streams from 1, so a
second hierarchy reused the first one's IDs and value changes landed on the
wrong signal. Writers now continue numbering across hierarchies.
"""

from __future__ import annotations

import ctypes

import pytest

from trlog import _native
from trlog._reader import TrlReader
from trlog._writer import TrlWriter
from trlog._types import HierKind, ScopeType, SignalEncoding

needs_lib = pytest.mark.skipif(
    _native._LIB is None,
    reason="libtrl.so not built; build c/ and/or set TRL_LIBRARY_PATH",
)

HIERS = [
    (1, HierKind.HK_DESIGN, "design", ["a", "b"], ["s0"]),
    (2, HierKind.HK_SW, "sw", ["x", "y", "z"], ["s1", "s2"]),
]

VALUES = {"a": 1, "b": 2, "x": 3, "y": 4, "z": 5}


def _add_stream(w, h, stype, name):
    if isinstance(w, TrlWriter):
        return h.add_stream(stype, name)
    _native._LIB.trl_hier_add_stream.argtypes = [ctypes.c_void_p, ctypes.c_uint32, ctypes.c_uint32]
    _native._LIB.trl_hier_add_stream.restype = ctypes.c_uint32
    return int(_native._LIB.trl_hier_add_stream(w._handle, stype, w.intern(name)))


def _write(W, path):
    var_ids, stream_ids = {}, {}
    with W(str(path), compress=False) as w:
        sig_t = w.add_signal_type(SignalEncoding.SE_2STATE, 8)
        stype = w.add_stream_type("bus") if isinstance(w, TrlWriter) else 1
        for hid, kind, hname, vars_, streams in HIERS:
            with w.begin_hierarchy(hier_id=hid, kind=kind, name=hname) as h:
                h.begin_scope(ScopeType.ST_MODULE, "top_" + hname)
                for v in vars_:
                    var_ids[v] = h.add_var(v, sig_t)
                for s in streams:
                    stream_ids[s] = _add_stream(w, h, stype, s)
                h.end_scope()
        with w.begin_vc_block(0) as vc:
            for v, vid in var_ids.items():
                vc.add_change(vid, 0, VALUES[v])
    return var_ids, stream_ids


def _check(path, var_ids, stream_ids):
    assert len(set(var_ids.values())) == len(var_ids)
    assert len(set(stream_ids.values())) == len(stream_ids)
    assert 0 not in var_ids.values() and 0 not in stream_ids.values()
    with TrlReader(str(path)) as r:
        for hid, _, _, vars_, streams in HIERS:
            blk = r.hierarchies[hid]
            assert sorted(blk.vars) == sorted(var_ids[v] for v in vars_)
            assert sorted(blk.streams) == sorted(stream_ids[s] for s in streams)
        for v, vid in var_ids.items():
            assert [(c.time, c.value) for c in r.read_signal(vid)] == [(0, VALUES[v])]


def test_python_writer_ids_unique(tmp_path):
    p = tmp_path / "py.trl"
    _check(p, *_write(TrlWriter, p))


@needs_lib
def test_c_writer_ids_unique(tmp_path):
    p = tmp_path / "c.trl"
    _check(p, *_write(_native._CtypesWriter, p))
