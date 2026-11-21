"""Cross-check for the C recording session (trl/trl_rec.h, phase 2).

``c/tests/test_rec.c`` builds a trace through the new session API and is run
via ctest, writing its output next to the C test binaries. This test reads
that file back with the pure-Python reference reader and checks that the
session layer's stream declarations, schemas and records decode as expected.
Skipped (not failed) when the C test hasn't been built/run yet.
"""

from __future__ import annotations

import pathlib

import pytest

from trlog import TrlReader
from trlog._types import TxnFull, TxnBegin, TxnAttrRecord, TxnEnd, TxnLink, TxnMeta

FIXTURE = (pathlib.Path(__file__).resolve().parents[2]
           / "build" / "c" / "tests" / "phase2_session.trl")

pytestmark = pytest.mark.skipif(
    not FIXTURE.exists(),
    reason="build/c/tests/phase2_session.trl not found; build c/ and run ctest first",
)


def _all_records(r):
    return [rec for blk in r.iter_txn_blocks() for rec in blk]


def test_streams_declared_with_qualified_paths():
    with TrlReader(str(FIXTURE)) as r:
        names = set()
        for hier in r.hierarchies.values():
            for hstream in hier.streams.values():
                names.add(r.string_table.lookup(hstream.name_str_id))
    assert {"env", "env.axi", "env.quiet", "env.pkt", "dbg"} <= names


def test_axi_full_records_decode():
    with TrlReader(str(FIXTURE)) as r:
        full = [rec for rec in _all_records(r) if isinstance(rec, TxnFull)]
    assert len(full) == 20
    first = sorted(full, key=lambda rec: rec.txn_id)[0]
    vals = {a.field_idx: a.value for a in first.attrs}
    assert vals[0] == 0x1000          # addr
    assert vals[1] == 0               # len


def test_incremental_pkt_records_decode():
    # txn_id is assigned in program order regardless of which stream's block
    # ends up first in the file (trl_db_close flushes streams in an
    # unspecified order), so txn 1..20 are the axi loop, 21/22 are pkt t1/t2.
    t1_id, t2_id = 21, 22

    with TrlReader(str(FIXTURE)) as r:
        records = _all_records(r)
    begins = {rec.txn_id: rec for rec in records if isinstance(rec, TxnBegin)}
    attrs = [rec for rec in records if isinstance(rec, TxnAttrRecord)]
    ends = {rec.txn_id for rec in records if isinstance(rec, TxnEnd)}
    links = [rec for rec in records if isinstance(rec, TxnLink)]
    metas = [rec for rec in records if isinstance(rec, TxnMeta)]

    assert t1_id in begins and t2_id in begins
    assert begins[t2_id].parent_txn_id == t1_id

    t1_attrs = [a for rec in attrs if rec.txn_id == t1_id for a in rec.attrs]
    by_field = {a.field_idx: a.value for a in t1_attrs}
    assert by_field[0] == 64            # hdr.len
    assert by_field[1] == 3             # hdr.kind
    assert by_field[5] == b"\xde\xad\xbe\xef"   # payload

    assert {t1_id, t2_id} <= ends
    assert len(links) == 1
    assert links[0].source_txn_id == t2_id
    assert links[0].target_txn_id == t1_id
    assert len(metas) == 1
    assert r.string_table.lookup(metas[0].key_str_id) == "trlog.origin.component"
    assert r.string_table.lookup(metas[0].value_str_id) == "seq1"


def test_adhoc_stream_grows_its_schema():
    dbg_id = 23
    with TrlReader(str(FIXTURE)) as r:
        records = _all_records(r)
    attrs = [rec for rec in records if isinstance(rec, TxnAttrRecord)]
    dbg_attrs = [a for rec in attrs if rec.txn_id == dbg_id for a in rec.attrs]
    by_field = {a.field_idx: a.value for a in dbg_attrs}
    assert by_field[0] == "hello"
    assert by_field[1] == 2


def test_excluded_stream_has_no_records():
    with TrlReader(str(FIXTURE)) as r:
        quiet_inst_id = None
        for hier in r.hierarchies.values():
            for inst_id, hstream in hier.streams.items():
                if r.string_table.lookup(hstream.name_str_id) == "env.quiet":
                    quiet_inst_id = inst_id
        assert quiet_inst_id is not None
        recs = [rec for blk in r.iter_txn_blocks(stream_inst_id=quiet_inst_id) for rec in blk]
    assert recs == []
