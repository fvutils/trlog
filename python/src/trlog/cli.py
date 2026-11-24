"""``trlog`` command-line front end.

    trlog <verb> TRACE [options] [EXPR]

Output is formatted text on a TTY, plain text otherwise, and JSON only with
``--json``/``--jsonl``. Exit status follows grep: 0 = matches,
1 = no matches, 2 = usage, expression or file error. The verbs run on
:class:`trlog.query.Trace`. Design background: query_cli_design.md §3-§4.
"""

from __future__ import annotations

import argparse
import difflib
import io
import os
import shlex
import shutil
import sys
from typing import Callable, Dict, List, Optional

from .query._engine import LINK_KINDS, ScanStats, Selection, Trace, Txn
from .query._expr import QueryError
from .query._format import (
    Style, json_dumps, show_block, stream_prefix, table, txn_json, txn_line_groups,
    txn_lines, value_json, value_text,
)
from .query._values import format_time, parse_time, parse_window

EXIT_MATCH, EXIT_NOMATCH, EXIT_ERROR = 0, 1, 2

DEFAULT_LIMIT = 20
BY_STREAM_CAP = 20


# ---------------------------------------------------------------------------
# Output
# ---------------------------------------------------------------------------

class Output:
    """Picks the output mode (§3.6) and writes results or errors."""

    def __init__(self, args: argparse.Namespace, stream=None, err=None) -> None:
        self.f = stream or sys.stdout
        self.err = err              # text-mode errors; None = sys.stderr
        self.json = bool(getattr(args, "json", False))
        self.jsonl = bool(getattr(args, "jsonl", False))
        self.pretty = bool(getattr(args, "pretty", False))
        tty = hasattr(self.f, "isatty") and self.f.isatty()
        color = getattr(args, "color", "auto")
        use_color = tty if color == "auto" else color == "always"
        width = shutil.get_terminal_size((120, 24)).columns if tty else None
        self.style = Style(use_color, width)

    @property
    def structured(self) -> bool:
        return self.json or self.jsonl

    def write(self, s: str = "") -> None:
        self.f.write(s + "\n")

    def lines(self, lines: List[str]) -> None:
        for l in lines:
            self.write(l)

    def emit(self, env: dict) -> None:
        self.write(json_dumps(env, pretty=self.pretty))

    def error(self, e: QueryError) -> None:
        if self.structured:
            self.emit(e.info)
            return
        info = e.info
        err = self.err or sys.stderr
        err.write(f"trlog: error: {info['message']}\n")
        if "pos" in info and isinstance(info["pos"], int) and info.get("expr"):
            err.write(f"  {info['expr']}\n  {' ' * info['pos']}^\n")
        if info.get("did_you_mean"):
            err.write(f"  did you mean: {', '.join(map(str, info['did_you_mean']))}\n")
        for key in ("valid_values", "known_types", "known_functions"):
            if info.get(key):
                err.write(f"  {key.replace('_', ' ')}: "
                          f"{', '.join(map(str, info[key]))}\n")
        if info.get("hint"):
            err.write(f"  hint: {info['hint']}\n")


# ---------------------------------------------------------------------------
# Argument parsing
# ---------------------------------------------------------------------------

def _add_output(p: argparse.ArgumentParser) -> None:
    g = p.add_argument_group("output")
    g.add_argument("--json", action="store_true", help="one JSON envelope")
    g.add_argument("--jsonl", action="store_true",
                   help="one JSON record per line (full stream paths), then a summary line")
    g.add_argument("--pretty", action="store_true", help="indent JSON output")
    g.add_argument("--color", choices=("auto", "always", "never"), default="auto")


def _add_selection(p: argparse.ArgumentParser, expr: bool = True,
                   window: bool = True) -> None:
    g = p.add_argument_group("selection")
    g.add_argument("-s", "--stream", action="append", default=[], metavar="GLOB",
                   help="stream path glob (repeatable, or comma-separated)")
    g.add_argument("-T", "--type", action="append", default=[], metavar="NAME",
                   help="transaction type (repeatable, or comma-separated)")
    if window:
        g.add_argument("-t", "--time", metavar="WINDOW",
                       help="time window: A..B, ..B, A.., @T or @T±W (units fs..s); "
                            "selects txns overlapping it")
    if expr:
        g.add_argument("-w", "--where", metavar="EXPR", help="filter expression")
        p.add_argument("expr", nargs="*", metavar="EXPR",
                       help="filter expression (see `trlog help expr`); several "
                            "words are joined, so quoting is only needed for "
                            "shell characters such as ' \" ( ) < > & |")


def _add_paging(p: argparse.ArgumentParser, limit: int = DEFAULT_LIMIT,
                sortable: bool = True) -> None:
    g = p.add_argument_group("paging")
    g.add_argument("--limit", type=int, default=limit,
                   help=f"max results (default {limit}; 0 = summary only)")
    g.add_argument("--offset", type=int, default=0)
    if sortable:
        g.add_argument("--sort", metavar="KEY[,-KEY]",
                       help="sort keys (default start,id); '-' for descending")
        g.add_argument("--first", action="store_true", help="first match only")
        g.add_argument("--last", action="store_true", help="last match only")
        g.add_argument("--nth", type=int, metavar="N", help="Nth match only (1-based)")


def _add_projection(p: argparse.ArgumentParser, default_with: str = "",
                    paged: bool = True) -> None:
    g = p.add_argument_group("projection")
    g.add_argument("--with", dest="with_", default=default_with,
                   metavar="links,meta,parent,end,children",
                   help="extra sections per record")
    g.add_argument("--fields", metavar="F1,F2", help="only these attribute fields")
    g.add_argument("--full-paths", action="store_true",
                   help="JSON: do not factor out a common stream prefix")
    if paged:
        g.add_argument("--max-bytes", type=int, default=0, metavar="N",
                       help="cap JSON output size; rows are dropped from the end, "
                            "but at least one row is always returned")
    g.add_argument("--max-field-bytes", type=int, default=64, metavar="N",
                   help="abbreviate bytes fields beyond N bytes (default 64)")


class _ArgParser(argparse.ArgumentParser):
    """Usage errors become a structured QueryError (JSON-able, exit 2)."""

    def error(self, message):
        verb = self.prog.split()[-1]
        raise QueryError("usage", message, verb=verb,
                         hint=f"see `trlog help {verb}`")


def _depth(text: str) -> Optional[int]:
    """--up/--down value: a level count or 'all' (None)."""
    if text.lower() in ("all", "*", "inf"):
        return None
    n = int(text)
    if n < 0:
        raise ValueError(text)
    return n


_depth.__name__ = "depth (N or all)"


def _parser(verb: str, desc: str) -> argparse.ArgumentParser:
    p = _ArgParser(prog=f"trlog {verb}", description=desc)
    p.add_argument("trace", metavar="TRACE", help="trace file (.trl)")
    return p


