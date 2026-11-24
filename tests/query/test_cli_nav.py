"""CLI behaviour of the Q2 verbs that the golden files do not pin down."""

import io
import json
import sys

import pytest

from trlog import cli


def run(argv, stdin=""):
    out, err = io.StringIO(), io.StringIO()
    saved = sys.stdin, sys.stdout, sys.stderr
    sys.stdin, sys.stdout, sys.stderr = io.StringIO(stdin), out, err
    try:
        rc = cli.main(argv)
    finally:
        sys.stdin, sys.stdout, sys.stderr = saved
    return rc, out.getvalue(), err.getvalue()


def test_batch_exit_codes(corpus_path):
    p = str(corpus_path)
    assert run(["batch", p], "find addr == 0xdead\n")[0] == 1
    assert run(["batch", p], "find addr == 0xdead\ncount\n")[0] == 0
    assert run(["batch", p], "count\nfind adr == 1\n")[0] == 2
    assert run(["batch", p], "")[0] == 0


def test_batch_json_is_one_envelope_per_command(corpus_path):
    rc, out, _ = run(["batch", str(corpus_path), "--json"],
                     "find -T msg --jsonl --pretty\nshow t:30\nbatch\n")
    envs = [json.loads(l) for l in out.splitlines()]
    assert [e["seq"] for e in envs] == [1, 2, 3]
    assert envs[0]["cmd"] == "find" and envs[0]["summary"]["matched"] == 4
    # the graph built by show is reused by later commands
    assert envs[1]["cmd"] == "show"
    assert envs[2]["error"] == "usage" and "nested" in envs[2]["message"]
    assert rc == 2


def test_batch_reuses_graph(corpus_path):
    _, out, _ = run(["batch", str(corpus_path), "--json"], "tree t:10\nfind -T msg\n")
    envs = [json.loads(l) for l in out.splitlines()]
    assert "cached" not in envs[0]["scan"]
    assert envs[1]["scan"]["cached"] is True


def test_batch_text_errors_inline(corpus_path):
    rc, out, err = run(["batch", str(corpus_path)], "find adr == 1\ncount\n")
    assert err == ""
    lines = out.splitlines()
    assert lines[0] == "== [1] find adr == 1"
    assert lines[1].startswith("trlog: error: unknown field")
    assert "== [2] count" in lines and lines[-1] == "19"
    assert rc == 2


def test_batch_bad_trace(tmp_path):
    rc, out, _ = run(["batch", str(tmp_path / "nope.trl"), "--json"], "info\n")
    assert rc == 2 and json.loads(out)["error"] == "file_error"


@pytest.mark.parametrize("topic", ["agent", "batch", "refs", "tree", "links",
                                   "around", "active", "stats", "hist", "gaps",
                                   "match", "export"])
def test_help_topics(topic):
    rc, out, _ = run(["help", topic])
    assert rc == 0 and out.strip()


def test_help_agent_fits_a_page():
    _, out, _ = run(["help", "agent"])
    lines = out.splitlines()
    assert len(lines) <= 50 and max(len(l) for l in lines) <= 88


def test_links_up_down_defaults(corpus_path):
    def dists(*extra):
        _, out, _ = run(["links", str(corpus_path), "t:30", "--json", *extra])
        return [r["dist"] for r in json.loads(out)["results"]]
    assert dists() == [-1, 0, 1]
    assert dists("--up", "all") == [-1, 0]          # only --up: down = 0
    assert dists("--down", "2") == [0, 1]           # only --down: up = 0
    assert dists("--up", "1", "--down", "1") == [-1, 0, 1]


def test_usage_error_json(corpus_path):
    rc, out, err = run(["around", str(corpus_path), "--json"])
    assert rc == 2 and err == ""
    e = json.loads(out)
    assert e["error"] == "usage" and e["verb"] == "around"


def test_unquoted_expr_words_are_joined(corpus_path):
    rc, out, _ = run(["find", str(corpus_path), "addr", "==", "0x4000", "-T",
                      "axi_wr", "--json"])
    assert rc == 0
    env = json.loads(out)
    assert env["query"]["where"] == "addr == 0x4000"
    assert [r["ref"] for r in env["results"]] == ["t:10"]
