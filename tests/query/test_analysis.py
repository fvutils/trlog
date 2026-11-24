"""Analysis verbs (phase Q3): stats, hist, gaps, match, export.

Uses the shared query corpus plus a small request/response trace (``bus``)
built here, whose layout is chosen so each matching and gap rule has a case.
"""

import io
import json
import sqlite3
import sys

import pytest

import trlog.query as tq
from trlog import cli
from trlog.query import QueryError
from trlog.query import _analysis as an


BIG = (1 << 64) - 1


def _load_bus_example():
    """build_bus lives in examples/query_bus.py (the docs' match examples)."""
    import importlib.util
    import pathlib
    p = pathlib.Path(__file__).resolve().parents[2] / "examples" / "query_bus.py"
    spec = importlib.util.spec_from_file_location("trlog_example_query_bus", p)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


build_bus = _load_bus_example().build_bus


@pytest.fixture(scope="module")
def bus_path(tmp_path_factory):
    p = tmp_path_factory.mktemp("q3") / "bus.trl"
    build_bus(p)
    return p


@pytest.fixture
def bus(bus_path):
    with tq.open(bus_path) as trace:
        yield trace


@pytest.fixture
def q(corpus_path):
    with tq.open(corpus_path) as trace:
        yield trace


def refs(txns):
    return [t.id for t in txns]


def pairs(res):
    return [(a.id, b.id) for a, b, _ in res.pairs]


# -- stats -----------------------------------------------------------------------

def test_percentile_nearest_rank():
    v = list(range(1, 101))
    assert an.percentile(v, 50) == 50 and an.percentile(v, 99) == 99
    assert an.percentile([7], 99) == 7
    assert an.percentile([1, 2], 50) == 1 and an.percentile([1, 2], 90) == 2


def test_stats_by_group_and_refs(q):
    r = q.stats("dur", by=["type"])
    g = {k[0]: s for k, s in r.groups}
    assert g["axi_rd"].n == 5 and g["axi_rd"].missing == 1      # t:32 is open
    assert g["seq_item"].min == 19000 and g["seq_item"].min_txn.id == 1
    assert r.is_time and r.matched == 19
    # largest n first, ties by key
    assert [k[0] for k, _ in r.groups] == ["axi_rd", "axi_wr", "msg", "seq_item"]


def test_stats_expression_and_parent(q):
    r = q.stats("addr & 0xff", types=["axi_wr"])
    (_, s), = r.groups
    assert (s.min, s.max, s.n) == (0, 0x30, 5) and not r.is_time
    # parent.X needs the whole graph even when the selection is narrow
    r = q.stats("parent.prio", types=["axi_wr"], by=["stream"])
    assert {k[0]: (s.min, s.max) for k, s in r.groups} == {
        "top.env.axi0.mon": (1, 1), "top.env.axi1.mon": (5, 5)}


def test_stats_rejects_non_numeric(q):
    for target in ("resp", "text", "stream"):
        with pytest.raises(QueryError) as e:
            q.stats(target)
        assert e.value.info["error"] == "not_numeric"


def test_stats_x_values_count_as_missing(q):
    r = q.stats("bus", types=["axi_rd"])
    (_, s), = r.groups
    assert s.n == 4 and s.missing == 2       # t:31 has x/z bits, t:32 no value


def test_stats_big_and_float(bus):
    (_, s), = bus.stats("addr", types="req", streams="top.bus.req").groups
    assert s.max == BIG and s.max_txn.id == 4
    (_, s), = bus.stats("q").groups
    assert s.mean == pytest.approx(5.5 / 4) and s.missing == 2


# -- hist ------------------------------------------------------------------------

def test_nice_bin():
    assert an.nice_bin(0, 99, 20) == 5
    assert an.nice_bin(0, 100, 20) == 10      # 100 would need a 21st bin of 5
    assert an.nice_bin(1000, 70000, 20) == 5000
    assert an.nice_bin(3, 3, 20) == 1
    assert an.nice_bin(0, 1.0, 4, integral=False) == 0.5
    # alignment can need a wider bin than span / max_bins
    assert an.nice_bin(9, 21, 2) == 20


def test_hist_bins_are_left_closed(q):
    h = q.hist("start", bin=10000)
    assert h.lo == 0 and len(h.counts) == 8 and sum(h.counts) == 19
    # t:103 starts at exactly 70000: it opens the last bin
    assert h.counts[-1] == 1 and h.edges(7) == (70000, 80000)