def build_parser(verb: str) -> argparse.ArgumentParser:
    if verb == "info":
        p = _parser(verb, "File facts: span, units, counts, metadata, next steps.")
    elif verb == "streams":
        p = _parser(verb, "Streams with txn counts, types, first/last time.")
        p.add_argument("glob", nargs="?", metavar="GLOB", help="stream path glob")
        p.add_argument("-s", "--stream", action="append", default=[], metavar="GLOB")
        p.add_argument("--tree", action="store_true", help="text: show as a hierarchy")
        _add_paging(p, limit=100, sortable=False)
    elif verb == "schema":
        p = _parser(verb, "Transaction types: fields, kinds, enum values.")
        p.add_argument("types", nargs="*", metavar="TYPE", help="type name or glob")
        p.add_argument("--sample", type=int, default=0, metavar="N",
                       help="add observed min/max/top values from N txns per type")
    elif verb == "find":
        p = _parser(verb, "List transactions matching a selection, with a summary.")
        _add_selection(p)
        _add_paging(p)
        _add_projection(p)
        p.add_argument("--count", action="store_true",
                       help="print only the match count (same as `count`)")
    elif verb == "show":
        p = _parser(verb, "Full records for txn refs (t:ID), with links, meta, "
                          "parent and children.")
        p.add_argument("refs", nargs="+", metavar="REF", help="t:ID or t:ID@TIME")
        _add_projection(p, default_with="links,meta,parent,end,children")
    elif verb == "count":
        p = _parser(verb, "Count matching transactions, optionally grouped.")
        _add_selection(p)
        p.add_argument("--by", metavar="KEY[,KEY]", default="",
                       help="group keys: stream, type, open, parent, meta[K], a field")
        _add_paging(p, limit=50, sortable=False)
    elif verb == "tree":
        p = _parser(verb, "Parent/child structure around a txn: its ancestors up "
                          "to the root and its descendants.")
        p.add_argument("ref", metavar="REF", help="t:ID")
        p.add_argument("--up", type=_depth, default=None, metavar="N|all",
                       help="ancestor levels (default all, i.e. up to the root)")
        p.add_argument("--down", type=_depth, default=1, metavar="N|all",
                       help="descendant levels (default 1)")
        p.add_argument("--limit", type=int, default=200,
                       help="max nodes (default 200); descendants are cut "
                            "breadth first")
        _add_projection(p, paged=False)
    elif verb == "links":
        p = _parser(verb, "Follow link edges (cause, related, ...) from a txn, "
                          "transitively. Up = towards causes (incoming edges), "
                          "down = towards effects (outgoing edges).")
        p.add_argument("ref", metavar="REF", help="t:ID")
        p.add_argument("--kind", action="append", default=[], metavar="K",
                       help=f"edge kinds to follow: {', '.join(LINK_KINDS)} "
                            "(repeatable or comma-separated; default all)")
        p.add_argument("--up", type=_depth, default=argparse.SUPPRESS,
                       metavar="N|all",
                       help="levels against edge direction (default 1, or 0 "
                            "when only --down is given)")
        p.add_argument("--down", type=_depth, default=argparse.SUPPRESS,
                       metavar="N|all",
                       help="levels along edge direction (default 1, or 0 "
                            "when only --up is given)")
        p.add_argument("--limit", type=int, default=200, help="max nodes (default 200)")
        _add_projection(p, paged=False)
    elif verb == "around":
        p = _parser(verb, "What happened near a txn or a time, merged across "
                          "streams in start order.")
        p.add_argument("anchor", metavar="REF|@TIME", help="t:ID or @TIME (e.g. @45ns)")
        p.add_argument("-n", type=int, default=5, metavar="N",
                       help="txns before and after the anchor (default 5)")
        p.add_argument("--window", metavar="W",
                       help="instead of -n: every txn overlapping the anchor "
                            "extended by W each side (e.g. 200ns); keep W well "
                            "below the trace span shown by `info`")
        p.add_argument("--same-stream", action="store_true",
                       help="only the anchor txn's stream")
        _add_selection(p, window=False)
        p.add_argument("--limit", type=int, default=50,
                       help="--window mode: max results (default 50)")
        p.add_argument("--offset", type=int, default=0)
        _add_projection(p, paged=False)
    elif verb == "active":
        p = _parser(verb, "Transactions in progress at an instant: started at or "
                          "before T and not yet ended (open, or end > T).")
        p.add_argument("at", metavar="@TIME", help="e.g. @45ns, 45ns or 45000 (ticks)")
        _add_selection(p, window=False)
        _add_paging(p)
        _add_projection(p)
    elif verb == "stats":
        p = _parser(verb, "n, min, max, mean, p50/p90/p99 of a numeric value over the "
                          "matching txns, with refs to the min and max txns.")
        p.add_argument("target", metavar="VALUE",
                       help="dur, start, end, a field, or a numeric expression "
                            "such as 'addr & 0xfff'")
        _add_selection(p)
        p.add_argument("--by", metavar="KEY[,KEY]", default="",
                       help="group keys: stream, type, open, parent, meta[K], a field")
        _add_paging(p, limit=50, sortable=False)
    elif verb == "hist":
        p = _parser(verb, "Histogram of a numeric value over the matching txns "
                          "(bins of equal width).")
        p.add_argument("target", metavar="VALUE",
                       help="start (throughput over time), dur, a field, or a "
                            "numeric expression")
        _add_selection(p)
        p.add_argument("--bin", metavar="W",
                       help="bin width; times take units (1us). Default: a 1/2/5 "
                            "width giving at most --bins bins")
        p.add_argument("--bins", type=int, default=20, metavar="N",
                       help="max bins when --bin is not given (default 20)")
    elif verb == "gaps":
        p = _parser(verb, "Idle intervals per stream: no matching txn in progress. "
                          "Searches the -t window, or the whole trace span, so "
                          "leading and trailing silence counts too.")
        _add_selection(p)
        p.add_argument("--min", metavar="W", default="0",
                       help="only gaps longer than W, e.g. 2us (default 0)")
        g = p.add_argument_group("paging")
        g.add_argument("--limit", type=int, default=DEFAULT_LIMIT,
                       help=f"max results (default {DEFAULT_LIMIT})")
        g.add_argument("--offset", type=int, default=0)
        g.add_argument("--sort", default="-dur", metavar="KEY[,-KEY]",
                       help="dur, start, end, stream (default -dur: longest first)")
    elif verb == "match":
        p = _parser(verb, "Pair B txns (e.g. responses) with A txns (requests) of "
                          "equal key; report pairs with lat = b.end - a.start, and "
                          "the unmatched ones.")
        p.add_argument("--a", required=True, metavar="EXPR", dest="a",
                       help="selects the A side, e.g. \"type == 'req'\"")
        p.add_argument("--b", required=True, metavar="EXPR", dest="b",
                       help="selects the B side")
        p.add_argument("--on", metavar="ON",
                       help="key: 'a.tag == b.tag && a.id == b.parent', or a bare "
                            "FIELD for a.FIELD == b.FIELD. Default: no key (pair "
                            "in order)")
        p.add_argument("--order", choices=("fifo", "any"), default="fifo",
                       help="fifo: oldest unmatched a that started at or before b "
                            "(default); any: the a whose start is nearest b's")
        p.add_argument("--within", metavar="W",
                       help="pair only when |b.start - a.start| <= W. In fifo "
                            "order an a too old for the current b is dropped, so "
                            "pairs can differ from a run without --within, not "
                            "just be fewer")
        p.add_argument("--unmatched", choices=("a", "b"),
                       help="list the unmatched A or B txns as full records "
                            "(fields etc.) instead of the pairs; without it the "
                            "JSON envelope carries their refs in `unmatched`")
        g = p.add_argument_group("selection")
        g.add_argument("-s", "--stream", action="append", default=[], metavar="GLOB",
                       help="stream path glob, applied to both sides")
        g.add_argument("-t", "--time", metavar="WINDOW",
                       help="time window, applied to both sides")
        g = p.add_argument_group("paging")
        g.add_argument("--limit", type=int, default=DEFAULT_LIMIT,
                       help=f"max results (default {DEFAULT_LIMIT})")
        g.add_argument("--offset", type=int, default=0)
        g.add_argument("--sort", default="start", metavar="KEY",
                       help="pairs: start (default), lat or -lat")
        _add_projection(p, paged=False)
    elif verb == "export":
        p = _parser(verb, "Write the matching txns to SQLite or Parquet for SQL "
                          "and Python analysis. Tables: txn(id, stream_id, type_id, "
                          "start, end, dur, parent, open), attr_<type>(id, one "
                          "column per field), link(src, tgt, kind, label), "
                          "meta(id, key, value), stream, type, trace. Every id is "
                          "the txn id, so attr_<type>.id = txn.id. SQLite also has "
                          "a txn_<type> view per type: id, stream (path), start, "
                          "end, dur, parent, open and the fields, already joined. "
                          "No sqlite3 CLI? Python's sqlite3 module reads it.")
        p.add_argument("format", choices=("sqlite", "parquet"), metavar="FORMAT",
                       help="sqlite (one file) or parquet (a directory; needs pyarrow)")
        p.add_argument("-o", "--out", required=True, metavar="OUT",
                       help="output file (sqlite) or directory (parquet)")
        p.add_argument("--force", action="store_true", help="replace OUT if it exists")
        _add_selection(p)
    else:
        raise KeyError(verb)
    _add_output(p)
    return p


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _csv(values) -> List[str]:
    out: List[str] = []
    for v in values or ():
        out.extend(x.strip() for x in v.split(",") if x.strip())
    return out


def _selection(trace: Trace, args) -> Selection:
    expr = " ".join(args.expr) if isinstance(args.expr, list) else args.expr
    expr = expr or None
    if args.where:
        if expr:
            expr = f"({args.where}) && ({expr})"
        else:
            expr = args.where
    window = (None, None)
    if getattr(args, "time", None):
        try:
            window = parse_window(args.time, trace.timescale_exp)
        except ValueError as e:
            raise QueryError("bad_time", str(e), value=args.time)
    try:
        return trace.select(expr, _csv(args.stream) or None, _csv(args.type) or None,
                            window)
    except QueryError as e:
        if "pos" in e.info:
            e.info.setdefault("expr", expr)
        raise


def _query_dict(trace: Trace, sel: Selection) -> dict:
    return sel.query_dict()


def _page(args, matched: int):
    """Resolve --first/--last/--nth/--limit/--offset into (offset, limit)."""
    if getattr(args, "first", False):
        return 0, 1
    if getattr(args, "last", False):
        return max(0, matched - 1), 1
    if getattr(args, "nth", None):
        return max(0, args.nth - 1), 1
    return max(0, args.offset), max(0, args.limit)


def _next_hint(offset: int, limit: int, returned: int, matched: int) -> Optional[str]:
    if offset + returned >= matched:
        return None
    if limit == 0:
        return f"--limit {DEFAULT_LIMIT}"
    return f"--offset {offset + returned}"


def _by_stream(counts: Dict[str, int]) -> dict:
    items = list(counts.items())
    if len(items) <= BY_STREAM_CAP:
        return {"by_stream": dict(items)}
    rest = items[BY_STREAM_CAP:]
    return {"by_stream": dict(items[:BY_STREAM_CAP]),
            "by_stream_omitted": {"streams": len(rest), "txns": sum(n for _, n in rest)}}


def _fit_bytes(env: dict, max_bytes: int, results_key: str = "results") -> dict:
    """Drop rows from the end until the encoded envelope fits (§3.5).

    At least one row is always kept, so ``next`` moves on even when a
    single row is larger than ``max_bytes``."""
    if not max_bytes:
        return env
    rows = env[results_key]
    while len(rows) > 1 and len(json_dumps(env).encode()) > max_bytes:
        rows.pop()
        s = env["summary"]
        s["returned"] = len(rows)
        s["truncated"] = True
        off = env.get("_offset", 0)
        env["next"] = f"--offset {off + len(rows)}"
        env["max_bytes_hit"] = True
    return env


def _footer(trace: Trace, matched: int, offset: int, returned: int,
            next_hint: Optional[str], span) -> str:
    ts = trace.timescale_exp
    s = f"-- {matched} matched"
    if returned < matched:
        s += f", showing {returned}"
        if offset:
            s += f" from offset {offset}"
        if next_hint:
            s += f" ({next_hint} for more)"
    if span:
        s += f"; {format_time(span[0], ts)} .. {format_time(span[1], ts)}"
    return s


# ---------------------------------------------------------------------------
# Verbs
# ---------------------------------------------------------------------------

