"""TrlWriter(compressor=...) selects the block compressor (issue #22).

The ``zstandard`` package is optional and may not be installed, so the
wiring is checked with a stand-in module that wraps zlib: every compressed
block must carry FLAG_COMPRESS_ALG, and the file must read back through it.
"""
import sys
import types
import zlib

import pytest

from trlog import TrlWriter, TrlReader
from trlog._types import (
    FLAG_COMPRESSED, FLAG_COMPRESS_ALG, BlockType, FieldDef, FieldType as FT,
    ScopeType, SignalEncoding, TxnAttr, TxnFull,
)


@pytest.fixture
def fake_zstd(monkeypatch):
    mod = types.ModuleType("zstandard")

    class ZstdCompressor:
        def compress(self, data):
            return b"FZ" + zlib.compress(data)

    class ZstdDecompressor:
        def decompress(self, data, max_output_size=0):
            assert data[:2] == b"FZ"
            return zlib.decompress(data[2:])

    mod.ZstdCompressor = ZstdCompressor
    mod.ZstdDecompressor = ZstdDecompressor
    monkeypatch.setitem(sys.modules, "zstandard", mod)
    return mod


def _write(path, **kw):
    with TrlWriter(str(path), **kw) as w:
        st = w.add_signal_type(SignalEncoding.SE_2STATE, 8)
        w.add_trace_metadata("tool", "x" * 40)
        tid = w.add_txn_schema("Pkt", [
            FieldDef(name_str_id=w.intern("a"), field_type=FT.FT_U16)])
        with w.begin_hierarchy() as h:
            h.begin_scope(ScopeType.ST_MODULE, "top")
            v = h.add_var("d", st)
            h.end_scope()
        with w.begin_vc_block(0) as vc:
            for t in range(1, 50):
                vc.add_change(v, t, t & 0xff)
        with w.begin_txn_block(0) as txn:
            txn.write_full(1, tid, 1, 0, 5, 0, [TxnAttr(0, 3)])
    return v


def _block_flags(path):
    with TrlReader(str(path)) as r:
        r._file.seek(0)
        return [(t, f) for t, f, _p, _o in r._iter_raw_blocks()]


def test_zstd_blocks_flagged_and_readable(tmp_path, fake_zstd):
    p = tmp_path / "z.trl"
    v = _write(p, compressor="zstd")
    flags = _block_flags(p)
    compressed = [(t, f) for t, f in flags if f & FLAG_COMPRESSED]
    assert compressed, flags
    assert all(f & FLAG_COMPRESS_ALG for _, f in compressed), flags
    kinds = {t for t, _ in compressed}
    for bt in (BlockType.BLK_VC_DATA, BlockType.BLK_TXN_DATA,
               BlockType.BLK_HIERARCHY, BlockType.BLK_STR_TABLE,
               BlockType.BLK_META):
        assert int(bt) in kinds, (bt, flags)
    with TrlReader(str(p)) as r:
        assert len(list(r.read_signal(v))) >= 49
        fulls = [rec for blk in r.iter_txn_blocks() for rec in blk
                 if isinstance(rec, TxnFull)]
        assert [a.value for a in fulls[0].attrs] == [3]


def test_zlib_default_has_no_alg_flag(tmp_path):
    p = tmp_path / "zl.trl"
    _write(p)
    assert not any(f & FLAG_COMPRESS_ALG for _, f in _block_flags(p))


def test_unknown_compressor_rejected(tmp_path):
    with pytest.raises(ValueError):
        TrlWriter(str(tmp_path / "x.trl"), compressor="brotli")


def test_zstd_without_package_fails_at_open(tmp_path, monkeypatch):
    monkeypatch.setitem(sys.modules, "zstandard", None)
    p = tmp_path / "n.trl"
    with pytest.raises(ImportError):
        TrlWriter(str(p), compressor="zstd")
