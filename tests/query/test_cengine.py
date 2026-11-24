"""The optional C scan engine (query_impl_status.md, Q4).

- With no native library, everything works on the pure-Python engine.
- With it, both engines give exactly the same results: on the golden
  corpus (test_cli_golden runs under both), and on randomized traces and
  expressions here (differential tests).
"""

import itertools
import os
import pathlib
import random
import re

import pytest

import trlog.query as tq
from trlog._types import FieldDef, FieldType, LinkType, Radix, ScopeType, TxnAttr
from trlog._writer import TrlWriter
from trlog.query import _cengine
from trlog.query._expr import QueryError

C_AVAILABLE = _cengine.load() is not None
needs_c = pytest.mark.skipif(not C_AVAILABLE, reason="libtrl with trl_qscan not built")


# ---------------------------------------------------------------------------
# Engine selection without a library
# ---------------------------------------------------------------------------

@pytest.fixture
def no_library(monkeypatch):
    """Pretend libtrl is missing, as on a platform without a native wheel."""
    monkeypatch.setattr(_cengine, "_lib_tried", True)
    monkeypatch.setattr(_cengine, "_lib", None)
    monkeypatch.setattr(_cengine, "_lib_error", "libtrl not found (test)")


def test_no_library_uses_python(no_library, corpus_path, monkeypatch):
    monkeypatch.delenv("TRLOG_ENGINE", raising=False)
    with tq.open(corpus_path) as t:
        assert t.engine == "python"
        assert t.engine_note == "libtrl not found (test)"
        assert t.count("len > 1").matched == 7
        assert t.info()["engine"] == "python"


def test_no_library_engine_c_is_an_error(no_library, corpus_path):
    with pytest.raises(QueryError) as e:
        tq.open(corpus_path, engine="c")
    assert e.value.info["error"] == "engine_unavailable"


def test_no_library_cli(no_library, corpus_path, capsys, monkeypatch):
    from trlog.cli import main
    monkeypatch.delenv("TRLOG_ENGINE", raising=False)
    assert main(["count", str(corpus_path), "len > 1"]) == 0
    assert capsys.readouterr().out.strip() == "7"


def test_library_with_other_abi_is_not_used(monkeypatch, corpus_path):
    """A libtrl built for another qscan ABI is skipped, not misread."""
    if not C_AVAILABLE:
        pytest.skip("libtrl with trl_qscan not built")
    path = _cengine._lib._name
    monkeypatch.setattr(_cengine, "_lib_tried", False)
    monkeypatch.setattr(_cengine, "_lib", None)
    monkeypatch.setattr(_cengine, "_lib_error", None)
    monkeypatch.setattr(_cengine, "QSCAN_ABI", _cengine.QSCAN_ABI + 1)
    monkeypatch.setattr(_cengine, "_candidates", lambda: [pathlib.Path(path)])
    assert _cengine.load() is None
    assert "ABI" in _cengine.load_error()
    with tq.open(corpus_path, engine="auto") as t:
        assert t.engine == "python"


def test_engine_env(corpus_path, monkeypatch):
    monkeypatch.setenv("TRLOG_ENGINE", "python")
    with tq.open(corpus_path) as t:
        assert t.engine == "python"
    monkeypatch.setenv("TRLOG_ENGINE", "fast")
    with pytest.raises(QueryError) as e:
        tq.open(corpus_path)
    assert e.value.info["error"] == "bad_engine"


@needs_c
def test_auto_uses_c(corpus_path, monkeypatch):
    monkeypatch.delenv("TRLOG_ENGINE", raising=False)
    with tq.open(corpus_path) as t:
        assert t.engine == "c"
        t.find("len > 1")
        assert t.c_fallbacks == []


# ---------------------------------------------------------------------------
# Randomized traces
# ---------------------------------------------------------------------------

KINDS = [FieldType.FT_U8, FieldType.FT_U16, FieldType.FT_U32, FieldType.FT_U64,
         FieldType.FT_I8, FieldType.FT_I32, FieldType.FT_I64, FieldType.FT_BOOL,
         FieldType.FT_TIME, FieldType.FT_ENUM, FieldType.FT_BITVEC,
         FieldType.FT_LOGICVEC, FieldType.FT_STRING, FieldType.FT_BYTES,
         FieldType.FT_F32, FieldType.FT_F64]
NAMES = ["x", "y", "addr", "f1", "mode", "s"]
LABELS = ["OKAY", "ERR", "RETRY", "x"]
STRS = ["", "a", "abc", "OKAY", "ERR", "héllo", "x*y"]


