"""Integration tests for compression in writer/reader pipeline."""

import pytest
from trlog import TrlWriter, TrlReader, SignalEncoding, ScopeType


class TestCompression:
    def test_uncompressed_roundtrip(self, tmp_path):
        p = tmp_path / "t.trl"
        with TrlWriter(str(p), compress=False) as w:
            sig_t = w.add_signal_type(SignalEncoding.SE_2STATE, 1)
            with w.begin_hierarchy() as h:
                h.begin_scope(ScopeType.ST_MODULE, "top")
                v = h.add_var("clk", sig_t)
                h.end_scope()
            with w.begin_vc_block(0) as vc:
                for i in range(50):
                    vc.add_change(v, i * 10, i % 2)

        with TrlReader(str(p)) as r:
            changes = r.read_signal(v)
            assert len(changes) == 50

    def test_zlib_compressed_roundtrip(self, tmp_path):
        p = tmp_path / "t.trl"
        with TrlWriter(str(p), compress=True, compressor="zlib") as w:
            sig_t = w.add_signal_type(SignalEncoding.SE_2STATE, 1)
            with w.begin_hierarchy() as h:
                h.begin_scope(ScopeType.ST_MODULE, "top")
                v = h.add_var("clk", sig_t)
                h.end_scope()
            with w.begin_vc_block(0) as vc:
                for i in range(200):
                    vc.add_change(v, i * 5, i % 2)

        with TrlReader(str(p)) as r:
            changes = r.read_signal(v)
            assert len(changes) == 200

    def test_compressed_smaller_than_uncompressed(self, tmp_path):
        """Compressed file should be smaller than uncompressed for repetitive data."""
        p_compressed = tmp_path / "c.trl"
        p_plain = tmp_path / "p.trl"

        def write(path, compress):
            with TrlWriter(str(path), compress=compress) as w:
                sig_t = w.add_signal_type(SignalEncoding.SE_2STATE, 1)
                with w.begin_vc_block(0) as vc:
                    for i in range(1000):
                        vc.add_change(sig_t, i * 2, i % 2)

        write(p_compressed, True)
        write(p_plain, False)
        assert p_compressed.stat().st_size < p_plain.stat().st_size
