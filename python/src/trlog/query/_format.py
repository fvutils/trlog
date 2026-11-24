"""Rendering of query results: JSON records (§3.3-3.5) and text (§3.6)."""

from __future__ import annotations

import json
import math
import re
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

from ._engine import FieldInfo, Trace, Txn
from ._values import MISSING, EnumVal, LogicVal, format_time

JSON_INT_MAX = 1 << 53


# ---------------------------------------------------------------------------
# Values
# ---------------------------------------------------------------------------

def _hex_bytes(v: bytes, max_bytes: int) -> str:
    if max_bytes and len(v) > max_bytes:
        return "0x" + v[:max_bytes].hex() + f"…(+{len(v) - max_bytes} bytes)"
    return "0x" + v.hex()


def value_json(v, fi: Optional[FieldInfo], max_bytes: int = 64):
    """Encode one field value for JSON (§3.4)."""
    if v is MISSING or v is None:
        return None
    if isinstance(v, EnumVal):
        return v.label if v.label is not None else int(v)
    if isinstance(v, LogicVal):
        return v.hex()
    if isinstance(v, bool):
        return v
    if isinstance(v, int):
        if fi is not None and fi.kind in ("bitvec", "logicvec"):
            return hex(v)
        if -JSON_INT_MAX <= v <= JSON_INT_MAX:
            return v
        return hex(v)
    if isinstance(v, float):
        if math.isnan(v) or math.isinf(v):
            return str(v)
        return v
    if isinstance(v, (bytes, bytearray)):
        return _hex_bytes(bytes(v), max_bytes)
    if isinstance(v, (list, tuple)):
        return [value_json(x, None, max_bytes) for x in v]
    return v


_PLAIN_STR = re.compile(r"^[A-Za-z0-9_./:+\-@]+$")


def value_text(v, fi: Optional[FieldInfo], ts_exp: int, max_bytes: int = 64) -> str:
    """Format one field value for text output."""
    if v is MISSING or v is None:
        return "-"
    if isinstance(v, EnumVal):
        return v.label if v.label is not None else str(int(v))
    if isinstance(v, LogicVal):
        return v.hex()
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, int):
        kind = fi.kind if fi else ""
        radix = fi.radix if fi else "dec"
        if kind == "time":
            return format_time(v, ts_exp, strip=True)
        if kind in ("bitvec", "logicvec") or radix == "hex":
            return hex(v) if v >= 0 else "-" + hex(-v)
        if radix == "bin":
            return bin(v)
        if radix == "oct":
            return oct(v)
        return str(v)
    if isinstance(v, float):
        return repr(v)
    if isinstance(v, (bytes, bytearray)):
        return _hex_bytes(bytes(v), max_bytes)
    if isinstance(v, str):
        return v if _PLAIN_STR.match(v) else json.dumps(v, ensure_ascii=False)
    return str(v)


def _field_info(trace: Trace, t: Txn, name: str) -> Optional[FieldInfo]:
    ti = trace.types.get(t.type_id)
    return ti.field(name) if ti else None


# ---------------------------------------------------------------------------
# Transaction records
# ---------------------------------------------------------------------------

def txn_json(t: Txn, trace: Trace, with_: Iterable[str] = (),
             fields: Optional[Sequence[str]] = None, max_bytes: int = 64) -> dict:
    """One transaction as a JSON-ready dict (§3.1 / §3.5 default projection)."""
    w = set(with_)
    d: dict = {"ref": t.ref, "stream": t.stream, "type": t.type, "start": t.start}
    if t.end is None:
        d["open"] = True
    else:
        d["dur"] = t.end - t.start
    if "end" in w:
        d["end"] = t.end
    if "parent" in w:
        d["parent"] = None if t.parent is None else f"t:{t.parent}"
    names = list(t.fields) if fields is None else [f for f in fields if f in t.fields]
    d["fields"] = {n: value_json(t.fields[n], _field_info(trace, t, n), max_bytes)
                   for n in names}
    if "meta" in w:
        d["meta"] = dict(t.meta)
    if "links" in w:
        d["links"] = [_link_json(l) for l in t.links]
    if "children" in w and t.children is not None:
        d["children"] = [f"t:{c}" for c in t.children]
    return d


