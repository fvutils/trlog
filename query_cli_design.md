# trlog query CLI: design for searching, filtering and navigating transactions

Status: **reviewed, decisions recorded in §11** (2026-11-21). Phases Q1-Q4 are implemented; progress, decisions made during implementation and known limits are in `query_impl_status.md`.

## 1. Goals and audience

The CLI answers questions about a `.trl` trace. Each call is a single batch
command: open the file, run one or more queries, print the results, exit.

- **Primary audience: LLM agents.** An agent has no screen and no
  persistent session. It pays for every byte of output in context, and it
  works in loops: orient, narrow, drill down, follow a reference, repeat.
  The CLI has to make each of those steps one cheap call, with output that
  is predictable, compact, self-describing and easy to chain.
- **Secondary audience: humans at a terminal.** They use the same commands
  and expression language, but get aligned text with human time units, and
  trees instead of JSON.
- **Fast.** "How many writes to 0x4000?" on a multi-GB trace should take
  well under a second when the index can narrow it, and seconds (not
  minutes) when it has to scan the whole file.

Interactive and exploratory work is expected to go through other routes:
a targeted `trlog export` to SQLite or Parquet, or a custom Python script
that uses the same query engine as a library (§7.1). So the engine's
Python API is a supported public interface, not just CLI internals.

Non-goals: interactive TUI, waveform rendering, editing trace contents, and
a server/daemon or MCP mode (§11, decision 4).

## 2. The questions people actually ask

These come from verification debug, which is the main use. Each row says
which verb answers it (§4).

| # | Question | Verb |
|---|----------|------|
| **Orientation** | | |
| Q1 | What is in this trace: streams, types, time span, counts, metadata? | `info` |
| Q2 | Which streams exist under `top.env.agent*`? How busy is each one? | `streams` |
| Q3 | What fields does `axi_rd` have, and what values do they take? | `schema` |
| **Find** | | |
| Q4 | All writes to address 0x4000 between 10us and 20us | `find` |
| Q5 | First (or last, or nth) error response | `find --first/--last` |
| Q6 | Transactions that never ended (hung or still open at end of sim) | `find 'open'` |
| Q7 | Transactions slower than 500ns, slowest first | `find ... --sort -dur` |
| Q8 | Transactions recorded from `my_scoreboard.sv:212` | `find 'meta["trlog.src.file"] ...'` |
| **Inspect and navigate** | | |
| Q9 | Show everything about transaction t:1234 | `show` |
| Q10 | Its parent, children, and causal links, up or down N levels | `tree`, `links` |
| Q11 | Root cause chain: follow cause links back to the origin | `links --up --kind cause` |
| Q12 | What else happened around 15.2us, on every stream? | `around` |
| Q13 | The 5 transactions before and after t:1234 on the same stream | `around t:1234 --same-stream` |
| **Aggregate** | | |
| Q14 | Count by stream / type / field value (resp=OKAY vs SLVERR) | `count --by` |
| Q15 | Latency distribution (min, p50, p99, max) per stream | `stats dur --by stream` |
| Q16 | Throughput over time: transactions per microsecond window | `hist start --bin 1us` |
| Q17 | Gaps: where did stream X go quiet for more than 2us? | `gaps` |
| **Correlate** | | |
| Q18 | Pair requests with responses by tag; latency of each pair; unmatched ones | `match` |
| Q19 | Which requests were outstanding at time T? | `find 'start <= T && (end > T \|\| open)'` (`active` alias) |
| Q20 | Compare with a passing run: where do the two traces first diverge? | `diff` |
| **Signals** (lower priority) | | |
| Q21 | What was `top.dut.state` at time T, or during t:1234? | `value` |

Two observations shape the rest of the design:

1. **Agents mostly ask set questions first, then drill into instances.**
   "How many, where, which kinds?" comes before "show me this one". So
   every listing gives a summary of the whole matched set, even when it
   only returns 20 rows, and every row carries a reference the agent can
   pass straight back to `show`, `tree` or `around`.
2. **Most debug is relational.** Request to response, parent to child,
   cause to effect, this run to the passing run. Those relations get their
   own verbs, rather than leaving agents to emulate joins with several
   `find` calls and their own bookkeeping.

