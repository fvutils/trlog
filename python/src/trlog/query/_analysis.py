"""Analysis verbs (phase Q3): stats, hist, gaps, match (query_cli_design.md §4.1).

These work on the joined transactions that ``Trace._matches`` returns, so they
run unchanged on whichever scan engine produced them (§7.1: the C engine is an
optional accelerator; the pure-Python engine must always work).
"""

from __future__ import annotations

import bisect
import math
import re
from collections import defaultdict, deque
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence, Tuple

from ._expr import BUILTINS, QueryError, ValueExpr, compile_value
from ._rows import FieldKey, Rows, group_order, key_columns
from ._values import MISSING, EnumVal, LogicVal

TIME_NAMES = frozenset({"start", "end", "dur"})
MAX_BINS = 10000
PERCENTILES = (50, 90, 99)


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

def value_expr(trace, text: str, sel) -> Tuple[ValueExpr, bool]:
    """Compile ``text`` against the selection's types. Returns the expression
    and whether its values are times (start/end/dur or a time-kind field)."""
    names = {trace.types[i].name for i in sel.type_ids} if sel.type_ids else None
    sv = trace.schema_view(names)
    vx = compile_value(text, sv)
    is_time = vx.name in TIME_NAMES or (
        vx.name is not None and bool(sv.field_specs(vx.name))
        and all(fs.kind == "time" for fs in sv.field_specs(vx.name)))
    return vx, is_time


def field_types(trace, vx: ValueExpr) -> Optional[frozenset]:
    """Type ids that define the field when ``vx`` is a bare field name, else
    None. Txns of other types are skipped by stats/hist rather than counted
    as missing a value."""
    name = vx.name
    if not name or name in BUILTINS or "." in name or name.startswith("meta["):
        return None
    return frozenset(t.id for t in trace.types.values() if t.field(name) is not None)


def matched_txns(trace, sel, vexprs: Sequence[ValueExpr] = (), stats=None):
    """Matching txns in (start, id) order. Value expressions that read
    ``parent.X``/``nchildren``/``nlinks`` get what they need first."""
    want_links = any(v.uses_links for v in vexprs)
    if any(v.uses_parent or v.uses_children for v in vexprs):
        every = trace.graph(stats)
        for v in vexprs:
            v.set_parent_lookup(every.get)
    txns = trace._matches(sel, want_links=want_links, stats=stats)
    txns.sort(key=lambda t: (t.start, t.id))
    return txns


def numeric(v):
    """The number in a value, or None (missing, string, x/z, bool)."""
    if v is MISSING or v is None or isinstance(v, (bool, str, bytes, LogicVal)):
        return None
    if isinstance(v, EnumVal):
        return None
    if isinstance(v, float) and math.isnan(v):
        return None
    return v if isinstance(v, (int, float)) else None


def key_value(v):
    """A hashable, comparable key from a field value (None when missing)."""
    if v is MISSING or v is None:
        return None
    if isinstance(v, EnumVal):
        return v.label if v.label is not None else int(v)
    return v


def check_numeric(vx: ValueExpr, verb: str) -> None:
    if vx.kinds and "num" not in vx.kinds:
        kind = "/".join(sorted(vx.kinds))
        raise QueryError("not_numeric", f"'{vx.text}' is {kind}, not a number",
                         name=vx.text, verb=verb,
                         hint="use `trlog count --by FIELD` for the value counts of "
                              "enum and string fields")


def percentile(sorted_vals: list, p: float):
    """Nearest-rank percentile: always a value that occurs in the data."""
    n = len(sorted_vals)
    return sorted_vals[max(0, math.ceil(p / 100.0 * n) - 1)]


# ---------------------------------------------------------------------------
# stats
# ---------------------------------------------------------------------------

