"""Integration tests for index-based random access."""

import pytest
from trlog import TrlWriter, TrlReader, SignalEncoding, ZstNoIndexError
from trlog._types import HierKind


class TestRandomAccess:
    def _write_file(self, path, num_blocks=5, changes_per_block=100):
        """Write a multi-block VC file; return list of (start_time, var_id)."""
        with TrlWriter(str(path), compress=False) as w:
            sig_t = w.add_signal_type(SignalEncoding.SE_2STATE, 1)
            for b in range(num_blocks):
                start = b * 1000
                with w.begin_vc_block(start) as vc:
                    for i in range(changes_per_block):
                        vc.add_change(sig_t, start + i * 10, i % 2)
        return sig_t

    def test_seek_time_returns_valid_offset(self, tmp_path):
        p = tmp_path / "t.trl"
        sig_t = self._write_file(p)
        with TrlReader(str(p)) as r:
            off = r.seek_time(2000)
            assert off > 0

    def test_seek_time_selects_correct_block(self, tmp_path):
        p = tmp_path / "t.trl"
        sig_t = self._write_file(p, num_blocks=3, changes_per_block=10)
        with TrlReader(str(p)) as r:
            changes = r.read_signal(sig_t, start=1000, end=1990)
            times = [c.time for c in changes]
            assert all(1000 <= t <= 1990 for t in times)
            assert len(times) == 10

    def test_no_index_raises(self, tmp_path):
        """A TrlReader on a file with index_offset=0 should raise ZstNoIndexError."""
        p = tmp_path / "t.trl"
        # Write normally and zero out the index_offset
        sig_t = self._write_file(p)
        import struct
        from trlog._writer import _INDEX_OFFSET_FIELD_POS
        raw = bytearray(p.read_bytes())
        struct.pack_into('<Q', raw, _INDEX_OFFSET_FIELD_POS, 0)
        p.write_bytes(bytes(raw))

        with TrlReader(str(p)) as r:
            assert r._index is None
            with pytest.raises(ZstNoIndexError):
                r.seek_time(500)

    def test_read_signal_time_range(self, tmp_path):
        p = tmp_path / "t.trl"
        with TrlWriter(str(p), compress=False) as w:
            sig_t = w.add_signal_type(SignalEncoding.SE_2STATE, 1)
            with w.begin_vc_block(0) as vc:
                for i in range(100):
                    vc.add_change(sig_t, i * 10, i % 2)
        with TrlReader(str(p)) as r:
            changes = r.read_signal(sig_t, start=200, end=400)
            assert all(200 <= c.time <= 400 for c in changes)
            assert len(changes) > 0

    def test_read_signal_all_without_range(self, tmp_path):
        p = tmp_path / "t.trl"
        with TrlWriter(str(p), compress=False) as w:
            sig_t = w.add_signal_type(SignalEncoding.SE_2STATE, 1)
            with w.begin_vc_block(0) as vc:
                for i in range(50):
                    vc.add_change(sig_t, i * 5, i % 2)
        with TrlReader(str(p)) as r:
            changes = r.read_signal(sig_t)
            assert len(changes) == 50