def test_hist_auto_and_float(bus):
    assert bus.hist("q", max_bins=4).bin == 1
    h = bus.hist("q", max_bins=5)
    assert h.bin == 0.5 and h.lo == 0.5 and h.counts == [1, 1, 1, 0, 1]
    assert h.n == 4 and h.missing == 2


def test_hist_errors(q):
    with pytest.raises(QueryError) as e:
        q.hist("start", bin=1)
    assert e.value.info["error"] == "too_many_bins"
    with pytest.raises(QueryError):
        q.hist("start", bin=0)
    assert q.hist("dur", where="addr == 0xdead").counts == []


# -- gaps ------------------------------------------------------------------------

def gap_tuples(res):
    return [(g.stream.rsplit(".", 1)[-1], g.start, g.end,
             g.prev.id if g.prev else None, g.next.id if g.next else None)
            for g in res.gaps]


def test_gaps_min_is_strict_and_edges(bus):
    r = bus.gaps(min=1, sort="stream,start")
    assert r.range == (0, 23)
    assert gap_tuples(r) == [
        # mon: 21 covers 0..10 (22 inside it), so the gap after it starts at 10
        ("mon", 10, 12, 21, 23), ("mon", 13, 23, 23, None),
        ("req", 7, 20, 4, 5), ("req", 21, 23, 5, None),
        # rsp: leading gap; 15 is open, so no trailing gap
        ("rsp", 0, 8, None, 11), ("rsp", 16, 19, 14, 15)]
    # 1ns gaps between the early requests are not longer than --min 1
    assert all(g.dur > 1 for g in r.gaps)


def test_gaps_window_and_sort(bus):
    r = bus.gaps(streams="top.bus.req", time=(5, 22))
    assert r.range == (5, 22)
    # longest first; equal lengths by start
    assert [(g.start, g.end) for g in r.gaps] == [(7, 20), (5, 6), (21, 22)]
    r = bus.gaps(streams="top.bus.req", time=(5, 22), sort="start")
    assert [g.start for g in r.gaps] == [5, 7, 21]
    with pytest.raises(QueryError) as e:
        bus.gaps(sort="size")
    assert e.value.info["error"] == "bad_sort"


def test_gaps_silent_streams(bus):
    r = bus.gaps(where="tag == 5")
    assert r.silent == ["top.bus.mon", "top.bus.req"] and r.streams == 1


# -- match -----------------------------------------------------------------------

def test_parse_on():
    assert an.parse_on("tag") == [("tag", "tag")]
    assert an.parse_on("tag, addr") == [("tag", "tag"), ("addr", "addr")]
    assert an.parse_on("b.parent == a.id && a.tag == b.tag") == [
        ("id", "parent"), ("tag", "tag")]
    assert an.parse_on(None) == [] and an.parse_on(" ") == []
    for bad in ("a.x = b.y", "a.x == a.y", "x == y", "a.tag == b.tag && junk"):
        with pytest.raises(QueryError) as e:
            an.parse_on(bad)
        assert e.value.info["error"] == "bad_on"


def test_match_fifo(bus):
    r = bus.match("type == 'req'", "type == 'rsp'", on="tag", streams="top.bus.r*")
    assert pairs(r) == [(1, 12), (2, 11), (3, 13)]
    assert refs(r.unmatched_a) == [4, 5]          # 5's response came first
    assert refs(r.unmatched_b) == [14, 15]
    assert r.no_key_a == 0 and r.no_key_b == 1    # 16 has no tag
    assert [an.lat_of(a, b) for a, b, _ in r.pairs] == [11, 7, 10]
    assert r.lat.n == 3 and r.lat.min == 7 and r.lat.max_txn[0].id == 1


def test_match_is_one_scan(bus):
    """Both sides come from one pass (Q4): each block is read once."""
    r = bus.match("type == 'req'", "type == 'rsp'", on="tag")
    full = bus.count()
    assert r.stats.blocks_read == full.stats.blocks_read


def test_match_keeps_pushdown(bus):
    """The union scan keeps the sides' stream restriction."""
    r = bus.match("stream == 'top.bus.req'", "stream == 'top.bus.rsp' && tag > 0",
                  on="tag")
    both = bus.count("stream in ('top.bus.req', 'top.bus.rsp')")
    assert r.stats.blocks_read == both.stats.blocks_read < bus.count().stats.blocks_read
    assert pairs(r) == pairs(bus.match("type == 'req'", "type == 'rsp'", on="tag",
                                       streams="top.bus.r*"))


