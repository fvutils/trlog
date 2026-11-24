"""Cross-check for the C API fixes (issues #15 #18 #19).

``c/tests/test_capi.c`` is run via ctest and writes its files next to the C
test binaries. This test reads them back with the pure-Python reader.
Skipped (not failed) when the C test hasn't been built/run yet.
"""

from __future__ import annotations

import pathlib

import pytest

from trlog import TrlReader
from trlog._types import TxnFull

OUT = pathlib.Path(__file__).resolve().parents[2] / "build" / "c" / "tests"
HIER = OUT / "capi_hier.trl"
REC = OUT / "capi_rec.trl"

pytestmark = pytest.mark.skipif(
    not (HIER.exists() and REC.exists()),
    reason="build/c/tests/capi_*.trl not found; build c/ and run ctest first",
)


def _attrs(r, node):
    return {r.string_table.lookup(a.key_str_id): r.string_table.lookup(a.value_str_id)
            for a in node.attrs}


def test_hier_add_attr_attaches_to_scope_and_var():
    with TrlReader(str(HIER)) as r:
        hier = next(iter(r.hierarchies.values()))
        (top,) = hier._root_children
        assert r.string_table.lookup(top.name_str_id) == "top"
        assert _attrs(r, top) == {"scope_key": "scope_val"}
        (var,) = hier.vars.values()
        assert _attrs(r, var) == {"var_key": "var_val"}


def _streams(r):
    return {r.string_table.lookup(s.name_str_id): inst_id
            for hier in r.hierarchies.values() for inst_id, s in hier.streams.items()}


def test_rec_full_records_bit_and_logic_vectors():
    with TrlReader(str(REC)) as r:
        full = sorted((rec for blk in r.iter_txn_blocks() for rec in blk
                       if isinstance(rec, TxnFull)), key=lambda rec: rec.txn_id)
    assert [rec.txn_id for rec in full] == [1, 2]
    vals = {a.field_idx: a.value for a in full[0].attrs}
    assert vals[0] == 7
    assert vals[1] == (12, 0xABC)            # (width, value); masked to 12 bits
    assert vals[2] == (8, 0x5A, 0)           # (width, aval, bval); no X/Z
    assert vals[3] == (64, 0xDEADBEEFCAFEF00D)
    # The BYTES field cannot be passed in 64 bits; the rest is recorded.
    assert {a.field_idx: a.value for a in full[1].attrs} == {0: 3}


def test_disabled_stream_records_nothing():
    with TrlReader(str(REC)) as r:
        off = _streams(r)["off"]
        recs = [rec for blk in r.iter_txn_blocks(stream_inst_id=off) for rec in blk]
    assert recs == []
