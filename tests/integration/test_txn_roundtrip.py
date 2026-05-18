"""Integration tests for TxnDataBlock encode/decode roundtrip."""

import pytest
from trlog._txn_data import TxnDataBlock
from trlog._types import (
    FieldType, TxnRecordTag, LinkType,
    TxnFull, TxnBegin, TxnAttrRecord, TxnEnd, TxnLink, TxnAttr, TxnMeta,
    WellKnownAttr,
)


def make_attr(field_idx, value, ft=FieldType.FT_U64):
    """Helper: create a TxnAttr with a field_type hint."""
    a = TxnAttr(field_idx=field_idx, value=value)
    a._field_type = ft
    return a


def roundtrip(blk: TxnDataBlock, schemas=None):
    """Encode then decode a TxnDataBlock."""
    encoded = blk.encode_block()
    flags = encoded[1]
    blk2 = TxnDataBlock(compress=False)
    return blk2.read_block(encoded[10:], flags=flags, schemas=schemas)


class TestTrFull:
    def test_minimal_full(self):
        blk = TxnDataBlock(start_time=0, compress=False)
        blk.write_full(
            stream_inst_id=1, txn_type_id=2, txn_id=100,
            start=10, end=20, parent=0, attrs=[],
        )
        records = roundtrip(blk)
        assert len(records) == 1
        r = records[0]
        assert isinstance(r, TxnFull)
        assert r.stream_inst_id == 1
        assert r.txn_type_id == 2
        assert r.txn_id == 100
        assert r.start_time == 10
        assert r.end_time == 20
        assert r.parent_txn_id == 0

    def test_full_with_attrs(self):
        schemas = {5: [FieldType.FT_U64, FieldType.FT_STRING, FieldType.FT_F64]}
        attrs = [
            TxnAttr(field_idx=0, value=42),
            TxnAttr(field_idx=1, value="hello"),
            TxnAttr(field_idx=2, value=3.14),
        ]
        blk = TxnDataBlock(start_time=0, compress=False)
        blk.set_schema(5, schemas[5])
        blk.write_full(
            stream_inst_id=3, txn_type_id=5, txn_id=200,
            start=100, end=200, parent=50, attrs=attrs,
        )
        records = roundtrip(blk, schemas=schemas)
        assert len(records) == 1
        r = records[0]
        assert isinstance(r, TxnFull)
        assert len(r.attrs) == 3
        assert r.attrs[0].value == 42
        assert r.attrs[1].value == "hello"
        assert abs(r.attrs[2].value - 3.14) < 1e-10

    def test_many_full_records(self):
        blk = TxnDataBlock(start_time=0, compress=False)
        for i in range(100):
            blk.write_full(
                stream_inst_id=1, txn_type_id=1, txn_id=i,
                start=i * 10, end=i * 10 + 5, parent=0, attrs=[],
            )
        records = roundtrip(blk)
        assert len(records) == 100
        assert all(isinstance(r, TxnFull) for r in records)
        assert [r.txn_id for r in records] == list(range(100))


class TestTrBeginEnd:
    def test_begin_end_roundtrip(self):
        blk = TxnDataBlock(start_time=0, compress=False)
        blk.write_begin(stream_inst_id=1, txn_type_id=3, txn_id=55, start=100, parent=0)
        blk.write_end(txn_id=55, end_time=200)
        records = roundtrip(blk)
        assert len(records) == 2
        b = records[0]
        e = records[1]
        assert isinstance(b, TxnBegin)
        assert b.txn_id == 55
        assert b.start_time == 100
        assert isinstance(e, TxnEnd)
        assert e.txn_id == 55
        assert e.end_time == 200

    def test_parent_txn(self):
        blk = TxnDataBlock(start_time=0, compress=False)
        blk.write_begin(stream_inst_id=1, txn_type_id=1, txn_id=10, start=0, parent=5)
        records = roundtrip(blk)
        assert records[0].parent_txn_id == 5


class TestTrAttr:
    def test_attr_u64(self):
        attr = make_attr(0, 12345, FieldType.FT_U64)
        blk = TxnDataBlock(start_time=0, compress=False)
        blk.write_attr(txn_id=42, attrs=[attr])
        records = roundtrip(blk)
        assert len(records) == 1
        r = records[0]
        assert isinstance(r, TxnAttrRecord)
        assert r.txn_id == 42
        assert r.attrs[0].value == 12345

    def test_attr_string(self):
        attr = make_attr(0, "hello world", FieldType.FT_STRING)
        blk = TxnDataBlock(start_time=0, compress=False)
        blk.write_attr(txn_id=7, attrs=[attr])
        records = roundtrip(blk)
        r = records[0]
        assert r.attrs[0].value == "hello world"

    def test_attr_f64(self):
        attr = make_attr(0, 2.718, FieldType.FT_F64)
        blk = TxnDataBlock(start_time=0, compress=False)
        blk.write_attr(txn_id=1, attrs=[attr])
        records = roundtrip(blk)
        assert abs(records[0].attrs[0].value - 2.718) < 1e-10


