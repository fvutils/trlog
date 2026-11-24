"""CLI polish from the docs refresh (docs_refresh_status.md #26 #27 #29 #31 #32).

- #26: --jsonl rows carry full stream paths; no stream_prefix anywhere.
- #27: no design-doc section references in --help text.
- #29: export keeps trace metadata (trace_meta) apart from export info (trace).
- #31: footers use the singular for one ("1 pair", "1 gap").
- #32: the match examples' trace is written by examples/query_bus.py.
"""

import json
import pathlib
import sqlite3
import subprocess
import sys

import pytest

import trlog.query as tq
from trlog import cli

ROOT = pathlib.Path(__file__).resolve().parents[2]
EXAMPLE = ROOT / "examples" / "query_bus.py"


@pytest.fixture(scope="module")
def bus_path(tmp_path_factory):
    p = tmp_path_factory.mktemp("polish") / "bus.trl"
    env = {"PYTHONPATH": str(ROOT / "python" / "src")}
    subprocess.run([sys.executable, str(EXAMPLE), str(p)], check=True,
                   env={**__import__("os").environ, **env})
    return p


def _run(capsys, *argv):
    cli.main(list(argv))
    return capsys.readouterr().out


# -- #26 ----------------------------------------------------------------------

@pytest.mark.parametrize("verb", ["find", "streams"])
def test_jsonl_rows_have_full_paths(corpus_path, capsys, verb):
    lines = _run(capsys, verb, str(corpus_path), "-s", "top.env.axi0.*",
                 "--jsonl").splitlines()
    rows, last = [json.loads(l) for l in lines[:-1]], json.loads(lines[-1])
    assert rows and all(r["stream"].startswith("top.env.axi0.") for r in rows)
    assert "stream_prefix" not in last and "results" not in last


def test_json_still_factors_prefix(corpus_path, capsys):
    env = json.loads(_run(capsys, "find", str(corpus_path), "-s", "top.env.axi0.*",
                          "--json"))
    assert env["stream_prefix"].startswith("top.env.axi0")
    assert not env["results"][0]["stream"].startswith("top.")


# -- #27 ----------------------------------------------------------------------

@pytest.mark.parametrize("verb", sorted(cli.VERBS))
def test_help_has_no_design_refs(verb, capsys):
    try:
        cli.main([verb, "--help"])
    except SystemExit:
        pass
    out = capsys.readouterr().out
    assert out and "§" not in out


# -- #29 ----------------------------------------------------------------------

def test_export_trace_meta_separate(corpus_path, tmp_path):
    out = tmp_path / "c.db"
    with tq.open(corpus_path) as t:
        t.export("sqlite", str(out))
    con = sqlite3.connect(out)
    trace = dict(con.execute("SELECT key, value FROM trace").fetchall())
    assert set(trace) == {"source", "query", "time_unit", "timescale_exp"}
    meta = dict(con.execute("SELECT key, value FROM trace_meta").fetchall())
    assert meta["tool"] == "query_corpus" and json.loads(meta["seed"]) == 1
    assert not set(meta) & {"query", "time_unit", "timescale_exp"}


# -- #31 ----------------------------------------------------------------------

def test_singular_footers(bus_path, capsys):
    out = _run(capsys, "match", str(bus_path), "--a", "type=='req' && tag==2",
               "--b", "type=='rsp'", "--on", "tag")
    assert "-- 1 pair;" in out
    out = _run(capsys, "gaps", str(bus_path), "-s", "top.bus.req", "--min", "5ns")
    assert "-- 1 gap over" in out
    out = _run(capsys, "hist", str(bus_path), "tag", "-s", "top.bus.mon")
    assert "3 values in 1 bin of" in out
    out = _run(capsys, "streams", str(bus_path), "-s", "top.bus.req")
    assert "-- 1 stream" in out and "1 streams" not in out


# -- #32 ----------------------------------------------------------------------

def test_example_bus_trace(bus_path):
    with tq.open(bus_path) as t:
        res = t.match("type == 'req'", "type == 'rsp'", on="tag")
        assert {(a.id, b.id) for a, b, _ in res.pairs} >= {(2, 11), (1, 12)}
