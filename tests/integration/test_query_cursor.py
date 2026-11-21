"""Cross-check for the C reading session (trl/trl_query.h, phase 3).

``c/tests/test_query.c`` writes a small trace through trl_rec.h with a
block_bytes of 1 (so a stream's buffer auto-flushes between almost every
call, forcing a transaction's TR_BEGIN/TR_ATTR/TR_END/TR_LINK/TR_META to
split across several blocks), then reads it back with trl_query.h's cursor
and checks the joined transactions. This test independently re-derives the
same joined transactions from the raw per-tag records the pure-Python
reader returns (``iter_txn_blocks``, which does no joining of its own) and
checks they match what a correct join must produce. There is no reference
cursor on the Python side to compare the C one against directly (phase 2's
notes say so), so this is a from-first-principles check, not a byte-for-byte
diff against a second implementation.

Skipped (not failed) when the C test hasn't been built/run yet.
"""

from __future__ import annotations

import pathlib

import pytest

from trlog import TrlReader
from trlog._types import TxnFull, TxnBegin, TxnAttrRecord, TxnEnd, TxnLink, TxnMeta

FIXTURE = (pathlib.Path(__file__).resolve().parents[2]
           / "build" / "c" / "tests" / "phase3_query.trl")

pytestmark = pytest.mark.skipif(
    not FIXTURE.exists(),
    reason="build/c/tests/phase3_query.trl not found; build c/ and run ctest first",
)


def _joined_transactions(r):
    """Re-derive joined transactions from the raw per-tag records, the same
    way trl_query.h's cursor must, so this test does not simply trust the
    thing it is checking."""
    records = [rec for blk in r.iter_txn_blocks() for rec in blk]

    open_txns = {}
    done = {}
    for rec in records:
        if isinstance(rec, TxnFull):
            done[rec.txn_id] = {
                "start": rec.start_time, "end": rec.end_time,
                "parent": rec.parent_txn_id, "attrs": list(rec.attrs),
                "links": [], "metas": [],
            }
        elif isinstance(rec, TxnBegin):
            open_txns[rec.txn_id] = {
                "start": rec.start_time, "end": None,
                "parent": rec.parent_txn_id, "attrs": [],
                "links": [], "metas": [],
            }
        elif isinstance(rec, TxnAttrRecord):
            open_txns[rec.txn_id]["attrs"].extend(rec.attrs)
        elif isinstance(rec, TxnEnd):
            t = open_txns.pop(rec.txn_id)
            t["end"] = rec.end_time
            done[rec.txn_id] = t
        elif isinstance(rec, TxnLink):
            target = open_txns.get(rec.source_txn_id) or done.get(rec.source_txn_id)
            target["links"].append(rec)
        elif isinstance(rec, TxnMeta):
            target = open_txns.get(rec.txn_id) or done.get(rec.txn_id)
            target["metas"].append(rec)

    for txn_id, t in open_txns.items():
        done[txn_id] = t
    return done


def test_cross_block_join_matches_independent_reconstruction():
    with TrlReader(str(FIXTURE)) as r:
        joined = _joined_transactions(r)

    # Program order: t1 (begin/set*3/meta/end), t2 (begin/set/link/end), t3 (full).
    txn_ids = sorted(joined)
    assert len(txn_ids) == 3
    t1_id, t2_id, t3_id = txn_ids

    t1 = joined[t1_id]
    assert t1["start"] == 100 and t1["end"] == 150
    by_field = {a.field_idx: a.value for a in t1["attrs"]}
    assert by_field == {0: 1, 1: 2, 2: 3}
    assert len(t1["metas"]) == 1

    t2 = joined[t2_id]
    assert t2["parent"] == t1_id
    assert len(t2["links"]) == 1
    assert t2["links"][0].target_txn_id == t1_id

    t3 = joined[t3_id]
    assert t3["start"] == 130 and t3["end"] == 140
    # trl_rec_full emits every field in the schema, not just the ones the
    # caller set, so fields 1 and 2 come back as their zero value too.
    assert {a.field_idx: a.value for a in t3["attrs"]} == {0: 42, 1: 0, 2: 0}


def test_streams_and_schema_present():
    with TrlReader(str(FIXTURE)) as r:
        names = set()
        for hier in r.hierarchies.values():
            for hstream in hier.streams.values():
                names.add(r.string_table.lookup(hstream.name_str_id))
        assert "s" in names

        found = False
        for i in range(1, 64):
            entry = r.type_registry.get_txn_schema(i)
            if entry is None:
                continue
            if r.string_table.lookup(entry.name_str_id) == "pkt":
                found = True
                assert len(entry.fields) == 3
        assert found
