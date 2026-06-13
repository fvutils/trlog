"""core.bytesplit transform + codec — Phase 3 (impl-plan §3, design §5.4).

Covers the pure byte-plane-split transform, the self-contained codec block
(byte-exact REAL + wide-vector round-trip), the size benefit on REAL data, the
end-to-end writer/reader path, and the structural-codec skip-unknown contract.
"""

from __future__ import annotations

import io
import struct
import zlib

import pytest

from trlog._writer import TrlWriter
from trlog._reader import TrlReader
from trlog._types import SignalEncoding, ScopeType, VcChange, FLAG_STRUCT_CODEC
from trlog._bytesplit import byte_split, byte_unsplit, BytesplitBlock
from trlog.codec import CORE_BYTESPLIT, _VC_CODECS


# ---------------------------------------------------------------------------
# Pure transform
# ---------------------------------------------------------------------------

class TestTransform:
    @pytest.mark.parametrize("width", [1, 2, 3, 4, 8])
    def test_roundtrip(self, width):
        raw = bytes((i * 7 + 3) & 0xFF for i in range(width * 17))
        assert byte_unsplit(byte_split(raw, width), width) == raw

    def test_width_one_is_identity(self):
        raw = b"abcdef"
        assert byte_split(raw, 1) == raw
        assert byte_unsplit(raw, 1) == raw

    def test_planes_are_grouped(self):
        # two 2-byte values 0x1122, 0x3344 -> planes [0x11,0x33][0x22,0x44]
        raw = bytes([0x11, 0x22, 0x33, 0x44])
        assert byte_split(raw, 2) == bytes([0x11, 0x33, 0x22, 0x44])

    def test_bad_length_raises(self):
        with pytest.raises(ValueError):
            byte_split(b"abc", 2)


# ---------------------------------------------------------------------------
# Self-contained codec block
# ---------------------------------------------------------------------------

class TestBytesplitBlock:
    def test_real_and_wide_roundtrip_exact(self):
        sig = {1: (SignalEncoding.SE_REAL, 0), 2: (SignalEncoding.SE_2STATE, 32)}
        blk = BytesplitBlock(0, sig, compress=True)
        reals = [(i * 10, 1000.0 + i * 0.5) for i in range(30)]
        wides = [(i * 10, (i * 0x1234567) & 0xFFFFFFFF) for i in range(30)]
        for t, v in reals:
            blk.add_change(1, t, v)
        for t, v in wides:
            blk.add_change(2, t, v)
        block = blk.encode_block(codec_id_str=5)
        assert block[1] & FLAG_STRUCT_CODEC
        got = []
        BytesplitBlock.decode_changes(block[10:], got.append, VcChange)
        rec = {(c.var_id, c.time): c.value for c in got}
        for t, v in reals:
            assert rec[(1, t)] == v
        for t, v in wides:
            assert rec[(2, t)] == v

    def test_nan_inf_real(self):
        sig = {1: (SignalEncoding.SE_REAL, 0)}
        blk = BytesplitBlock(0, sig, compress=False)
        import math
        blk.add_change(1, 0, float("nan"))
        blk.add_change(1, 10, float("inf"))
        blk.add_change(1, 20, float("-inf"))
        got = []
        BytesplitBlock.decode_changes(blk.encode_block(0)[10:], got.append, VcChange)
        assert math.isnan(got[0].value)
        assert got[1].value == float("inf") and got[2].value == float("-inf")

    def test_size_benefit_on_real(self):
        """bytesplit+zlib beats raw-concat+zlib on slowly-varying REAL data."""
        vals = [1000.0 + 0.001 * i for i in range(1000)]
        raw = b"".join(struct.pack("<d", v) for v in vals)
        plain = len(zlib.compress(raw))
        split = len(zlib.compress(byte_split(raw, 8)))
        assert split < plain, f"bytesplit ({split}) !< raw ({plain})"


# ---------------------------------------------------------------------------
# End-to-end via writer / reader
# ---------------------------------------------------------------------------