## 3. How results are modelled

### 3.1 The transaction record

Every verb that returns transactions uses the same record shape:

```
ref     t:1234                      stable handle (txn id), accepted by every verb
stream  top.env.axi0.mon            full hierarchical path
type    axi_rd                      schema name
start   15200000   end 15240000     integer trace time; dur is derived
dur     40000
parent  t:1200 | null
open    false                       true when end is missing or truncated
fields  {addr: 0x4000, len: 4, resp: "OKAY"}
links   [{kind: cause, dir: out, to: t:1290, label: "..."}]   (only when asked)
meta    {"trlog.src.file": "...", ...}                          (only when asked)
```

### 3.2 References

References are short typed tokens that every verb accepts where it expects
a target:

| Form | Meaning |
|------|---------|
| `t:1234` | a transaction, by txn id |
| `s:top.env.axi0.mon` | a stream, by full path (globs accepted where a set is fine) |
| `@15.2us` or `@15200000` | a point in time |
| `v:top.dut.state` | a signal (VC variable) |

Txn ids are unique within a trace, so `t:<id>` is a stable handle across
calls. An agent can hold it in its notes and come back to it later.

### 3.3 The result envelope

JSON is produced only when asked for (`--json` / `--jsonl`, §3.6). JSON
output is a single envelope, so an agent can always find the counts
and the truncation status in the same place:

```json
{
  "cmd": "find",
  "query": {"where": "addr == 0x4000 && type == 'axi_wr'", "time": [10000000, 20000000]},
  "time_unit": "ps",
  "summary": {
    "matched": 312, "returned": 20, "truncated": true,
    "span": [10012000, 19987000],
    "by_stream": {"top.env.axi0.mon": 300, "top.env.axi1.mon": 12}
  },
  "results": [ {"ref": "t:1234", "...": "..."} ],
  "next": "--offset 20",
  "scan": {"blocks_read": 41, "blocks_skipped": 977, "ms": 38}
}
```

- `summary` is computed over the full matched set, not just the page. This
  answers "how many and where" in the same call, and stops the agent from
  drawing conclusions from the first page.
- `truncated` and `next` are always present. When results are cut short,
  the output says so and tells the agent how to get more. Silent
  truncation is the main failure mode to avoid with agents.
- `scan` reports what the query cost, so an agent (or a human) can tell a
  cheap indexed query from a full scan.

`--jsonl` streams one record per line, followed by a final line holding
`{"summary": ...}`. It is meant for piping into `jq` or for very large
exports.

### 3.4 Value encoding in JSON

- Integers up to 2^53 are JSON numbers. Wider integers, bitvecs and
  logicvecs are hex strings (`"0x1f"`, with `x`/`z` digits for 4-state).
- Enum fields are their name string. `--raw` adds the numeric value.
- `bytes` fields are hex strings, abbreviated beyond `--max-field-bytes`
  (default 64) with a `"…(+N bytes)"` marker.
- Times are integers in trace units, and the envelope's `time_unit` says
  which unit. Text mode prints human units (`15.200us`).
- Missing fields are omitted, not null, which keeps rows short.

### 3.5 Keeping output small

These defaults matter most for agents, because they decide whether one call
costs 2 KB or 200 KB of context:

- `--limit` defaults to 20 for listings. `--limit 0` gives the summary only.
- The default projection is `ref, stream, type, start, dur, fields`. Links
  and meta appear only with `--with links,meta`. `--fields addr,resp` narrows
  `fields`, and `--cols` picks top-level columns.
- Stream paths can be long and repetitive. In JSON, a page whose rows all
  share a common prefix states it once (`"stream_prefix": "top.env."`) and
  the rows carry the remainder. `--full-paths` turns this off.
- `--max-bytes N` is a hard cap on output size. When it is hit, rows are
  dropped from the end and `truncated` is set.

### 3.6 Output formats

There are three output modes:

| Mode | When | What |
|------|------|------|
| Formatted text | default when stdout is a TTY | Aligned columns, colour, human time units, terminal-width-aware truncation of long values |
| Plain text | default when stdout is not a TTY | The same lines and summary footer, with no ANSI escapes, no colour and no width-based truncation, so `grep`, `awk` and an agent's context all see stable text |
| JSON | only with `--json` or `--jsonl` | The envelope in §3.3 |

