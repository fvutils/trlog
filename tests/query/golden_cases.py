"""Golden CLI cases for the query corpus (query_cli_design.md §9).

Each case is ``(name, argv)``; ``TRACE`` in argv is replaced with the corpus
path. JSON cases (``--json``/``--jsonl`` in argv) are stored as ``NAME.json``,
the others as plain text ``NAME.txt`` (stdout, then stderr after a
``--- stderr`` line when non-empty), both preceded by an ``exit: N`` line.

Regenerate deliberately after reviewing an intended output change::

    PYTHONPATH=python/src:. python -m tests.query.golden_cases
"""

from __future__ import annotations

import contextlib
import io
import json
import pathlib
import re
import tempfile

GOLDEN_DIR = pathlib.Path(__file__).parent / "golden"

CASES = [
    # orient
    ("info", ["info", "TRACE", "--json"]),
    ("info_text", ["info", "TRACE"]),
    ("streams", ["streams", "TRACE", "--json"]),
    ("streams_tree", ["streams", "TRACE", "--tree"]),
    ("streams_glob", ["streams", "TRACE", "top.env.axi*"]),
    ("schema_sample", ["schema", "TRACE", "--sample", "10", "--json"]),
    ("schema_text", ["schema", "TRACE", "axi_rd", "msg", "--sample", "10"]),
    # find
    ("find_all", ["find", "TRACE", "--limit", "50", "--json"]),
    ("find_text", ["find", "TRACE"]),
    ("find_errors", ["find", "TRACE", "-T", "axi_rd,axi_wr", "resp != 'OKAY'",
                     "--with", "links,meta,parent", "--json"]),
    ("find_errors_text", ["find", "TRACE", "-T", "axi_rd,axi_wr", "resp != 'OKAY'",
                          "--with", "links,meta,parent,children"]),
    ("find_links_other_stream", ["find", "TRACE", "-s", "top.env.axi1.mon",
                                 "--with", "links"]),
    ("find_window", ["find", "TRACE", "-t", "40ns..50ns", "--json"]),
    ("find_window_early_stop", ["find", "TRACE", "-t", "..20ns", "--json"]),
    ("find_open", ["find", "TRACE", "open", "--json"]),
    ("find_page", ["find", "TRACE", "--limit", "3", "--offset", "2", "--json"]),
    ("find_summary_only", ["find", "TRACE", "--limit", "0", "--json"]),
    ("find_max_bytes", ["find", "TRACE", "--max-bytes", "900", "--json"]),
    ("find_jsonl", ["find", "TRACE", "-T", "axi_rd", "--jsonl"]),
    ("find_sort", ["find", "TRACE", "-T", "axi_rd,axi_wr", "--sort", "-dur,id",
                   "--limit", "4"]),
    ("find_last", ["find", "TRACE", "-T", "msg", "--last", "--json"]),
    ("find_parent", ["find", "TRACE", "parent.name == 'err_test'", "--json"]),
    ("find_meta", ["find", "TRACE", "meta[\"note\"] =~ /inj/", "--json"]),
    ("find_fields", ["find", "TRACE", "-s", "top.env.axi0.mon",
                     "addr in 0x4000..0x4010", "--fields", "addr,resp", "--json"]),
    ("find_count", ["find", "TRACE", "-T", "msg", "sev == 'ERROR'", "--count"]),
    ("find_none", ["find", "TRACE", "addr == 0xdead"]),
    # show / count
    ("show", ["show", "TRACE", "t:31", "t:3", "t:999", "--json"]),
    ("show_text", ["show", "TRACE", "t:30", "t:4"]),
    ("count_by", ["count", "TRACE", "--by", "stream,resp", "--json"]),
    ("count_by_text", ["count", "TRACE", "--by", "type,open"]),
    # errors
    ("err_unknown_field", ["find", "TRACE", "adr == 1", "--json"]),
    ("err_unknown_field_text", ["find", "TRACE", "adr == 1"]),
    ("err_unknown_enum", ["find", "TRACE", "resp == 'OKY'", "--json"]),
    ("err_type", ["find", "TRACE", "addr == '0x4000'", "--json"]),
    ("err_unknown_stream", ["find", "TRACE", "-s", "top.env.axi9*", "--json"]),
    ("err_stream_glob_eq", ["find", "TRACE", "stream == 'top.env.*'", "--json"]),
    ("err_unknown_type", ["find", "TRACE", "-T", "axi_write", "--json"]),
    ("err_bad_time", ["find", "TRACE", "-t", "10qq", "--json"]),
    ("err_syntax", ["find", "TRACE", "addr = 1", "--json"]),
    ("err_planned_verb", ["index", "TRACE", "--json"]),
    ("err_unknown_verb", ["fnd", "TRACE"]),
    ("err_bad_ref", ["show", "TRACE", "s:top.log", "--json"]),
    # navigation (Q2)
    ("find_nchildren", ["find", "TRACE", "nchildren > 0", "--with", "children"]),
    ("find_nlinks", ["find", "TRACE", "nlinks >= 1", "-s", "top.env.*", "--json"]),
    ("tree", ["tree", "TRACE", "t:10", "--json"]),
    ("tree_text", ["tree", "TRACE", "t:1", "--down", "all"]),
    ("tree_limit", ["tree", "TRACE", "t:1", "--down", "all", "--limit", "3", "--json"]),
    ("tree_depth_text", ["tree", "TRACE", "t:4", "--down", "0"]),
    ("links_up", ["links", "TRACE", "t:101", "--up", "all", "--json"]),
    ("links_text", ["links", "TRACE", "t:30"]),
    ("links_kind", ["links", "TRACE", "t:32", "--kind", "related", "--down", "all"]),
    ("links_none", ["links", "TRACE", "t:10"]),
    ("around_ref", ["around", "TRACE", "t:31", "-n", "2"]),
    ("around_time", ["around", "TRACE", "@45ns", "-n", "2", "--json"]),
    ("around_time_text", ["around", "TRACE", "@45ns", "-n", "2", "--with", "links"]),
    ("around_window", ["around", "TRACE", "t:31", "--window", "1ns", "--json"]),
    ("around_same_stream", ["around", "TRACE", "t:31", "--same-stream"]),
    ("active", ["active", "TRACE", "@65ns", "--json"]),
    ("active_text", ["active", "TRACE", "@43ns", "--with", "links"]),
    ("batch", ["batch", "TRACE", "--json"],
     "info\n# a comment\n\ntree t:31\nfind \"resp == 'SLVERR'\" --first\n"
     "links t:999\nfnd\nfind --nope\ncount --by type\n"),
    ("batch_text", ["batch", "TRACE"],
     "count --by open\ntrlog find TRACE -T msg --last\nhelp refs\n"
     "around t:31 -n 1 --same-stream\n"),
    ("err_tree_not_found", ["tree", "TRACE", "t:999", "--json"]),
    ("err_usage", ["find", "TRACE", "--nope", "--json"]),
    ("err_usage_text", ["tree", "TRACE", "t:1", "--down", "x"]),
    ("err_link_kind", ["links", "TRACE", "t:30", "--kind", "causes", "--json"]),
    ("err_around_same_stream_time", ["around", "TRACE", "@45ns", "--same-stream",
                                     "--json"]),
    ("help_agent", ["help", "agent"]),
    # analysis (Q3)
    ("stats_dur_by_type", ["stats", "TRACE", "dur", "--by", "type", "--json"]),
    ("stats_text", ["stats", "TRACE", "dur", "--by", "stream"]),
    ("stats_expr", ["stats", "TRACE", "addr & 0xff", "-T", "axi_rd,axi_wr",
                    "resp == 'OKAY'", "--json"]),
    ("err_stats_enum", ["stats", "TRACE", "resp", "--json"]),
    ("hist_start_text", ["hist", "TRACE", "start", "--bin", "10ns"]),
    ("hist_dur", ["hist", "TRACE", "dur", "-T", "axi_wr,axi_rd", "--json"]),
    ("hist_field_auto", ["hist", "TRACE", "len", "--bins", "4", "--json"]),
    ("err_hist_too_many_bins", ["hist", "TRACE", "start", "--bin", "1", "--json"]),
    ("gaps_text", ["gaps", "TRACE", "--min", "4ns"]),
    ("gaps_typed", ["gaps", "TRACE", "-s", "top.env.*", "-T", "axi_rd", "--min",
                    "4ns", "--json"]),
    ("gaps_window", ["gaps", "TRACE", "-t", "20ns..50ns", "--sort", "stream,start",
                     "--json"]),
    ("match_parent", ["match", "TRACE", "--a", "type == 'seq_item'", "--b",
                      "stream ~ 'top.env.axi*'", "--on", "a.id == b.parent",
                      "--json"]),
    ("match_parent_text", ["match", "TRACE", "--a", "type == 'seq_item'", "--b",
                           "stream ~ 'top.env.axi*'", "--on", "a.id == b.parent"]),
    ("match_unmatched_b", ["match", "TRACE", "--a", "type == 'seq_item'", "--b",
                           "stream ~ 'top.env.axi*'", "--on", "a.id == b.parent",
                           "--unmatched", "b", "--limit", "3", "--json"]),
    ("match_fifo_lat", ["match", "TRACE", "--a", "type == 'axi_wr'", "--b",
                        "type == 'msg'", "--sort", "-lat", "--json"]),
    ("err_match_on", ["match", "TRACE", "--a", "open", "--b", "open", "--on",
                      "a.x = b.y", "--json"]),
    ("err_match_usage", ["match", "TRACE", "--a", "open", "--json"]),
    ("batch_q3", ["batch", "TRACE", "--json"],
     "stats dur --by type\nhist start --bin 20ns\ngaps --min 10ns\n"
     "match --a \"type == 'axi_wr'\" --b \"type == 'axi_rd'\" --on addr\n"),
]