def _value(rng, ft):
    if ft == FieldType.FT_U8:
        return rng.randrange(256)
    if ft == FieldType.FT_U16:
        return rng.randrange(1 << 16)
    if ft == FieldType.FT_U32:
        return rng.choice([0, 1, 7, rng.randrange(1 << 32)])
    if ft in (FieldType.FT_U64, FieldType.FT_TIME):
        return rng.choice([0, 3, 1 << 40, (1 << 63) - 1, 1 << 63, (1 << 64) - 1,
                           rng.randrange(1 << 64)])
    if ft == FieldType.FT_I8:
        return rng.randrange(-128, 128)
    if ft == FieldType.FT_I32:
        return rng.randrange(-(1 << 31), 1 << 31)
    if ft == FieldType.FT_I64:
        return rng.choice([-(1 << 63), -1, 0, 5, (1 << 63) - 1])
    if ft == FieldType.FT_BOOL:
        return rng.random() < 0.5
    if ft == FieldType.FT_ENUM:
        return rng.randrange(6)             # 4 and 5 have no label
    if ft == FieldType.FT_BITVEC:
        w = rng.choice([1, 8, 13, 63, 64, 65, 80])
        return (w, rng.randrange(1 << w))
    if ft == FieldType.FT_LOGICVEC:
        w = rng.choice([4, 8, 64, 70])
        b = 0 if rng.random() < 0.6 else rng.randrange(1 << w)
        return (w, rng.randrange(1 << w), b)
    if ft == FieldType.FT_STRING:
        return rng.choice(STRS)
    if ft == FieldType.FT_BYTES:
        return bytes(rng.randrange(256) for _ in range(rng.randrange(4)))
    if ft == FieldType.FT_F32:
        return rng.choice([0.0, 1.5, -2.25, 1e30])
    return rng.choice([0.0, 0.1, -3.0, float("inf"), 1e300])


def build_random(path, seed: int):
    rng = random.Random(seed)
    layout = rng.choice(["row", "row", "row", "column", "adaptive"])
    with TrlWriter(str(path), timescale_exp=-12, compress=rng.random() < 0.6,
                   column_layout=layout) as w:
        w._delta_encode_txn = rng.random() < 0.8
        enum = w.add_enum_type("resp", [(l, i) for i, l in enumerate(LABELS)])
        types = []                          # (type id, name, [ft])
        for tn in ["req", "rsp", "req", "evt"]:   # "req" twice: two ids, one name
            names = rng.sample(NAMES, rng.randrange(1, len(NAMES)))
            fts = [rng.choice(KINDS) for _ in names]
            fds = [FieldDef(name_str_id=w.intern(n), field_type=ft,
                            display_hint=Radix.RX_HEX,
                            enum_type_id=enum if ft == FieldType.FT_ENUM else 0)
                   for n, ft in zip(names, fts)]
            types.append((w.add_txn_schema(tn, fds), tn, fts))
        st = w.add_stream_type("mon")
        with w.begin_hierarchy(name="design") as h:
            h.begin_scope(ScopeType.ST_MODULE, "top")
            streams = [h.add_stream(st, n) for n in ("a0", "a1", "b")]
            h.end_scope()

        def attrs(fts, typed):
            out = []
            for i, ft in enumerate(fts):
                if rng.random() < 0.2:
                    continue                # field left unset
                v = _value(rng, ft)
                a = TxnAttr(field_idx=i, value=v)
                if typed:
                    a._field_type = ft
                out.append(a)
            rng.shuffle(out)
            return out

        tid = 1
        opened = []                          # (txn id, fts)
        all_ids = []
        t = 0
        for blk in range(rng.randrange(2, 7)):
            sid = rng.choice(streams + [None])
            with w.begin_txn_block(t, stream_inst_id=sid) as b:
                for _ in range(rng.randrange(1, 25)):
                    s = sid if sid is not None else rng.choice(streams)
                    ty, _tn, fts = rng.choice(types)
                    start = t + rng.randrange(0, 50)
                    r = rng.random()
                    new_id = tid if rng.random() > 0.05 or not all_ids else rng.choice(all_ids)
                    parent = rng.choice([0, 0] + all_ids[-5:])
                    if r < 0.55:
                        b.write_full(s, ty, new_id, start, start + rng.randrange(0, 40),
                                     parent, attrs(fts, False))
                        all_ids.append(new_id)
                        tid += 1
                    elif r < 0.75:
                        b.write_begin(s, ty, new_id, start, parent)
                        opened.append((new_id, fts))
                        all_ids.append(new_id)
                        tid += 1
                    elif r < 0.85 and opened:
                        oid, ofts = rng.choice(opened)
                        b.write_attr(oid, attrs(ofts, True))
                    elif r < 0.92 and opened:
                        oid, _ = opened.pop(rng.randrange(len(opened)))
                        b.write_end(oid, start + rng.randrange(0, 30))
                    elif r < 0.96 and all_ids:
                        b.write_meta(rng.choice(all_ids + [9999]), rng.choice(["k", "n"]),
                                     rng.choice(STRS))
                    elif all_ids:
                        b.write_link(rng.choice(list(LinkType)), rng.choice(all_ids),
                                     rng.choice(all_ids + [9999]),
                                     rng.choice([0, w.intern("lbl")]))
                    t = start
            t += rng.randrange(0, 100)