class TestTrLink:
    def test_link_roundtrip(self):
        blk = TxnDataBlock(start_time=0, compress=False)
        blk.write_link(link_type=LinkType.LT_PARENT_CHILD, src=10, tgt=20, label_id=0)
        records = roundtrip(blk)
        assert len(records) == 1
        r = records[0]
        assert isinstance(r, TxnLink)
        assert r.link_type == LinkType.LT_PARENT_CHILD
        assert r.source_txn_id == 10
        assert r.target_txn_id == 20

    def test_link_with_label(self):
        blk = TxnDataBlock(start_time=0, compress=False)
        blk.write_link(link_type=LinkType.LT_RELATED, src=1, tgt=2, label_id=99)
        records = roundtrip(blk)
        assert records[0].label_str_id == 99


class TestTrMeta:
    def test_meta_roundtrip(self):
        blk = TxnDataBlock(start_time=0, compress=False)
        blk.write_full(stream_inst_id=1, txn_type_id=1, txn_id=10,
                       start=0, end=100, parent=0, attrs=[])
        blk.write_meta(txn_id=10, key_str_id=50, value_str_id=60)

        records = roundtrip(blk)
        assert len(records) == 2
        assert isinstance(records[0], TxnFull)
        m = records[1]
        assert isinstance(m, TxnMeta)
        assert m.txn_id == 10
        assert m.key_str_id == 50
        assert m.value_str_id == 60

    def test_multiple_meta_same_txn(self):
        blk = TxnDataBlock(start_time=0, compress=False)
        blk.write_full(stream_inst_id=1, txn_type_id=1, txn_id=7,
                       start=0, end=50, parent=0, attrs=[])
        blk.write_meta(txn_id=7, key_str_id=1, value_str_id=10)
        blk.write_meta(txn_id=7, key_str_id=2, value_str_id=20)
        blk.write_meta(txn_id=7, key_str_id=3, value_str_id=30)

        records = roundtrip(blk)
        metas = [r for r in records if isinstance(r, TxnMeta)]
        assert len(metas) == 3
        assert [m.key_str_id for m in metas] == [1, 2, 3]

    def test_meta_on_open_transaction(self):
        """TR_META can be emitted between TR_BEGIN and TR_END."""
        blk = TxnDataBlock(start_time=0, compress=False)
        blk.write_begin(stream_inst_id=1, txn_type_id=1, txn_id=5, start=0, parent=0)
        blk.write_meta(txn_id=5, key_str_id=99, value_str_id=100)
        blk.write_end(txn_id=5, end_time=200)

        records = roundtrip(blk)
        assert len(records) == 3
        assert isinstance(records[0], TxnBegin)
        assert isinstance(records[1], TxnMeta)
        assert records[1].txn_id == 5
        assert isinstance(records[2], TxnEnd)

    def test_meta_after_end(self):
        """TR_META after TR_END is valid (whole-file readers collect all metadata)."""
        blk = TxnDataBlock(start_time=0, compress=False)
        blk.write_begin(stream_inst_id=1, txn_type_id=1, txn_id=3, start=0, parent=0)
        blk.write_end(txn_id=3, end_time=10)
        blk.write_meta(txn_id=3, key_str_id=5, value_str_id=6)

        records = roundtrip(blk)
        assert len(records) == 3
        assert isinstance(records[2], TxnMeta)
        assert records[2].txn_id == 3

    def test_meta_large_txn_id(self):
        txn_id = (1 << 60) + 999
        blk = TxnDataBlock(start_time=0, compress=False)
        blk.write_full(stream_inst_id=1, txn_type_id=1, txn_id=txn_id,
                       start=0, end=1, parent=0, attrs=[])
        blk.write_meta(txn_id=txn_id, key_str_id=1, value_str_id=2)

        records = roundtrip(blk)
        m = records[1]
        assert isinstance(m, TxnMeta)
        assert m.txn_id == txn_id


class TestMixedRecords:
    def test_mixed_types_ordering(self):
        """Verify record type ordering is preserved."""
        blk = TxnDataBlock(start_time=0, compress=False)
        blk.write_begin(stream_inst_id=1, txn_type_id=1, txn_id=1, start=0, parent=0)
        blk.write_attr(txn_id=1, attrs=[make_attr(0, 100, FieldType.FT_U64)])
        blk.write_end(txn_id=1, end_time=50)
        blk.write_full(
            stream_inst_id=1, txn_type_id=1, txn_id=2, start=60, end=80, parent=0, attrs=[],
        )
        blk.write_link(link_type=LinkType.LT_PARENT_CHILD, src=2, tgt=1, label_id=0)
        blk.write_meta(txn_id=2, key_str_id=77, value_str_id=88)
        records = roundtrip(blk)
        assert len(records) == 6
        assert isinstance(records[0], TxnBegin)
        assert isinstance(records[1], TxnAttrRecord)
        assert isinstance(records[2], TxnEnd)
        assert isinstance(records[3], TxnFull)
        assert isinstance(records[4], TxnLink)
        assert isinstance(records[5], TxnMeta)

    def test_end_time_tracking(self):
        blk = TxnDataBlock(start_time=0, compress=False)
        blk.write_full(
            stream_inst_id=1, txn_type_id=1, txn_id=1, start=0, end=1000, parent=0, attrs=[],
        )
        assert blk.end_time == 1000

    def test_large_txn_ids(self):
        txn_id = (1 << 62) + 7
        blk = TxnDataBlock(start_time=0, compress=False)
        blk.write_begin(stream_inst_id=1, txn_type_id=1, txn_id=txn_id, start=0, parent=0)
        blk.write_end(txn_id=txn_id, end_time=10)
        records = roundtrip(blk)
        assert records[0].txn_id == txn_id
        assert records[1].txn_id == txn_id
