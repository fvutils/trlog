"""Unit tests for varint encode/decode (_codec.py)."""

import pytest
from trlog._codec import (
    encode_uvarint, decode_uvarint,
    encode_svarint, decode_svarint,
)


# ---- Unsigned varint --------------------------------------------------------

class TestUvarint:
    @pytest.mark.parametrize("n,expected_bytes", [
        (0,       b'\x00'),
        (1,       b'\x01'),
        (127,     b'\x7f'),
        (128,     b'\x80\x01'),
        (16383,   b'\xff\x7f'),
        (16384,   b'\x80\x80\x01'),
        (2**32,   b'\x80\x80\x80\x80\x10'),
        (2**63,   b'\x80\x80\x80\x80\x80\x80\x80\x80\x80\x01'),
    ])
    def test_encode(self, n, expected_bytes):
        assert encode_uvarint(n) == expected_bytes

    @pytest.mark.parametrize("n", [
        0, 1, 127, 128, 16383, 16384, 2**32, 2**63,
    ])
    def test_roundtrip(self, n):
        buf = encode_uvarint(n)
        val, offset = decode_uvarint(buf)
        assert val == n
        assert offset == len(buf)

    def test_negative_raises(self):
        with pytest.raises(ValueError):
            encode_uvarint(-1)

    def test_truncated_raises(self):
        # 0x80 signals more bytes, but there are none
        with pytest.raises(ValueError, match="Truncated"):
            decode_uvarint(b'\x80')

    def test_multi_value_stream(self):
        """Decode successive values at incrementing offsets."""
        values = [0, 127, 128, 300, 16384]
        buf = b''.join(encode_uvarint(v) for v in values)
        offset = 0
        decoded = []
        for _ in range(len(values)):
            v, offset = decode_uvarint(buf, offset)
            decoded.append(v)
        assert decoded == values
        assert offset == len(buf)

    def test_decode_with_trailing_bytes(self):
        buf = encode_uvarint(42) + b'\xff\xff'
        val, offset = decode_uvarint(buf)
        assert val == 42
        # offset points right after the varint
        assert buf[offset:] == b'\xff\xff'


# ---- Signed varint ----------------------------------------------------------

class TestSvarint:
    @pytest.mark.parametrize("n", [
        0, -1, 1, 63, -64, -(2**31), 2**31 - 1,
    ])
    def test_roundtrip(self, n):
        buf = encode_svarint(n)
        val, offset = decode_svarint(buf)
        assert val == n
        assert offset == len(buf)

    def test_zero_is_compact(self):
        assert encode_svarint(0) == b'\x00'

    def test_minus_one_is_compact(self):
        # zigzag(-1) = 1 → 1 byte
        assert encode_svarint(-1) == b'\x01'

    def test_multi_value_stream(self):
        values = [0, -1, 63, -64, 2**31 - 1, -(2**31)]
        buf = b''.join(encode_svarint(v) for v in values)
        offset = 0
        decoded = []
        for _ in range(len(values)):
            v, offset = decode_svarint(buf, offset)
            decoded.append(v)
        assert decoded == values
