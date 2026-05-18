"""Unit tests for StringTable."""

import pytest
from trlog._string_table import StringTable


class TestIntern:
    def test_idempotent(self):
        st = StringTable()
        assert st.intern("hello") == st.intern("hello")

    def test_monotonic_starting_at_one(self):
        st = StringTable()
        id1 = st.intern("a")
        id2 = st.intern("b")
        id3 = st.intern("c")
        assert id1 == 1
        assert id2 == 2
        assert id3 == 3

    def test_id_zero_is_null(self):
        st = StringTable()
        assert st.lookup(0) == ""

    def test_lookup_roundtrip(self):
        st = StringTable()
        sid = st.intern("hello world")
        assert st.lookup(sid) == "hello world"

    def test_lookup_unknown_raises(self):
        st = StringTable()
        with pytest.raises(KeyError):
            st.lookup(99)


class TestBlockRoundtrip:
    def test_encode_decode_preserves_table(self):
        st = StringTable()
        st.intern("alpha")
        st.intern("beta")
        st.intern("γδε")   # utf-8

        block = st.encode_block()

        st2 = StringTable()
        # skip the 10-byte common block header
        st2.read_block(block[10:])

        assert st2.lookup(1) == "alpha"
        assert st2.lookup(2) == "beta"
        assert st2.lookup(3) == "γδε"

    def test_multiple_blocks_accumulate(self):
        """Reading two blocks accumulates IDs correctly."""
        st1 = StringTable()
        st1.intern("a")
        st1.intern("b")
        block1 = st1.encode_block()

        st2 = StringTable()
        st2.intern("c")
        block2 = st2.encode_block()

        reader = StringTable()
        reader.read_block(block1[10:])
        reader.read_block(block2[10:])

        assert reader.lookup(1) == "a"
        assert reader.lookup(2) == "b"
        assert reader.lookup(3) == "c"

    def test_empty_table_encodes(self):
        st = StringTable()
        block = st.encode_block()
        st2 = StringTable()
        st2.read_block(block[10:])
        assert len(st2) == 0

    def test_zlib_compression_roundtrip(self):
        st = StringTable()
        for i in range(50):
            st.intern(f"signal_{i:04d}")

        block = st.encode_block(compress=True)
        flags = block[1]
        assert flags & 0x01  # compressed flag set
        assert not (flags & 0x02)  # ZLib, not Zstd

        st2 = StringTable()
        # payload starts at byte 10; flags tell the reader to decompress
        st2.read_block(block[10:], flags=flags)
        for i in range(50):
            assert st2.intern(f"signal_{i:04d}") == i + 1
