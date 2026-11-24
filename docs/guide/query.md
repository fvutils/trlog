# Querying traces from Python

The `trlog.query` package is the Python API behind the
{doc}`trlog command <cli>`. Every verb of the command is a method of
{class}`trlog.query.Trace` that returns plain Python objects, so a script
can run the same queries, chain them, and work with the results directly.

The examples use the same corpus trace as the command guide (`run.trl`,
built by `tests/query/corpus.py`), and `bus.trl` for `match`. The output
shown in comments is real.

## Opening a trace

{func}`trlog.query.open` returns a {class}`~trlog.query.Trace`. Use it as a
context manager, or call `close()` when done.

```python
import trlog.query as tq

with tq.open("run.trl") as q:
    print(q.engine, q.time_unit, q.span())
    # c ps (1000, 70000)
```

Every time in the API is an integer tick count in the trace's time unit
(`q.time_unit`, from `q.timescale_exp`); here one tick is 1 ps.

`engine` picks the scan engine: `"auto"` (the default: the C engine in
`libtrl` when it loads, else pure Python), `"python"`, or `"c"` (an error
when the library is not available). Without the argument, the
`TRLOG_ENGINE` environment variable is used. Both engines give the same
results. See {ref}`cli-engines` for where the library is
looked for and which inputs always run in Python.

```python
with tq.open("run.trl", engine="python") as q:
    print(q.engine, q.count("resp == 'OKAY'").matched)
    # python 8
```

## The catalog

`Trace.streams` maps stream ids to {class}`~trlog.query.StreamInfo`, and
`Trace.types` maps type ids to {class}`~trlog.query.TypeInfo`. Both are
built when the file opens, without scanning transaction blocks.

```python
with tq.open("run.trl") as q:
    for s in q.streams.values():
        print(s.id, s.path, s.decl_name, s.kind)
    # 1 top.env.axi0.mon axi_mon axi
    # 2 top.env.axi1.mon axi_mon axi
    # 3 top.env.seqr seq sequencer
    # 4 top.log log log

    ti = q.type_by_name("axi_wr")
    print([(f.name, f.kind) for f in ti.fields])
    # [('addr', 'u64'), ('len', 'u8'), ('resp', 'enum'), ('strb', 'bytes')]
    print(ti.field("resp").enum_labels)
    # {0: 'OKAY', 1: 'EXOKAY', 2: 'SLVERR', 3: 'DECERR'}
```

`stream_summary()` and `schema()` return the rows that the `streams` and
`schema` commands print, and `info()` the facts of `trlog info`.

## Selections

The selecting methods (`find`, `count`, `active`, `around`, `stats`,
`hist`, `gaps`, `export`) take the same arguments as the command's
selection options:

| Argument | Command option | Value |
|----------|----------------|-------|
| `where` | `EXPR` / `-w` | a filter expression string (see {ref}`cli-expressions`) |
| `streams` | `-s` | a stream path glob, or a list of them |
| `types` | `-T` | a type name or glob, or a list of them |
| `time` | `-t` | `(lo, hi)` in ticks, inclusive; `None` for an open side |

`parse_window` and `parse_time` convert the command's time syntax to
ticks:

```python
with tq.open("run.trl") as q:
    lo, hi = tq.parse_window("20ns..30ns", q.timescale_exp)
    print(lo, hi)
    # 20000 30000
    r = q.find(streams="top.env.*.mon", time=(lo, hi), sort="-start")
    print([t.ref for t in r.txns])
    # ['t:22', 't:21', 't:20']
```

`Trace.select()` resolves a selection once, and each method also accepts
it as `selection=`:

```python
with tq.open("run.trl") as q:
    sel = q.select("len > 1", types="axi_*")
    print(sel.query_dict(), sorted(sel.type_ids))
    # {'where': 'len > 1', 'types': ['axi_*']} [1, 2]
    print(q.count(selection=sel).matched)
    # 7
```

A method may change the window of a selection it is given (`active` and
`around` set it), so resolve a fresh one for each call when that matters.

## Finding transactions

`find()` returns a {class}`~trlog.query.FindResult` holding every match
(the API does not page), sorted by `sort` (default `"start,id"`; prefix a
key with `-` for descending).