@dataclass
class Summary:
    """n, min, max, mean and percentiles of one group.

    Attributes:
        n: Number of values.
        missing: Rows that have the field but no usable value.
        min: Smallest value, or None when ``n`` is 0.
        max: Largest value, or None.
        mean: Mean value, or None.
        pct: ``{50: p50, 90: p90, 99: p99}``, nearest rank, so each is a
            value that occurs in the data.
        min_txn: What holds the min: a :class:`~trlog.query._rows.TxnRef`
            (the (start, id)-first one on ties), or an ``(a, b)`` pair for
            ``match`` latencies.
        max_txn: What holds the max, likewise.
    """
    n: int = 0
    missing: int = 0
    min: Optional[float] = None
    max: Optional[float] = None
    mean: Optional[float] = None
    pct: Dict[int, float] = field(default_factory=dict)
    min_txn: object = None          # the txn (or pair) holding min / max
    max_txn: object = None

    @classmethod
    def of(cls, items: List[tuple], missing: int = 0) -> "Summary":
        """``items`` is a list of (value, owner) with numeric values."""
        s = cls(n=len(items), missing=missing)
        if not items:
            return s
        items = sorted(items, key=lambda it: it[0])     # stable: first owner wins
        vals = [v for v, _ in items]
        s.min, s.min_txn = items[0]
        s.max = vals[-1]
        s.max_txn = next(o for v, o in items if v == s.max)
        s.mean = math.fsum(vals) / len(vals)
        s.pct = {p: percentile(vals, p) for p in PERCENTILES}
        return s


def summary_of_rows(rows, idx: List[int], vals: list, missing: int = 0) -> Summary:
    """Summary of rows ``idx`` with numeric values ``vals[i]``. The same as
    :meth:`Summary.of` over the rows in (start, id) order: among equal
    values the (start, id)-first row owns min and max, min shows its value
    and max shows the (start, id)-last one (1 and 1.0 are equal, not alike).
    """
    s = Summary(n=len(idx), missing=missing)
    if not idx:
        return s
    vs = [vals[i] for i in idx]
    if all(type(v) is int for v in vs):
        svals = sorted(vs)
        s.min, s.max = svals[0], svals[-1]
        s.min_txn = rows.ref(rows.first([i for i in idx if vals[i] == s.min]))
        s.max_txn = rows.ref(rows.first([i for i in idx if vals[i] == s.max]))
    else:
        st, ids = rows.starts, rows.ids
        order = sorted(idx, key=lambda i: (vals[i], st[i], ids[i]))
        svals = [vals[i] for i in order]
        s.min, s.min_txn = svals[0], rows.ref(order[0])
        s.max = svals[-1]
        s.max_txn = rows.ref(next(i for i in order if vals[i] == s.max))
    s.mean = math.fsum(svals) / len(svals)
    s.pct = {p: percentile(svals, p) for p in PERCENTILES}
    return s


@dataclass
class StatsResult:
    """Result of :meth:`trlog.query.Trace.stats`.

    Attributes:
        selection: The selection that was run.
        target: The value expression text.
        is_time: True when the values are times (ticks).
        keys: The ``by`` keys.
        groups: ``(key values, Summary)``, largest n first, then by key.
            Without keys, one ``((), Summary)`` entry.
        matched: Number of matching transactions.
        stats: What the scan read.
    """

    selection: object
    target: str
    is_time: bool
    keys: List[str]
    groups: List[Tuple[tuple, Summary]]     # largest n first, then by key
    matched: int
    stats: object


def stats(trace, target: str, sel, by: Sequence[str] = (), scan_stats=None
          ) -> StatsResult:
    from ._engine import ScanStats, _sortable
    scan_stats = scan_stats if scan_stats is not None else ScanStats()
    vx, is_time = value_expr(trace, target, sel)
    check_numeric(vx, "stats")
    getters = [trace._group_key(k, sel) for k in by]       # validates the keys
    ftypes = field_types(trace, vx)
    if any(k.startswith("meta[") for k in by):
        txns = matched_txns(trace, sel, [vx], scan_stats)
        rows = Rows.from_txns(trace, txns, [vx])
        keycols = [[g(t) for t in txns] for g in getters]
    else:
        fields = [k for k in by if k not in ("stream", "type", "open", "parent")]
        rows = trace._rows(sel, [vx] + [FieldKey(k) for k in fields], scan_stats)
        keycols = key_columns(rows, by, {k: i + 1 for i, k in enumerate(fields)})
    vals = [numeric(v) for v in rows.vals[0]]
    buckets: Dict[tuple, list] = defaultdict(list)
    missing: Dict[tuple, int] = defaultdict(int)
    keys_of = list(zip(*keycols)) if by else None
    tids = rows.type_ids
    for i in (group_order(rows, keycols) if by else range(len(vals))):
        v = vals[i]
        if ftypes is not None and tids[i] not in ftypes:
            continue
        key = keys_of[i] if by else ()
        if v is None:
            missing[key] += 1
        else:
            buckets[key].append(i)
    keys = set(buckets) | set(missing)
    groups = [(k, summary_of_rows(rows, buckets.get(k, []), vals, missing.get(k, 0)))
              for k in keys]
    groups.sort(key=lambda kv: (-kv[1].n, -kv[1].missing, _sortable(kv[0])))
    return StatsResult(sel, target, is_time, list(by), groups, len(rows), scan_stats)