# ---------------------------------------------------------------------------
# Randomized expressions
# ---------------------------------------------------------------------------

def gen_expr(rng, depth=0, kind="cond") -> str:
    def leaf():
        r = rng.random()
        if r < 0.4:
            return rng.choice(NAMES)
        if r < 0.5:
            return rng.choice(["req", "rsp", "evt"]) + "." + rng.choice(NAMES)
        if r < 0.65:
            return rng.choice(["id", "start", "end", "dur", "parent", "open", "nlinks"])
        if r < 0.85:
            return str(rng.choice([0, 1, 2, 3, 5, 255, -1, 1 << 63, 1 << 64,
                                   (1 << 63) - 1]))
        if r < 0.9:
            return rng.choice(["1.5", "0.0", "10ns", "1.5ps", "true", "false"])
        if r < 0.95:
            return f"'{rng.choice(LABELS + STRS)}'"
        return rng.choice(['meta["k"]', "len(s)", "has(x)", "has(rsp.y)", "has(end)"])

    def num(d):
        if d > 2 or rng.random() < 0.4:
            return leaf()
        r = rng.random()
        if r < 0.5:
            op = rng.choice(["+", "-", "*", "/", "%", "&", "|", "^", "<<", ">>"])
            return f"({num(d + 1)} {op} {num(d + 1)})"
        if r < 0.65:
            return f"{rng.choice(['-', '+', '~'])}{num(d + 1)}"
        if r < 0.8:
            f = rng.choice(["abs", "min", "max"])
            n = 1 if f == "abs" else rng.randrange(1, 4)
            return f"{f}({', '.join(num(d + 1) for _ in range(n))})"
        if r < 0.9:
            return f"bit({num(d + 1)}, {num(d + 1)})"
        return f"bits({num(d + 1)}, {num(d + 1)}, {num(d + 1)})"

    def cond(d):
        r = rng.random()
        if d < 2 and r < 0.25:
            return f"({cond(d + 1)} {rng.choice(['&&', '||'])} {cond(d + 1)})"
        if d < 2 and r < 0.32:
            return f"!({cond(d + 1)})"
        if r < 0.62:
            op = rng.choice(["==", "!=", "<", "<=", ">", ">="])
            return f"{num(d)} {op} {num(d)}"
        if r < 0.72:
            lhs = rng.choice(["stream", "type"] + NAMES)
            return f"{lhs} {rng.choice(['==', '!='])} '{rng.choice(LABELS + ['top.a0', 'req', 'b'])}'"
        if r < 0.8:
            lhs = rng.choice(["stream", "type", "mode", "s", "x"])
            if rng.random() < 0.5:
                return f"{lhs} ~ '{rng.choice(['top.a*', '*', 'O*', 'r?q', 'E*'])}'"
            return f"{lhs} =~ /{rng.choice(['a', '^O', 'q$', 'R|E'])}/"
        if r < 0.9:
            x = rng.choice(NAMES + ["stream", "type", "start"])
            items = []
            for _ in range(rng.randrange(1, 4)):
                if rng.random() < 0.3:
                    items.append(f"{rng.randrange(5)}..{rng.randrange(3, 300)}")
                elif rng.random() < 0.4:
                    items.append(f"'{rng.choice(LABELS + ['top.b', 'evt'])}'")
                else:
                    items.append(str(rng.choice([0, 1, 3, 255, 1 << 63])))
            neg = "not " if rng.random() < 0.3 else ""
            return f"{x} {neg}in ({', '.join(items)})"
        return num(d)

    return cond(depth) if kind == "cond" else num(depth)