Agents therefore pass `--json` when they want structure. `help agent`
says so in its first line. Plain text stays useful for agents that only
need a quick look, and it is usually smaller than the JSON.

One line per transaction, aligned in columns:

```
t:1234  15.200us  +40ns  top.env.axi0.mon  axi_wr  addr=0x4000 len=4 resp=OKAY
t:1251  15.310us  +38ns  top.env.axi0.mon  axi_wr  addr=0x4000 len=1 resp=SLVERR
-- 312 matched, showing 20 (--offset 20 for more); 10.012us .. 19.987us
```

`tree` prints indented output, `show` prints a vertical key/value block,
and `stats` and `count` print small tables. `--color=auto|always|never`
overrides the TTY detection.

### 3.7 Exit codes

These follow grep, so shell pipelines and agents can branch without
parsing output: 0 means one or more matches, 1 means no matches (not an
error), 2 means a usage, expression or file error.

## 4. Command surface

```
trlog <verb> TRACE [options] [EXPR]
```

The common options are the same on every verb:

| Option | Meaning |
|--------|---------|
| `-s, --stream GLOB` | Stream selection by path glob, repeatable. This is pushed down to the per-stream block index. |
| `-T, --type NAME` | Type selection, repeatable |
| `-t, --time A..B` | Time window, with units (`10us..20us`, `..5us`, `@15us±200ns`). This is pushed down to the time index. |
| `EXPR` / `-w, --where EXPR` | Filter expression (§5) |
| `--json` / `--jsonl` | JSON output (§3.3). Without either, output is text: formatted on a TTY, plain otherwise (§3.6). |
| `--limit N --offset N --sort KEY[,-KEY]` | Paging and ordering. The default order is (start, id). |
| `--first` / `--last` / `--nth N` | Shorthand for the matching limit, offset and sort |
| `--with links,meta,children` | Optional sections to include in each record |

`-s`, `-T` and `-t` are kept out of the expression on purpose. They are
the selections the index can serve, so making them explicit means a query
is fast by construction. The same conditions written inside `EXPR` still
work, and the planner lifts them out when it can (§7.2).

### 4.1 Verbs

| Verb | Purpose and output |
|------|--------------------|
| `info` | Q1: file version, time span, time unit, trace metadata, hierarchy and stream counts, txn count, whether an index is present, and a short "try next" hint list. The first call an agent makes. |
| `streams [GLOB]` | Q2: path, type(s), txn count, first and last time, open count, and stream metadata. `--tree` shows the hierarchy. |
| `schema [TYPE]` | Q3: fields with kind and width, and enum values. `--sample N` adds observed min/max and the top-k values per field from a sample, so an agent learns what values are realistic before it writes a filter. |
| `find [EXPR]` | Q4-Q8: transaction listing plus the set summary. |
| `show REF...` | Q9: full records, with links, meta, parent and child refs included by default. |
| `tree REF [--up N] [--down N]` | Q10: parent/child structure. `--up` walks to the root. |
| `links REF [--kind K] [--up/--down N]` | Q11: follows link edges (cause, related, custom) transitively, and prints them as a chain or a DAG. |
| `around REF\|@TIME [-n N] [--window W] [--same-stream]` | Q12-13: what happened nearby, merged across streams in time order. |
| `count [EXPR] --by KEY[,KEY]` | Q14: group counts. KEY is `stream`, `type`, a field, or `meta[...]`. |
| `stats VALUE [EXPR] [--by KEY]` | Q15: n, min, max, mean, p50, p90, p99, plus a ref to the min and max transactions. VALUE is `dur`, `start`, a field, or any numeric expression. |
| `hist VALUE [--bin W \| --bins N] [EXPR]` | Q16: a binned histogram, as bars and a sparkline in text mode and an array in JSON. Without `--bin`, a 1/2/5 width is picked. |
| `gaps [-s GLOB] [--min W] [EXPR]` | Q17: idle intervals per stream that are longer than W, longest first, including leading and trailing silence in the `-t` window or the trace span. |
| `match --a EXPR --b EXPR [--on 'a.F == b.G'] [--order fifo\|any] [--within W] [--unmatched a\|b]` | Q18: pairs, plus unmatched-a and unmatched-b sets. Each pair carries `lat = b.end - a.start`. Latency stats are always in the summary (no `--stats` flag). |
| `active @TIME [EXPR]` | Q19: transactions that are open at that instant. |
| `diff TRACE_B [-s GLOB] [--key FIELD,...] [--ignore FIELD,...]` | Q20: aligns per stream, by ordinal or by key, and reports the first N divergences (missing, extra, or field mismatch) with refs into both traces. |
| `value v:PATH @TIME\|REF\|--time A..B` | Q21: a signal value at a time, during a transaction, or its changes in a window. |
| `batch` | Reads one command per line from stdin, opens the trace once, and writes each command's output in order (§6). |
| `export FORMAT -o OUT [EXPR] [--force]` | Writes the selected transactions to `sqlite` or `parquet` for ad-hoc analysis and Python scripts. Uses the same `-s`/`-T`/`-t`/`EXPR` selection, so an export can be targeted. The table layout is in §4.2. |
| `index TRACE` | Adds the query index (§7.4) to a trace that lacks one, in place. |
| `help [expr\|refs\|json\|VERB]` | Short, example-led help. `help agent` prints a one-page cheat sheet meant to be read into an LLM's context. |