# ---------------------------------------------------------------------------
# hist
# ---------------------------------------------------------------------------

@dataclass
class HistResult:
    """Result of :meth:`trlog.query.Trace.hist`.

    Bin ``k`` covers ``[lo + k*bin, lo + (k+1)*bin)``.

    Attributes:
        selection: The selection that was run.
        target: The value expression text.
        is_time: True when the values are times (ticks).
        bin: Bin width.
        lo: Left edge of the first bin.
        counts: Count per bin; empty bins between min and max are kept.
        n: Number of values.
        missing: Rows without a usable value.
        min: Smallest value, or None.
        max: Largest value, or None.
        matched: Number of matching transactions.
        stats: What the scan read.
    """

    selection: object
    target: str
    is_time: bool
    bin: float
    lo: float                       # left edge of the first bin
    counts: List[int]               # bin k covers [lo + k*bin, lo + (k+1)*bin)
    n: int
    missing: int
    min: Optional[float]
    max: Optional[float]
    matched: int
    stats: object

    def edges(self, k: int) -> Tuple[float, float]:
        """``(lo, hi)`` of bin ``k``; ``hi`` is excluded."""
        return self.lo + k * self.bin, self.lo + (k + 1) * self.bin


def nice_bin(lo: float, hi: float, max_bins: int = 20, integral: bool = True) -> float:
    """The smallest 1/2/5 x 10^k width that covers [lo, hi] in ``max_bins``."""
    span = hi - lo
    if span <= 0:
        return 1
    raw = span / max_bins
    exp = math.floor(math.log10(raw))
    w = 10 ** exp
    found = False
    while not found:
        for m in (1, 2, 5):
            w = m * 10 ** exp
            # the first bin starts at floor(lo / w) * w, so alignment can cost one
            if w >= raw and math.floor(hi / w) - math.floor(lo / w) + 1 <= max_bins:
                found = True
                break
        exp += 1
    if integral:
        w = max(1, int(round(w)))
    return w


