"""trlog.query API on the query corpus (tests/query/corpus.py)."""

import pathlib

import pytest

import trlog.query as tq
from trlog.query import EnumVal, LogicVal, QueryError

from . import corpus

FIXTURES = pathlib.Path(__file__).resolve().parents[1] / "golden" / "fixtures"


@pytest.fixture
def q(corpus_path):
    with tq.open(corpus_path) as trace:
        yield trace


def ids(txns):
    return [t.id for t in txns]


def test_catalog(q):
    assert q.time_unit == "ps"
    assert sorted(s.path for s in q.streams.values()) == [
        "top.env.axi0.mon", "top.env.axi1.mon", "top.env.seqr", "top.log"]
    assert q.stream_by_path("top.env.axi0.mon").metadata == {"protocol": "AXI4"}
    rd = q.type_by_name("axi_rd")
    assert [f.name for f in rd.fields] == ["addr", "len", "resp", "data", "bus"]
    assert rd.field("resp").enum_labels == {0: "OKAY", 1: "EXOKAY", 2: "SLVERR",
                                            3: "DECERR"}


def test_join_full_begin_attr_end(q):
    by_id = {t.id: t for t in q.find().txns}
    assert len(by_id) == 19
    t31 = by_id[31]          # BEGIN in one block, ATTR+END in the next
    assert (t31.start, t31.end, t31.parent) == (47000, 49500, 3)
    assert t31.fields["resp"] == EnumVal(3, "DECERR")
    assert t31.fields["resp"].label == "DECERR"
    assert isinstance(t31.fields["bus"], LogicVal)
    assert by_id[20].fields["bus"] == 0xA0        # 2-state logicvec -> int
    assert by_id[20].fields["data"] == 0xA0A00000  # bitvec -> int
    assert by_id[4].open and by_id[4].fields == {"name": "hang", "prio": 0}
    assert by_id[32].open and by_id[32].dur is None
    assert by_id[30].meta == {"note": "injected error"}
    assert by_id[1].meta["trlog.origin.file"] == "tb/seq_lib.sv"


def test_default_order_is_start_then_id(q):
    starts = [(t.start, t.id) for t in q.find().txns]
    assert starts == sorted(starts)


def test_links_both_directions(q):
    by_id = {t.id: t for t in q.find(want_links=True).txns}
    l = by_id[30].links
    assert [(x.kind, x.dir, x.other, x.label) for x in l] == [
        ("cause", "in", 3, "stimulus"), ("cause", "out", 101, "resp")]
    assert [(x.dir, x.other) for x in by_id[101].links] == [("in", 30)]
    assert [(x.kind, x.label) for x in by_id[32].links] == [("related", None)]
    # without want_links the scan does not collect them
    assert q.find("id == 30").txns[0].links == []


def test_links_found_outside_selected_streams_and_window(q):
    # The 30->101 / 31->102 / 32->103 LINK records are written in top.log's
    # blocks; selecting only axi1 (or a window ending before them) must still
    # report them.
    r = q.find(streams="top.env.axi1.mon", want_links=True)
    assert {t.id: [l.other for l in t.links] for t in r.txns} == {
        30: [3, 101], 31: [102], 32: [103]}
    r = q.find(time=(None, 43000), types="axi_wr", want_links=True)
    assert [l.other for l in r.txns[-1].links] == [3, 101]


def test_selection_and_filter(q):
    assert ids(q.find(types="axi_wr").txns) == [10, 11, 12, 13, 30]
    assert ids(q.find(streams="top.env.axi1.*").txns) == [30, 31, 32]
    assert ids(q.find("resp != 'OKAY'", types=["axi_rd", "axi_wr"]).txns) == [30, 31]
    assert ids(q.find("sev == 'ERROR'").txns) == [101, 102]
    assert ids(q.find("parent.name == 'err_test'").txns) == [30, 31]
    assert ids(q.find("open").txns) == [4, 32]


def test_time_window_is_inclusive_overlap(q):
    # txn 2 ends exactly at 40ns; open txns overlap every later window
    assert ids(q.find(time=(40000, 50000)).txns) == [2, 3, 30, 101, 31]
    assert ids(q.find(time=(80000, None)).txns) == [4, 32]
    assert ids(q.find(time=(None, 1000)).txns) == [1]


def test_time_upper_bound_skips_blocks(q):
    r = q.find(time=(None, 20000))
    assert ids(r.txns) == [1, 10, 11, 12, 13, 100]
    assert r.stats.blocks_total == 8
    assert r.stats.blocks_read == 3       # seqr, axi0 #1, log #1
    # same answer as a scan with no block skipping
    full = [t for t in q.find().txns if t.start <= 20000]
    assert ids(r.txns) == ids(full)