def _write_real_trace(buf, compress=True, n=50):
    with TrlWriter(buf, compress=compress) as w:
        rt = w.add_signal_type(SignalEncoding.SE_REAL, 0)
        with w.begin_hierarchy() as h:
            h.begin_scope(ScopeType.ST_MODULE, "top")
            v = h.add_var("analog", rt)
            h.end_scope()
        with w.begin_vc_block(0, codec=CORE_BYTESPLIT) as vc:
            for i in range(n):
                vc.add_change(v, i * 10, 1000.0 + i * 0.25)
    return v


class TestEndToEnd:
    @pytest.mark.parametrize("compress", [False, True])
    def test_roundtrip(self, compress):
        buf = io.BytesIO()
        _write_real_trace(buf, compress=compress)
        buf.seek(0)
        with TrlReader(buf) as r:
            changes = [(c.time, c.value) for blk in r.iter_vc_blocks() for c in blk]
        assert changes == [(i * 10, 1000.0 + i * 0.25) for i in range(50)]

    def test_block_is_flagged_and_reported(self):
        buf = io.BytesIO()
        _write_real_trace(buf)
        buf.seek(0)
        with TrlReader(buf) as r:
            report = r.vc_block_report()
            assert len(report) == 1
            assert report[0]["codec"] == CORE_BYTESPLIT
            assert report[0]["available"] is True
            assert report[0]["start"] == 0 and report[0]["end"] == 490
            assert r.missing_codecs() == []

    def test_unknown_codec_selection_rejected(self):
        buf = io.BytesIO()
        with TrlWriter(buf) as w:
            with pytest.raises(ValueError):
                w.begin_vc_block(0, codec="com.acme.not-registered")


# ---------------------------------------------------------------------------
# Skip-unknown: a reader lacking the codec enumerates + skips, never errors
# ---------------------------------------------------------------------------

class TestSkipUnknown:
    def test_reader_without_codec_skips_and_reports(self):
        buf = io.BytesIO()
        _write_real_trace(buf)

        saved = _VC_CODECS.pop(CORE_BYTESPLIT)
        try:
            buf.seek(0)
            with TrlReader(buf) as r:
                # The block is enumerated with codec id + time range...
                report = r.vc_block_report()
                assert report[0]["codec"] == CORE_BYTESPLIT
                assert report[0]["available"] is False
                assert report[0]["start"] == 0 and report[0]["end"] == 490
                assert r.missing_codecs() == [CORE_BYTESPLIT]
                # ...and iterating skips it rather than raising.
                blocks = list(r.iter_vc_blocks())
                assert all(len(b) == 0 for b in blocks)
        finally:
            _VC_CODECS[CORE_BYTESPLIT] = saved

    def test_other_blocks_still_readable_when_one_codec_missing(self):
        """A default-codec block alongside a bytesplit block stays readable even
        when bytesplit is unavailable (fail-open across blocks, §4.2)."""
        buf = io.BytesIO()
        with TrlWriter(buf, compress=False) as w:
            bit = w.add_signal_type(SignalEncoding.SE_2STATE, 1)
            rt = w.add_signal_type(SignalEncoding.SE_REAL, 0)
            with w.begin_hierarchy() as h:
                h.begin_scope(ScopeType.ST_MODULE, "top")
                clk = h.add_var("clk", bit)
                ana = h.add_var("ana", rt)
                h.end_scope()
            with w.begin_vc_block(0) as vc:          # default core codec
                for i in range(8):
                    vc.add_change(clk, i * 10, i % 2)
            with w.begin_vc_block(0, codec=CORE_BYTESPLIT) as vc:
                for i in range(8):
                    vc.add_change(ana, i * 10, float(i))

        saved = _VC_CODECS.pop(CORE_BYTESPLIT)
        try:
            buf.seek(0)
            with TrlReader(buf) as r:
                blocks = list(r.iter_vc_blocks())
                # default block present and decoded; bytesplit block skipped
                clk_changes = [(c.time, c.value) for b in blocks for c in b
                               if c.var_id == clk]
                assert clk_changes == [(i * 10, i % 2) for i in range(8)]
                assert r.missing_codecs() == [CORE_BYTESPLIT]
        finally:
            _VC_CODECS[CORE_BYTESPLIT] = saved