def cmd_info(trace: Trace, args, out: Output) -> int:
    info = trace.info()
    stats: ScanStats = info.pop("_stats")
    path = args.trace
    hints = [f"trlog streams {path}",
             f"trlog schema {path}",
             f"trlog count {path} --by stream,type"]
    if info["types"]:
        tname = next(iter(trace.types.values())).name
        hints.append(f"trlog find {path} -T {tname} --limit 5")
    if info["open"]:
        hints.append(f"trlog find {path} open")
    if out.structured:
        env = {"cmd": "info", "time_unit": trace.time_unit, "info": info,
               "try": hints, "scan": stats.to_dict()}
        out.emit(env)
        return EXIT_MATCH
    st = out.style
    ts = trace.timescale_exp
    span = info["span"]
    rows = [
        ("file", info["file"]),
        ("version", info["version"]),
        ("time unit", f"{info['time_unit']} (10^{info['timescale_exp']} s)"),
        ("span", f"{format_time(span[0], ts)} .. {format_time(span[1], ts)}  "
                 f"({span[0]} .. {span[1]})" if span else "-"),
        ("txns", f"{info['txns']} ({info['open']} open)"),
        ("streams", str(info["streams"])),
        ("types", str(info["types"])),
        ("signals", str(info["signals"])),
        ("hierarchies", ", ".join(info["hierarchies"]) or "-"),
        ("blocks", f"{info['txn_blocks']} txn, "
                   f"{'-' if info['vc_blocks'] is None else info['vc_blocks']} vc"),
        ("index", ("yes" if info["index"] else "no")
         + ", query index: " + ("yes" if info["qindex"] else "no")),
        ("engine", info["engine"]),
    ]
    for k, v in info["metadata"].items():
        rows.append((f"meta {k}", value_text(v, None, ts)))
    wd = max(len(k) for k, _ in rows)
    for k, v in rows:
        out.write(st.c("key", k.ljust(wd)) + "  " + v)
    for w in info["warnings"]:
        out.write(st.c("warn", "warning: ") + w)
    out.write(st.c("head", "try next:"))
    for h in hints:
        out.write("  " + h)
    return EXIT_MATCH


def cmd_streams(trace: Trace, args, out: Output) -> int:
    globs = _csv(args.stream) + ([args.glob] if args.glob else [])
    rows, stats = trace.stream_summary(globs or None)
    offset, limit = max(0, args.offset), max(0, args.limit)
    page = rows[offset: offset + limit] if limit else []
    nxt = _next_hint(offset, limit, len(page), len(rows))
    if out.structured:
        env = {"cmd": "streams", "query": {"streams": globs} if globs else {},
               "time_unit": trace.time_unit,
               "summary": {"streams": len(rows), "returned": len(page),
                           "truncated": nxt is not None,
                           "txns": sum(r["count"] for r in rows)},
               "results": [_jsonable_meta(r) for r in page], "next": nxt,
               "scan": stats.to_dict()}
        if out.jsonl:
            _write_jsonl(out, env)
        else:
            out.emit(env)
        return EXIT_MATCH if rows else EXIT_NOMATCH
    st = out.style
    ts = trace.timescale_exp
    if args.tree:
        out.lines(_stream_tree(page, st))
    else:
        tab = [[r["stream"], r["decl"] or "-", str(r["count"]), str(r["open"]),
                "-" if r["first"] is None else format_time(r["first"], ts),
                "-" if r["last"] is None else format_time(r["last"], ts),
                ",".join(r["types"]) or "-"] for r in page]
        out.lines(table(["stream", "decl", "txns", "open", "first", "last", "types"],
                        tab, st, right=(2, 3, 4, 5)))
    s = f"-- {_n(len(rows), 'stream')}"
    if len(page) < len(rows):
        s += f", showing {len(page)}" + (f" ({nxt} for more)" if nxt else "")
    out.write(s)
    return EXIT_MATCH if rows else EXIT_NOMATCH


def _jsonable_meta(r: dict) -> dict:
    r = dict(r)
    r["metadata"] = {k: value_json(v, None) for k, v in r["metadata"].items()}
    return r


def _stream_tree(rows: List[dict], st: Style) -> List[str]:
    out: List[str] = []
    prev: List[str] = []
    for r in rows:
        segs = r["stream"].split(".")
        common = 0
        while common < min(len(prev), len(segs) - 1) and prev[common] == segs[common]:
            common += 1
        for d in range(common, len(segs) - 1):
            out.append("  " * d + segs[d])
        out.append("  " * (len(segs) - 1) + st.c("stream", segs[-1])
                   + f"  {r['count']} txns" + (f", {r['open']} open" if r["open"] else "")
                   + (f"  [{','.join(r['types'])}]" if r["types"] else ""))
        prev = segs[:-1]
    return out


def cmd_schema(trace: Trace, args, out: Output) -> int:
    rows, stats = trace.schema(args.types or None, sample=max(0, args.sample))
    by_name = {t.name: t for t in trace.types.values()}
    if out.structured:
        for r in rows:
            ti = by_name.get(r["type"])
            for fd in r["fields"]:
                obs = fd.get("observed")
                if obs:
                    fi = ti.field(fd["name"]) if ti else None
                    for k in ("min", "max"):
                        if k in obs:
                            obs[k] = value_json(obs[k], fi)
                    if "top" in obs:
                        obs["top"] = [[value_json(v, fi), n] for v, n in obs["top"]]
        env = {"cmd": "schema", "time_unit": trace.time_unit,
               "summary": {"types": len(rows)}, "results": rows,
               "scan": stats.to_dict()}
        out.emit(env)
        return EXIT_MATCH if rows else EXIT_NOMATCH
    st = out.style
    ts = trace.timescale_exp
    for i, r in enumerate(rows):
        if i:
            out.write()
        head = st.c("type", r["type"]) + st.c("dim", f"  (id {r['id']})")
        if "sampled" in r:
            head += st.c("dim", f"  sampled {r['sampled']}")
        out.write(head)
        ti = by_name.get(r["type"])
        tab = []
        for fd in r["fields"]:
            kind = fd["kind"] + (f" {fd['enum']}" if fd.get("enum") else "")
            extra = ""
            if fd.get("values") is not None:
                extra = "{" + ", ".join(f"{k}={v}" for k, v in fd["values"].items()) + "}"
            obs = fd.get("observed")
            if obs is not None:
                fi = ti.field(fd["name"]) if ti else None
                parts = []
                if "min" in obs:
                    parts.append(f"min={value_text(obs['min'], fi, ts)} "
                                 f"max={value_text(obs['max'], fi, ts)}")
                if obs.get("top"):
                    parts.append("top: " + " ".join(
                        f"{value_text(v, fi, ts)}({n})" for v, n in obs["top"]))
                    if obs.get("distinct", 0) > len(obs["top"]):
                        parts.append(f"({obs['distinct']} distinct)")
                if obs["missing"]:
                    parts.append(f"missing={obs['missing']}")
                extra = (extra + "  " if extra else "") + "  ".join(parts)
            tab.append(["  " + fd["name"], kind, fd.get("radix", "-"), extra])
        if tab:
            out.lines(table(["  field", "kind", "radix", ""], tab, st))
        else:
            out.write("  (no fields)")
    return EXIT_MATCH if rows else EXIT_NOMATCH


def _find_envelope(trace: Trace, args, txns: List[Txn], sel: Selection,
                   stats: ScanStats, cmd: str) -> dict:
    matched = len(txns)
    offset, limit = _page(args, matched)
    page = txns[offset: offset + limit] if limit else []
    with_ = _csv([args.with_])
    fields = _csv([args.fields]) if args.fields else None
    results = [txn_json(t, trace, with_, fields, args.max_field_bytes) for t in page]
    env: dict = {"cmd": cmd, "query": _query_dict(trace, sel),
                 "time_unit": trace.time_unit}
    span = None
    if txns:
        span = [min(t.start for t in txns),
                max(t.start if t.end is None else t.end for t in txns)]
    from collections import Counter
    summary = {"matched": matched, "returned": len(page),
               "truncated": offset + len(page) < matched, "span": span}
    summary.update(_by_stream(dict(Counter(t.stream for t in txns).most_common())))
    env["summary"] = summary
    if results and not args.full_paths:
        prefix = stream_prefix([r["stream"] for r in results])
        if prefix:
            env["stream_prefix"] = prefix
            for r in results:
                r["stream"] = r["stream"][len(prefix):]
    env["results"] = results
    env["next"] = _next_hint(offset, limit, len(page), matched)
    env["scan"] = stats.to_dict()
    env["_offset"] = offset
    return env


def cmd_find(trace: Trace, args, out: Output) -> int:
    sel = _selection(trace, args)
    if args.count:
        res = trace.count(by=(), selection=sel)
        return _emit_count(trace, args, out, res, cmd="find")
    with_ = _csv([args.with_])
    res = trace.find(sort=args.sort, want_links="links" in with_, selection=sel)
    return _emit_find(trace, args, out, res, "find")


def cmd_active(trace: Trace, args, out: Output) -> int:
    at = _parse_at(trace, args.at)
    sel = _selection(trace, args)
    res = trace.active(at, sort=args.sort, selection=sel)
    if "links" in _csv([args.with_]):
        _attach_links(trace, res.txns)
    return _emit_find(trace, args, out, res, "active", {"at": at})


def _emit_find(trace: Trace, args, out: Output, res, cmd: str,
               extra_query: Optional[dict] = None) -> int:
    """find-style listing (envelope or text rows) for a FindResult."""
    sel = res.selection
    with_ = _csv([args.with_])
    if "children" in with_:
        _attach_children(trace, res.txns)
    matched = res.matched
    if out.structured:
        env = _find_envelope(trace, args, res.txns, sel, res.stats, cmd)
        if extra_query:
            env["query"].update(extra_query)
        _fit_bytes(env, args.max_bytes)
        env.pop("_offset", None)
        if out.jsonl:
            _write_jsonl(out, env)
        else:
            out.emit(env)
        return EXIT_MATCH if matched else EXIT_NOMATCH
    offset, limit = _page(args, matched)
    page = res.txns[offset: offset + limit] if limit else []
    fields = _csv([args.fields]) if args.fields else None
    out.lines(txn_lines(page, trace, out.style, with_, fields, args.max_field_bytes))
    out.write(out.style.c("dim", _footer(trace, matched, offset, len(page),
                                         _next_hint(offset, limit, len(page), matched),
                                         res.span())))
    return EXIT_MATCH if matched else EXIT_NOMATCH