```python
with tq.open("run.trl") as q:
    r = q.find("resp != 'OKAY'", types=["axi_rd", "axi_wr"])
    print(r.matched, r.span(), r.by_stream())
    # 2 (42000, 49500) {'top.env.axi1.mon': 2}
    for t in r.txns:
        print(t.ref, t.stream, t.type, t.start, t.end, t.dur, t.open, t.parent)
        print(t.fields)
    # t:30 top.env.axi1.mon axi_wr 42000 45000 3000 False 3
    # {'addr': 32768, 'len': 1, 'resp': EnumVal(2, 'SLVERR'), 'strb': b'\xff'}
    # t:31 top.env.axi1.mon axi_rd 47000 49500 2500 False 3
    # {'addr': 32772, 'len': 1, 'resp': EnumVal(3, 'DECERR'), 'data': 0, 'bus': LogicVal(0xzf)}
```

### Transactions and values

A {class}`~trlog.query.Txn` has `id`, `stream`, `type`, `start`, `end`
(None while open), `parent` (None when there is none), `fields`, `meta`,
`links` and `children`, plus the properties `ref`, `open` and `dur`.
`links` holds {class}`~trlog.query.Link` records only when the scan read
links (`want_links=True`, or any navigation method); `children` is filled
by the navigation methods and `show`.

Field values are normalised:

- integers, bitvecs and 2-state logicvecs are `int`;
- enums are {class}`~trlog.query.EnumVal`, an `int` that also carries its
  `label`;
- logicvecs with x or z bits are {class}`~trlog.query.LogicVal`;
- strings, floats and bytes are `str`, `float` and `bytes`.

```python
v = r.txns[0].fields["resp"]
print(repr(v), v.label, v == 2)
# EnumVal(2, 'SLVERR') SLVERR True
```

{data}`~trlog.query.MISSING` stands for an absent value inside the
expression engine. In `Txn.fields` an absent field is simply not a key.

## Counting

`count()` returns a {class}`~trlog.query.CountResult`. With `by`, `groups`
holds `(key values, count)` pairs, largest first; a missing key value is
`None`.

```python
with tq.open("run.trl") as q:
    c = q.count(by=["stream", "type"])
    print(c.matched, c.keys)
    # 19 ['stream', 'type']
    for key, n in c.groups:
        print(key, n)
    # ('top.env.axi0.mon', 'axi_rd') 4
    # ('top.env.axi0.mon', 'axi_wr') 4
    # ('top.env.seqr', 'seq_item') 4
    # ('top.log', 'msg') 4
    # ('top.env.axi1.mon', 'axi_rd') 2
    # ('top.env.axi1.mon', 'axi_wr') 1
```

## Navigating

`show`, `tree`, `links` and `around` with an anchor work on the
whole-trace graph from `Trace.graph()`. The first call builds it with one
full scan and keeps it on the `Trace`, so later navigation in the same
script is cheap. Treat the returned `Txn` objects as read-only, because
they are shared with the cache.

```python
with tq.open("run.trl") as q:
    found, missing, stats = q.show([31, 999])
    t = found[0]
    print(t.ref, t.meta, t.links, t.children, missing)
    # t:31 {} [Link(kind='cause', dir='out', other=102, label='resp')] [] [999]

    tr = q.tree(3, down=None)                 # down=None: all levels
    print(tr.root.ref, [(d, x.ref) for d, x in tr.nodes], tr.truncated)
    # t:3 [(0, 't:3'), (1, 't:30'), (1, 't:31')] False

    lr = q.links(101, kinds=["cause"], up=None, down=0)
    print([(d, x.ref) for d, x in lr.nodes])
    # [(-2, 't:3'), (-1, 't:30'), (0, 't:101')]
    print(lr.edges)
    # [(3, 30, 'cause', 'stimulus'), (30, 101, 'cause', 'resp')]

    ar = q.around(anchor=30, n=2)
    print([t.ref for t in ar.txns], ar.n_before, ar.more_before, ar.more_after)
    # ['t:23', 't:3', 't:30', 't:101', 't:31'] 2 True True

    at = tq.parse_time("48ns", q.timescale_exp)
    print(at, [t.ref for t in q.active(at).txns])
    # 48000 ['t:3', 't:31']
```

`tree` and `links` take ids, not `t:ID` strings. In `tree`, `up` and
`down` of `None` mean no limit; node depths are relative to the focus
(ancestors negative). In `links`, distances are negative towards causes and
positive towards effects. An unknown id raises a `not_found`
{class}`~trlog.query.QueryError`; `show` instead returns it in the missing
list.

## Analysis

`stats`, `hist`, `gaps` and `match` return result objects from
`trlog.query._analysis` (see the {doc}`API reference <../api/query>`).

