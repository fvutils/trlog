"""Unit tests for per-stream transaction index entries."""

import pytest
from trlog._index import IndexBlock


class TestPerStreamIndex:
    """Verify that IndexBlock correctly round-trips stream_inst_id."""

    def test_add_txn_entry_with_stream_id(self):
        """Entries with explicit stream_inst_id survive encode/decode."""
        idx = IndexBlock()
        idx.add_txn_entry(100, 200, 0x1000, stream_inst_id=1)
        idx.add_txn_entry(200, 300, 0x2000, stream_inst_id=2)
        idx.add_txn_entry(300, 400, 0x3000, stream_inst_id=3)

        encoded = idx.encode_block()
        idx2 = IndexBlock()
        idx2.read_block(encoded[10:])

        assert len(idx2._txn_entries) == 3
        assert idx2._txn_entries[0] == (100, 200, 0x1000, 1)
        assert idx2._txn_entries[1] == (200, 300, 0x2000, 2)
        assert idx2._txn_entries[2] == (300, 400, 0x3000, 3)

    def test_add_txn_entry_default_stream_id(self):
        """Entries without stream_inst_id decode as 0xFFFFFFFF."""
        idx = IndexBlock()
        idx.add_txn_entry(10, 20, 0x100)
        idx.add_txn_entry(30, 40, 0x200)

        encoded = idx.encode_block()
        idx2 = IndexBlock()
        idx2.read_block(encoded[10:])

        assert len(idx2._txn_entries) == 2
        for s, e, off, sid in idx2._txn_entries:
            assert sid == 0xFFFFFFFF

    def test_find_txn_blocks_for_stream(self):
        """find_txn_blocks_for_stream filters by stream_inst_id."""
        idx = IndexBlock()
        idx.add_txn_entry(0, 100, 0x1000, stream_inst_id=1)
        idx.add_txn_entry(0, 100, 0x2000, stream_inst_id=2)
        idx.add_txn_entry(100, 200, 0x3000, stream_inst_id=1)
        idx.add_txn_entry(100, 200, 0x4000, stream_inst_id=3)

        offsets = idx.find_txn_blocks_for_stream(1)
        assert offsets == [0x1000, 0x3000]

        offsets = idx.find_txn_blocks_for_stream(2)
        assert offsets == [0x2000]

        offsets = idx.find_txn_blocks_for_stream(99)
        assert offsets == []

    def test_find_txn_blocks_for_stream_time_range(self):
        """find_txn_blocks_for_stream respects start/end time bounds."""
        idx = IndexBlock()
        idx.add_txn_entry(0, 99, 0x1000, stream_inst_id=1)
        idx.add_txn_entry(100, 199, 0x2000, stream_inst_id=1)
        idx.add_txn_entry(200, 299, 0x3000, stream_inst_id=1)
        idx.add_txn_entry(0, 299, 0x4000, stream_inst_id=2)

        # Only stream 1 blocks overlapping [50, 150]
        offsets = idx.find_txn_blocks_for_stream(1, start=50, end=150)
        assert offsets == [0x1000, 0x2000]

    def test_find_txn_blocks_unfiltered_still_works(self):
        """find_txn_blocks (no stream filter) returns all entries."""
        idx = IndexBlock()
        idx.add_txn_entry(0, 100, 0x1000, stream_inst_id=1)
        idx.add_txn_entry(0, 100, 0x2000, stream_inst_id=2)
        idx.add_txn_entry(100, 200, 0x3000, stream_inst_id=3)

        offsets = idx.find_txn_blocks(0, 200)
        assert set(offsets) == {0x1000, 0x2000, 0x3000}

    def test_backward_compat_no_stream_section(self):
        """Index with all-default stream_inst_id produces smaller payload."""
        idx = IndexBlock()
        idx.add_txn_entry(0, 100, 0x1000)  # default 0xFFFFFFFF
        idx.add_txn_entry(100, 200, 0x2000)

        encoded = idx.encode_block()
        payload = encoded[10:]

        # Compare: an index with per-stream entries should be strictly larger
        idx_with = IndexBlock()
        idx_with.add_txn_entry(0, 100, 0x1000, stream_inst_id=1)
        idx_with.add_txn_entry(100, 200, 0x2000, stream_inst_id=2)
        encoded_with = idx_with.encode_block()
        assert len(encoded) < len(encoded_with)

        # Still readable
        idx2 = IndexBlock()
        idx2.read_block(payload)
        assert len(idx2._txn_entries) == 2
        # find_txn_blocks_for_stream returns all when all are unknown
        offsets = idx2.find_txn_blocks_for_stream(1)
        assert offsets == [0x1000, 0x2000]

    def test_old_reader_ignores_stream_section(self):
        """Basic txn entries are always written; a reader that stops at 0x01 still works."""
        idx = IndexBlock()
        idx.add_txn_entry(0, 100, 0x1000, stream_inst_id=1)
        idx.add_txn_entry(100, 200, 0x2000, stream_inst_id=2)

        # An old writer (no stream ids) produces a legacy payload
        idx_legacy = IndexBlock()
        idx_legacy.add_txn_entry(0, 100, 0x1000)
        idx_legacy.add_txn_entry(100, 200, 0x2000)
        encoded_legacy = idx_legacy.encode_block()
        payload_legacy = encoded_legacy[10:]

        idx2 = IndexBlock()
        idx2.read_block(payload_legacy)
        assert len(idx2._txn_entries) == 2
        for _, _, _, sid in idx2._txn_entries:
            assert sid == 0xFFFFFFFF

        # New-format index (with stream IDs) decodes fully
        encoded_new = idx.encode_block()
        payload_new = encoded_new[10:]
        idx3 = IndexBlock()
        idx3.read_block(payload_new)
        assert len(idx3._txn_entries) == 2
        assert idx3._txn_entries[0][3] == 1
        assert idx3._txn_entries[1][3] == 2