def _attach_children(trace: Trace, txns: List[Txn]) -> None:
    """Children over the whole trace (the cached graph: one full scan; §7.3)."""
    every = trace.graph()
    for t in txns:
        g = every.get(t.id)
        t.children = list(g.children) if g is not None else []


def _attach_links(trace: Trace, txns: List[Txn]) -> None:
    every = trace.graph()
    for t in txns:
        g = every.get(t.id)
        if g is not None and g is not t:
            t.links = list(g.links)


def cmd_count(trace: Trace, args, out: Output) -> int:
    sel = _selection(trace, args)
    res = trace.count(by=_csv([args.by]), selection=sel)
    return _emit_count(trace, args, out, res, cmd="count")


def _emit_count(trace: Trace, args, out: Output, res, cmd: str) -> int:
    groups = res.groups
    offset = max(0, getattr(args, "offset", 0))
    limit = max(0, getattr(args, "limit", 50))
    if not res.keys:
        page, offset, limit = groups, 0, len(groups)
    else:
        page = groups[offset: offset + limit] if limit else []
    nxt = _next_hint(offset, limit, len(page), len(groups)) if res.keys else None
    if out.structured:
        results = []
        for key, n in page:
            row = {k: value_json(v, None) for k, v in zip(res.keys, key)}
            row["count"] = n
            results.append(row)
        env = {"cmd": "count",
               "query": _query_dict(trace, res.selection), "time_unit": trace.time_unit,
               "summary": {"matched": res.matched, "groups": len(groups),
                           "returned": len(page),
                           "truncated": nxt is not None},
               "keys": res.keys, "results": results, "next": nxt,
               "scan": res.stats.to_dict()}
        out.emit(env)
        return EXIT_MATCH if res.matched else EXIT_NOMATCH
    if not res.keys:
        out.write(str(res.matched))
        return EXIT_MATCH if res.matched else EXIT_NOMATCH
    ts = trace.timescale_exp
    tab = [[value_text(v, None, ts) for v in key] + [str(n)] for key, n in page]
    out.lines(table(res.keys + ["count"], tab, out.style, right=(len(res.keys),)))
    s = f"-- {res.matched} matched in {_n(len(groups), 'group')}"
    if len(page) < len(groups):
        s += f", showing {len(page)}" + (f" ({nxt} for more)" if nxt else "")
    out.write(out.style.c("dim", s))
    return EXIT_MATCH if res.matched else EXIT_NOMATCH


def parse_ref(text: str) -> int:
    """``t:1234``, ``t:1234@15200000`` or ``1234`` -> txn id."""
    t = text.strip()
    if t.startswith(("s:", "v:", "@")):
        raise QueryError("bad_ref", f"'{text}' is not a transaction ref", ref=text,
                         hint="show takes t:ID refs; use `trlog streams` for s:, "
                              "and value/around for v: and @TIME (later phases)")
    if t.startswith("t:"):
        t = t[2:]
    t = t.split("@", 1)[0]
    try:
        return int(t, 0)
    except ValueError:
        raise QueryError("bad_ref", f"cannot parse ref '{text}'", ref=text,
                         hint="refs look like t:1234 (as printed by find)")


def cmd_show(trace: Trace, args, out: Output) -> int:
    ids = [parse_ref(r) for r in args.refs]
    found, missing, stats = trace.show(ids)
    with_ = _csv([args.with_])
    fields = _csv([args.fields]) if args.fields else None
    if out.structured:
        env = {"cmd": "show", "time_unit": trace.time_unit,
               "summary": {"requested": len(ids), "found": len(found),
                           "not_found": [f"t:{i}" for i in missing]},
               "results": [txn_json(t, trace, with_, fields, args.max_field_bytes)
                           for t in found],
               "scan": stats.to_dict()}
        out.emit(env)
        return EXIT_MATCH if found else EXIT_NOMATCH
    for i, t in enumerate(found):
        if i:
            out.write()
        out.lines(show_block(t, trace, out.style, args.max_field_bytes))
    for i in missing:
        out.write(out.style.c("warn", f"-- t:{i}: not found"))
    return EXIT_MATCH if found else EXIT_NOMATCH


# -- navigation (Q2) ---------------------------------------------------------

def _parse_at(trace: Trace, text: str) -> int:
    try:
        return int(parse_time(text[1:] if text.startswith("@") else text,
                              trace.timescale_exp))
    except ValueError as e:
        raise QueryError("bad_time", str(e), value=text,
                         hint="times look like @45ns, @1.5us or @45000 (ticks)")


def _write_jsonl(out: Output, env: dict) -> None:
    """One line per row, then the envelope without results. Each line must
    stand alone, so rows carry full stream paths (no stream_prefix)."""
    prefix = env.pop("stream_prefix", "")
    for r in env["results"]:
        if prefix and "stream" in r:
            r = dict(r, stream=prefix + r["stream"])
        out.write(json_dumps(r))
    out.write(json_dumps({k: v for k, v in env.items() if k != "results"}))


def _strip_prefix(results: List[dict], args, env: dict) -> None:
    if results and not getattr(args, "full_paths", False):
        prefix = stream_prefix([r["stream"] for r in results])
        if prefix:
            env["stream_prefix"] = prefix
            for r in results:
                r["stream"] = r["stream"][len(prefix):]


def _proj(args):
    with_ = _csv([args.with_])
    fields = _csv([args.fields]) if args.fields else None
    return with_, fields


def cmd_tree(trace: Trace, args, out: Output) -> int:
    res = trace.tree(parse_ref(args.ref), up=args.up, down=args.down,
                     limit=max(1, args.limit))
    with_, fields = _proj(args)
    shown = {t.id for _, t in res.nodes}
    nxt = None
    if res.truncated:
        nxt = f"--limit {args.limit * 2}"
    elif res.hidden_children and args.down is not None:
        nxt = f"--down {args.down + 1}"
    if out.structured:
        results = []
        for d, t in res.nodes:
            r = txn_json(t, trace, with_, fields, args.max_field_bytes)
            r["depth"] = d
            r["parent"] = None if t.parent is None else f"t:{t.parent}"
            r["nchildren"] = len(t.children or ())
            results.append(r)
        env = {"cmd": "tree", "focus": res.focus.ref, "root": res.root.ref,
               "time_unit": trace.time_unit,
               "query": {"up": _depth_json(args.up), "down": _depth_json(args.down),
                         "limit": args.limit},
               "summary": {"nodes": len(results), "truncated": res.truncated,
                           "hidden_children": res.hidden_children}}
        if res.dangling_parent is not None:
            env["summary"]["dangling_parent"] = f"t:{res.dangling_parent}"
        _strip_prefix(results, args, env)
        env["results"] = results
        env["next"] = nxt
        env["scan"] = res.stats.to_dict()
        out.emit(env)
        return EXIT_MATCH
    top = min(d for d, _ in res.nodes)
    txns = [t for _, t in res.nodes]
    pre = [[">" if t is res.focus else ""] for t in txns]
    refs = ["  " * (d - top) + t.ref for d, t in res.nodes]
    post = []
    for d, t in res.nodes:
        hid = sum(1 for c in (t.children or ()) if c not in shown)
        post.append(f"[+{hid} children]" if hid and d >= 0 else "")
    out.lines(txn_lines(txns, trace, out.style, with_, fields, args.max_field_bytes,
                        pre=pre, ref_text=refs, post=post))
    s = f"-- {_n(len(txns), 'node')}; root {res.root.ref}"
    if res.dangling_parent is not None:
        s += f" (its parent t:{res.dangling_parent} is not in the trace)"
    if res.hidden_children:
        s += f"; {_n(res.hidden_children, 'descendant')} not shown"
    if nxt:
        s += f" ({nxt} for more)"
    out.write(out.style.c("dim", s))
    return EXIT_MATCH


def _n(n: int, word: str) -> str:
    return f"{n} {word}" + ("" if n == 1 else "s")


def _depth_json(v: Optional[int]):
    return "all" if v is None else v


def cmd_links(trace: Trace, args, out: Output) -> int:
    # --up/--down default to SUPPRESS, so an absent attribute = not given
    up_given, down_given = hasattr(args, "up"), hasattr(args, "down")
    up = args.up if up_given else (0 if down_given else 1)
    down = args.down if down_given else (0 if up_given else 1)
    kinds = _csv(args.kind) or None
    res = trace.links(parse_ref(args.ref), kinds=kinds, up=up, down=down,
                      limit=max(1, args.limit))
    with_, fields = _proj(args)
    nxt = f"--limit {args.limit * 2}" if res.truncated else None
    edges = [{"from": f"t:{a}", "to": f"t:{b}", "kind": k,
              **({"label": lb} if lb else {})} for a, b, k, lb in res.edges]
    if out.structured:
        results = []
        for d, t in res.nodes:
            r = txn_json(t, trace, with_, fields, args.max_field_bytes)
            r["dist"] = d
            results.append(r)
        env = {"cmd": "links", "focus": res.focus.ref, "time_unit": trace.time_unit,
               "query": {"kinds": kinds or list(LINK_KINDS), "up": _depth_json(up),
                         "down": _depth_json(down), "limit": args.limit},
               "summary": {"nodes": len(results), "edges": len(edges),
                           "truncated": res.truncated}}
        if res.missing:
            env["summary"]["missing"] = [f"t:{i}" for i in res.missing]
        _strip_prefix(results, args, env)
        env["results"] = results
        env["edges"] = edges
        env["next"] = nxt
        env["scan"] = res.stats.to_dict()
        out.emit(env)
        return EXIT_MATCH if edges else EXIT_NOMATCH
    txns = [t for _, t in res.nodes]
    pre = [[f"{d:+d}" if d else "0"] for d, _ in res.nodes]
    out.lines(txn_lines(txns, trace, out.style, with_, fields, args.max_field_bytes,
                        pre=pre))
    if edges:
        out.write(out.style.c("head", "edges"))
        rows = [[e["from"], "->", e["to"], e["kind"], e.get("label", "")] for e in edges]
        w = [max(len(r[i]) for r in rows) for i in range(5)]
        for r in rows:
            out.write("  " + "  ".join(c.ljust(w[i]) for i, c in enumerate(r)).rstrip())
    s = (f"-- {_n(len(txns), 'node')}, {_n(len(edges), 'edge')} "
         "(- = causes/up, + = effects/down)")
    if res.missing:
        s += f"; not in trace: {' '.join(f't:{i}' for i in res.missing)}"
    if nxt:
        s += f"; truncated ({nxt} for more)"
    out.write(out.style.c("dim", s))
    return EXIT_MATCH if edges else EXIT_NOMATCH