def _canon(v):
    """Type-exact form of a value (True vs 1, EnumVal vs int, ...)."""
    if isinstance(v, tq.EnumVal):
        return ("enum", int(v), v.label)
    if isinstance(v, tq.LogicVal):
        return ("logic", v.width, v.aval, v.bval)
    if isinstance(v, float):
        return ("float", repr(v))
    return (type(v).__name__, v)


def _canon_txn(t):
    return (t.id, t.stream_id, t.stream, t.type_id, t.type, t.start, t.end, t.parent,
            [(k, _canon(v)) for k, v in t.fields.items()],
            list(t.meta.items()),
            [(l.kind, l.dir, l.other, l.label) for l in t.links])


def _outcome(fn):
    try:
        return ("ok", fn())
    except Exception as e:   # noqa: BLE001 - both engines must fail the same way
        return ("error", type(e).__name__, str(e))


# TRL_FUZZ_SEEDS=1000 for a longer differential run
SEEDS = range(int(os.environ.get("TRL_FUZZ_SEEDS", "40")))


@needs_c
@pytest.mark.parametrize("seed", SEEDS)
def test_differential_random(seed, tmp_path):
    path = tmp_path / f"r{seed}.trl"
    build_random(path, seed)
    rng = random.Random(1000 + seed)
    tp = tq.open(path, engine="python")
    tc = tq.open(path, engine="c")
    try:
        # Plain scans (graph, show, schema sampling use these).
        for kw in ({}, {"want_links": True}, {"want_meta": False}, {"max_txns": 3},
                   {"t_hi": 120}, {"stream_ids": frozenset({1})}):
            a = _outcome(lambda: [_canon_txn(t) for t in tp.scan(**kw).values()])
            b = _outcome(lambda: [_canon_txn(t) for t in tc.scan(**kw).values()])
            assert a == b, kw
        ran = 0
        for attempt in itertools.count():
            # the first draws are fixed per seed; draw on only if too few were valid
            if attempt >= 60 and (ran > 10 or attempt >= 600):
                break
            expr = gen_expr(rng)
            window = rng.choice([(None, None), (None, 100), (40, None), (30, 200),
                                 (0.5, 99)])
            streams = rng.choice([None, "top.a*", "top.b"])
            types = rng.choice([None, "req", "rsp,evt"])
            links = rng.random() < 0.3
            try:
                tp.select(expr, streams, types and types.split(","), window)
            except QueryError:
                continue
            ran += 1

            def q(t):
                r = t.find(expr, streams=streams, types=types and types.split(","),
                           time=window, want_links=links)
                return [_canon_txn(x) for x in r.txns], r.stats.blocks_read, \
                    r.stats.duplicate_ids
            a, b = _outcome(lambda: q(tp)), _outcome(lambda: q(tc))
            assert a == b, f"seed {seed}: {expr!r} window={window} streams={streams} types={types}"
        assert ran > 10
    finally:
        tp.close()
        tc.close()


def _canon_summary(s):
    return (s.n, s.missing, _canon(s.min), _canon(s.max),
            None if s.mean is None else repr(s.mean),
            sorted((p, _canon(v)) for p, v in s.pct.items()),
            s.min_txn and s.min_txn.ref, s.max_txn and s.max_txn.ref)


def _canon_key(k):
    return tuple(_canon(x) for x in k)