`find --count` is the same as `count` with no grouping. It is kept because
agents reach for it naturally.

### 4.2 Export layout

`export sqlite` and `export parquet` write the same normalised tables. In
Parquet each table is a separate file in the output directory.

| Table | Columns |
|-------|---------|
| `txn` | `id, stream_id, type_id, start, end, dur, parent, open` |
| `attr_<type>` | one table per type: `id` plus one typed column per field. Enums are stored as their name; wide vectors as hex text. |
| `link` | `src, tgt, kind, label` |
| `meta` | `id, key, value` |
| `stream` | `id, path, type, first, last, count` plus stream metadata as JSON text |
| `type` | `id, name, fields` (JSON text) |
| `trace` | key/value trace metadata, including `time_unit` |

One table per type keeps field columns typed, so SQL like
`WHERE addr BETWEEN 0x4000 AND 0x40ff` works without casts. A `txn_<type>`
view joins `txn` with each attribute table for convenience.

SQLite comes from the standard library. Parquet needs `pyarrow`, which is
an optional dependency, and the command says how to install it when it
is missing.

## 5. Filter expression language

The language is small, C/Python-like, and evaluated per transaction. It is
designed so an LLM writes it correctly the first time. It uses familiar
operators, has no surprising precedence, and reports errors that say what
to do instead.

### 5.1 Names

| Name | Meaning |
|------|---------|
| `id start end dur parent open` | Built-in header fields. `open` is a bool. `dur` is missing when the transaction is open. |
| `stream type` | Strings: the full stream path and the schema name |
| `addr`, `resp`, ... | Attribute fields of the transaction's type, looked up by name |
| `axi_rd.addr` | A type-qualified field, for when field names clash across types |
| `meta["trlog.src.file"]` | A meta value (string) |
| `nlinks nchildren` | Edge counts. These need the reverse index (§7.3). |
| `parent.FIELD` | One level up, for example `parent.type == 'seq_item'` |

### 5.2 Literals and operators

- Literals are integers in decimal, `0x`, `0b` or `'h`-style (`32'h4000` is
  accepted because SV users type it), floats, strings in `'...'` or `"..."`,
  times with units (`10ns`, `1.5us`), `true` and `false`.
- Comparisons are `== != < <= > >=`.
- Logic is `&& || !`, and `and or not` are accepted as aliases.
- Arithmetic and bit operators are `+ - * / % & | ^ ~ << >>`. A mask test
  looks like `(addr & 0xfff) == 0`.
- Membership is `resp in ('SLVERR', 'DECERR')`, and ranges are
  `addr in 0x4000..0x4fff`.
- Matching uses `stream ~ 'top.*.axi?.mon'` for globs and `msg =~ /timeout|err/`
  for regex.
- Functions: `has(field)`, `abs()`, `min()`, `max()`, `bit(x, n)`,
  `bits(x, hi, lo)`.