def cmd_around(trace: Trace, args, out: Output) -> int:
    a = args.anchor.strip()
    anchor = at = None
    if a.startswith("@"):
        at = _parse_at(trace, a)
    else:
        anchor = parse_ref(a)
    window = None
    if args.window:
        try:
            window = int(parse_time(args.window, trace.timescale_exp))
        except ValueError as e:
            raise QueryError("bad_time", str(e), value=args.window)
    sel = _selection(trace, args)
    res = trace.around(anchor=anchor, at=at, n=max(0, args.n), window=window,
                       same_stream=args.same_stream, selection=sel)
    with_, fields = _proj(args)
    if "links" in with_:
        _attach_links(trace, res.txns)
    if "children" in with_:
        _attach_children(trace, res.txns)
    txns = res.txns
    if window is not None:
        matched = len(txns)
        offset, limit = max(0, args.offset), max(0, args.limit)
        txns = txns[offset: offset + limit] if limit else []
        nxt = _next_hint(offset, limit, len(txns), matched)
    else:
        nxt = f"-n {max(1, args.n) * 2}" if res.more_before or res.more_after else None
    anchor_json = {"time": res.at}
    if res.focus is not None:
        anchor_json = {"ref": res.focus.ref, "time": res.at}
    if out.structured:
        results = []
        for t in txns:
            r = txn_json(t, trace, with_, fields, args.max_field_bytes)
            r["dt"] = t.start - res.at
            if t is res.focus:
                r["anchor"] = True
            results.append(r)
        q = res.selection.query_dict()
        q.pop("time", None)
        q.update({"same_stream": args.same_stream})
        if window is not None:
            q["window"] = window
            summary = {"matched": matched, "returned": len(txns),
                       "truncated": nxt is not None,
                       "span": list(res.selection.window)}
        else:
            q["n"] = args.n
            nb = res.n_before
            summary = {"returned": len(txns), "before": nb,
                       "after": len(txns) - nb - (1 if res.focus else 0),
                       "more_before": res.more_before, "more_after": res.more_after}
        env = {"cmd": "around", "anchor": anchor_json, "time_unit": trace.time_unit,
               "query": q, "summary": summary}
        _strip_prefix(results, args, env)
        env["results"] = results
        env["next"] = nxt
        env["scan"] = res.stats.to_dict()
        out.emit(env)
        return EXIT_MATCH if txns else EXIT_NOMATCH
    ts = trace.timescale_exp
    pre = []
    for t in txns:
        dt = t.start - res.at
        dts = "0" if dt == 0 else ("+" if dt > 0 else "-") + format_time(abs(dt), ts,
                                                                         strip=True)
        pre.append([">" if t is res.focus else "", dts])
    groups = txn_line_groups(txns, trace, out.style, with_, fields,
                             args.max_field_bytes, pre=pre)
    if res.focus is None and window is None and res.n_before is not None:
        # mark the anchor instant between the rows before and after it
        groups.insert(res.n_before,
                      [out.style.c("warn", f"-- @{format_time(res.at, ts)}")])
    out.lines([l for g in groups for l in g])
    if window is not None:
        lo, hi = res.selection.window
        s = (f"-- {matched} overlapping {format_time(lo, ts)} .. "
             f"{format_time(hi, ts)}")
        if len(txns) < matched:
            s += f", showing {len(txns)}" + (f" ({nxt} for more)" if nxt else "")
    else:
        s = f"-- {res.n_before} before, " \
            f"{len(txns) - res.n_before - (1 if res.focus else 0)} after " \
            f"{res.focus.ref if res.focus else '@' + format_time(res.at, ts)}"
        more = [w for w, f in (("before", res.more_before), ("after", res.more_after))
                if f]
        if more:
            s += f"; more {' and '.join(more)} ({nxt})"
    out.write(out.style.c("dim", s))
    return EXIT_MATCH if txns else EXIT_NOMATCH


# -- analysis (Q3) -----------------------------------------------------------

def _num_or_time(trace: Trace, text: str, what: str):
    """A width: a time with a unit (2us), or a plain number (ticks / value)."""
    t = text.strip()
    try:
        if t[-1:].isalpha() and not t.lower().startswith("0x"):
            return parse_time(t, trace.timescale_exp)
        try:
            return int(t, 0)
        except ValueError:
            v = float(t)
            return int(v) if v.is_integer() else v
    except (ValueError, IndexError) as e:
        raise QueryError("bad_value", f"bad {what} value {text!r}", value=text,
                         hint="use a number, or a time with a unit such as 2us")


def _num_json(v, is_time: bool, digits: int = 3):
    if v is None:
        return None
    if isinstance(v, float):
        if v.is_integer() and abs(v) < 2 ** 53:
            return int(v)
        return round(v, digits)
    return value_json(v, None)


def _num_text(v, is_time: bool, ts: int) -> str:
    if v is None:
        return "-"
    if is_time:
        return format_time(v, ts, strip=True)
    if isinstance(v, float):
        return f"{v:.6g}"
    return str(v)


PCTS = (50, 90, 99)


def cmd_stats(trace: Trace, args, out: Output) -> int:
    sel = _selection(trace, args)
    res = trace.stats(args.target, by=_csv([args.by]), selection=sel)
    groups = res.groups
    offset, limit = max(0, args.offset), max(0, args.limit)
    page = groups[offset: offset + limit] if limit else []
    nxt = _next_hint(offset, limit, len(page), len(groups))
    n_total = sum(s.n for _, s in groups)
    n_missing = sum(s.missing for _, s in groups)
    if out.structured:
        rows = []
        for key, s in page:
            row = {k: value_json(v, None) for k, v in zip(res.keys, key)}
            row.update({"n": s.n, "missing": s.missing,
                        "min": _num_json(s.min, res.is_time),
                        "max": _num_json(s.max, res.is_time),
                        "mean": _num_json(s.mean, res.is_time)})
            for p in PCTS:
                row[f"p{p}"] = _num_json(s.pct.get(p), res.is_time)
            row["min_ref"] = s.min_txn.ref if s.min_txn is not None else None
            row["max_ref"] = s.max_txn.ref if s.max_txn is not None else None
            rows.append(row)
        q = _query_dict(trace, sel)
        q["value"] = args.target
        env = {"cmd": "stats", "query": q, "time_unit": trace.time_unit,
               "value_is_time": res.is_time,
               "summary": {"matched": res.matched, "n": n_total,
                           "missing": n_missing, "groups": len(groups),
                           "returned": len(page), "truncated": nxt is not None},
               "keys": res.keys, "results": rows, "next": nxt,
               "scan": res.stats.to_dict()}
        out.emit(env)
        return EXIT_MATCH if n_total else EXIT_NOMATCH
    ts = trace.timescale_exp
    heads = res.keys + ["n", "missing", "min", "p50", "p90", "p99", "max", "mean",
                        "min_ref", "max_ref"]
    tab = []
    for key, s in page:
        tab.append([value_text(v, None, ts) for v in key]
                   + [str(s.n), str(s.missing)]
                   + [_num_text(v, res.is_time, ts)
                      for v in (s.min, s.pct.get(50), s.pct.get(90), s.pct.get(99),
                                s.max, s.mean)]
                   + [s.min_txn.ref if s.min_txn else "-",
                      s.max_txn.ref if s.max_txn else "-"])
    nk = len(res.keys)
    out.lines(table(heads, tab, out.style, right=range(nk, nk + 8)))
    s = f"-- {args.target}: {_n(n_total, 'value')} from {res.matched} matched"
    if n_missing:
        s += f" ({n_missing} without a value)"
    if res.keys:
        s += f", {len(groups)} groups"
        if len(page) < len(groups):
            s += f", showing {len(page)}" + (f" ({nxt} for more)" if nxt else "")
    out.write(out.style.c("dim", s))
    return EXIT_MATCH if n_total else EXIT_NOMATCH


SPARK = "▁▂▃▄▅▆▇█"