def test_match_fifo_within(bus):
    r = bus.match("type == 'req'", "type == 'rsp'", on="tag", within=6,
                  streams="top.bus.r*")
    # 12 @10: 1 @0 is too old and dropped, 3 @4 is exactly 6 away
    assert pairs(r) == [(2, 11), (3, 12)]
    assert refs(r.unmatched_a) == [1, 4, 5]
    assert refs(r.unmatched_b) == [13, 14, 15]


def test_match_any_allows_b_first(bus):
    r = bus.match("type == 'req'", "type == 'rsp'", on="tag", order="any",
                  streams="top.bus.r*")
    # 12 @10 takes the nearest tag-1 request (3 @4), leaving 1 for 13
    assert pairs(r) == [(1, 13), (2, 11), (3, 12), (5, 15)]
    assert r.lat.missing == 1                     # 15 is still open


def test_match_never_pairs_a_txn_with_itself(bus):
    r = bus.match("tag == 1", "tag == 1", on="tag")
    assert pairs(r) == [(1, 3), (3, 12), (12, 13)]


def test_match_without_key_pairs_in_order(bus):
    r = bus.match("stream == 'top.bus.req'", "stream == 'top.bus.rsp'")
    assert pairs(r) == [(1, 11), (2, 12), (3, 13), (4, 14), (5, 16)]
    assert refs(r.unmatched_b) == [15]


def test_match_bad_order(bus):
    with pytest.raises(QueryError):
        bus.match("open", "open", order="lifo")


# -- export ----------------------------------------------------------------------

def test_export_sqlite_tables_and_views(q, tmp_path):
    out = tmp_path / "c.db"
    info = q.export("sqlite", str(out))
    assert info["tables"]["txn"] == 19 and info["tables"]["link"] == 4
    con = sqlite3.connect(out)
    assert con.execute("SELECT count(*) FROM txn WHERE open").fetchone()[0] == 2
    rows = con.execute("SELECT id, stream, resp, bus FROM txn_axi_rd "
                       "WHERE resp != 'OKAY' ORDER BY id").fetchall()
    assert rows == [(31, "top.env.axi1.mon", "DECERR", "0xzf")]
    # SQL over typed columns works without casts
    assert con.execute("SELECT count(*) FROM attr_axi_wr WHERE addr BETWEEN "
                       "0x4000 AND 0x40ff").fetchone()[0] == 4
    assert con.execute("SELECT strb FROM attr_axi_wr WHERE id = 30").fetchone()[0] \
        == b"\xff"
    assert con.execute("SELECT value FROM trace WHERE key = 'time_unit'"
                       ).fetchone()[0] == "ps"
    assert con.execute("SELECT value FROM meta WHERE id = 30 AND key = 'note'"
                       ).fetchone()[0] == "injected error"
    st = dict(con.execute("SELECT path, count FROM stream").fetchall())
    assert st["top.log"] == 4
    fields = json.loads(con.execute("SELECT fields FROM type WHERE name = 'axi_rd'"
                                    ).fetchone()[0])
    assert fields[2]["enum"]["3"] == "DECERR"


def test_export_selection_keeps_edges_to_unselected(q, tmp_path):
    out = tmp_path / "s.db"
    info = q.export("sqlite", str(out), types="msg")
    assert info["tables"]["txn"] == 4
    con = sqlite3.connect(out)
    # every link touches a msg, from axi txns that were not exported
    assert con.execute("SELECT count(*) FROM link").fetchone()[0] == 3
    assert con.execute("SELECT count(*) FROM attr_axi_rd").fetchone()[0] == 0
    assert json.loads(con.execute("SELECT value FROM trace WHERE key = 'query'"
                                  ).fetchone()[0]) == {"types": ["msg"]}


def test_export_big_ints_as_hex(bus, tmp_path):
    out = tmp_path / "b.db"
    bus.export("sqlite", str(out), streams="top.bus.req")
    con = sqlite3.connect(out)
    assert con.execute("SELECT addr FROM attr_req WHERE id = 4").fetchone()[0] \
        == hex(BIG)
    assert con.execute("SELECT addr FROM attr_req WHERE id = 1").fetchone()[0] == 0x10