### 5.3 Missing values

A field that is absent (wrong type, not set, or an open transaction's
`dur`) makes any comparison that uses it false, including `!=`. This means
`addr != 0` never matches transactions that have no `addr`. To select those
explicitly, use `has(addr)` or `!has(addr)`. This is the rule that surprises
people least when a filter spans mixed types.

### 5.4 Errors meant for agents

Errors are returned as structured JSON with exit code 2:

```json
{"error": "unknown_field", "name": "adr", "pos": 0,
 "did_you_mean": ["addr"], "known_fields": {"axi_rd": ["addr","len","resp"]},
 "hint": "run `trlog schema TRACE axi_rd` for field kinds"}
```

Type errors (comparing a string field with an integer), unknown enum names
and unknown streams all name the valid alternatives in the same way. The
aim is that a wrong guess costs one retry, not a round of exploration.

## 6. Batch mode and chaining

Agents often want several views of the same trace in a row. Opening a
large trace repeatedly wastes time, so `batch` opens it once:

```
$ trlog batch run.trl <<'EOF'
info
count --by stream,resp -T axi_wr
find resp == 'SLVERR' --first --with links
EOF
```

Output follows §3.6. In text mode, each command's output is preceded by
a `== [n] <command line>` header line. With `--json`, it prints one
envelope per line, in order, each tagged with `"seq": n`. An error in one
command does not stop the others.

The same flow works without `batch`, because refs in one call's output are
valid arguments to the next call. Every record carries `ref`, and `show`,
`tree`, `links` and `around` all take refs. Batch mode only saves the open
cost.