def cmd_hist(trace: Trace, args, out: Output) -> int:
    sel = _selection(trace, args)
    width = _num_or_time(trace, args.bin, "--bin") if args.bin else None
    if args.bins < 1:
        raise QueryError("bad_value", "--bins must be at least 1", value=args.bins)
    res = trace.hist(args.target, bin=width, max_bins=args.bins, selection=sel)
    if out.structured:
        q = _query_dict(trace, sel)
        q["value"] = args.target
        env = {"cmd": "hist", "query": q, "time_unit": trace.time_unit,
               "value_is_time": res.is_time,
               "summary": {"matched": res.matched, "n": res.n, "missing": res.missing,
                           "min": _num_json(res.min, res.is_time),
                           "max": _num_json(res.max, res.is_time),
                           "bins": len(res.counts),
                           "peak": max(res.counts) if res.counts else 0},
               "bin": _num_json(res.bin, res.is_time),
               "results": [{"lo": _num_json(res.edges(k)[0], res.is_time),
                            "hi": _num_json(res.edges(k)[1], res.is_time),
                            "count": c} for k, c in enumerate(res.counts)],
               "scan": res.stats.to_dict()}
        out.emit(env)
        return EXIT_MATCH if res.n else EXIT_NOMATCH
    ts = trace.timescale_exp
    if not res.counts:
        out.write(out.style.c("dim", f"-- {args.target}: no values "
                                     f"({res.matched} matched, {res.missing} without "
                                     "a value)"))
        return EXIT_NOMATCH
    peak = max(res.counts)
    tab = []
    for k, c in enumerate(res.counts):
        lo, hi = res.edges(k)
        bar = "#" * (0 if not c else max(1, round(40 * c / peak)))
        tab.append([_num_text(lo, res.is_time, ts), _num_text(hi, res.is_time, ts),
                    str(c), bar])
    out.lines(table(["lo", "hi", "count", ""], tab, out.style, right=(0, 1, 2)))
    spark = "".join(" " if not c else SPARK[min(7, (c * 8 - 1) // peak)]
                    for c in res.counts)
    s = (f"-- {args.target}: {_n(res.n, 'value')} in {_n(len(res.counts), 'bin')} of "
         f"{_num_text(res.bin, res.is_time, ts)}")
    if res.missing:
        s += f"; {res.missing} without a value"
    out.write(out.style.c("dim", s) + "  " + spark)
    return EXIT_MATCH if res.n else EXIT_NOMATCH


SILENT_CAP = 20


def cmd_gaps(trace: Trace, args, out: Output) -> int:
    sel = _selection(trace, args)
    min_gap = _num_or_time(trace, args.min, "--min")
    res = trace.gaps(min=min_gap, sort=args.sort, selection=sel)
    gs = res.gaps
    offset, limit = max(0, args.offset), max(0, args.limit)
    page = gs[offset: offset + limit] if limit else []
    nxt = _next_hint(offset, limit, len(page), len(gs))
    from collections import Counter
    by_stream = dict(Counter(g.stream for g in gs).most_common())
    if out.structured:
        rows = [{"stream": g.stream, "start": g.start, "end": g.end, "dur": g.dur,
                 "prev": g.prev.ref if g.prev else None,
                 "next": g.next.ref if g.next else None} for g in page]
        q = _query_dict(trace, sel)
        q["min"] = min_gap
        summary = {"gaps": len(gs), "returned": len(page),
                   "truncated": nxt is not None, "range": list(res.range),
                   "streams": res.streams}
        summary.update(_by_stream(by_stream))
        summary["silent_streams"] = res.silent[:SILENT_CAP]
        if len(res.silent) > SILENT_CAP:
            summary["silent_streams_omitted"] = len(res.silent) - SILENT_CAP
        env = {"cmd": "gaps", "query": q, "time_unit": trace.time_unit,
               "summary": summary}
        _strip_prefix(rows, args, env)
        env.update({"results": rows, "next": nxt, "scan": res.stats.to_dict()})
        out.emit(env)
        return EXIT_MATCH if gs else EXIT_NOMATCH
    ts = trace.timescale_exp
    tab = [[g.stream, format_time(g.start, ts), format_time(g.end, ts),
            format_time(g.dur, ts, strip=True),
            g.prev.ref if g.prev else "(start)",
            g.next.ref if g.next else "(end)"] for g in page]
    out.lines(table(["stream", "from", "to", "idle", "prev", "next"], tab,
                    out.style, right=(1, 2, 3)))
    s = (f"-- {_n(len(gs), 'gap')} over {format_time(min_gap, ts, strip=True)} in "
         f"{format_time(res.range[0], ts)} .. {format_time(res.range[1], ts)}")
    if len(page) < len(gs):
        s += f", showing {len(page)}" + (f" ({nxt} for more)" if nxt else "")
    if res.silent:
        names = ", ".join(res.silent[:5]) + (", ..." if len(res.silent) > 5 else "")
        s += f"; {len(res.silent)} streams with no matching txn: {names}"
    out.write(out.style.c("dim", s))
    return EXIT_MATCH if gs else EXIT_NOMATCH


def _match_sel(trace: Trace, expr: str, args) -> Selection:
    window = (None, None)
    if args.time:
        try:
            window = parse_window(args.time, trace.timescale_exp)
        except ValueError as e:
            raise QueryError("bad_time", str(e), value=args.time)
    try:
        return trace.select(expr, _csv(args.stream) or None, None, window)
    except QueryError as e:
        if "pos" in e.info:
            e.info.setdefault("expr", expr)
        raise


def _lat_summary(lat, ts_json=True) -> dict:
    d = {"n": lat.n, "missing": lat.missing}
    if lat.n:
        d.update({"min": lat.min, "p50": lat.pct[50], "p90": lat.pct[90],
                  "p99": lat.pct[99], "max": lat.max,
                  "mean": _num_json(lat.mean, True),
                  "min_pair": [lat.min_txn[0].ref, lat.min_txn[1].ref],
                  "max_pair": [lat.max_txn[0].ref, lat.max_txn[1].ref]})
    return d


def cmd_match(trace: Trace, args, out: Output) -> int:
    from .query import _analysis
    terms = _analysis.parse_on(args.on)
    within = _num_or_time(trace, args.within, "--within") if args.within else None
    sel_a = _match_sel(trace, args.a, args)
    sel_b = _match_sel(trace, args.b, args)
    res = _analysis.match(trace, sel_a, sel_b, terms, args.order, within)
    pairs = res.pairs
    sort = (args.sort or "start").strip()
    if sort.lstrip("-") not in ("start", "lat"):
        raise QueryError("bad_sort", f"cannot sort pairs by '{sort}'", name=sort,
                         valid_values=["start", "lat", "-lat"])
    if sort.lstrip("-") == "lat":
        sign = -1 if sort.startswith("-") else 1

        def lat_key(p):     # pairs whose b is still open sort last either way
            lat = _analysis.lat_of(p[0], p[1])
            return (lat is None, sign * (lat or 0), p[0].start, p[0].id)
        pairs = sorted(pairs, key=lat_key)
    elif sort.startswith("-"):
        pairs = list(reversed(pairs))
    rows_src = (res.unmatched_a if args.unmatched == "a" else
                res.unmatched_b if args.unmatched == "b" else pairs)
    offset, limit = max(0, args.offset), max(0, args.limit)
    page = rows_src[offset: offset + limit] if limit else []
    nxt = _next_hint(offset, limit, len(page), len(rows_src))
    keys = [f"{x}" if x == y else f"{x}={y}" for x, y in res.on]
    with_, fields = _proj(args)
    if out.structured:
        q = {"a": args.a, "b": args.b, "order": res.order}
        if args.on:
            q["on"] = args.on
        if within is not None:
            q["within"] = within
        q.update({k: v for k, v in sel_a.query_dict().items() if k != "where"})
        summary = {"pairs": len(res.pairs), "unmatched_a": len(res.unmatched_a),
                   "unmatched_b": len(res.unmatched_b), "no_key_a": res.no_key_a,
                   "no_key_b": res.no_key_b, "returned": len(page),
                   "truncated": nxt is not None, "lat": _lat_summary(res.lat)}
        env = {"cmd": "match", "query": q, "time_unit": trace.time_unit,
               "summary": summary}
        if args.unmatched:
            env["results_are"] = f"unmatched_{args.unmatched}"
            rows = [txn_json(t, trace, with_, fields, args.max_field_bytes)
                    for t in page]
            _strip_prefix(rows, args, env)
        else:
            env["results_are"] = "pairs"
            rows = []
            for a, b, k in page:
                r = {"a": a.ref, "b": b.ref, "a_start": a.start, "b_start": b.start,
                     "lat": _analysis.lat_of(a, b)}
                if keys:
                    r["key"] = {n: value_json(v, None) for n, v in zip(keys, k)}
                rows.append(r)
            env["unmatched"] = {"a": [t.ref for t in res.unmatched_a[:limit]],
                                "b": [t.ref for t in res.unmatched_b[:limit]]}
        env.update({"results": rows, "next": nxt,
                    "scan": res.stats.to_dict()})
        out.emit(env)
        return EXIT_MATCH if rows_src else EXIT_NOMATCH
    ts = trace.timescale_exp
    if args.unmatched:
        out.lines(txn_lines(page, trace, out.style, with_, fields,
                            args.max_field_bytes))
    else:
        tab = []
        for a, b, k in page:
            lat = _analysis.lat_of(a, b)
            tab.append([a.ref, b.ref, format_time(a.start, ts),
                        "open" if lat is None else format_time(lat, ts, strip=True)]
                       + [value_text(v, None, ts) for v in k])
        out.lines(table(["a", "b", "a.start", "lat"] + keys, tab, out.style,
                        right=(2, 3)))
    s = (f"-- {_n(len(res.pairs), 'pair')}; unmatched: {len(res.unmatched_a)} a, "
         f"{len(res.unmatched_b)} b")
    if res.no_key_a or res.no_key_b:
        s += f"; without a key: {res.no_key_a} a, {res.no_key_b} b"
    if res.lat.n:
        s += (f"; lat p50 {format_time(res.lat.pct[50], ts, strip=True)}, "
              f"p99 {format_time(res.lat.pct[99], ts, strip=True)}, "
              f"max {format_time(res.lat.max, ts, strip=True)}")
    if len(page) < len(rows_src):
        s += f"; showing {len(page)}" + (f" ({nxt} for more)" if nxt else "")
    if not args.unmatched:
        for side, lst in (("a", res.unmatched_a), ("b", res.unmatched_b)):
            if lst:
                s += (f"\n-- unmatched {side}: "
                      + " ".join(t.ref for t in lst[:8])
                      + (" ..." if len(lst) > 8 else "")
                      + f" (--unmatched {side} to list)")
    for line in s.split("\n"):
        out.write(out.style.c("dim", line))
    return EXIT_MATCH if rows_src else EXIT_NOMATCH


def cmd_export(trace: Trace, args, out: Output) -> int:
    sel = _selection(trace, args)
    info = trace.export(args.format, args.out, force=args.force, selection=sel)
    n = info["tables"]["txn"]
    if out.structured:
        env = {"cmd": "export", "query": _query_dict(trace, sel),
               "time_unit": trace.time_unit, "format": info["format"],
               "out": info["out"], "summary": {"txns": n, "tables": info["tables"]},
               "views": info["views"]}
        out.emit(env)
        return EXIT_MATCH if n else EXIT_NOMATCH
    tabs = ", ".join(f"{k} {v}" for k, v in info["tables"].items())
    out.write(f"wrote {_n(n, 'txn')} to {info['out']} ({info['format']})")
    out.write(out.style.c("dim", f"-- tables: {tabs}"))
    if info["views"]:
        out.write(out.style.c("dim", f"-- views: {', '.join(info['views'])}"))
    return EXIT_MATCH if n else EXIT_NOMATCH


VERBS: Dict[str, Callable] = {
    "info": cmd_info, "streams": cmd_streams, "schema": cmd_schema,
    "find": cmd_find, "show": cmd_show, "count": cmd_count,
    "tree": cmd_tree, "links": cmd_links, "around": cmd_around, "active": cmd_active,
    "stats": cmd_stats, "hist": cmd_hist, "gaps": cmd_gaps, "match": cmd_match,
    "export": cmd_export,
}

PLANNED = {
    "index": "Q5", "diff": "Q5", "value": "Q5",
}


# ---------------------------------------------------------------------------
# Help
# ---------------------------------------------------------------------------

USAGE = """\
usage: trlog <verb> TRACE [options] [EXPR]

Search, filter and navigate transactions in a .trl trace.

verbs:
  info      file facts, counts and suggested next commands (start here)
  streams   streams with txn counts, types and time range
  schema    transaction types, fields, kinds and enum values (--sample N)
  find      transactions matching a selection, with a summary
  show      full records for t:ID refs (fields, meta, links, parent, children)
  count     match counts, optionally --by stream,type,FIELD,...
  tree      parent/child structure around t:ID (--up N|all, --down N|all)
  links     follow cause/related links from t:ID (--up = causes, --down = effects)
  around    what happened near t:ID or @TIME, across streams (-n N, --window W)
  active    transactions in progress at @TIME
  stats     n, min, p50/p90/p99, max, mean of dur or a field (--by KEY)
  hist      histogram of start (throughput), dur or a field (--bin W)
  gaps      idle intervals per stream (--min W)
  match     pair requests and responses (--a/--b/--on), latency, unmatched
  export    write the selection to sqlite or parquet (-o OUT)
  batch     run one command per stdin line, opening the trace once
  help      help [agent|expr|refs|json|batch|VERB]
  config    --libdir | --incdir | --svdir | --version: paths for C/DPI/VPI builds

selection (most verbs): -s GLOB  -T TYPE  -t A..B (inclusive overlap)  and an EXPR
output: text by default (formatted on a TTY, plain when piped); --json for an
envelope with summary, results, next and scan; --jsonl for one row per line.
exit status: 0 matches, 1 no matches, 2 error.
engine: the native libtrl scan engine when it loads, else pure Python (same
results); TRLOG_ENGINE=python|c|auto picks one, and `info` shows which ran.
"""

HELP_TOPICS = {
    "expr": """\
Filter expressions (evaluated per transaction)

names      id start end dur parent open stream type
           nlinks nchildren  edge counts over the whole trace (full scan)
           FIELD             attribute of the txn's type, e.g. addr
           TYPE.FIELD        type-qualified, e.g. axi_rd.addr
           meta["KEY"]       per-txn metadata string
           parent.NAME       one level up, e.g. parent.type == 'seq_item'
literals   42 0x4000 0b101 32'h4000 'h4000 1.5 'str' "str" true false
           times: 10ns 1.5us (converted to trace units)
compare    == != < <= > >=    logic: && || !  (and or not)
arith/bit  + - * / % & | ^ ~ << >>   e.g. (addr & 0xfff) == 0
member     resp in ('SLVERR','DECERR')   addr in 0x4000..0x4fff   x not in (1,2)
match      stream ~ 'top.*.mon' (glob)   text =~ /timeout|err/ (regex)
functions  has(f) abs(x) min(a,..) max(a,..) bit(x,n) bits(x,hi,lo) len(s)

A missing field (other type, unset, or dur of an open txn) makes every
comparison false, including !=. Use has(f) / !has(f) to test presence.
Enum fields compare by label ('OKAY') or by number. Bit operators bind
tighter than comparisons (Python order), so addr & 0xf == 0 works.
""",
    "refs": """\
References

t:1234          a transaction by id (every result row carries one); taken by
                show, tree, links and around
t:1234@15200000 same, with a time hint (accepted, ignored for now)
@15us           a point in time, for around and active (@15200000 = ticks)
s:PATH          a stream: use -s PATH (globs allowed) on find/count/around/active
v:PATH          a signal (phase Q5)
""",
    "agent": None,      # filled in below (HELP_AGENT)
    "json": """\
JSON envelope (--json)

{"cmd", "query", "time_unit",
 "summary": {"matched", "returned", "truncated", "span", "by_stream"},
 "stream_prefix"?: prefix removed from each row's stream (--full-paths disables),
 "results": [{"ref","stream","type","start","dur"|"open","fields", ...}],
 "next": "--offset N" or null,
 "scan": {"blocks_read","blocks_skipped","ms","index","qindex"}}

summary covers the whole matched set, not just the page. Times are integers in
time_unit. Integers wider than 2^53, bitvecs and logicvecs are hex strings
(x/z digits for 4-state); enums are labels; bytes are hex. Errors are
{"error": CODE, "message", "did_you_mean"?, "hint"?, ...} with exit status 2.
""",
}


HELP_AGENT = """\
trlog cheat sheet (for LLM agents). Read this, then query; refs chain between calls.

  trlog VERB TRACE [options] [EXPR]      add --json for machine-readable output

START   trlog info T --json         span, time unit, streams, types, counts, hints
        trlog streams T 'top.env.*' paths, types, txn counts, first/last time
        trlog schema T axi_wr --sample 200   fields, kinds, enum labels, real values
FIND    trlog find T -T axi_wr,axi_rd -t 10us..20us "addr in 0x4000..0x40ff"
        trlog find T open --sort start         never-ended txns
        trlog find T -T axi_rd --sort -dur --limit 5      slowest first
        trlog find T "resp == 'SLVERR'" --first --with links,meta,parent
INSPECT trlog show T t:1234 [t:...]     fields, meta, links, parent, children
NAV     trlog tree T t:1234             ancestors to the root + children (--down all)
        trlog links T t:1234 --up all --kind cause    root-cause chain (up = causes)
        trlog around T t:1234 -n 5      5 txns before/after, all streams (--same-stream)
        trlog around T @15.2us --window 200ns   overlapping 15.2us +-200ns; size W
                                        from the span in `info`, or use -n
        trlog active T @15.2us [EXPR]   txns in progress at that instant
ANALYZE trlog count T --by stream,resp  groups; a missing value is its own null group
        trlog stats T dur --by stream   n min p50 p90 p99 max mean + min/max refs
        trlog hist T start --bin 1us    txns per 1us (throughput); dur, fields too
        trlog gaps T -s 'top.*.mon' --min 2us     idle stretches, longest first
        trlog match T --a "type=='req'" --b "type=='rsp'" --on tag   pairs, lat
                     (= b.end - a.start); --unmatched a|b lists those txns in full
        trlog export T sqlite -o run.db [-T ..] [EXPR]   SQL tables; views
                     txn_<type> = txn + stream path + fields (`help export`)
BATCH   printf '%s\\n' 'count --by type' 'find open' | trlog batch T --json

SELECT  -s GLOB (stream path)  -T TYPE[,TYPE]  -t A..B (10us..20us, ..5us, @15us±200ns)
        -t keeps txns overlapping [A, B], both ends inclusive. Prefer these to EXPR.
EXPR    fields by name (addr, resp), TYPE.FIELD, id start end dur parent open stream
        type nlinks nchildren, meta["key"], parent.FIELD.
        == != < <= > >=  && || !  + - * / % & | ^ ~ << >>  in (a,b)  in A..B
        stream ~ 'glob'  text =~ /re/  has(f) abs min max bit(x,n) bits(x,hi,lo) len
        Literals: 42 0x4000 32'h4000 1.5 'str' true; times 10ns 1.5us.
        Enums compare by label: resp == 'OKAY'. Quote EXPR for the shell.
MISSING A field the txn lacks (other type, unset, dur of open txn) makes any
        comparison false, even !=. Test presence with has(f) / !has(f).
PAGING  default --limit 20. summary.matched is the full count; truncated/next say
        how to get more (--offset N). --first --last --nth N. --sort -dur,id.
        --fields a,b and --max-bytes N keep output small; --limit 0 = summary only.
JSON    {cmd, query, time_unit, summary{matched, returned, truncated, span, ...},
        stream_prefix?, results[{ref, stream, type, start, dur|open, fields}], next,
        scan}. Times are integers in time_unit; stream names drop stream_prefix.
        Big ints/bitvecs are hex strings (x/z for 4-state), enums labels, bytes hex.
ERRORS  exit 2 with {"error": CODE, "message", "did_you_mean"?, "valid_values"?,
        "hint"?}; fix the named thing and retry. Exit 1 = ran fine, no matches.
"""

HELP_BATCH = """\
trlog batch TRACE [--json] < commands

Opens TRACE once and runs one command per stdin line, in order. A line is
`VERB [options] [EXPR]` without the trace path (a leading `trlog` and a
repeated trace path are tolerated). Blank lines and # comments are skipped.
The whole-trace graph built by show/tree/links is reused by later lines.

text:   each command's output follows a `== [n] <line>` header; errors are
        printed in place and do not stop the run.
--json: one compact envelope per line, each with "seq": n and "exit": code
        (errors are {"seq", "exit": 2, "error", ...}).
exit:   2 if any command failed, else 0 if any matched, else 1.

  trlog batch run.trl --json <<'EOF'
  info
  count --by stream,resp -T axi_wr
  find "resp == 'SLVERR'" --first --with links
  EOF
"""

HELP_TOPICS["agent"] = HELP_AGENT


def cmd_help(argv: List[str], f=None) -> int:
    f = f or sys.stdout
    if not argv:
        f.write(USAGE)
        return EXIT_MATCH
    topic = argv[0]
    if topic in HELP_TOPICS:
        f.write(HELP_TOPICS[topic])
        return EXIT_MATCH
    if topic in VERBS:
        f.write(build_parser(topic).format_help())
        return EXIT_MATCH
    if topic == "batch":
        f.write(HELP_BATCH)
        return EXIT_MATCH
    (f if f is not sys.stdout else sys.stderr).write(
        f"trlog: no help topic '{topic}' "
        f"(topics: {', '.join(list(HELP_TOPICS) + list(VERBS) + ['batch'])})\n")
    return EXIT_ERROR


# ---------------------------------------------------------------------------
# Batch (§6)
# ---------------------------------------------------------------------------

class _BatchJsonOutput(Output):
    """Collects a command's envelopes so ``seq`` and ``exit`` can be added."""

    def __init__(self, args, f) -> None:
        super().__init__(args, stream=io.StringIO())
        self.real = f
        self.envs: List[dict] = []

    def emit(self, env: dict) -> None:
        self.envs.append(env)


def _batch_argv(line: str, trace_path: str) -> List[str]:
    lex = shlex.shlex(line, posix=True)
    lex.whitespace_split = True
    if os.name == "nt":
        lex.escape = ""  # keep the backslashes in Windows paths
    argv = list(lex)
    if argv and argv[0] == "trlog":
        argv = argv[1:]
    # tolerate the trace path repeated on the line (`find run.trl ...`)
    if len(argv) > 1 and (argv[1] == trace_path
                          or os.path.basename(argv[1]) == os.path.basename(trace_path)
                          and argv[1].endswith(".trl")):
        del argv[1]
    return argv


def cmd_batch(argv: List[str]) -> int:
    """``trlog batch TRACE [--json]``: one command per stdin line, one open."""
    p = _ArgParser(prog="trlog batch",
                   description="Run one command per line from stdin against "
                               "TRACE, opening it once. Lines are `VERB [options] "
                               "[EXPR]` without the trace; blank lines and # "
                               "comments are skipped.")
    p.add_argument("trace", metavar="TRACE")
    p.add_argument("--json", action="store_true",
                   help="one JSON envelope per line, each with seq and exit")
    p.add_argument("--color", choices=("auto", "always", "never"), default="auto")
    want_json = "--json" in argv or "--jsonl" in argv
    try:
        bargs = p.parse_args(argv)
    except QueryError as e:
        Output(argparse.Namespace(json=want_json)).error(e)
        return EXIT_ERROR
    except SystemExit as e:
        return EXIT_ERROR if e.code else EXIT_MATCH
    real = sys.stdout
    try:
        trace = Trace(bargs.trace)
    except QueryError as e:
        Output(bargs).error(e)
        return EXIT_ERROR
    codes: List[int] = []
    with trace:
        seq = 0
        for raw in sys.stdin:
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            seq += 1
            if bargs.json:
                rc, envs = _batch_one_json(trace, line, bargs)
                for env in envs:
                    real.write(json_dumps({"seq": seq, "exit": rc, **env}) + "\n")
            else:
                real.write(f"== [{seq}] {line}\n")
                rc = _batch_one_text(trace, line, bargs, real)
            real.flush()
            codes.append(rc)
    if EXIT_ERROR in codes:
        return EXIT_ERROR
    return EXIT_MATCH if EXIT_MATCH in codes or not codes else EXIT_NOMATCH


def _batch_parse(trace: Trace, line: str, force_json: bool):
    """-> (verb, args) or raises QueryError."""
    try:
        argv = _batch_argv(line, trace.path)
    except ValueError as e:
        raise QueryError("usage", f"cannot split line: {e}",
                         hint="quote expressions with ' or \"")
    if not argv:
        raise QueryError("usage", "empty command")
    verb, rest = argv[0], argv[1:]
    if verb == "help":
        return verb, rest
    if verb == "batch":
        raise QueryError("usage", "batch cannot be nested inside a batch")
    if verb not in VERBS:
        _unknown_verb(verb)
    if force_json:
        rest = [a for a in rest if a not in ("--jsonl", "--pretty")] + ["--json"]
    args = build_parser(verb).parse_intermixed_args(
        [trace.path] + _join_dash_values(rest))
    return verb, args


def _batch_one_json(trace: Trace, line: str, bargs):
    out = _BatchJsonOutput(argparse.Namespace(json=True), sys.stdout)
    try:
        verb, args = _batch_parse(trace, line, True)
        if verb == "help":
            buf = io.StringIO()
            rc = cmd_help(args, buf)
            return rc, [{"cmd": "help", "text": buf.getvalue()}]
        rc = VERBS[verb](trace, args, out)
    except QueryError as e:
        return EXIT_ERROR, [e.info]
    except SystemExit as e:       # -h inside a batch line
        return (EXIT_ERROR if e.code else EXIT_MATCH), [
            {"cmd": "help", "text": "use `trlog help VERB` for help"}]
    return rc, out.envs


def _batch_one_text(trace: Trace, line: str, bargs, f) -> int:
    try:
        verb, args = _batch_parse(trace, line, False)
    except QueryError as e:
        Output(bargs, stream=f, err=f).error(e)
        return EXIT_ERROR
    except SystemExit as e:
        return EXIT_ERROR if e.code else EXIT_MATCH
    if verb == "help":
        return cmd_help(args, f)
    args.color = bargs.color if args.color == "auto" else args.color
    out = Output(args, stream=f, err=f)
    try:
        return VERBS[verb](trace, args, out)
    except QueryError as e:
        out.error(e)
        return EXIT_ERROR


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def _unknown_verb(verb: str) -> None:
    if verb in PLANNED:
        raise QueryError("not_supported", f"'{verb}' is planned for phase "
                         f"{PLANNED[verb]} and not available yet", verb=verb)
    dym = difflib.get_close_matches(verb, list(VERBS) + ["batch", "help", "config"], n=3)
    raise QueryError("unknown_verb", f"unknown verb '{verb}'", verb=verb,
                     did_you_mean=dym or None,
                     known_verbs=list(VERBS) + ["batch", "help", "config"])


def _join_dash_values(argv: List[str]) -> List[str]:
    """Let ``--sort -dur`` through: argparse would take ``-dur`` for an option."""
    out: List[str] = []
    i = 0
    while i < len(argv):
        a = argv[i]
        if a == "--sort" and i + 1 < len(argv) and argv[i + 1].startswith("-"):
            out.append(f"--sort={argv[i + 1]}")
            i += 2
            continue
        out.append(a)
        i += 1
    return out


def cmd_config(argv: List[str]) -> int:
    """``trlog config``: where the native libraries, headers and SV live.

    Prints one directory per line, for simulator and compiler command
    lines, e.g. ``-sv_lib $(trlog config --libdir)/trl_sv``.
    """
    from . import __version__
    from ._libpath import get_libdirs, get_incdirs, get_svdirs
    p = argparse.ArgumentParser(
        prog="trlog config",
        description="Print where trlog's native libraries, C headers and "
                    "SystemVerilog package are installed.")
    g = p.add_mutually_exclusive_group(required=True)
    g.add_argument("--libdir", action="store_true",
                   help="directories holding libtrl, libtrl_sv and libtrlog_vpi")
    g.add_argument("--incdir", action="store_true", help="C include directories")
    g.add_argument("--svdir", action="store_true",
                   help="directories holding trl_pkg.sv")
    g.add_argument("--version", action="store_true", help="the trlog version")
    try:
        args = p.parse_args(argv)
    except SystemExit as e:
        return EXIT_ERROR if e.code else EXIT_MATCH
    if args.version:
        print(__version__)
        return EXIT_MATCH
    dirs = (get_libdirs() if args.libdir else
            get_incdirs() if args.incdir else get_svdirs())
    for d in dirs:
        print(d)
    return EXIT_MATCH if dirs else EXIT_NOMATCH


def main(argv: Optional[List[str]] = None) -> int:
    """Run one ``trlog`` command; the console-script entry point.

    Args:
        argv: Arguments after the program name, e.g.
            ``["find", "run.trl", "open"]``. None uses ``sys.argv[1:]``.

    Returns:
        Exit status 0 when the command matched something, 1 when it
        matched nothing, 2 on an error. With no arguments it prints the
        usage and returns 2.
    """
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv or argv[0] in ("-h", "--help"):
        sys.stdout.write(USAGE)
        return EXIT_MATCH if argv else EXIT_ERROR
    verb, rest = argv[0], argv[1:]
    if verb == "help":
        return cmd_help(rest)
    if verb == "batch":
        return cmd_batch(rest)
    if verb == "config":
        return cmd_config(rest)
    want_json = "--json" in rest or "--jsonl" in rest
    try:
        if verb not in VERBS:
            _unknown_verb(verb)
        args = build_parser(verb).parse_intermixed_args(_join_dash_values(rest))
    except QueryError as e:
        Output(argparse.Namespace(json=want_json)).error(e)
        return EXIT_ERROR
    except SystemExit as e:
        return EXIT_ERROR if e.code else EXIT_MATCH
    out = Output(args)
    try:
        with Trace(args.trace) as trace:
            return VERBS[verb](trace, args, out)
    except QueryError as e:
        out.error(e)
        return EXIT_ERROR
    except BrokenPipeError:
        # e.g. `trlog find ... | head`; silence the flush error at exit
        try:
            sys.stdout = open(os.devnull, "w")
        except OSError:
            pass
        return EXIT_MATCH


if __name__ == "__main__":
    sys.exit(main())