```python
with tq.open("run.trl") as q:
    s = q.stats("dur", types=["axi_rd", "axi_wr"], by=["stream"])
    print(s.target, s.is_time, s.keys, s.matched)
    # dur True ['stream'] 11
    for key, g in s.groups:
        print(key, g.n, g.missing, g.min, g.pct[50], g.max, g.mean,
              g.min_txn.ref, g.max_txn.ref)
    # ('top.env.axi0.mon',) 8 0 2500 2500 3000 2750.0 t:20 t:10
    # ('top.env.axi1.mon',) 2 1 2500 2500 3000 2750.0 t:31 t:30

    h = q.hist("start", bin=10000)
    print(h.bin, h.lo, h.counts, h.n, h.edges(0))
    # 10000 0 [3, 3, 3, 2, 4, 1, 2, 1] 19 (0, 10000)

    gp = q.gaps(min=5000)
    print(gp.range, gp.streams, gp.silent)
    # (1000, 70000) 4 []
    for g in gp.gaps[:3]:
        print(g.stream, g.start, g.end, g.dur,
              g.prev.ref if g.prev else None, g.next.ref if g.next else None)
    # top.env.axi1.mon 1000 42000 41000 None t:30
    # top.env.axi0.mon 36500 70000 33500 t:23 None
    # top.log 19000 45500 26500 t:100 t:101
```

`hist(bin=...)` and `gaps(min=...)` take ticks, not time strings; use
`parse_time` to convert. `Summary.min_txn` and `max_txn`, and `Gap.prev`
and `next`, are light `TxnRef` objects (`id`, `start`, `end`, `stream`,
`type`, `ref`), not full transactions.

`match` pairs B transactions with A transactions. `on` is the `--on` text
(`"tag"` or `"a.tag == b.tag"`) or a list of `(a_expr, b_expr)` pairs.
Each pair is `(a, b, key)`:

```python
with tq.open("bus.trl") as q:
    m = q.match("type == 'req'", "type == 'rsp'", on="tag",
                streams=["top.bus.req", "top.bus.rsp"])
    print(m.order, m.on, len(m.pairs), m.no_key_b)
    # fifo [('tag', 'tag')] 3 1
    for a, b, key in m.pairs:
        print(a.ref, b.ref, key, b.end - a.start)
    # t:1 t:12 (1,) 11
    # t:2 t:11 (2,) 7
    # t:3 t:13 (1,) 10
    print([t.ref for t in m.unmatched_a], [t.ref for t in m.unmatched_b])
    # ['t:4', 't:5'] ['t:14', 't:15']
    print(m.lat.n, m.lat.min, m.lat.pct, m.lat.max)
    # 3 7 {50: 10, 90: 11, 99: 11} 11
```

## Exporting

`export()` writes the selection to SQLite or Parquet in the layout
described in {ref}`cli-export` and returns the row counts:

```python
import sqlite3

with tq.open("run.trl") as q:
    n = q.export("sqlite", "api.db", types="axi_wr", force=True)
    print(n["tables"]["txn"], n["tables"]["link"], n["views"])
    # 5 2 ['txn_axi_rd', 'txn_axi_wr', 'txn_msg', 'txn_seq_item']

db = sqlite3.connect("api.db")
print(db.execute("SELECT id, stream, start, dur, addr, resp FROM txn_axi_wr "
                 "ORDER BY start").fetchall()[-1])
# (30, 'top.env.axi1.mon', 42000, 3000, 32768, 'SLVERR')
```

## Errors

Every user-facing problem (an unknown field, a bad expression, an unknown
stream glob, a missing ref, an unreadable file) raises
{class}`~trlog.query.QueryError`. Its `info` dict is the JSON error object
of the command: `error` (a code) and `message`, plus keys such as `pos`,
`did_you_mean` and `hint`.

```python
with tq.open("run.trl") as q:
    try:
        q.find("rsp == 'OKAY'")
    except tq.QueryError as e:
        print(e)
        print(e.info["error"], e.info["did_you_mean"])
    # unknown field 'rsp'
    # unknown_field ['resp']
```

## Lower-level pieces

- {func}`~trlog.query.compile_filter` compiles an expression against a
  {class}`~trlog.query.SchemaView` (from `Trace.schema_view()`) into a
  {class}`~trlog.query.Filter`, whose `match(txn)` evaluates it on one
  transaction.
- {func}`~trlog.query.format_time` formats ticks for display:
  `format_time(45500, -12)` gives `'45.500ns'`, and with `strip=True`
  `'45.5ns'`.
- `Trace.c_fallbacks` lists the scans the C engine handed back to Python,
  with the reason. It is empty when C handled everything.