def test_export_exists_and_force(q, tmp_path):
    out = tmp_path / "e.db"
    out.write_text("keep me")
    with pytest.raises(QueryError) as e:
        q.export("sqlite", str(out))
    assert e.value.info["error"] == "exists" and out.read_text() == "keep me"
    q.export("sqlite", str(out), force=True)
    assert sqlite3.connect(out).execute("SELECT count(*) FROM txn").fetchone()[0] == 19
    assert [p.name for p in tmp_path.iterdir()] == ["e.db"]     # no temp left


def test_export_bad_format(q, tmp_path):
    with pytest.raises(QueryError) as e:
        q.export("csv", str(tmp_path / "x"))
    assert e.value.info["valid_values"] == ["sqlite", "parquet"]


def test_export_parquet_without_pyarrow(q, tmp_path, monkeypatch):
    monkeypatch.setitem(sys.modules, "pyarrow", None)
    with pytest.raises(QueryError) as e:
        q.export("parquet", str(tmp_path / "p"))
    assert e.value.info["error"] == "missing_dependency"
    assert "pip install pyarrow" in e.value.info["hint"]
    assert not (tmp_path / "p").exists()


def test_export_parquet(q, tmp_path):
    pq = pytest.importorskip("pyarrow.parquet")
    out = tmp_path / "p"
    info = q.export("parquet", str(out))
    assert sorted(p.name for p in out.iterdir()) == sorted(
        f"{t}.parquet" for t in info["tables"])
    t = pq.read_table(out / "attr_axi_rd.parquet").to_pydict()
    assert t["resp"][t["id"].index(31)] == "DECERR"
    assert pq.read_table(out / "txn.parquet").num_rows == 19


# -- CLI -------------------------------------------------------------------------

def run(argv):
    out, err = io.StringIO(), io.StringIO()
    saved = sys.stdout, sys.stderr
    sys.stdout, sys.stderr = out, err
    try:
        rc = cli.main(argv)
    finally:
        sys.stdout, sys.stderr = saved
    return rc, out.getvalue(), err.getvalue()


def test_cli_widths(bus_path):
    p = str(bus_path)
    _, out, _ = run(["gaps", p, "--min", "1ns", "--json"])
    assert json.loads(out)["query"]["min"] == 1
    _, out, _ = run(["hist", p, "q", "--bin", "0.5", "--json"])
    assert json.loads(out)["bin"] == 0.5
    rc, out, _ = run(["gaps", p, "--min", "soon", "--json"])
    assert rc == 2 and json.loads(out)["error"] == "bad_value"


def test_cli_match_sort_lat_puts_open_last(bus_path):
    rc, out, _ = run(["match", str(bus_path), "--a", "type == 'req'", "--b",
                      "type == 'rsp'", "--on", "tag", "--order", "any", "-s",
                      "top.bus.r*", "--sort", "-lat", "--json"])
    env = json.loads(out)
    assert rc == 0 and [r["lat"] for r in env["results"]] == [14, 7, 7, None]
    assert env["summary"]["lat"]["max_pair"] == ["t:1", "t:13"]
    assert env["scan"]["blocks_skipped"] >= 0


def test_cli_match_exit_codes(bus_path):
    p = str(bus_path)
    base = ["match", p, "--a", "type == 'req'", "--b", "type == 'rsp'", "--on", "tag"]
    assert run(base)[0] == 0
    assert run(base + ["--unmatched", "a"])[0] == 0
    assert run(["match", p, "--a", "tag == 9", "--b", "tag == 8"])[0] == 1


def test_cli_export_text(corpus_path, tmp_path):
    out = tmp_path / "t.db"
    rc, text, _ = run(["export", str(corpus_path), "sqlite", "-o", str(out),
                       "-T", "axi_wr"])
    assert rc == 0 and text.startswith(f"wrote 5 txns to {out} (sqlite)")
    assert "views: txn_axi_rd" in text
    rc, text, _ = run(["export", str(corpus_path), "sqlite", "-o", str(out),
                       "--force", "addr == 0xdead", "--json"])
    assert rc == 1 and json.loads(text)["summary"]["txns"] == 0


def test_cli_hist_text_has_sparkline(corpus_path):
    rc, text, _ = run(["hist", str(corpus_path), "start", "--bin", "10ns"])
    last = text.splitlines()[-1]
    assert rc == 0 and last.startswith("-- start: 19 values in 8 bins of 10ns")
    assert last.endswith("▆▆▆▄█▂▄▂")
