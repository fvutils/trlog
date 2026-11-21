"""C <-> Python parity for every FT_* field kind (sv_api_design.md §10, phase 1).

The ``txn_field_kinds`` golden fixture is written by the pure-Python reference.
``c/tests/test_golden.c`` builds the same trace through the C API and must
match it byte-for-byte; here the Python reader decodes the fixture and the
ctypes-native writer (when ``libtrl.so`` is built) reproduces it.
"""

from __future__ import annotations

import pathlib

import pytest

from trlog import TrlReader
from trlog._types import FieldType
from trlog import _native
from tests.golden import corpus

FIXTURE = (pathlib.Path(__file__).resolve().parents[1]
           / "golden" / "fixtures" / "txn_field_kinds.trl")


def _expected(k):
    out = []
    for _, ft, vals in corpus.TXN_FIELD_KINDS:
        v = vals[k]
        if ft == FieldType.FT_F32:
            v = pytest.approx(v)
        out.append(v)
    return out


def _records(stream_inst_id=None):
    with TrlReader(str(FIXTURE)) as r:
        return [rec for blk in r.iter_txn_blocks(stream_inst_id=stream_inst_id)
                for rec in blk]


def test_every_kind_has_a_field():
    kinds = {ft for _, ft, _ in corpus.TXN_FIELD_KINDS}
    assert kinds == set(FieldType)


def test_fixture_decodes_every_kind():
    recs = {rec.txn_id: rec for rec in _records()}
    assert sorted(recs) == [1, 2, 3, 4]
    for txn_id, k in ((1, 0), (2, 1), (3, 0)):
        got = [a.value for a in sorted(recs[txn_id].attrs, key=lambda a: a.field_idx)]
        assert got == _expected(k), f"txn {txn_id}"
    assert [(a.field_idx, a.value) for a in recs[4].attrs] == [(15, 7)]
    assert recs[2].parent_txn_id == 1


def test_per_stream_read_spans_mixed_and_stream_blocks():
    assert [rec.txn_id for rec in _records(stream_inst_id=1)] == [1]
    assert [rec.txn_id for rec in _records(stream_inst_id=2)] == [2, 3, 4]


@pytest.mark.skipif(_native._LIB is None,
                    reason="libtrl.so not built; build c/ and/or set TRL_LIBRARY_PATH")
def test_native_writer_matches_fixture(tmp_path):
    entry = next(e for e in corpus.entries() if e.name == "txn_field_kinds")
    out = tmp_path / "kinds.trl"
    entry.build(_native._CtypesWriter, str(out))
    assert out.read_bytes() == FIXTURE.read_bytes()
