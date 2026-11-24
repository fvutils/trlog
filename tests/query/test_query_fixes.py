"""Query fixes from the docs refresh (docs_refresh_status.md #25 #28 #30).

- #25: ordering an enum field against a label compares enum values, not
  label strings, in both engines.
- #28: --max-bytes smaller than one row still returns one row, so paging
  makes progress.
- #30: a time window that is not a whole number of ticks is an error.
"""

import json

import pytest

import trlog.query as tq
from trlog import cli
from trlog.query import _cengine
from trlog.query._values import parse_window

ENGINES = ["python", pytest.param("c", marks=pytest.mark.skipif(
    _cengine.load() is None, reason="libtrl with trl_qscan not built"))]


@pytest.mark.parametrize("engine", ENGINES)
@pytest.mark.parametrize("label_expr, int_expr", [
    ("resp >= 'SLVERR'", "resp >= 2"),
    ("resp > 'EXOKAY'", "resp > 1"),
    ("resp < 'SLVERR'", "resp < 2"),
    ("resp <= 'OKAY'", "resp <= 0"),
    ("'SLVERR' <= resp", "2 <= resp"),
    ("resp in 'EXOKAY'..'DECERR'", "resp in 1..3"),
])
def test_enum_order_by_value(corpus_path, engine, label_expr, int_expr):
    with tq.open(corpus_path, engine=engine) as t:
        want = [x.ref for x in t.find(int_expr).txns]
        assert want, int_expr
        assert [x.ref for x in t.find(label_expr).txns] == want


@pytest.mark.parametrize("engine", ENGINES)
def test_enum_order_includes_decerr(corpus_path, engine):
    # DECERR (3) sorts before SLVERR (2) as a string; by value it is above it.
    with tq.open(corpus_path, engine=engine) as t:
        labels = {x.fields["resp"].label for x in t.find("resp >= 'SLVERR'").txns}
        assert labels == {"SLVERR", "DECERR"}


def _find_json(corpus_path, capsys, *extra):
    capsys.readouterr()
    cli.main(["find", str(corpus_path), "--json", *extra])
    return json.loads(capsys.readouterr().out)


def test_max_bytes_below_one_row_makes_progress(corpus_path, capsys):
    env = _find_json(corpus_path, capsys, "--max-bytes", "10")
    assert len(env["results"]) == 1
    assert env["next"] == "--offset 1"
    assert env["max_bytes_hit"] is True
    env2 = _find_json(corpus_path, capsys, "--max-bytes", "10", "--offset", "1")
    assert len(env2["results"]) == 1
    assert env2["results"][0] != env["results"][0]
    assert env2["next"] == "--offset 2"


def test_max_bytes_large_keeps_all(corpus_path, capsys):
    full = _find_json(corpus_path, capsys)
    env = _find_json(corpus_path, capsys, "--max-bytes", "10000000")
    assert env["results"] == full["results"]
    assert "max_bytes_hit" not in env


@pytest.mark.parametrize("text", ["1.5ps..", "..1.5ps", "@1.5ps", "@10ps±0.5ps",
                                  "0.0015ns..2ns"])
def test_window_fractional_tick_is_error(text):
    with pytest.raises(ValueError, match="whole number of ticks"):
        parse_window(text, -12)


@pytest.mark.parametrize("text, want", [
    ("1.5ns..2ns", (1500, 2000)), ("@3ps±1ps", (2, 4)), ("..0.002ns", (None, 2)),
    ("10..20", (10, 20)),
])
def test_window_whole_ticks_ok(text, want):
    assert parse_window(text, -12) == want


def test_window_fractional_cli_error(corpus_path, capsys):
    capsys.readouterr()
    assert cli.main(["find", str(corpus_path), "-t", "1.5ps..", "--json"]) == 2
    err = json.loads(capsys.readouterr().out)
    assert err["error"] == "bad_time"
    assert "whole number of ticks" in err["message"]
