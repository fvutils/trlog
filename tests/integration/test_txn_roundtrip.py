"""Integration tests for TxnDataBlock encode/decode roundtrip."""

import pytest
from trlog._txn_data import TxnDataBlock
from trlog._types import (
    FieldType, TxnRecordTag, LinkType,
    TxnFull, TxnBegin, TxnAttrRecord, TxnEnd, TxnLink, TxnAttr, TxnMeta,
    WellKnownAttr,
    FLAG_TXN_DELTA,
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


def roundtrip_legacy(blk: TxnDataBlock, schemas=None):
    """Encode with legacy (no delta) then decode."""
    blk._delta_encode = False
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


class TestDeltaEncoding:
    """Tests specific to FLAG_TXN_DELTA encoding."""

    def test_flag_is_set(self):
        """Verify FLAG_TXN_DELTA (0x04) is present in block flags."""
        blk = TxnDataBlock(start_time=0, compress=False)
        blk.write_full(stream_inst_id=1, txn_type_id=1, txn_id=1,
                       start=10, end=20, parent=0, attrs=[])
        encoded = blk.encode_block()
        assert encoded[1] & FLAG_TXN_DELTA

    def test_legacy_flag_not_set(self):
        """Verify legacy encoding does NOT set FLAG_TXN_DELTA."""
        blk = TxnDataBlock(start_time=0, compress=False)
        blk._delta_encode = False
        blk.write_full(stream_inst_id=1, txn_type_id=1, txn_id=1,
                       start=10, end=20, parent=0, attrs=[])
        encoded = blk.encode_block()
        assert not (encoded[1] & FLAG_TXN_DELTA)

    def test_sequential_full_records(self):
        """Sequential TR_FULL records roundtrip correctly with delta."""
        blk = TxnDataBlock(start_time=0, compress=False)
        for i in range(1000):
            blk.write_full(stream_inst_id=1, txn_type_id=1, txn_id=i,
                           start=i * 10, end=i * 10 + 5, parent=0, attrs=[])
        records = roundtrip(blk)
        assert len(records) == 1000
        for i, r in enumerate(records):
            assert r.txn_id == i
            assert r.start_time == i * 10
            assert r.end_time == i * 10 + 5

    def test_delta_smaller_than_legacy(self):
        """Delta encoding produces smaller payload than legacy."""
        blk_delta = TxnDataBlock(start_time=0, compress=False)
        blk_legacy = TxnDataBlock(start_time=0, compress=False)
        blk_legacy._delta_encode = False
        for i in range(100):
            for b in (blk_delta, blk_legacy):
                b.write_full(stream_inst_id=1, txn_type_id=1, txn_id=i,
                             start=i * 10, end=i * 10 + 5, parent=0, attrs=[])
        delta_size = len(blk_delta.encode_block())
        legacy_size = len(blk_legacy.encode_block())
        assert delta_size < legacy_size

    def test_mixed_records_with_delta(self):
        """Mixed record types roundtrip correctly with delta encoding."""
        blk = TxnDataBlock(start_time=0, compress=False)
        blk.write_begin(stream_inst_id=1, txn_type_id=1, txn_id=1, start=0, parent=0)
        blk.write_attr(txn_id=1, attrs=[make_attr(0, 100, FieldType.FT_U64)])
        blk.write_end(txn_id=1, end_time=50)
        blk.write_full(stream_inst_id=1, txn_type_id=1, txn_id=2,
                       start=60, end=80, parent=0, attrs=[])
        blk.write_link(link_type=LinkType.LT_PARENT_CHILD, src=2, tgt=1, label_id=0)
        blk.write_meta(txn_id=2, key_str_id=77, value_str_id=88)
        records = roundtrip(blk)
        assert len(records) == 6
        assert isinstance(records[0], TxnBegin) and records[0].txn_id == 1
        assert isinstance(records[1], TxnAttrRecord) and records[1].txn_id == 1
        assert isinstance(records[2], TxnEnd) and records[2].end_time == 50
        assert isinstance(records[3], TxnFull) and records[3].txn_id == 2
        assert isinstance(records[4], TxnLink) and records[4].source_txn_id == 2
        assert records[4].target_txn_id == 1
        assert isinstance(records[5], TxnMeta) and records[5].txn_id == 2

    def test_negative_deltas(self):
        """Txn IDs that decrease (backward references) roundtrip correctly."""
        blk = TxnDataBlock(start_time=0, compress=False)
        blk.write_full(stream_inst_id=1, txn_type_id=1, txn_id=100,
                       start=0, end=10, parent=0, attrs=[])
        blk.write_full(stream_inst_id=1, txn_type_id=1, txn_id=50,
                       start=20, end=30, parent=0, attrs=[])
        blk.write_full(stream_inst_id=1, txn_type_id=1, txn_id=200,
                       start=40, end=50, parent=0, attrs=[])
        records = roundtrip(blk)
        assert [r.txn_id for r in records] == [100, 50, 200]

    def test_zero_duration_transaction(self):
        """Zero-duration (start == end) transactions roundtrip."""
        blk = TxnDataBlock(start_time=0, compress=False)
        blk.write_full(stream_inst_id=1, txn_type_id=1, txn_id=1,
                       start=100, end=100, parent=0, attrs=[])
        records = roundtrip(blk)
        assert records[0].start_time == 100
        assert records[0].end_time == 100

    def test_with_attrs_and_delta(self):
        """Attributes survive delta encoding unchanged."""
        schemas = {5: [FieldType.FT_U64, FieldType.FT_STRING]}
        attrs = [
            TxnAttr(field_idx=0, value=42),
            TxnAttr(field_idx=1, value="hello"),
        ]
        blk = TxnDataBlock(start_time=0, compress=False)
        blk.set_schema(5, schemas[5])
        blk.write_full(stream_inst_id=1, txn_type_id=5, txn_id=1,
                       start=0, end=10, parent=0, attrs=attrs)
        records = roundtrip(blk, schemas=schemas)
        assert records[0].attrs[0].value == 42
        assert records[0].attrs[1].value == "hello"

    def test_compressed_delta(self):
        """Delta + zlib compression roundtrips correctly."""
        blk = TxnDataBlock(start_time=0, compress=True)
        for i in range(500):
            blk.write_full(stream_inst_id=1, txn_type_id=1, txn_id=i,
                           start=i * 10, end=i * 10 + 5, parent=0, attrs=[])
        encoded = blk.encode_block()
        blk2 = TxnDataBlock(compress=False)
        records = blk2.read_block(encoded[10:], flags=encoded[1])
        assert len(records) == 500
        assert records[499].txn_id == 499

    def test_legacy_roundtrip_still_works(self):
        """Legacy (no delta) encoding still roundtrips when flag is absent."""
        blk = TxnDataBlock(start_time=0, compress=False)
        blk._delta_encode = False
        blk.write_full(stream_inst_id=1, txn_type_id=1, txn_id=42,
                       start=100, end=200, parent=0, attrs=[])
        records = roundtrip(blk)
        assert records[0].txn_id == 42
        assert records[0].start_time == 100
        assert records[0].end_time == 200


# ---------------------------------------------------------------------------
# Phase 7: Column-oriented layout tests
# ---------------------------------------------------------------------------

def roundtrip_column(blk: TxnDataBlock, schemas=None):
    """Encode (column mode) then decode a TxnDataBlock."""
    encoded = blk.encode_block()
    flags = encoded[1]
    blk2 = TxnDataBlock(compress=False)
    return blk2.read_block(encoded[10:], flags=flags, schemas=schemas), encoded


class TestColumnLayout:

    def test_column_roundtrip_basic(self):
        """100 TR_FULL records roundtrip in column mode."""
        blk = TxnDataBlock(start_time=0, compress=False, column_layout="column")
        for i in range(100):
            blk.write_full(
                stream_inst_id=1, txn_type_id=1, txn_id=i,
                start=i * 10, end=i * 10 + 5, parent=0, attrs=[],
            )
        records, encoded = roundtrip_column(blk)
        assert len(records) == 100
        for i, r in enumerate(records):
            assert isinstance(r, TxnFull)
            assert r.txn_id == i
            assert r.start_time == i * 10
            assert r.end_time == i * 10 + 5

    def test_column_roundtrip_with_attrs(self):
        """100 TR_FULL records with 3 fixed-width attrs roundtrip in column mode."""
        schemas = {5: [FieldType.FT_U32, FieldType.FT_U64, FieldType.FT_U8]}
        blk = TxnDataBlock(start_time=0, compress=False, column_layout="column")
        blk.set_schema(5, schemas[5])
        for i in range(100):
            attrs = [
                TxnAttr(field_idx=0, value=i * 100),
                TxnAttr(field_idx=1, value=i * 1000),
                TxnAttr(field_idx=2, value=i % 256),
            ]
            blk.write_full(
                stream_inst_id=1, txn_type_id=5, txn_id=i,
                start=i * 10, end=i * 10 + 5, parent=0, attrs=attrs,
            )
        records, _ = roundtrip_column(blk, schemas=schemas)
        assert len(records) == 100
        for i, r in enumerate(records):
            assert r.attrs[0].value == i * 100
            assert r.attrs[1].value == i * 1000
            assert r.attrs[2].value == i % 256

    def test_column_smaller_than_row(self):
        """Column+zlib beats row+zlib for regular fixed-width data."""
        schemas = {1: [FieldType.FT_U32, FieldType.FT_U64, FieldType.FT_U8,
                        FieldType.FT_U32, FieldType.FT_U64]}
        blk_row = TxnDataBlock(start_time=0, compress=True, column_layout="row")
        blk_col = TxnDataBlock(start_time=0, compress=True, column_layout="column")
        for b in (blk_row, blk_col):
            b.set_schema(1, schemas[1])
        for i in range(1000):
            attrs = [
                TxnAttr(field_idx=0, value=i),
                TxnAttr(field_idx=1, value=i * 10),
                TxnAttr(field_idx=2, value=i % 256),
                TxnAttr(field_idx=3, value=i * 3),
                TxnAttr(field_idx=4, value=i * 100),
            ]
            for b in (blk_row, blk_col):
                b.write_full(
                    stream_inst_id=1, txn_type_id=1, txn_id=i,
                    start=i * 10, end=i * 10 + 5, parent=0, attrs=attrs,
                )
        row_size = len(blk_row.encode_block())
        col_size = len(blk_col.encode_block())
        assert col_size < row_size, f"Column ({col_size}) >= Row ({row_size})"

    def test_column_mixed_record_types(self):
        """Mixed TR_FULL, TR_BEGIN, TR_ATTR, TR_END, TR_LINK, TR_META roundtrip."""
        blk = TxnDataBlock(start_time=0, compress=False, column_layout="column")
        blk.write_begin(stream_inst_id=1, txn_type_id=1, txn_id=1, start=0, parent=0)
        blk.write_attr(txn_id=1, attrs=[make_attr(0, 100, FieldType.FT_U64)])
        blk.write_end(txn_id=1, end_time=50)
        blk.write_full(stream_inst_id=1, txn_type_id=1, txn_id=2,
                       start=60, end=80, parent=0, attrs=[])
        blk.write_link(link_type=LinkType.LT_PARENT_CHILD, src=2, tgt=1, label_id=0)
        blk.write_meta(txn_id=2, key_str_id=77, value_str_id=88)
        records, _ = roundtrip_column(blk)
        assert len(records) == 6
        assert isinstance(records[0], TxnBegin) and records[0].txn_id == 1
        assert isinstance(records[1], TxnAttrRecord) and records[1].txn_id == 1
        assert records[1].attrs[0].value == 100
        assert isinstance(records[2], TxnEnd) and records[2].end_time == 50
        assert isinstance(records[3], TxnFull) and records[3].txn_id == 2
        assert isinstance(records[4], TxnLink) and records[4].source_txn_id == 2
        assert records[4].target_txn_id == 1
        assert isinstance(records[5], TxnMeta) and records[5].key_str_id == 77

    def test_column_with_delta_encoding(self):
        """FLAG_TXN_COLUMN | FLAG_TXN_DELTA roundtrips correctly."""
        from trlog._types import FLAG_TXN_COLUMN
        blk = TxnDataBlock(start_time=0, compress=False, column_layout="column")
        for i in range(100):
            blk.write_full(
                stream_inst_id=1, txn_type_id=1, txn_id=i,
                start=i * 10, end=i * 10 + 5, parent=0, attrs=[],
            )
        records, encoded = roundtrip_column(blk)
        flags = encoded[1]
        assert flags & FLAG_TXN_DELTA, "FLAG_TXN_DELTA not set"
        assert flags & FLAG_TXN_COLUMN, "FLAG_TXN_COLUMN not set"
        assert len(records) == 100
        for i, r in enumerate(records):
            assert r.txn_id == i
            assert r.start_time == i * 10

    def test_auto_selects_column_for_fixed_width(self):
        """Auto mode selects column for all-fixed-width schemas with >= 32 records."""
        from trlog._types import FLAG_TXN_COLUMN
        schemas = {1: [FieldType.FT_U32, FieldType.FT_U64]}
        blk = TxnDataBlock(start_time=0, compress=False, column_layout="auto")
        blk.set_schema(1, schemas[1])
        for i in range(100):
            blk.write_full(
                stream_inst_id=1, txn_type_id=1, txn_id=i,
                start=i * 10, end=i * 10 + 5, parent=0,
                attrs=[TxnAttr(field_idx=0, value=i),
                       TxnAttr(field_idx=1, value=i * 10)],
            )
        encoded = blk.encode_block()
        assert encoded[1] & FLAG_TXN_COLUMN, "Auto should select column for fixed-width"

    def test_auto_selects_row_for_variable_width(self):
        """Auto mode selects row when schema contains FT_STRING."""
        from trlog._types import FLAG_TXN_COLUMN
        schemas = {1: [FieldType.FT_STRING]}
        blk = TxnDataBlock(start_time=0, compress=False, column_layout="auto")
        blk.set_schema(1, schemas[1])
        for i in range(100):
            blk.write_full(
                stream_inst_id=1, txn_type_id=1, txn_id=i,
                start=i * 10, end=i * 10 + 5, parent=0,
                attrs=[TxnAttr(field_idx=0, value=f"val{i}")],
            )
        encoded = blk.encode_block()
        assert not (encoded[1] & FLAG_TXN_COLUMN), "Auto should NOT select column for variable-width"

    def test_auto_selects_row_for_small_blocks(self):
        """Auto mode selects row when fewer than 32 records."""
        from trlog._types import FLAG_TXN_COLUMN
        blk = TxnDataBlock(start_time=0, compress=False, column_layout="auto")
        for i in range(10):
            blk.write_full(
                stream_inst_id=1, txn_type_id=1, txn_id=i,
                start=i * 10, end=i * 10 + 5, parent=0, attrs=[],
            )
        encoded = blk.encode_block()
        assert not (encoded[1] & FLAG_TXN_COLUMN), "Auto should NOT select column for small blocks"

    def test_adaptive_picks_better(self):
        """Adaptive mode produces output no larger than either pure row or pure column."""
        schemas = {1: [FieldType.FT_U32, FieldType.FT_U64]}
        blk_row = TxnDataBlock(start_time=0, compress=True, column_layout="row")
        blk_col = TxnDataBlock(start_time=0, compress=True, column_layout="column")
        blk_adapt = TxnDataBlock(start_time=0, compress=True, column_layout="adaptive")
        for b in (blk_row, blk_col, blk_adapt):
            b.set_schema(1, schemas[1])
        for i in range(500):
            attrs = [TxnAttr(field_idx=0, value=i), TxnAttr(field_idx=1, value=i * 10)]
            for b in (blk_row, blk_col, blk_adapt):
                b.write_full(
                    stream_inst_id=1, txn_type_id=1, txn_id=i,
                    start=i * 10, end=i * 10 + 5, parent=0, attrs=attrs,
                )
        row_size = len(blk_row.encode_block())
        col_size = len(blk_col.encode_block())
        adapt_size = len(blk_adapt.encode_block())
        assert adapt_size <= min(row_size, col_size), \
            f"Adaptive ({adapt_size}) > min(row={row_size}, col={col_size})"

    def test_column_negative_deltas(self):
        """Decreasing txn_ids and non-monotonic timestamps roundtrip."""
        blk = TxnDataBlock(start_time=0, compress=False, column_layout="column")
        blk.write_full(stream_inst_id=1, txn_type_id=1, txn_id=100,
                       start=0, end=10, parent=0, attrs=[])
        blk.write_full(stream_inst_id=1, txn_type_id=1, txn_id=50,
                       start=20, end=30, parent=0, attrs=[])
        blk.write_full(stream_inst_id=1, txn_type_id=1, txn_id=200,
                       start=40, end=50, parent=0, attrs=[])
        records, _ = roundtrip_column(blk)
        assert [r.txn_id for r in records] == [100, 50, 200]

    def test_column_multiple_txn_types(self):
        """Two txn_type_ids with different schemas roundtrip in column mode."""
        schemas = {1: [FieldType.FT_U32], 2: [FieldType.FT_U64, FieldType.FT_U8]}
        blk = TxnDataBlock(start_time=0, compress=False, column_layout="column")
        for tid, fts in schemas.items():
            blk.set_schema(tid, fts)
        for i in range(50):
            blk.write_full(
                stream_inst_id=1, txn_type_id=1, txn_id=i * 2,
                start=i * 20, end=i * 20 + 10, parent=0,
                attrs=[TxnAttr(field_idx=0, value=i * 111)],
            )
            blk.write_full(
                stream_inst_id=2, txn_type_id=2, txn_id=i * 2 + 1,
                start=i * 20 + 5, end=i * 20 + 15, parent=0,
                attrs=[TxnAttr(field_idx=0, value=i * 222),
                       TxnAttr(field_idx=1, value=i % 256)],
            )
        records, _ = roundtrip_column(blk, schemas=schemas)
        assert len(records) == 100
        for i in range(50):
            r1 = records[i * 2]
            r2 = records[i * 2 + 1]
            assert r1.txn_type_id == 1
            assert r1.attrs[0].value == i * 111
            assert r2.txn_type_id == 2
            assert r2.attrs[0].value == i * 222
            assert r2.attrs[1].value == i % 256

    def test_column_empty_block(self):
        """Empty block (0 records) roundtrips in column mode."""
        blk = TxnDataBlock(start_time=0, compress=False, column_layout="column")
        records, _ = roundtrip_column(blk)
        assert records == []
