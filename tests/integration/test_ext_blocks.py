"""Integration tests for BLK_EXT extension blocks."""

import pytest
from trlog import TrlWriter, TrlReader, ExtBlock


class TestExtBlocks:
    def test_roundtrip_covg(self, tmp_path):
        """Write a COVG ext block and read it back."""
        p = tmp_path / "t.trl"
        payload = b"\x00\x01\x02\x03coverage data"
        with TrlWriter(str(p), compress=False) as w:
            w.write_ext(b"COVG", ext_version=1, payload=payload)

        with TrlReader(str(p)) as r:
            blocks = list(r.iter_ext_blocks())
        assert len(blocks) == 1
        assert blocks[0].ext_type == b"COVG"
        assert blocks[0].ext_version == 1
        assert blocks[0].payload == payload

    def test_unknown_ext_silently_skipped(self, tmp_path):
        """iter_ext_blocks(b'COVG') ignores unknown ext_type blocks."""
        p = tmp_path / "t.trl"
        with TrlWriter(str(p), compress=False) as w:
            w.write_ext(b"UNKX", ext_version=0, payload=b"unknown")
            w.write_ext(b"COVG", ext_version=2, payload=b"known")

        with TrlReader(str(p)) as r:
            covg_blocks = list(r.iter_ext_blocks(b"COVG"))
        assert len(covg_blocks) == 1
        assert covg_blocks[0].ext_type == b"COVG"

    def test_iter_all_ext_blocks(self, tmp_path):
        """iter_ext_blocks() without filter yields all ext blocks."""
        p = tmp_path / "t.trl"
        with TrlWriter(str(p), compress=False) as w:
            w.write_ext(b"PERF", ext_version=1, payload=b"perf")
            w.write_ext(b"ANNO", ext_version=0, payload=b"annotation")
            w.write_ext(b"META", ext_version=3, payload=b"meta data")

        with TrlReader(str(p)) as r:
            all_blocks = list(r.iter_ext_blocks())
        assert len(all_blocks) == 3
        types = [b.ext_type for b in all_blocks]
        assert b"PERF" in types
        assert b"ANNO" in types
        assert b"META" in types

    def test_ext_type_4bytes_roundtrip(self, tmp_path):
        """ext_type as raw 4-byte value round-trips correctly."""
        p = tmp_path / "t.trl"
        ext_type = bytes([0x01, 0x02, 0x03, 0x04])
        with TrlWriter(str(p), compress=False) as w:
            w.write_ext(ext_type, ext_version=255, payload=b"binary ext type")

        with TrlReader(str(p)) as r:
            blocks = list(r.iter_ext_blocks())
        assert len(blocks) == 1
        assert blocks[0].ext_type == ext_type
        assert blocks[0].ext_version == 255
        assert blocks[0].payload == b"binary ext type"

    def test_empty_payload(self, tmp_path):
        """Ext block with empty payload round-trips."""
        p = tmp_path / "t.trl"
        with TrlWriter(str(p), compress=False) as w:
            w.write_ext(b"NOOP", ext_version=0, payload=b"")

        with TrlReader(str(p)) as r:
            blocks = list(r.iter_ext_blocks())
        assert len(blocks) == 1
        assert blocks[0].payload == b""

    def test_ext_block_encode_decode_standalone(self):
        """ExtBlock.encode_block / read_block roundtrip without a file."""
        blk = ExtBlock(ext_type=b"TEST", ext_version=42, payload=b"hello world")
        raw = blk.encode_block()
        # Strip the 10-byte block header before calling read_block
        recovered = ExtBlock.read_block(raw[10:])
        assert recovered == blk

    def test_mixed_file_with_vc_and_ext(self, tmp_path):
        """VC data and ext blocks coexist in same file."""
        from trlog._types import SignalEncoding, ScopeType
        p = tmp_path / "t.trl"
        with TrlWriter(str(p), compress=False) as w:
            sig_t = w.add_signal_type(SignalEncoding.SE_2STATE, 1)
            with w.begin_hierarchy() as h:
                h.begin_scope(ScopeType.ST_MODULE, "top")
                v = h.add_var("clk", sig_t)
                h.end_scope()
            with w.begin_vc_block(0) as vc:
                for i in range(10):
                    vc.add_change(v, i * 10, i % 2)
            w.write_ext(b"COVG", ext_version=1, payload=b"coverage")

        with TrlReader(str(p)) as r:
            ext_blocks = list(r.iter_ext_blocks())
            vc_changes = r.read_signal(v)
        assert len(ext_blocks) == 1
        assert ext_blocks[0].payload == b"coverage"
        assert len(vc_changes) == 10
