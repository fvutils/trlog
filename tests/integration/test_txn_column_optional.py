"""Column-layout TXN_DATA blocks with optional (unset) attributes (issue #6)."""

import pytest
from trlog import TrlWriter, TrlReader
from trlog._txn_data import TxnDataBlock
from trlog._types import FieldDef, FieldType as FT, TxnAttr, TxnFull

HDR = 10  # block header size


def _roundtrip(schema, recs, layout, delta=True):
    """recs: one list of (field_idx, value) per TR_FULL record."""
    b = TxnDataBlock(compress=False, column_layout=layout)
    b._delta_encode = delta
    b.set_schema(1, schema)
    for i, attrs in enumerate(recs):
        b.write_full(0, 1, i + 1, i * 10, i * 10 + 5, 0,
                     [TxnAttr(field_idx=f, value=v) for f, v in attrs])
    blk = b.encode_block()
    out = TxnDataBlock().read_block(blk[HDR:], blk[1], schemas={1: schema})
    return [sorted((a.field_idx, a.value) for a in r.attrs) for r in out]


_CASES = [
    ("all set", [FT.FT_U8, FT.FT_STRING], [[(0, 1), (1, "a")], [(0, 2), (1, "b")]]),
    ("shuffled", [FT.FT_U8, FT.FT_STRING], [[(1, "a"), (0, 1)], [(0, 2), (1, "b")]]),
    ("first unset", [FT.FT_U8, FT.FT_U8], [[(1, 7)], [(0, 1), (1, 8)]]),
    ("last empty u64", [FT.FT_U64], [[(0, 5)], []]),
    ("last empty str", [FT.FT_STRING], [[(0, "x")], []]),
    ("last empty f64", [FT.FT_F64], [[(0, 1.0)], []]),
    ("mixed", [FT.FT_U32, FT.FT_STRING, FT.FT_F64, FT.FT_BOOL],
     [[(0, 1), (2, 0.5)], [(1, "q"), (3, True)], [], [(0, 4), (1, "r"), (2, 2.0), (3, False)]]),
]


@pytest.mark.parametrize("delta", [True, False])
@pytest.mark.parametrize("layout", ["column", "adaptive"])
@pytest.mark.parametrize("name,schema,recs", _CASES, ids=[c[0] for c in _CASES])
def test_column_optional_attrs(name, schema, recs, layout, delta):
    assert _roundtrip(schema, recs, layout, delta) == [sorted(r) for r in recs]


def test_column_optional_attrs_auto_many_records():
    """'auto' picks column layout at >= 32 fixed-width records."""
    schema = [FT.FT_U32, FT.FT_U64]
    recs = [[(0, i)] if i % 3 else [(1, i * 2)] for i in range(40)]
    assert _roundtrip(schema, recs, "auto") == [sorted(r) for r in recs]


def test_column_optional_attrs_through_writer(tmp_path):
    p = tmp_path / "t.trl"
    with TrlWriter(str(p), column_layout="column") as w:
        tid = w.add_txn_schema("Pkt", [
            FieldDef(name_str_id=w.intern("a"), field_type=FT.FT_U16),
            FieldDef(name_str_id=w.intern("b"), field_type=FT.FT_STRING)])
        with w.begin_txn_block(0) as txn:
            txn.write_full(1, tid, 1, 0, 5, 0, [TxnAttr(1, "only-b")])
            txn.write_full(1, tid, 2, 10, 15, 0, [TxnAttr(0, 3), TxnAttr(1, "both")])
            txn.write_full(1, tid, 3, 20, 25, 0, [])
    with TrlReader(str(p)) as r:
        fulls = [rec for blk in r.iter_txn_blocks() for rec in blk
                 if isinstance(rec, TxnFull)]
    got = [sorted((a.field_idx, a.value) for a in f.attrs) for f in fulls]
    assert got == [[(1, "only-b")], [(0, 3), (1, "both")], []]
