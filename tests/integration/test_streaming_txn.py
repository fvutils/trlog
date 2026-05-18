"""Integration tests for streaming transaction data."""

import pytest
from trlog import TrlWriter, TrlReader
from trlog._types import FieldType, TxnFull, TxnBegin, TxnEnd, TxnAttr, TxnAttrRecord


def make_attr(field_idx, value, ft=FieldType.FT_U64):
    a = TxnAttr(field_idx=field_idx, value=value)
    a._field_type = ft
    return a


class TestStreamingTxn:
    def test_10000_full_txns(self, tmp_path):
        """10k transactions all recovered."""
        p = tmp_path / "t.trl"
        with TrlWriter(str(p), compress=False) as w:
            with w.begin_txn_block(0) as txn:
                for i in range(10_000):
                    txn.write_full(
                        stream_inst_id=1, txn_type_id=1, txn_id=i,
                        start=i * 10, end=i * 10 + 5, parent=0,
                    )

        with TrlReader(str(p)) as r:
            records = []
            for blk in r.iter_txn_blocks():
                records.extend(blk)
            assert len(records) == 10_000
            assert all(isinstance(rec, TxnFull) for rec in records)

    def test_cross_block_begin_end(self, tmp_path):
        """TR_BEGIN in block 1, TR_END in block 2; both recovered."""
        p = tmp_path / "t.trl"
        with TrlWriter(str(p), compress=False) as w:
            with w.begin_txn_block(0) as txn:
                txn.write_begin(stream_inst_id=1, txn_type_id=1, txn_id=99,
                                start=100, parent=0)
            with w.begin_txn_block(200) as txn:
                txn.write_end(txn_id=99, end_time=300)

        with TrlReader(str(p)) as r:
            records = []
            for blk in r.iter_txn_blocks():
                records.extend(blk)
            assert len(records) == 2
            assert isinstance(records[0], TxnBegin)
            assert isinstance(records[1], TxnEnd)
            assert records[0].txn_id == 99
            assert records[1].txn_id == 99

    def test_attr_across_blocks(self, tmp_path):
        """TR_ATTR records for open txn across multiple blocks all recovered."""
        p = tmp_path / "t.trl"
        with TrlWriter(str(p), compress=False) as w:
            with w.begin_txn_block(0) as txn:
                txn.write_begin(stream_inst_id=1, txn_type_id=1, txn_id=1, start=0, parent=0)
            with w.begin_txn_block(10) as txn:
                txn.write_attr(txn_id=1, attrs=[make_attr(0, 100)])
            with w.begin_txn_block(20) as txn:
                txn.write_attr(txn_id=1, attrs=[make_attr(0, 200)])
            with w.begin_txn_block(30) as txn:
                txn.write_end(txn_id=1, end_time=50)

        with TrlReader(str(p)) as r:
            records = []
            for blk in r.iter_txn_blocks():
                records.extend(blk)
            attr_records = [r for r in records if isinstance(r, TxnAttrRecord)]
            assert len(attr_records) == 2
            assert attr_records[0].attrs[0].value == 100
            assert attr_records[1].attrs[0].value == 200

    def test_multi_type_in_one_block(self, tmp_path):
        """Two different txn_type_ids in the same block with schemas."""
        from trlog._types import FieldDef
        p = tmp_path / "t.trl"
        with TrlWriter(str(p), compress=False) as w:
            fields1 = [FieldDef(name_str_id=0, field_type=FieldType.FT_U64)]
            fields2 = [FieldDef(name_str_id=0, field_type=FieldType.FT_STRING)]
            t1 = w.add_txn_schema("TypeA", fields1)
            t2 = w.add_txn_schema("TypeB", fields2)

            with w.begin_txn_block(0) as txn:
                txn.write_full(
                    stream_inst_id=1, txn_type_id=t1, txn_id=1,
                    start=0, end=10, parent=0,
                    attrs=[TxnAttr(field_idx=0, value=42)],
                )
                txn.write_full(
                    stream_inst_id=2, txn_type_id=t2, txn_id=2,
                    start=0, end=10, parent=0,
                    attrs=[TxnAttr(field_idx=0, value="hello")],
                )

        with TrlReader(str(p)) as r:
            records = []
            for blk in r.iter_txn_blocks():
                records.extend(blk)
            assert len(records) == 2
            assert records[0].txn_type_id == t1
            assert records[1].txn_type_id == t2
            assert records[0].attrs[0].value == 42
            assert records[1].attrs[0].value == "hello"