def test_early_stop_keeps_reading_for_open_txns(tmp_path):
    # A txn begun before t_hi whose END lives in a block starting after t_hi
    # must still be closed; that block cannot be skipped.
    p = tmp_path / "split.trl"
    from trlog._types import ScopeType
    from trlog._writer import TrlWriter
    with TrlWriter(str(p), compress=False) as w:
        ty = w.add_txn_schema("t", [])
        st = w.add_stream_type("s")
        with w.begin_hierarchy() as h:
            h.begin_scope(ScopeType.ST_MODULE, "top")
            s1 = h.add_stream(st, "a")
            s2 = h.add_stream(st, "b")
            h.end_scope()
        with w.begin_txn_block(0, stream_inst_id=s1) as b:
            b.write_begin(s1, ty, 1, 10, 0)
        with w.begin_txn_block(0, stream_inst_id=s2) as b:
            b.write_full(s2, ty, 2, 10, 20, 0)
        with w.begin_txn_block(1000, stream_inst_id=s1) as b:
            b.write_end(1, 1000)
        with w.begin_txn_block(1000, stream_inst_id=s2) as b:
            b.write_full(s2, ty, 3, 1000, 1001, 0)
    with tq.open(p) as q:
        r = q.find(time=(None, 50))
        assert [(t.id, t.end) for t in r.txns] == [(1, 1000), (2, 20)]
        assert r.stats.blocks_read == 3      # stream b's late block skipped


def test_stream_pushdown_from_expr(q):
    sel = q.select("stream == 'top.log' && sev == 'WARN'")
    assert sel.stream_ids == frozenset({q.stream_by_path("top.log").id})
    r = q.find(selection=sel)
    assert ids(r.txns) == [103]
    assert r.stats.blocks_read == 3


def test_sort(q):
    r = q.find(types=["axi_rd"], sort="-dur,id")
    # open txn 32 has no dur: missing values sort last
    assert ids(r.txns) == [20, 21, 22, 23, 31, 32]
    assert ids(q.find(types="msg", sort="-start").txns) == [103, 102, 101, 100]
    with pytest.raises(QueryError) as e:
        q.find(sort="adr")
    assert "addr" in e.value.info["did_you_mean"]


def test_count(q):
    c = q.count(by=["type"])
    assert c.groups == [(("axi_rd",), 6), (("axi_wr",), 5), (("msg",), 4),
                        (("seq_item",), 4)]
    c = q.count("type == 'axi_rd'", by=["resp", "open"])
    assert dict(c.groups) == {("OKAY", False): 4, ("DECERR", False): 1,
                              (None, True): 1}
    assert q.count("sev == 'ERROR'").matched == 2
    with pytest.raises(QueryError):
        q.count(by=["nosuch"])


def test_show(q):
    found, missing, _ = q.show([3, 999, 31])
    assert ids(found) == [3, 31]
    assert missing == [999]
    assert found[0].children == [30, 31]
    assert found[1].children == []
    assert [l.other for l in found[1].links] == [102]


def test_stream_summary(q):
    rows, _ = q.stream_summary()
    by = {r["stream"]: r for r in rows}
    assert by["top.env.axi1.mon"]["count"] == 3
    assert by["top.env.axi1.mon"]["open"] == 1
    assert by["top.env.axi0.mon"]["types"] == ["axi_wr", "axi_rd"]
    rows, _ = q.stream_summary("top.log")
    assert [r["stream"] for r in rows] == ["top.log"]


def test_schema_sample_stops_early(q):
    rows, stats = q.schema("msg", sample=1)
    assert rows[0]["sampled"] == 1
    sev = rows[0]["fields"][1]
    assert sev["values"] == {"INFO": 0, "WARN": 1, "ERROR": 2}
    assert sev["observed"]["top"] == [["INFO", 1]]
    assert stats.blocks_read < stats.blocks_total


def test_info(q):
    info = q.info()
    assert info["txns"] == 19 and info["open"] == 2
    assert info["span"] == [1000, 70000]
    assert info["metadata"] == {"tool": "query_corpus", "seed": 1}
    assert info["index"] is True and info["qindex"] is False
    assert info["warnings"] == []


def test_file_errors(tmp_path):
    with pytest.raises(QueryError) as e:
        tq.open(tmp_path / "nope.trl")
    assert e.value.info["error"] == "file_error"
    bad = tmp_path / "bad.trl"
    for junk in (b"not a trace at all", b"", b"\x00\x00" + b"\xff" * 30):
        bad.write_bytes(junk)
        with pytest.raises(QueryError) as e:
            tq.open(bad)
        assert e.value.info["error"] == "file_error"
        assert "not a trlog trace" in e.value.info["message"]


@pytest.mark.parametrize("name,count", [
    ("txn_field_kinds", 4), ("txn_row", 40), ("txn_column", 100),
    ("txn_schema_attrs", 32), ("callreturn", 1), ("empty", 0), ("multi_hier", 0),
])
def test_existing_fixtures(name, count):
    with tq.open(FIXTURES / f"{name}.trl") as q:
        assert q.find().matched == count
        q.info()
        q.stream_summary()
        q.schema(sample=5)


def test_field_kinds_fixture_values():
    with tq.open(FIXTURES / "txn_field_kinds.trl") as q:
        t1 = q.find("id == 1").txns[0]
        assert t1.fields["wide"] == (1 << 69) | 0x5A5A
        assert t1.fields["blob"] == b"\x00\x01\xff"
        assert isinstance(t1.fields["bus"], LogicVal)
        assert q.find("wide > 0x5a5a && blob == blob").matched == 2
