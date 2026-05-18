"""Integration tests for BLK_FILE_HDR — magic, version, header fields."""

import io
import struct
import pytest

from trlog import TrlWriter, TrlReader, ZstMagicError, ZstVersionError, ZstNoIndexError
from trlog._writer import TRL_MAGIC, TRL_VERSION_MAJOR, TRL_VERSION_MINOR, _INDEX_OFFSET_FIELD_POS


class TestMagic:
    def test_magic_bytes(self, tmp_path):
        p = tmp_path / "t.trl"
        with TrlWriter(str(p)):
            pass
        raw = p.read_bytes()
        # BLK_FILE_HDR block type = 0x00, block header = 10 bytes, then magic
        assert raw[0] == 0x00   # BLK_FILE_HDR
        assert raw[10:18] == TRL_MAGIC

    def test_version(self, tmp_path):
        p = tmp_path / "t.trl"
        with TrlWriter(str(p)):
            pass
        raw = p.read_bytes()
        assert raw[18] == TRL_VERSION_MAJOR
        assert raw[19] == TRL_VERSION_MINOR

    def test_bad_magic_raises(self, tmp_path):
        p = tmp_path / "t.trl"
        # Write a file with a valid block header but wrong magic bytes
        payload = b'WRONGMAG' + b'\x00' * 41   # 49 bytes payload
        block_len = 10 + len(payload)
        header = struct.pack('<BBQ', 0x00, 0, block_len)
        p.write_bytes(header + payload)
        with pytest.raises(ZstMagicError):
            with TrlReader(str(p)):
                pass

    def test_unsupported_version_raises(self, tmp_path):
        p = tmp_path / "t.trl"
        with TrlWriter(str(p)):
            pass
        raw = bytearray(p.read_bytes())
        raw[18] = 99   # unsupported version_major
        p.write_bytes(bytes(raw))
        with pytest.raises(ZstVersionError):
            with TrlReader(str(p)):
                pass


class TestTimescale:
    @pytest.mark.parametrize("ts_exp", [-9, -12, 0, -15])
    def test_timescale_roundtrip(self, tmp_path, ts_exp):
        p = tmp_path / "t.trl"
        with TrlWriter(str(p), timescale_exp=ts_exp):
            pass
        with TrlReader(str(p)) as r:
            assert r.timescale_exp == ts_exp

    def test_timezero_roundtrip(self, tmp_path):
        p = tmp_path / "t.trl"
        with TrlWriter(str(p), timezero=-1_000_000_000):
            pass
        with TrlReader(str(p)) as r:
            assert r.timezero == -1_000_000_000


class TestIndexOffset:
    def test_index_offset_zero_before_close(self, tmp_path):
        p = tmp_path / "t.trl"
        w = TrlWriter(str(p))
        # Before close, flush partial
        w._file.flush()
        raw = p.read_bytes()
        idx_off, = struct.unpack_from('<Q', raw, _INDEX_OFFSET_FIELD_POS)
        assert idx_off == 0
        w.close()

    def test_index_offset_patched_after_close(self, tmp_path):
        p = tmp_path / "t.trl"
        with TrlWriter(str(p)):
            pass
        raw = p.read_bytes()
        idx_off, = struct.unpack_from('<Q', raw, _INDEX_OFFSET_FIELD_POS)
        assert idx_off > 0
        assert idx_off < len(raw)

    def test_index_offset_points_to_index_block(self, tmp_path):
        p = tmp_path / "t.trl"
        with TrlWriter(str(p)):
            pass
        raw = p.read_bytes()
        idx_off, = struct.unpack_from('<Q', raw, _INDEX_OFFSET_FIELD_POS)
        from trlog._types import BlockType
        assert raw[idx_off] == int(BlockType.BLK_INDEX)


class TestFileRoundtrip:
    def test_empty_file_readable(self, tmp_path):
        p = tmp_path / "t.trl"
        with TrlWriter(str(p)):
            pass
        with TrlReader(str(p)) as r:
            assert r.version_major == TRL_VERSION_MAJOR
            assert list(r.iter_vc_blocks()) == []

    def test_context_manager_closes(self, tmp_path):
        p = tmp_path / "t.trl"
        with TrlWriter(str(p)) as w:
            pass
        assert w._closed

    def test_stringio_file(self):
        """Test writing to a BytesIO (non-seekable patch won't work but won't crash)."""
        buf = io.BytesIO()
        with TrlWriter(buf) as w:
            pass
        buf.seek(0)
        with TrlReader(buf) as r:
            assert r.version_major == TRL_VERSION_MAJOR