`$prev` substitution (feeding one line's refs into the next) is
intentionally out of v1. Agents can do that step themselves, and it adds a
scripting language we would then have to maintain.

## 7. Engine and performance

### 7.1 Where the code lives

The work splits into two parts:

- **Front end in Python.** This is argument parsing, the expression parser
  and its error messages, output formatting, and the `match`/`diff`/`stats`
  orchestration. It is quick to iterate on and easy to give good error
  messages.
- **Public Python API** (`trlog.query`) underneath the CLI. Each verb is a
  thin wrapper over a function that returns joined transaction records and
  summaries as Python objects, for example
  `q = trlog.query.open(path); q.find("resp != 'OKAY'", streams="top.*")`.
  Custom scripts use this directly, which is the main interactive route
  (§1).
- **Scan engine in C**, built on `query.c`, so the per-transaction loop
  never runs in Python. The front end compiles `EXPR` into a small stack
  bytecode, following the precedent of `_derived.py`'s derived-signal
  bytecode. The C engine evaluates it during the scan and returns only the
  rows and aggregates that survive.

Delivery is staged:

1. **Pure Python engine first**, over `TrlReader`. Adding a join step
   (BEGIN/ATTR/END/LINK/META to a transaction record) is needed anyway. This
   stage pins down the semantics and output with golden tests on the
   existing fixtures.
2. **C engine second**, behind the same tests. As built in Q4, this is
   `trl_qscan_*` in its own file (`c/include/trl/trl_qscan.h`,
   `c/src/qscan.c`), driven through ctypes by `trlog/query/_cengine.py`
   rather than `_native.py`. Python hands C the block plan, field types,
   field-name slots and value normalisation, so C only decodes, joins and
   filters. The filter bytecode is three-valued: keep, drop, or defer to
   the Python filter. That keeps the output exact for every expression,
   while C decides the common integer and enum cases. Blocks C does not
   decode the way Python does make the scan fall back to Python.

Getting the semantics right in step 1 is cheap. The speed comes in step 2
without changing any output.

**The C engine is an optional accelerator, not a requirement** (decision
of 2026-11-21). `trlog.query` must import and work fully on a platform with
no native library or wheel, using the pure-Python engine. The C engine is
used only when the native library loads, and both engines must give the
same output, which the differential tests check. No semantics may exist
only in C.

### 7.2 Pushdown and block skipping

- `-s`/`-T`/`-t`, and conjuncts in `EXPR` that only involve `stream`,
  `type`, `start` or `end`, are lifted into the scan plan. They then select
  TXN blocks through the index's per-block (start, end, stream_inst_id)
  entries.
- Index lookups are currently linear scans (`_index.py:195-237`). They
  become bisection over start-sorted entries, which is a small change.
- `read_stream` decodes first and then filters by time. The new engine
  passes the time range to `find_txn_blocks_for_stream`, which already
  accepts one.
- `--first`/`--nth` stop scanning as soon as they have enough rows,
  because blocks are visited in time order. `--last` scans blocks in
  reverse.
- Block decoding is the cost that matters. Within a block, rows are
  filtered before the join, so attrs are only materialised for rows whose
  header passes.

### 7.3 Things the current format cannot do cheaply

Three of these need extra index data in the trace (§7.4), and the last
one needs a cursor change:

- **Children and incoming links.** The file stores parent→child as
  `parent` on the child, and links as forward edges. `tree --down`,
  `nchildren` and incoming-link queries need a reverse map.
  - v1 builds that map during the scan, limited to the selected window,
    which is usually enough because children nest in time inside their
    parent.
  - A full-trace answer needs the reverse-edge section of the query index
    (§7.4).
- **Field-value search** (`addr == 0x4000` across the whole trace) is a full
  scan of the selected streams. The proposed fix is per-block min/max
  ("zone maps") for numeric fields, stored in the query index (§7.4). Address-range and ID searches would then skip most
  blocks. This is deferred until there is a benchmark that shows the need.
- **Txn id to block lookup** for `show t:1234` has no direct index. v1 takes
  an optional time hint from the ref (refs printed by the CLI can carry
  one, as `t:1234@15200000`, which still parses as `t:1234`). The query
  index holds an id→block table (§7.4).
- **C cursor memory.** The current C cursor loads the whole selection up
  front (`trl_query.h:22-26`). The scan engine has to be streaming, block
  by block, so that `count` and `stats` run in constant memory over large
  traces.
  - *Status after Q4:* the query scan (`trl_qscan`) is not streaming yet.
    TR_ATTR, TR_META and TR_LINK records may follow a txn's TR_END in a
    later block, and the reference engine attaches them, so a txn cannot
    be finished early.
  - Constant memory needs a writer guarantee about where those records may
    appear, which is a Q5 format question.
  - C does hold txns compactly and materialises only the rows that survive
    the filter.

### 7.4 Query index, stored in the trace

The extra index data lives inside the trace, not in a separate file. It is
a set of new sections in the existing `BLK_INDEX` block, after the current
sentinel sections 0x01 to 0x04 (`_index.py:93-120`):

| Sentinel | Section | Answers |
|----------|---------|---------|
| 0x05 | txn id → TXN block offset (sorted ranges, one entry per block) | `show t:1234` without a time hint |
| 0x06 | reverse edges: per TXN block, the blocks holding its children and incoming-link sources | `tree --down`, `nchildren`, incoming `links` over the whole trace |
| 0x07 | zone maps: per TXN block and numeric field, min and max | skipping blocks for field-value predicates |

How it gets there:

- **At close time.** The C and Python writers already patch the index
  offset into `FILE_HDR` when they close the trace. They also collect the
  data for these sections while writing. Id ranges and zone maps are cheap
  running min/max values. Reverse edges need a per-block parent set, which
  is bounded by the block size.
- **After the fact,** for traces that were written without the index or by
  older writers: `trlog index TRACE` scans the trace, appends a new
  `BLK_INDEX` block at the end of the file, and rewrites the 8-byte index
  offset in `FILE_HDR`. The old index block becomes dead bytes. This is a
  small, in-place change to the header; `--output NEW.trl` writes a copy
  instead for read-only traces.
- **When it is missing** (a crashed run with no close, or an unindexed
  trace), every verb still works. It falls back to the in-window and
  time-hint strategies in §7.3, and `scan` in the envelope reports
  `"qindex": false` so the cost is visible.

Two things to confirm before implementation:

- Old readers have to ignore the new sections. The current decoder checks
  for each sentinel in turn, so trailing unknown bytes should be skipped,
  but this needs a test with an old reader on a new file.
- The zone-map section adds write-time cost per field. It should be
  limited to integer fields and possibly made opt-in per type, to be
  decided after the Q4 benchmark.

## 8. Examples, end to end

```
# Orient
trlog info run.trl
trlog streams run.trl 'top.env.*'
trlog schema run.trl axi_wr --sample 1000

# Find and drill down
trlog find run.trl -T axi_wr "addr in 0x4000..0x40ff && resp != 'OKAY'" --first
trlog show run.trl t:1251 --with links,meta
trlog links run.trl t:1251 --up 5 --kind cause
trlog around run.trl t:1251 --window 200ns

# Aggregate
trlog count run.trl --by stream,resp -t 0..50us
trlog stats run.trl dur -s 'top.env.axi*' --by stream
trlog gaps run.trl -s top.env.axi0.mon --min 2us

# Correlate
trlog match run.trl --a "type=='req'" --b "type=='rsp'" --on 'a.tag == b.tag' --within 10us
trlog find run.trl open --sort start
trlog diff fail.trl pass.trl -s 'top.env.*.mon' --ignore timestamp --limit 5
```

## 9. Testing

- **Golden tests:** each verb runs against fixtures in `tests/golden/fixtures`,
  plus a new purpose-built `query_corpus.trl` that has parent/child, links,
  open transactions, mixed types per stream, and enums. The JSON envelopes
  are checked in as expected output.
- **Differential tests:** the Python engine and the C engine must produce
  byte-identical JSON on the same corpus.
  - Every golden case runs on both engines (Q4).
  - `tests/query/test_cengine.py` adds randomized traces covering every
    field kind, open/duplicate txns, META/LINK and mixed blocks, each run
    against randomized expressions.
  - Results are compared value-type exact.
- **Expression fuzzing:** generated expressions are checked against a
  reference evaluator written in Python.
- **Agent usability check:** a scripted set of the §2 questions is given to
  an LLM that has only `trlog help agent` as documentation. We measure
  whether it reaches the answer, how many calls that took, and how many
  bytes of output it read. This is the metric that matters for the primary
  audience, so it is worth having from the start.

## 10. Phasing (proposal)

| Phase | Scope |
|-------|-------|
| Q1 | Packaging and entry point (§11, decision 3). Public `trlog.query` API. Joined transaction iterator in Python. `info`, `streams`, `schema`, `find`, `show`, `count`. Expression parser and Python evaluator. JSON envelope and text formatter. Golden tests. |
| Q2 | Navigation: `tree`, `links`, `around`, `active`, plus the in-window reverse map. `batch`. `help agent`. |
| Q3 | Analysis: `stats`, `hist`, `gaps`, `match`. `export sqlite` / `export parquet`. |
| Q4 | C scan engine: bytecode evaluator, streaming cursor, index bisection, ctypes binding. Differential tests. Benchmark on a large generated trace. *Done, except the streaming cursor (see §7.3) and index bisection, which was not needed: the plan is computed once per scan.* |
| Q5 | Query index in the trace (§7.4): writer support in C and Python, `trlog index`, and engine use of it. `diff`. `value` (signals). |

## 11. Decisions (review of 2026-11-21)

1. **Custom language versus SQL:** custom verbs and expressions are the
   main interface. `trlog export sqlite|parquet` is the route to SQL
   (§4.2). No built-in `sql` verb.
2. **Output format:** formatted text on a TTY, plain text when stdout is
   not a TTY, and JSON only when selected with `--json`/`--jsonl` (§3.6).
3. **Entry point:** add one, with `trlog` as the root command. The Python
   package has no `pyproject.toml` today (`python/` holds only `src/`, and
   the environment is set up by ivpm). The plan is:
   - a `pyproject.toml` that packages `python/src/trlog`, with
     `[project.scripts] trlog = "trlog.cli:main"`;
   - a `trlog/__main__.py`, so `python -m trlog` works without
     installation;
   - the native library is still found through `TRL_LIBRARY_PATH`, as
     `_native.py` does today.
4. **MCP server:** out of scope. Interactive work goes through targeted
   export plus Python scripts, or custom scripts on `trlog.query` (§1, §7.1).
5. **Signals:** deferred to Q5 as proposed.
6. **Extra index data:** stored in the trace itself, as new `BLK_INDEX`
   sections written at close time, with `trlog index` to add them later
   (§7.4). No sidecar file.