def hist(trace, target: str, sel, bin_width: Optional[float] = None,
         max_bins: int = 20, scan_stats=None) -> HistResult:
    from ._engine import ScanStats
    scan_stats = scan_stats if scan_stats is not None else ScanStats()
    vx, is_time = value_expr(trace, target, sel)
    check_numeric(vx, "hist")
    if bin_width is not None and bin_width <= 0:
        raise QueryError("bad_value", "--bin must be greater than zero",
                         value=bin_width)
    rows = trace._rows(sel, [vx], scan_stats)
    ftypes = field_types(trace, vx)
    idx, missing = [], 0
    allv = [numeric(v) for v in rows.vals[0]]
    tids = rows.type_ids
    for i, v in enumerate(allv):
        if ftypes is not None and tids[i] not in ftypes:
            continue
        if v is None:
            missing += 1
        else:
            idx.append(i)
    vals = [allv[i] for i in idx]
    if not vals:
        return HistResult(sel, target, is_time, bin_width or 1, 0, [], 0, missing,
                          None, None, len(rows), scan_stats)
    integral = all(isinstance(v, int) for v in vals)
    if all(type(v) is int for v in vals):
        vmin, vmax = min(vals), max(vals)
    else:
        # min()/max() keep the first of equal values (1 vs 1.0) in (start, id) order
        ordered = [allv[i] for i in rows.by_start(idx)]
        vmin, vmax = min(ordered), max(ordered)
    w = bin_width if bin_width is not None else nice_bin(vmin, vmax, max_bins, integral)
    lo = math.floor(vmin / w) * w
    nb = int(math.floor((vmax - lo) / w)) + 1
    if nb > MAX_BINS:
        raise QueryError("too_many_bins",
                         f"--bin {w} gives {nb} bins over {vmin}..{vmax} "
                         f"(max {MAX_BINS})", bins=nb,
                         hint="use a wider --bin, or leave --bin out to pick one")
    counts = [0] * nb
    for v in vals:
        counts[min(nb - 1, int((v - lo) // w))] += 1
    if isinstance(lo, float) and lo.is_integer() and integral:
        lo = int(lo)
    return HistResult(sel, target, is_time, w, lo, counts, len(vals), missing,
                      vmin, vmax, len(rows), scan_stats)


# ---------------------------------------------------------------------------
# gaps
# ---------------------------------------------------------------------------

@dataclass
class Gap:
    """One idle interval of a stream.

    Attributes:
        stream: Stream path.
        start: Start of the gap, in ticks.
        end: End of the gap, in ticks.
        prev: :class:`~trlog.query._rows.TxnRef` whose end opened the gap,
            or None at the start of the range.
        next: The one whose start closed it, or None at the end.
    """

    stream: str
    start: int
    end: int
    prev: object                    # txn whose end opens the gap, None at the edge
    next: object                    # txn whose start closes it, None at the edge

    @property
    def dur(self) -> int:
        """Length of the gap in ticks."""
        return self.end - self.start


@dataclass
class GapsResult:
    """Result of :meth:`trlog.query.Trace.gaps`.

    Attributes:
        selection: The selection that was run.
        range: The ``(lo, hi)`` interval searched, in ticks.
        min: The ``min`` length; only longer gaps are kept.
        gaps: The :class:`Gap` records, in the requested order.
        streams: Streams with at least one selected transaction.
        silent: Selected streams with no matching transaction at all.
        stats: What the scan read.
    """

    selection: object
    range: Tuple[int, int]          # the interval searched
    min: int
    gaps: List[Gap]                 # sorted by the requested key
    streams: int                    # streams with at least one selected txn
    silent: List[str]               # selected streams with no matching txn at all
    stats: object


def gaps(trace, sel, min_gap: int = 0, sort: str = "-dur", scan_stats=None
         ) -> GapsResult:
    """Idle intervals per stream: no selected txn in progress for more than
    ``min_gap`` ticks. The searched range is the -t window, with open sides
    taken from the trace span, so a stream that stops early ends in a gap."""
    from ._engine import ScanStats
    scan_stats = scan_stats if scan_stats is not None else ScanStats()
    rows = trace._rows(sel, (), scan_stats)
    starts, ends = rows.starts, rows.ends
    lo, hi = sel.window
    if lo is None or hi is None:
        if sel.stream_ids is None and sel.type_ids is None and sel.filter is None:
            # the selection is the whole trace: no second scan for the span
            span = (min(starts),
                    max(s if e is None else e for s, e in zip(starts, ends))
                    ) if len(rows) else None
        else:
            span = trace.span()
        if span is None:
            span = (0, 0)
        lo = span[0] if lo is None else lo
        hi = span[1] if hi is None else hi
    per: Dict[str, list] = defaultdict(list)
    names = rows.stream_names()
    for i in rows.by_start(range(len(rows))):
        per[names[i]].append(i)
    out: List[Gap] = []
    inf = float("inf")
    for stream, idx in per.items():
        covered, holder = lo, None
        for i in idx:                                   # (start, id) order
            st = starts[i]
            if st - covered > min_gap:
                out.append(Gap(stream, covered, st,
                               None if holder is None else rows.ref(holder),
                               rows.ref(i)))
            end = inf if ends[i] is None else ends[i]
            if end > covered:
                covered, holder = end, i
        if covered != inf and hi - covered > min_gap:
            out.append(Gap(stream, covered, hi,
                           None if holder is None else rows.ref(holder), None))
    selected = (trace.streams[i].path for i in sel.stream_ids) \
        if sel.stream_ids is not None else (s.path for s in trace.streams.values())
    silent = sorted(p for p in selected if p not in per)
    out = sort_gaps(out, sort)
    return GapsResult(sel, (lo, hi), min_gap, out, len(per), silent, scan_stats)


GAP_SORT_KEYS = ("dur", "start", "end", "stream")


def sort_gaps(gs: List[Gap], sort: Optional[str]) -> List[Gap]:
    keys = [k.strip() for k in (sort or "-dur").split(",") if k.strip()]
    out = sorted(gs, key=lambda g: (g.stream, g.start))
    for k in reversed(keys):
        desc = k.startswith("-")
        name = k.lstrip("-+")
        if name not in GAP_SORT_KEYS:
            raise QueryError("bad_sort", f"cannot sort gaps by '{name}'", name=name,
                             valid_values=list(GAP_SORT_KEYS))
        out.sort(key=lambda g, n=name: getattr(g, n), reverse=desc)
    return out


# ---------------------------------------------------------------------------
# match
# ---------------------------------------------------------------------------

ORDERS = ("fifo", "any")

_ON_TERM = re.compile(r"^\s*([ab])\.(.+?)\s*==\s*([ab])\.(.+?)\s*$", re.S)
_ON_SPLIT = re.compile(r"&&|\band\b")
_ON_NAME = re.compile(r"^[A-Za-z_]\w*(\.[A-Za-z_]\w*)?$")


def parse_on(text: Optional[str]) -> List[Tuple[str, str]]:
    """``a.tag == b.tag && a.id == b.parent`` -> [("tag", "tag"), ("id", "parent")].
    A bare name ``tag`` (or ``tag,id``) means ``a.tag == b.tag``."""
    if not text or not text.strip():
        return []
    terms: List[Tuple[str, str]] = []
    if "==" not in text:
        for name in (n.strip() for n in text.split(",")):
            if not _ON_NAME.match(name):
                raise QueryError("bad_on", f"cannot read --on '{text}'", value=text,
                                 hint="write a.FIELD == b.FIELD, joined with &&; "
                                      "or bare FIELD names (comma-separated) when "
                                      "both sides use the same name")
            terms.append((name, name))
        return terms
    for part in _ON_SPLIT.split(text):
        m = _ON_TERM.match(part)
        if not m:
            raise QueryError("bad_on", f"cannot read --on term '{part.strip()}'",
                             value=text,
                             hint="write a.FIELD == b.FIELD, joined with &&; "
                                  "or a bare FIELD when both sides use the same name")
        s1, x1, s2, x2 = m.groups()
        if s1 == s2:
            raise QueryError("bad_on", f"--on term '{part.strip()}' must compare an "
                             "a. side with a b. side", value=text)
        terms.append((x1, x2) if s1 == "a" else (x2, x1))
    return terms


@dataclass
class MatchResult:
    """Result of :meth:`trlog.query.Trace.match`.

    Attributes:
        sel_a: Selection of the A side.
        sel_b: Selection of the B side.
        on: Key terms as ``(a_expr, b_expr)`` pairs; empty for no key.
        order: ``"fifo"`` or ``"any"``.
        within: The ``within`` bound in ticks, or None.
        pairs: ``(a, b, key)`` per pair, in A start order; ``a`` and ``b``
            are :class:`~trlog.query.Txn` and ``key`` is a tuple of key
            values.
        unmatched_a: A transactions with no pair.
        unmatched_b: B transactions with no pair.
        no_key_a: A transactions with a missing key value (not paired and
            not counted as unmatched).
        no_key_b: Likewise for B.
        lat: :class:`Summary` of ``b.end - a.start`` over the pairs; open
            B transactions count as missing.
        stats: What the scan read.
    """

    sel_a: object
    sel_b: object
    on: List[Tuple[str, str]]
    order: str
    within: Optional[int]
    pairs: List[tuple]              # (a, b, key)
    unmatched_a: List[object]
    unmatched_b: List[object]
    no_key_a: int                   # a txns with a missing key value
    no_key_b: int
    lat: Summary
    stats: object


def lat_of(a, b) -> Optional[int]:
    return None if b.end is None else b.end - a.start


def _match_sides(trace, sel_a, sel_b, vexprs, stats):
    """Both sides of ``match`` from one scan: select ``(A) || (B)`` over the
    union of the two sides' stream/type sets, then split the result with
    each side's own filter. ``-s``/``-T``/``-t`` are the same for both."""
    fa, fb = sel_a.filter, sel_b.filter
    if fa is None or fb is None:
        where = None                      # one side takes everything
    else:
        where = f"({sel_a.where}) || ({sel_b.where})"
    union = trace.select(where, sel_a.stream_globs, sel_a.type_names, sel_a.window)
    # Keep each side's pushdown: the union only needs streams/types that
    # either side can match.
    for attr in ("stream_ids", "type_ids"):
        x, y = getattr(sel_a, attr), getattr(sel_b, attr)
        setattr(union, attr, None if x is None or y is None else x | y)
    U = matched_txns(trace, union, list(vexprs) + [f for f in (fa, fb) if f], stats)
    if trace._graph_cache is not None:
        for f in (fa, fb):
            if f is not None:
                f.set_parent_lookup(trace._graph_cache.get)
    A = U if fa is None else [t for t in U if fa.match(t)]
    B = U if fb is None else [t for t in U if fb.match(t)]
    return A, B


def match(trace, sel_a, sel_b, on: Sequence[Tuple[str, str]] = (), order: str = "fifo",
          within: Optional[int] = None, scan_stats=None) -> MatchResult:
    """Pair each b with an a that has the same key.

    ``fifo``: the oldest unmatched a that started at or before b.
    ``any``: the unmatched a whose start is nearest b's, before or after it.
    ``within`` bounds ``|b.start - a.start|``. A txn never pairs with itself.
    """
    from ._engine import ScanStats
    if order not in ORDERS:
        raise QueryError("bad_value", f"unknown --order '{order}'", value=order,
                         valid_values=list(ORDERS))
    scan_stats = scan_stats if scan_stats is not None else ScanStats()
    ka = [value_expr(trace, x, sel_a)[0] for x, _ in on]
    kb = [value_expr(trace, y, sel_b)[0] for _, y in on]
    A, B = _match_sides(trace, sel_a, sel_b, ka + kb, scan_stats)

    def keys(txns, kx):
        out, none = [], 0
        for t in txns:
            k = tuple(key_value(f(t)) for f in kx)
            if any(v is None for v in k):
                none += 1
                k = None
            out.append(k)
        return out, none

    akeys, no_key_a = keys(A, ka)
    bkeys, no_key_b = keys(B, kb)
    pairs: List[tuple] = []
    paired_a = set()
    unmatched_b = []
    if order == "fifo":
        queues: Dict[tuple, deque] = defaultdict(deque)
        events = [(t.start, 0, t.id, t, k) for t, k in zip(A, akeys) if k is not None]
        events += [(t.start, 1, t.id, t, k) for t, k in zip(B, bkeys) if k is not None]
        events.sort(key=lambda e: e[:3])
        for _, side, _, t, k in events:
            q = queues[k]
            if side == 0:
                q.append(t)
                continue
            if within is not None:
                while q and t.start - q[0].start > within:
                    q.popleft()
            a = q[0] if q else None
            if a is not None and a.id == t.id:      # same txn on both sides
                a = q[1] if len(q) > 1 else None
            if a is None:
                unmatched_b.append(t)
                continue
            q.remove(a)
            paired_a.add(a.id)
            pairs.append((a, t, k))
    else:
        by_key: Dict[tuple, list] = defaultdict(list)
        for t, k in zip(A, akeys):
            if k is not None:
                by_key[k].append(t)
        used: Dict[tuple, set] = defaultdict(set)
        for t, k in zip(B, bkeys):
            if k is None:
                continue
            cands = by_key.get(k, [])
            starts = [c.start for c in cands]
            i0, i1 = 0, len(cands)
            if within is not None:
                i0 = bisect.bisect_left(starts, t.start - within)
                i1 = bisect.bisect_right(starts, t.start + within)
            best = min((c for c in cands[i0:i1]
                        if c.id not in used[k] and c.id != t.id),
                       key=lambda c: (abs(c.start - t.start), c.start, c.id),
                       default=None)
            if best is None:
                unmatched_b.append(t)
                continue
            used[k].add(best.id)
            paired_a.add(best.id)
            pairs.append((best, t, k))
    unmatched_a = [t for t, k in zip(A, akeys) if k is not None and t.id not in paired_a]
    unmatched_b.sort(key=lambda t: (t.start, t.id))
    pairs.sort(key=lambda p: (p[0].start, p[0].id))
    lat_items = [(lat_of(a, b), (a, b)) for a, b, _ in pairs if b.end is not None]
    lat = Summary.of(lat_items, missing=len(pairs) - len(lat_items))
    return MatchResult(sel_a, sel_b, list(on), order, within, pairs, unmatched_a,
                       unmatched_b, no_key_a, no_key_b, lat, scan_stats)