def _link_json(l) -> dict:
    d = {"kind": l.kind, "dir": l.dir, "to": f"t:{l.other}"}
    if l.label:
        d["label"] = l.label
    return d


def stream_prefix(paths: Sequence[str]) -> str:
    """Longest common dotted prefix (ending in '.') shared by all paths,
    leaving a non-empty remainder for each."""
    if len(paths) < 2:
        return ""
    parts = [p.split(".") for p in paths]
    common: List[str] = []
    for segs in zip(*parts):
        if all(s == segs[0] for s in segs):
            common.append(segs[0])
        else:
            break
    shortest = min(len(p) for p in parts)
    common = common[: shortest - 1]
    return ".".join(common) + "." if common else ""


# ---------------------------------------------------------------------------
# Text styling
# ---------------------------------------------------------------------------

_ANSI = {"ref": "36", "time": "0", "dur": "2", "stream": "34", "type": "35",
         "key": "2", "warn": "33", "err": "31", "head": "1", "dim": "2"}


class Style:
    """Formatted (TTY) or plain text. Plain never emits escapes or truncates."""

    def __init__(self, color: bool, width: Optional[int]) -> None:
        self.color = color
        self.width = width          # None = no truncation

    def c(self, role: str, s: str) -> str:
        if not self.color or not s:
            return s
        return f"\x1b[{_ANSI.get(role, '0')}m{s}\x1b[0m"

    def line(self, segs: List[Tuple[str, str]]) -> str:
        """Join (role, text) segments, truncating to the width on a TTY."""
        if self.width:
            out, used = [], 0
            for role, s in segs:
                if used + len(s) > self.width:
                    room = self.width - used - 1
                    if room > 0:
                        out.append((role, s[:room]))
                    out.append(("dim", "…"))
                    break
                out.append((role, s))
                used += len(s)
            segs = out
        return "".join(self.c(r, s) for r, s in segs)


def _cols(rows: List[List[str]]) -> List[int]:
    if not rows:
        return []
    return [max(len(r[i]) for r in rows) for i in range(len(rows[0]))]


def txn_lines(txns: Sequence[Txn], trace: Trace, st: Style, with_=(),
              fields=None, max_bytes: int = 64,
              pre: Optional[Sequence[Sequence[str]]] = None,
              ref_text: Optional[Sequence[str]] = None,
              post: Optional[Sequence[str]] = None) -> List[str]:
    """One line per txn, aligned; optional links/meta as indented lines.

    ``pre`` adds leading columns per row (right-aligned, e.g. a marker or a
    time offset), ``ref_text`` replaces the ref cell (e.g. indented for a
    tree) and ``post`` appends a dim note to each row.
    """
    return [l for g in txn_line_groups(txns, trace, st, with_, fields, max_bytes,
                                       pre, ref_text, post) for l in g]


