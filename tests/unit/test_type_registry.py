"""Unit tests for TypeRegistry."""

import pytest
from trlog._type_registry import TypeRegistry
from trlog._types import (
    SignalEncoding, Radix, FieldType,
    FieldDef, EnumValue,
)
from trlog._exceptions import ZstFormatError


class TestSignalType:
    def test_add_and_encode_decode(self):
        reg = TypeRegistry()
        sid = reg.add_signal_type(SignalEncoding.SE_2STATE, bit_width=1)
        assert sid == 1

        block = reg.encode_block()
        reg2 = TypeRegistry()
        reg2.read_block(block[10:])

        e = reg2.get_signal_type(1)
        assert e is not None
        assert e.encoding == SignalEncoding.SE_2STATE
        assert e.bit_width == 1

    def test_all_encodings_round_trip(self):
        reg = TypeRegistry()
        for enc in SignalEncoding:
            reg.add_signal_type(enc, bit_width=32)
        block = reg.encode_block()
        reg2 = TypeRegistry()
        reg2.read_block(block[10:])
        for i, enc in enumerate(SignalEncoding, start=1):
            assert reg2.get_signal_type(i).encoding == enc


class TestTxnSchema:
    def test_add_and_encode_decode(self):
        reg = TypeRegistry()
        fields = [
            FieldDef(name_str_id=1, field_type=FieldType.FT_U32),
            FieldDef(name_str_id=2, field_type=FieldType.FT_STRING),
        ]
        tid = reg.add_txn_schema(name_str_id=3, fields=fields)
        assert tid == 1

        block = reg.encode_block()
        reg2 = TypeRegistry()
        reg2.read_block(block[10:])

        schema = reg2.get_txn_schema(1)
        assert schema.name_str_id == 3
        assert len(schema.fields) == 2
        assert schema.fields[0].field_type == FieldType.FT_U32
        assert schema.fields[1].field_type == FieldType.FT_STRING

    def test_duplicate_txn_type_id_raises(self):
        """Reading a block with a dup txn_type_id should raise ZstFormatError."""
        reg = TypeRegistry()
        reg.add_txn_schema(name_str_id=1)
        block = reg.encode_block()

        # Reading the same block twice into the same registry should cause a dup
        reg2 = TypeRegistry()
        reg2.read_block(block[10:])
        with pytest.raises(ZstFormatError):
            reg2.read_block(block[10:])


class TestEnumType:
    def test_add_and_encode_decode(self):
        reg = TypeRegistry()
        values = [
            EnumValue(integer_value=0,  label_str_id=10),
            EnumValue(integer_value=-1, label_str_id=11),
            EnumValue(integer_value=42, label_str_id=12),
        ]
        eid = reg.add_enum_type(name_str_id=5, values=values)
        assert eid == 1

        block = reg.encode_block()
        reg2 = TypeRegistry()
        reg2.read_block(block[10:])

        et = reg2.get_enum_type(1)
        assert et.name_str_id == 5
        assert len(et.values) == 3
        assert et.values[1].integer_value == -1
        assert et.values[2].label_str_id == 12


class TestStreamDecl:
    def test_add_and_encode_decode(self):
        reg = TypeRegistry()
        sid = reg.add_stream_decl(name_str_id=7, kind_str_id=8, default_txn_type=1)
        assert sid == 1

        block = reg.encode_block()
        reg2 = TypeRegistry()
        reg2.read_block(block[10:])

        sd = reg2.get_stream_decl(1)
        assert sd.name_str_id == 7
        assert sd.kind_str_id == 8
        assert sd.default_txn_type == 1


class TestMultiBlock:
    def test_multiple_blocks_accumulate_without_collision(self):
        reg1 = TypeRegistry()
        reg1.add_signal_type(SignalEncoding.SE_2STATE, bit_width=1)
        block1 = reg1.encode_block()

        reg2 = TypeRegistry()
        # Manually set next IDs to continue from reg1
        reg2._next_sig_id = 2
        reg2.add_signal_type(SignalEncoding.SE_4STATE, bit_width=4)
        block2 = reg2.encode_block()

        combined = TypeRegistry()
        combined.read_block(block1[10:])
        combined.read_block(block2[10:])

        assert combined.get_signal_type(1).encoding == SignalEncoding.SE_2STATE
        assert combined.get_signal_type(2).encoding == SignalEncoding.SE_4STATE
