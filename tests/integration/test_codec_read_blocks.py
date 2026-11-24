"""Codec-encoded value-change blocks are read correctly (issues #8 #9).

``TrlReader.read_signal(var, start=...)`` reads only the indexed blocks in
range; it must decode each with that block's codec, and skip blocks whose codec
is not registered, as ``iter_vc_blocks`` does. The C reader must skip a
structural-codec block it has no codec for instead of failing with
``TRL_ERR_CORRUPT``.
"""

from __future__ import annotations

import ctypes

import pytest

from trlog import _native
from trlog._reader import TrlReader
from trlog._writer import TrlWriter
from trlog._types import ScopeType, SignalEncoding
from trlog.codec import CORE_BYTESPLIT, _VC_CODECS

CLK = [(i * 10, i % 2) for i in range(8)]
ANA = [(i * 10, float(i) + 0.25) for i in range(8)]


def _write(path, compress=False):
    with TrlWriter(str(path), compress=compress) as w:
        bit = w.add_signal_type(SignalEncoding.SE_2STATE, 1)
        rt = w.add_signal_type(SignalEncoding.SE_REAL, 0)
        with w.begin_hierarchy() as h:
            h.begin_scope(ScopeType.ST_MODULE, "top")
            clk = h.add_var("clk", bit)
            ana = h.add_var("ana", rt)
            h.end_scope()
        with w.begin_vc_block(0) as vc:                       # core codec
            for t, v in CLK:
                vc.add_change(clk, t, v)
        with w.begin_vc_block(0, codec=CORE_BYTESPLIT) as vc:  # bytesplit
            for t, v in ANA:
                vc.add_change(ana, t, v)
    return clk, ana


@pytest.mark.parametrize("compress", [False, True])
def test_read_signal_start_uses_block_codec(tmp_path, compress):
    path = tmp_path / "bs.trl"
    clk, ana = _write(path, compress)
    with TrlReader(str(path)) as r:
        for start in (0, 25):
            got = [(c.time, c.value) for c in r.read_signal(ana, start=start)]
            assert got == [(t, v) for t, v in ANA if t >= start]
            got = [(c.time, c.value) for c in r.read_signal(clk, start=start)]
            assert got == [(t, v) for t, v in CLK if t >= start]
        # start= and no start agree
        assert r.read_signal(ana, start=0) == r.read_signal(ana)


def test_read_signal_start_skips_unknown_codec(tmp_path):
    path = tmp_path / "bs.trl"
    clk, ana = _write(path)
    saved = _VC_CODECS.pop(CORE_BYTESPLIT)
    try:
        with TrlReader(str(path)) as r:
            assert r.read_signal(ana, start=0) == []
            got = [(c.time, c.value) for c in r.read_signal(clk, start=0)]
            assert got == CLK
    finally:
        _VC_CODECS[CORE_BYTESPLIT] = saved


_VC_CB = ctypes.CFUNCTYPE(None, ctypes.c_uint32, ctypes.c_uint64,
                          ctypes.POINTER(ctypes.c_uint8), ctypes.c_uint32,
                          ctypes.c_void_p)


@pytest.mark.skipif(_native._LIB is None,
                    reason="libtrl.so not built; build c/ and/or set TRL_LIBRARY_PATH")
@pytest.mark.parametrize("compress", [False, True])
def test_c_reader_skips_unknown_codec_block(tmp_path, compress):
    lib = _native._LIB
    lib.trl_iter_vc.argtypes = [ctypes.c_void_p, _VC_CB, ctypes.c_void_p]
    lib.trl_iter_vc.restype = ctypes.c_int
    lib.trl_reader_close.argtypes = [ctypes.c_void_p]
    lib.trl_reader_close.restype = ctypes.c_int

    path = tmp_path / "bs.trl"
    clk, ana = _write(path, compress)
    r = lib.trl_reader_open(str(path).encode())
    assert r, "trl_reader_open failed"
    seen = []

    def on_vc(var_id, time, val, n, user):
        seen.append((var_id, time, bytes(val[:n])))

    cb = _VC_CB(on_vc)
    try:
        assert lib.trl_iter_vc(r, cb, None) == 0
    finally:
        lib.trl_reader_close(r)
    # The C library has no bytesplit codec: the core block is read, the
    # bytesplit block skipped.
    assert sorted((t, int.from_bytes(b, "big")) for v, t, b in seen if v == clk) == CLK
    assert not [s for s in seen if s[0] == ana]