def is_json(argv) -> bool:
    return "--json" in argv or "--jsonl" in argv


def _normalize_json_line(line: str) -> str:
    try:
        obj = json.loads(line)
    except ValueError:
        return line
    if isinstance(obj, dict) and isinstance(obj.get("scan"), dict):
        obj["scan"]["ms"] = 0
    if isinstance(obj, dict) and isinstance(obj.get("info"), dict) \
            and "engine" in obj["info"]:
        obj["info"]["engine"] = "*"     # python or c: same output otherwise
    return json.dumps(obj, ensure_ascii=False, sort_keys=False)


def run_case(argv, trace_path, stdin: str = None) -> str:
    """Run the CLI in-process and return normalised output text.

    ``stdin`` (for ``batch``) may say TRACE for the trace path too.
    """
    import sys
    from trlog.cli import main
    real = [str(trace_path) if a == "TRACE" else a for a in argv]
    out, err = io.StringIO(), io.StringIO()
    saved = sys.stdin
    sys.stdin = io.StringIO((stdin or "").replace("TRACE", str(trace_path)))
    try:
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = main(real)
    finally:
        sys.stdin = saved
    text = out.getvalue()
    if is_json(argv):
        text = "\n".join(_normalize_json_line(l) for l in text.splitlines()) + "\n"
    if err.getvalue():
        text += "--- stderr\n" + err.getvalue()
    text = text.replace(str(trace_path), "TRACE")
    text = re.sub(r"(?m)^(engine\s+)(c|python)$", r"\1*", text)
    return f"exit: {rc}\n" + text


def golden_path(name, argv) -> pathlib.Path:
    return GOLDEN_DIR / (name + (".json" if is_json(argv) else ".txt"))


def main() -> None:
    from .corpus import build
    GOLDEN_DIR.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory() as d:
        trace = pathlib.Path(d) / "query_corpus.trl"
        build(trace)
        for name, argv, *stdin in CASES:
            p = golden_path(name, argv)
            p.write_text(run_case(argv, trace, *stdin), encoding="utf-8")
            print(f"wrote {p}")


if __name__ == "__main__":
    main()