@needs_c
@pytest.mark.parametrize("seed", SEEDS)
def test_differential_aggregates(seed, tmp_path):
    """count --by, stats, hist, gaps and info: C projects columns (no Txn per
    row) and must still agree with Python exactly."""
    path = tmp_path / f"a{seed}.trl"
    build_random(path, seed)
    rng = random.Random(5000 + seed)
    tp = tq.open(path, engine="python")
    tc = tq.open(path, engine="c")
    try:
        def info(t):
            d = t.info()
            d.pop("engine")
            st = d.pop("_stats")
            return d, st.blocks_read, st.duplicate_ids
        assert _outcome(lambda: info(tp)) == _outcome(lambda: info(tc))
        ran = 0
        for attempt in itertools.count():
            # the first draws are fixed per seed; draw on only if too few were valid
            if attempt >= 40 and (ran > 5 or attempt >= 400):
                break
            where = gen_expr(rng) if rng.random() < 0.5 else None
            value = gen_expr(rng, kind=rng.choice(["num", "num", "cond"]))
            by = rng.choice([[], ["stream"], ["type"], ["open"], ["parent"],
                             [rng.choice(NAMES)], ["type", rng.choice(NAMES)]])
            window = rng.choice([(None, None), (30, 200), (None, 100)])
            try:
                sel = tp.select(where, None, None, window)
                tp.select(where, None, None, window)
                from trlog.query._analysis import value_expr
                value_expr(tp, value, sel)
                for k in by:
                    tp._group_key(k, sel)
            except QueryError:
                continue
            ran += 1

            def stats(t):
                r = t.stats(value, where=where, by=by, time=window)
                return ([(_canon_key(k), _canon_summary(g)) for k, g in r.groups],
                        r.matched, r.stats.blocks_read)

            def hist(t):
                r = t.hist(value, where=where, time=window)
                return (_canon(r.bin), _canon(r.lo), r.counts, r.n, r.missing,
                        _canon(r.min), _canon(r.max), r.matched)

            def count(t):
                r = t.count(where, by=by, time=window)
                return [(_canon_key(k), n) for k, n in r.groups], r.matched, \
                    r.stats.blocks_read, r.stats.duplicate_ids
            def gaps(t):
                r = t.gaps(min=rng_gap, where=where, time=window, sort="stream,start")
                return ([(g.stream, g.start, g.end, g.prev and g.prev.ref,
                          g.next and g.next.ref) for g in r.gaps],
                        r.range, r.streams, r.silent, r.stats.blocks_read)
            rng_gap = (0, 1, 5)[ran % 3]       # not from rng: keeps the seeds
            for f in (stats, hist, count, gaps):
                a, b = _outcome(lambda: f(tp)), _outcome(lambda: f(tc))
                assert a == b, (f"seed {seed} {f.__name__}: value={value!r} "
                                f"where={where!r} by={by} window={window}")
        assert ran > 5
    finally:
        tp.close()
        tc.close()


@needs_c
def test_aggregates_skip_txn_objects(corpus_path, monkeypatch):
    """On C, a count/stats over integer data builds no Txn at all."""
    from trlog.query import _engine
    built = []
    real = _engine.Txn.__init__

    def counting(self, *a, **kw):
        built.append(1)
        real(self, *a, **kw)
    with tq.open(corpus_path, engine="c") as t:
        monkeypatch.setattr(_engine.Txn, "__init__", counting)
        assert t.count("len > 1").matched == 7
        r = t.stats("dur", by=["stream"])
        assert r.matched == 19
        t.hist("len")
        t.info()
        assert built == []


@needs_c
def test_random_traces_mostly_run_in_c(tmp_path):
    """The differential test is only worth something if C does the work:
    row-layout traces must not fall back (column layout always does)."""
    in_c = 0
    for seed in SEEDS:
        path = tmp_path / f"r{seed}.trl"
        build_random(path, seed)
        with tq.open(path, engine="c") as t:
            try:
                t.find("dur > 1 || id == 3")
            except Exception:   # noqa: BLE001
                # Column/adaptive traces the Python reader cannot read back
                # either (a codec bug outside Q4; see query_impl_status.md).
                assert t.c_fallbacks and "layout" in t.c_fallbacks[0]
                continue
            if not t.c_fallbacks:
                in_c += 1
            else:
                assert re.search(r"layout|kind|UTF-8", t.c_fallbacks[0]), t.c_fallbacks
    assert in_c >= len(SEEDS) // 2


@needs_c
def test_defer_and_keep_both_happen(corpus_path):
    """A mixed filter: C decides the integer part, Python the string part."""
    with tq.open(corpus_path, engine="c") as t:
        r = t.find("len >= 0 && name =~ /burst/ || resp == 'DECERR'")
        assert t._cscan is not None and not t.c_fallbacks
        with tq.open(corpus_path, engine="python") as p:
            assert [x.id for x in r.txns] == [x.id for x in p.find(
                "len >= 0 && name =~ /burst/ || resp == 'DECERR'").txns]


@needs_c
def test_column_layout_falls_back(tmp_path):
    path = tmp_path / "col.trl"
    with TrlWriter(str(path), timescale_exp=-12, column_layout="column") as w:
        ty = w.add_txn_schema("t", [FieldDef(name_str_id=w.intern("v"),
                                             field_type=FieldType.FT_U32)])
        st = w.add_stream_type("s")
        with w.begin_hierarchy() as h:
            h.begin_scope(ScopeType.ST_MODULE, "top")
            s = h.add_stream(st, "m")
            h.end_scope()
        with w.begin_txn_block(0, stream_inst_id=s) as b:
            for i in range(10):
                b.write_full(s, ty, i + 1, i * 10, i * 10 + 5, 0, [TxnAttr(0, i)])
    with tq.open(path, engine="c") as t:
        assert [x.id for x in t.find("v > 6").txns] == [8, 9, 10]
        assert t.c_fallbacks and "layout" in t.c_fallbacks[0]
