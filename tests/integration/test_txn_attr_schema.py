"""write_attr() (TR_ATTR) encodes each value by its schema field type (issue #5)."""

import pytest
from trlog import TrlWriter, TrlReader
from trlog._types import FieldDef, FieldType, TxnAttr, TxnAttrRecord


_FIELDS = [
    ("count", FieldType.FT_U64, 12345678901),
    ("name", FieldType.FT_STRING, "hello"),
    ("ratio", FieldType.FT_F64, 0.125),
    ("delta", FieldType.FT_I32, -7),
    ("ok", FieldType.FT_BOOL, True),
]


def _write(path, layout, split_blocks):
    with TrlWriter(str(path), column_layout=layout) as w:
        fields = [FieldDef(name_str_id=w.intern(n), field_type=ft)
                  for n, ft, _ in _FIELDS]
        tid = w.add_txn_schema("Pkt", fields)
        attrs = [TxnAttr(field_idx=i, value=v) for i, (_, _, v) in enumerate(_FIELDS)]
        if split_blocks:
            # The attrs arrive in a later block than the TR_BEGIN.
            with w.begin_txn_block(0) as txn:
                txn.write_begin(stream_inst_id=1, txn_type_id=tid, txn_id=5,
                                start=0, parent=0)
            with w.begin_txn_block(10) as txn:
                txn.write_attr(txn_id=5, attrs=attrs)
                txn.write_end(txn_id=5, end_time=20)
        else:
            with w.begin_txn_block(0) as txn:
                txn.write_begin(stream_inst_id=1, txn_type_id=tid, txn_id=5,
                                start=0, parent=0)
                txn.write_attr(txn_id=5, attrs=attrs)
                txn.write_end(txn_id=5, end_time=20)
        return attrs


@pytest.mark.parametrize("layout", ["row", "column"])
@pytest.mark.parametrize("split_blocks", [False, True])
def test_write_attr_uses_schema_types(tmp_path, layout, split_blocks):
    p = tmp_path / "t.trl"
    attrs = _write(p, layout, split_blocks)
    # The caller's TxnAttr objects are not modified.
    assert not any(hasattr(a, "_field_type") for a in attrs)

    with TrlReader(str(p)) as r:
        recs = [rec for blk in r.iter_txn_blocks() for rec in blk
                if isinstance(rec, TxnAttrRecord)]
    assert len(recs) == 1
    got = {a.field_idx: a.value for a in recs[0].attrs}
    for i, (_, ft, v) in enumerate(_FIELDS):
        assert got[i] == v, (ft, got[i], v)


def test_write_attr_unknown_txn_falls_back_to_u64(tmp_path):
    """Without a TR_BEGIN the schema is unknown; ints still round-trip."""
    p = tmp_path / "t.trl"
    with TrlWriter(str(p)) as w:
        with w.begin_txn_block(0) as txn:
            txn.write_attr(txn_id=9, attrs=[TxnAttr(field_idx=0, value=42)])
    with TrlReader(str(p)) as r:
        recs = [rec for blk in r.iter_txn_blocks() for rec in blk
                if isinstance(rec, TxnAttrRecord)]
    assert recs[0].attrs[0].value == 42
