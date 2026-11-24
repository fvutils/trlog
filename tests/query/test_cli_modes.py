"""Output-mode selection (query_cli_design.md §3.6) and the module entry point."""

import io
import json
import os
import pathlib
import subprocess
import sys

from trlog import cli

REPO = pathlib.Path(__file__).resolve().parents[2]


class FakeTty(io.StringIO):
    def isatty(self):
        return True


def run(argv, stream):
    old = sys.stdout
    sys.stdout = stream
    try:
        return cli.main(argv)
    finally:
        sys.stdout = old


def test_tty_is_coloured_and_width_limited(corpus_path, monkeypatch):
    monkeypatch.setenv("COLUMNS", "60")
    out = FakeTty()
    assert run(["find", str(corpus_path), "-T", "axi_rd", "--limit", "2"], out) == 0
    text = out.getvalue()
    assert "\x1b[" in text
    for line in text.splitlines()[:2]:
        visible = __import__("re").sub(r"\x1b\[[0-9;]*m", "", line)
        assert len(visible) <= 60
        assert visible.endswith("…")


def test_plain_has_no_escapes_or_truncation(corpus_path):
    out = io.StringIO()
    run(["find", str(corpus_path), "-T", "axi_rd", "--limit", "2"], out)
    text = out.getvalue()
    assert "\x1b[" not in text and "…" not in text
    assert "bus=0xa0" in text


def test_color_flag_overrides_detection(corpus_path):
    out = io.StringIO()
    run(["find", str(corpus_path), "--limit", "1", "--color", "always"], out)
    assert "\x1b[" in out.getvalue()
    out = FakeTty()
    run(["find", str(corpus_path), "--limit", "1", "--color", "never"], out)
    assert "\x1b[" not in out.getvalue()


def test_json_only_when_selected_even_on_tty(corpus_path):
    out = FakeTty()
    run(["count", str(corpus_path), "--json"], out)
    env = json.loads(out.getvalue())
    assert env["summary"]["matched"] == 19
    out = FakeTty()
    run(["count", str(corpus_path)], out)
    assert out.getvalue().strip() == "19"


def test_python_m_trlog(corpus_path):
    env = dict(os.environ, PYTHONPATH=str(REPO / "python" / "src"))
    r = subprocess.run([sys.executable, "-m", "trlog", "find", str(corpus_path),
                        "open", "--json"], capture_output=True, text=True, env=env)
    assert r.returncode == 0, r.stderr
    assert [x["ref"] for x in json.loads(r.stdout)["results"]] == ["t:4", "t:32"]
    r = subprocess.run([sys.executable, "-m", "trlog", "find", str(corpus_path),
                        "addr == 0xdead"], capture_output=True, text=True, env=env)
    assert r.returncode == 1
    r = subprocess.run([sys.executable, "-m", "trlog"], capture_output=True,
                       text=True, env=env)
    assert r.returncode == 2 and "usage: trlog" in r.stdout


def test_help_topics(capsys):
    for topic in ([], ["expr"], ["refs"], ["json"], ["find"]):
        assert cli.main(["help"] + topic) == 0
    assert cli.main(["help", "nosuch"]) == 2
    assert "Filter expressions" in capsys.readouterr().out