def txn_line_groups(txns: Sequence[Txn], trace: Trace, st: Style, with_=(),
                    fields=None, max_bytes: int = 64,
                    pre: Optional[Sequence[Sequence[str]]] = None,
                    ref_text: Optional[Sequence[str]] = None,
                    post: Optional[Sequence[str]] = None) -> List[List[str]]:
    """Like :func:`txn_lines`, but the lines of each txn as one group."""
    ts = trace.timescale_exp
    w = set(with_)
    npre = len(pre[0]) if pre else 0
    cells = []
    for i, t in enumerate(txns):
        dur = "open" if t.end is None else "+" + format_time(t.end - t.start, ts,
                                                             strip=True)
        ref = ref_text[i] if ref_text else t.ref
        cells.append(list(pre[i] if pre else ()) +
                     [ref, format_time(t.start, ts), dur, t.stream, t.type])
    widths = _cols(cells)
    roles = ("dim",) * npre + ("ref", "time", "dur", "stream", "type")
    out = []
    for i, (t, row) in enumerate(zip(txns, cells)):
        segs = []
        for j, (role, cell, wd) in enumerate(zip(roles, row, widths)):
            right = j < npre or role == "time"
            segs.append((role, cell.rjust(wd) if right else cell.ljust(wd)))
            segs.append(("", "  " if j >= npre else " "))
        names = list(t.fields) if fields is None else [f for f in fields if f in t.fields]
        for k, n in enumerate(names):
            if k:
                segs.append(("", " "))
            segs.append(("key", n + "="))
            segs.append(("", value_text(t.fields[n], _field_info(trace, t, n), ts,
                                        max_bytes)))
        while segs and segs[-1][1].strip() == "":
            segs.pop()
        if post and post[i]:
            segs.append(("dim", "  " + post[i]))
        grp = [st.line(segs)]
        out.append(grp)
        pad = " " * (sum(wd + 1 for wd in widths[:npre]) + widths[npre] + 2)
        if "parent" in w and t.parent is not None:
            grp.append(pad + st.c("key", "parent ") + st.c("ref", f"t:{t.parent}"))
        if "links" in w:
            for l in t.links:
                grp.append(pad + st.c("key", f"link {l.dir} {l.kind} ")
                           + st.c("ref", f"t:{l.other}")
                           + (f" ({l.label})" if l.label else ""))
        if "meta" in w:
            for k, v in t.meta.items():
                grp.append(pad + st.c("key", "meta ") + f"{k}={value_text(v, None, ts)}")
        if "children" in w and t.children:
            grp.append(pad + st.c("key", "children ")
                       + " ".join(st.c("ref", f"t:{c}") for c in t.children))
    return out


def show_block(t: Txn, trace: Trace, st: Style, max_bytes: int = 64) -> List[str]:
    """Vertical key/value block for ``show``."""
    ts = trace.timescale_exp
    out = [st.c("ref", t.ref) + "  " + st.c("type", t.type)]

    def kv(k, v, role=""):
        out.append("  " + st.c("key", k.ljust(9)) + st.c(role, v))
    kv("stream", t.stream, "stream")
    kv("start", f"{format_time(t.start, ts)}  ({t.start})")
    if t.end is None:
        kv("end", "open")
    else:
        kv("end", f"{format_time(t.end, ts)}  ({t.end})")
        kv("dur", format_time(t.end - t.start, ts, strip=True))
    kv("parent", "-" if t.parent is None else f"t:{t.parent}", "ref")
    if t.children is not None:
        kv("children", " ".join(f"t:{c}" for c in t.children) or "-", "ref")
    if t.fields:
        out.append("  " + st.c("head", "fields"))
        wd = max(len(n) for n in t.fields)
        for n, v in t.fields.items():
            out.append("    " + st.c("key", n.ljust(wd)) + "  "
                       + value_text(v, _field_info(trace, t, n), ts, max_bytes))
    if t.meta:
        out.append("  " + st.c("head", "meta"))
        wd = max(len(k) for k in t.meta)
        for k, v in t.meta.items():
            out.append("    " + st.c("key", k.ljust(wd)) + "  " + v)
    if t.links:
        out.append("  " + st.c("head", "links"))
        for l in t.links:
            out.append(f"    {l.dir:3s} {l.kind:12s} " + st.c("ref", f"t:{l.other}")
                       + (f"  {l.label}" if l.label else ""))
    return out


def table(headers: List[str], rows: List[List[str]], st: Style,
          right: Iterable[int] = ()) -> List[str]:
    """A simple aligned table with a header row."""
    right = set(right)
    widths = _cols([headers] + rows)
    out = []

    def fmt(r, role):
        cells = [(c.rjust(w) if i in right else c.ljust(w))
                 for i, (c, w) in enumerate(zip(r, widths))]
        return st.line([(role, "  ".join(cells).rstrip())])
    out.append(fmt(headers, "head"))
    for r in rows:
        out.append(fmt(r, ""))
    return out


def json_dumps(obj, pretty: bool = False) -> str:
    if pretty:
        return json.dumps(obj, ensure_ascii=False, indent=2, default=_json_default)
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":"),
                      default=_json_default)


def _json_default(o):
    if isinstance(o, (bytes, bytearray)):
        return "0x" + bytes(o).hex()
    if isinstance(o, (set, frozenset)):
        return sorted(o)
    return str(o)
