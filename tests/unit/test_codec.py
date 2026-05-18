"""Unit tests for field encode/decode (_codec.py)."""

import math
import struct

import pytest
from trlog._codec import encode_field, decode_field
from trlog._types import FieldType


def roundtrip(ft, value):
    buf = encode_field(ft, value)
    result, offset = decode_field(ft, buf)
    assert offset == len(buf)
    return result


class TestFixedWidthFields:
    @pytest.mark.parametrize("ft,value", [
        (FieldType.FT_U8,  0),
        (FieldType.FT_U8,  255),
        (FieldType.FT_U16, 0),
        (FieldType.FT_U16, 65535),
        (FieldType.FT_U32, 0),
        (FieldType.FT_U32, 2**32 - 1),
        (FieldType.FT_U64, 0),
        (FieldType.FT_U64, 2**64 - 1),
        (FieldType.FT_I8,  -128),
        (FieldType.FT_I8,  127),
        (FieldType.FT_I16, -32768),
        (FieldType.FT_I16, 32767),
        (FieldType.FT_I32, -(2**31)),
        (FieldType.FT_I32, 2**31 - 1),
        (FieldType.FT_I64, -(2**63)),
        (FieldType.FT_I64, 2**63 - 1),
    ])
    def test_roundtrip(self, ft, value):
        assert roundtrip(ft, value) == value

    @pytest.mark.parametrize("ft,size", [
        (FieldType.FT_U8,  1),
        (FieldType.FT_U16, 2),
        (FieldType.FT_U32, 4),
        (FieldType.FT_U64, 8),
        (FieldType.FT_I8,  1),
        (FieldType.FT_I16, 2),
        (FieldType.FT_I32, 4),
        (FieldType.FT_I64, 8),
    ])
    def test_wire_size(self, ft, size):
        assert len(encode_field(ft, 0)) == size

    def test_little_endian_u32(self):
        buf = encode_field(FieldType.FT_U32, 0x01020304)
        assert buf == b'\x04\x03\x02\x01'

    def test_little_endian_u64(self):
        buf = encode_field(FieldType.FT_U64, 0x0102030405060708)
        assert buf == b'\x08\x07\x06\x05\x04\x03\x02\x01'


class TestFloat:
    def test_f32_roundtrip(self):
        val = 3.14
        result = roundtrip(FieldType.FT_F32, val)
        assert abs(result - val) < 1e-5

    def test_f64_roundtrip(self):
        for val in [0.0, 1.0, -1.0, 3.141592653589793, float('inf'), float('-inf')]:
            result = roundtrip(FieldType.FT_F64, val)
            if math.isinf(val):
                assert math.isinf(result) and (val > 0) == (result > 0)
            else:
                assert result == val

    def test_f64_nan(self):
        result = roundtrip(FieldType.FT_F64, float('nan'))
        assert math.isnan(result)


class TestBool:
    def test_true(self):
        assert roundtrip(FieldType.FT_BOOL, True) is True

    def test_false(self):
        assert roundtrip(FieldType.FT_BOOL, False) is False

    def test_truthy_value(self):
        assert roundtrip(FieldType.FT_BOOL, 42) is True


class TestString:
    def test_empty(self):
        assert roundtrip(FieldType.FT_STRING, "") == ""

    def test_ascii(self):
        assert roundtrip(FieldType.FT_STRING, "hello") == "hello"

    def test_utf8_multibyte(self):
        s = "héllo wörld 日本語"
        assert roundtrip(FieldType.FT_STRING, s) == s

    def test_large_string(self):
        s = "x" * 4096
        assert roundtrip(FieldType.FT_STRING, s) == s


class TestBitvec:
    @pytest.mark.parametrize("bit_width,value", [
        (1,  0),
        (1,  1),
        (7,  0x7F),
        (8,  0xFF),
        (31, 0x7FFFFFFF),
        (32, 0xFFFFFFFF),
        (64, 2**64 - 1),
        (65, 0),
        (65, (1 << 65) - 1),
    ])
    def test_roundtrip(self, bit_width, value):
        result = roundtrip(FieldType.FT_BITVEC, (bit_width, value))
        assert result == (bit_width, value)


class TestBytes:
    def test_empty(self):
        assert roundtrip(FieldType.FT_BYTES, b"") == b""

    def test_nonempty(self):
        assert roundtrip(FieldType.FT_BYTES, b"\x01\x02\x03") == b"\x01\x02\x03"


class TestTime:
    def test_zero(self):
        assert roundtrip(FieldType.FT_TIME, 0) == 0

    def test_large(self):
        val = 2**63 - 1
        assert roundtrip(FieldType.FT_TIME, val) == val


class TestEnum:
    def test_zero(self):
        assert roundtrip(FieldType.FT_ENUM, 0) == 0

    def test_max(self):
        val = 2**32 - 1
        assert roundtrip(FieldType.FT_ENUM, val) == val
