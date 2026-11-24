# trlog query CLI: implementation status

Design: `query_cli_design.md`. This file tracks implementation progress so
a fresh session can pick up from here.

Run the tests with:

```
PYTHONPATH=python/src packages/python/bin/python -m pytest tests -q > logs/q1_pytest.log 2>&1
```

Baseline before Q1 (2026-11-21): 402 passed, 50 skipped.

## Phase Q1: entry point, `trlog.query` API, core verbs

| Step | Scope | Status |
|------|-------|--------|
| 1 | Expression language: lexer, parser, static checks, evaluator (`trlog/query/_expr.py`, values in `_values.py`) | done |
| 2 | Engine: catalog (streams, types, time units), joined txn scan, `info`/`streams`/`schema`/`find`/`show`/`count` (`trlog/query/_engine.py`) | done |
| 3 | CLI: argparse, text and JSON renderers, exit codes (`trlog/cli.py`, `trlog/query/_format.py`, `trlog/__main__.py`) | done |
| 4 | Packaging: `pyproject.toml` with the `trlog` console script | done; verified by `pip install .` into a scratch venv |
| 5 | Test corpus (`tests/query/corpus.py`) plus unit and golden tests | done |

**Q1 is complete** (2026-11-21). Full suite: 558 passed, 50 skipped (402
baseline + 156 new in `tests/query/`). Nothing is committed yet.

## Phase Q2: navigation, batch, help agent

Done 2026-11-21. Uncommitted, on top of the uncommitted Q1 work.

| Step | Scope | Status |
|------|-------|--------|
| 1 | Engine: cached whole-trace graph (txns + links + children) shared by `show`/`tree`/`links`/`--with children`; `nlinks`/`nchildren` in expressions | done |
| 2 | `tree REF [--up N\|all] [--down N\|all] [--limit N]` | done |
| 3 | `links REF [--kind K] [--up N\|all] [--down N\|all]` | done |
| 4 | `around REF\|@T [-n N] [--window W] [--same-stream]` and `active @T` | done |
| 5 | `batch TRACE [--json]`; argparse errors are structured `usage` errors | done |
| 6 | `help agent` and `help batch`; corpus cause chain; goldens and tests; docs | done |

**Q2 is complete.** Full suite: 616 passed, 50 skipped (Q1 ended at 558).
`tests/query/` now has 214 tests: 67 goldens, plus `test_nav.py` (API) and
`test_cli_nav.py` (batch, help, defaults).

### Semantics decided during Q2

- **Graph cache.** `Trace.graph()` does one full scan with links and meta,
  fills `children` (sorted by start, id) on every txn, and keeps the result
  on the `Trace`. `show`, `tree`, `links`, `around REF`, `--with children`,
  `parent.X` and `nchildren` use it. Once it exists, every later query in
  the same `Trace` (a `batch` run, a script) filters it instead of
  rescanning; `scan.cached: true` says so. This is the §7.3 reverse map
  over the whole trace rather than "in-window": it is simpler and always
  correct, and the Python engine scans fully anyway. Q5's index replaces it.
- **`nlinks`** counts link edges in both directions; **`nchildren`** counts
  txns whose parent is this one, across all streams and all time.
- **`tree`** defaults to `--up all --down 1`. Descendants are chosen breadth
  first under `--limit` (default 200), so a cut drops deep levels, not later
  siblings. Output is pre-order with `depth` relative to the focus
  (ancestors negative) and `nchildren` per node. `summary.hidden_children`
  counts only the focus's descendants cut by depth or limit; an ancestor's
  other children are off the path by design. `next` suggests `--limit 2N`
  or `--down N+1`. Exit 0.
- **`links`**: up = against edge direction (towards causes), down = along it
  (towards effects). No flags means one hop each way; giving only one of
  `--up`/`--down` sets the other to 0. A node reached both ways keeps the
  up distance. `edges` lists only edges between returned nodes, ordered
  from the furthest cause down. Exit 1 when there are no edges. Kinds:
  `cause`, `parent_child`, `related`, `custom`.
- **`around`**: anchor time is the txn's start (or `@T`). `-n N` (default 5)
  gives N rows starting before and N after by (start, id), with the anchor
  row always shown (even if the selection would exclude it), plus
  `more_before`/`more_after`. `--window W` instead gives every txn
  overlapping `[start - W, end + W]` (inclusive, paged with `--limit 50`).
  Rows carry `dt = start - anchor time`. `--same-stream` needs a txn anchor.
  `-t` is not accepted (the anchor defines time).
- **`active @T`**: `start <= T` and (`open` or `end > T`), so a txn ending
  exactly at T is not active. `-t` is not accepted. Same envelope as `find`
  plus `query.at`.
- **Missing refs** (`tree`/`links`/`around` on an unknown id) are a
  `not_found` error, exit 2. (`show` keeps its Q1 behaviour: `not_found`
  list in the summary, exit 1 when none are found.)
- **`batch`**: lines are `VERB [options] [EXPR]` without the trace; a
  leading `trlog` and a repeated trace path are dropped; blank lines and
  `#` comments are skipped; nested `batch` is an error. Text mode prints
  `== [n] line` before each command, and errors in place on stdout (not
  stderr) so order is kept. `--json` forces `--json` on every line
  (dropping `--jsonl`/`--pretty`) and prints one compact envelope per
  line with `seq` and `exit` first. Exit 2 if any command failed, else 0
  if any matched, else 1.
- **EXPR as several words.** `EXPR` is `nargs="*"` and the words are joined
  with spaces, so `trlog find T addr == 0x4000` and the same batch line
  work unquoted. Quoting is still needed for shell characters.
- **Usage errors** (bad option, bad `--down` value, missing argument) are
  now `{"error": "usage", "verb", "hint": "see trlog help VERB"}` in JSON
  mode and `trlog: error: ...` in text mode, exit 2, instead of argparse's
  usage dump.

### Q2 timings (200k-txn trace, 8 streams, pure Python)

| Command | Time | Peak RSS |
|---------|------|----------|
| `tree t:ID` / `links t:ID` / `around t:ID -n 3` | 1.5-1.8 s | ~160 MB |
| `find 'nchildren>0' --count` | 1.6 s | ~160 MB |
| `active @1us` (upper-bound block skipping) | 0.44 s | 80 MB |
| `batch` of tree + links + around + find --count | 1.8 s total | 165 MB |

Each navigation verb costs one full scan; the batch shows the graph cache
working (four commands, one scan).

## Phase Q3: analysis and export

Done 2026-11-21. Uncommitted, on top of the uncommitted Q1/Q2 work.

| Step | Scope | Status |
|------|-------|--------|
| 0 | §9 agent usability check after Q2 (`query_usability_q2.md`): 14/14 answered, 19 query calls, 19.5 KB read; fixes applied | done |
| 1 | `export sqlite` (§4.2 tables, `txn_<type>` views, indexes) | done |
| 2 | `export parquet` (optional `pyarrow`; `trlog[parquet]` extra) | done; verified in a scratch venv with pyarrow (2 tests skip without it) |
| 3 | `stats VALUE [--by KEY]` | done |
| 4 | `hist VALUE [--bin W \| --bins N]` | done |
| 5 | `gaps [--min W] [--sort]` | done |
| 6 | `match --a --b [--on] [--order fifo\|any] [--within W] [--unmatched a\|b]` | done |
| 7 | help (`help agent`, verb list), goldens, tests, docs | done |
| 8 | §9 usability rerun with the Q3 questions: 7/7 answered, 19 calls, 20 KB; help fixes applied (`query_usability_q2.md`, second run) | done |

**Q3 is complete.** Full suite: 670 passed, 51 skipped (Q2 ended at 616).
New: `tests/query/test_analysis.py` (32 tests, own req/rsp trace `build_bus`)
and 18 golden cases (85 in total).

### Semantics decided during Q3

- **Engine neutrality.** All of Q3 lives in `query/_analysis.py` and
  `query/_export.py` and works on joined txns from `Trace._matches`, so
  it runs unchanged on any scan engine (see the Q4 constraint below).
- **VALUE** (`stats`, `hist`) is any expression, compiled by
  `compile_value` (new in `_expr.py`): `dur`, `start`, a field,
  `addr & 0xfff`, `parent.prio`. Enum, string and bytes values are a
  `not_numeric` error that points to `count --by`. Times (start/end/dur or
  a `time`-kind field) are formatted as times in text mode, and JSON says
  `value_is_time`.
- **Missing values.** For a bare field name, txns whose type has no such
  field are skipped. `missing` counts txns that have the field but no
  usable value: unset, x/z bits, or `dur` of an open txn. For other
  expressions every matched txn counts.
- **Percentiles** use nearest rank, so they are always a value that occurs
  in the data. `min_ref`/`max_ref` name the first txn (by start, id) that
  holds the min or max. Groups are ordered by n descending, then by key.
- **hist bins** are left-closed `[lo, lo+W)`, with `lo = floor(min/W)*W`.
  Empty bins between min and max are kept. With no `--bin`, the width is
  the smallest 1/2/5×10^k that fits in `--bins` (default 20) bins after
  alignment. Integer data gets integer widths. More than 10000 bins is a
  `too_many_bins` error. Text output is a bar table plus a sparkline.
- **gaps**: per stream, the parts of the range where no selected txn is in
  progress, kept when they are longer than `--min` (strictly). Overlapping
  txns merge, and `prev` is the txn whose end opened the gap. An open txn
  covers to the end. The range is the `-t` window, with open sides taken
  from the trace span, so leading and trailing silence count (`prev`/`next`
  is null at an edge). The default sort is `-dur`, with ties by (stream,
  start). Selected streams with no matching txn are listed as
  `silent_streams`, not as gaps.
- **match** pairs one-to-one, and a txn never pairs with itself.
  - `--on` is `a.X == b.Y` terms joined by `&&` (X and Y can be any
    expression), or bare names (`tag`, `tag,addr`). With no key, pairs
    follow order.
  - `fifo` (the default) takes the oldest unmatched a with an equal key
    that started at or before b; at equal start times a comes first.
    `any` takes the unmatched a with the nearest start, before or after.
  - `--within W` bounds |b.start − a.start|; in fifo an a that is too old
    for the current b is dropped as unmatched.
  - A txn with a missing key value is counted in `no_key_a`/`no_key_b`,
    not as unmatched.
  - `lat = b.end − a.start`, or null while b is open. `--sort lat|-lat`
    puts open pairs last.
  - `-s`/`-t` apply to both sides; there is no `-T`, use
    `type == '...'` in `--a`/`--b`.
  - The summary carries lat stats with `min_pair`/`max_pair`, and the
    envelope carries the first `--limit` unmatched refs of each side.
    `--unmatched a|b` lists those txns in full instead of the pairs.
  - Exit 0 when the listed rows are non-empty.
- **export**:
  - Tables follow §4.2. `attr_<type>` tables exist for every type, even
    when empty.
  - Ints outside int64, and 4-state values with x/z bits, are stored as
    hex text; enums as labels; bytes as blobs.
  - `link` holds every edge that touches a selected txn, including edges
    to unselected txns. `stream` lists all streams, with first, last and
    count over the selection. `trace` holds the source path, time unit,
    timescale, the query (JSON) and the trace metadata.
  - SQLite adds `txn_<type>` views (txn + stream path + fields) and
    indexes on start, (stream_id, start), link src/tgt and meta id.
  - An existing OUT is an `exists` error unless `--force`. Parquet only
    replaces a directory that holds nothing but `*.parquet` files.
  - Writes go to a temp file or directory beside OUT, then are renamed.
  - Without pyarrow, Parquet is a `missing_dependency` error with the
    install hint, and nothing is written.
- **Usability fixes (from the Q2 check):**
  - the help now says `-t` overlap is inclusive;
  - `around --window` sizing advice;
  - the `count --by` null group is noted;
  - a `-T a,b` example;
  - `schema --sample` now counts 4-state values (bug) and prints
    `(N distinct)`.

### Q3 timings (200k-txn trace, pure Python)

| Command | Time | Peak RSS |
|---------|------|----------|
| `stats dur --by stream` | 1.7 s | 150 MB |
| `hist start --bin 100ns` | 1.5 s | 150 MB |
| `gaps --min 1ns` | 1.5 s | 150 MB (3.5 s before the whole-trace span shortcut) |
| `match --a .. --b .. --on addr` | 3.5 s | 170 MB (two scans, one per side) |
| `export sqlite` | 1.9 s | 180 MB; 13.7 MB .db |

Every verb is one scan, except `match` (two) and `gaps` with a narrowed
selection (a second scan for the trace span, cached per `Trace`). Q4's
engine should give `match` a single pass over the union of both sides.

## Constraint for Q4

The C scan engine must be an **optional accelerator**. Platforms without a
native wheel keep using the pure-Python engine, with identical output
(design §7.1). Q3 code must therefore stay engine-neutral: verbs work on
the `Trace` API, not on reader internals that only one engine would have.

## Phase Q4: C scan engine (optional accelerator)

Done 2026-11-21. Uncommitted, on top of the uncommitted Q1-Q3 work.

**Q4 is complete.** Full suite: 848 passed, 50 skipped, with the C engine
(the default when libtrl is built) and with `TRLOG_ENGINE=python` (Q3
ended at 670). C tests: 6/6 (`ctest` in `build/c`; new `c_qscan`).
Every golden case now runs on both engines against the same file.

### Where the time goes (profile, `count 'addr > 0x1000 && len < 4'`, 200k txns)

| Part | Share |
|------|-------|
| Block decode (`_txn_data._decode_payload` and below) | ~60% |
| Building `Txn` objects (`new_txn`, `add_attrs`) | ~25% |
| Filter evaluation | ~15% |

### Found while testing (outside Q4)

- **Python engine: evaluation could raise.** The fuzzer hit
  `3 << 9223372036854775807` (MemoryError while constant-folding). Also
  `bit(x, -1)`, `bits()` with a huge width, `int(inf)` inside `bit()`, and
  unary operators, `abs`, `min`/`max` on a raw tuple value raised instead
  of giving a missing value. **Fixed** (`_expr.py`): evaluation is now
  total; a left shift or `bits()` width over `MAX_SHIFT` (65536 bits) is
  missing. C mirrors the `bits()` cap.
- **Column/adaptive TXN layout does not round-trip** (writer/reader bug,
  not fixed). `_encode_attrs_column` (`_txn_data.py:506-513`) writes a
  value only for the attrs a TR_FULL record has, but
  `_decode_attrs_column` (`_txn_data.py:689-702`) reads one value from
  every schema column for every record. Nothing records per-record
  presence, so one unset field misaligns the column for all later
  records. Minimal repro: `logs/column_layout_repro.py` (one string
  field, two records, the second with no attrs). Row layout, the
  default, is not affected. A fix needs a presence mask per record, which
  is an on-disk format change, so it needs a decision. The C engine does
  not read column blocks (it falls back), so it is unaffected.

### Approach

- **C does decode, join, pushdown, window and filter**; Python builds
  `Txn` objects only for the rows that survive. The new C API is
  `trl_qscan_*` (`c/include/trl/trl_qscan.h`, `c/src/qscan.c`). It is
  separate from the SV cursor (`trl_query.h`), which keeps its half-open
  window. The query scan uses the CLI's inclusive window.
- **Python hands C everything that is a semantic choice**: the block plan
  (index entries in visit order), the decoder's field types per txn type,
  the field-name slots and value normalisation per (type, field), and the
  stream/type id sets for `stream`/`type` tests. C only reads blocks at
  given offsets. It does not parse the index, type registry or hierarchy,
  so the two engines cannot disagree about the catalog.
- **Three-valued filter.** The filter compiles to a small bytecode. C
  evaluates it per txn to keep, drop or **defer**. Defer means C cannot be
  sure of Python's answer: strings, floats, `meta[...]`, `len()`, ints
  beyond int64, x/z equality, enum ordering, and any runtime error
  Python would raise. Deferred rows are materialised, and Python applies
  the full filter to them. So the output is exact for every expression,
  and C is fast for the common integer and enum cases.
- **Fallback.** Any block C cannot decode (zstd, column layout,
  struct-codec flag, an unknown tag, times or ids outside int64, a
  corrupt block) makes the C run return `FALLBACK`. Python then reruns the
  query on the pure-Python engine, so errors and odd files behave exactly
  as before.
- **Engine selection.** `TRLOG_ENGINE=auto|python|c` (default `auto`), or
  `trlog.query.open(path, engine=...)`. `auto` uses C when the library loads
  and has the `trl_qscan_*` symbols, else Python. `c` makes a missing
  library an error, which tests use. `info` reports the engine.
- **Not streaming yet.** C holds the selected txns in compact form until
  the scan ends, because TR_META, TR_ATTR and TR_LINK may follow a txn's
  TR_END in any later block, and Python attaches them. Constant-memory
  streaming needs a writer guarantee about record placement (proposed for
  Q5).

| Step | Scope | Status |
|------|-------|--------|
| 0 | Plan; benchmark trace generator in the repo (`benchmarks/query/bigtrace.py`); baseline timings | done |
| 1 | C `trl_qscan` (`c/include/trl/trl_qscan.h`, `c/src/qscan.c`): block reads by offset, zlib/raw, row delta/non-delta decode, Python-identical join, pushdown, `t_hi` block skipping, inclusive window, bytecode eval (keep/drop/defer), packed results, fallback status | done; clean under `-Wall -Wextra`, ASan and UBSan |
| 2 | Python `query/_cengine.py`: optional loader, AST→bytecode compiler, result unpacking into `Txn`s identical to `Trace.scan` | done |
| 3 | Engine selection in `Trace` (`engine=`, `TRLOG_ENGINE`), `info.engine`, fallback on C failure | done |
| 4 | Differential tests (`tests/query/test_cengine.py`): goldens under both engines, 40 random traces × 60 expressions in the suite (1000 more seeds run once: no mismatch), no-library tests; full suite under both engines | done: 803 passed, 51 skipped on each |
| 5 | Single-pass `match` (engine-neutral): one scan of `(A) \|\| (B)` over the union of both sides' stream/type sets, split by each side's filter | done; goldens changed only in `scan` (16 → 8 blocks read) |
| 6 | Benchmark (`benchmarks/query/bench.py`), memory trims, C harness `c/tests/test_qscan.c`, docs (design §7, `trlog help`, this file) | done |
| 7 | Aggregates without `Txn` objects: `count`, `info`, `stats`, `hist` run on columns (`query/_rows.py`). C projects them (`TRL_QS_PROJECT`, value programs, qscan ABI 2), and Python fills the same columns from `Txn`s | done: 1000 extra fuzz seeds, no mismatch |
| 8 | `gaps` on `Rows` too: no `Txn` per row; `prev`/`next` are `TxnRef`s | done: added to the aggregate differential test, 1000 seeds, no mismatch |
| 9 | Fuzz harness: `TRL_FUZZ_SEEDS` sets the seed count (default 40); a seed with too few valid random queries draws more (after its fixed first draws, up to 10x) instead of failing the `ran >` floor (seed 507) | done: `TRL_FUZZ_SEEDS=1000`, both differential tests, 2000 passed (`logs/fuzz_floor_1000.log`) |

### Semantics and behaviour decided during Q4

- **Identical output.** The engines differ only in `info.engine` and
  `scan.ms`. `scan.blocks_read`, `duplicate_ids` and row order are the
  same, because C follows the Python block plan and dict-order rules.
- **Window.** The query scan uses the CLI's inclusive `[A, B]` overlap,
  the same as Python. The SV read API (`trl_query.h`, `trl_cursor_*`)
  keeps its own documented half-open `[from, to)`. They are separate APIs,
  so nothing needed reconciling beyond that.
- **When C is used.** `auto` uses C when libtrl loads with the
  `trl_qscan_*` symbols. The loader looks in `$TRL_LIBRARY_PATH`, beside
  the `trlog` package (where a native wheel would put it), then
  `build/c`, `c/build` and `build` in the repo. With no library, or a
  library from before Q4, everything runs in Python and nothing else
  changes. A library whose `trl_qscan_abi_version()` differs from
  `_cengine.QSCAN_ABI` is not used either, so a stale or newer build is
  never misread. `Trace.c_fallbacks` lists scans that C handed back, with
  the reason.
- **Still on Python, by design:** traces without an index (linear scan),
  column/adaptive TXN layout, zstd blocks, window bounds that are not
  int64, and `parent.X`/`nchildren` filters (which use the cached graph,
  built by a C scan).
- **Deferred terms:** strings (except enum labels and `stream`/`type`
  against literals, which C handles through sets computed by the Python
  comparison functions), floats, `meta[...]`, `len()`, ints beyond
  int64, x/z equality, raw tuple values, and `=~` with a non-string
  literal (then every row is deferred).
- **`match` reads each block once** and keeps pushdown when both sides
  restrict streams or types.
- **Aggregates (step 7).** `count`, `info`, `stats` and `hist` aggregate
  over `Rows` (`query/_rows.py`): columns of id, start, end, stream, type,
  parent and the projected values.
  - The Python engine fills the columns from its `Txn`s.
  - The C engine evaluates the value expression and `--by FIELD` keys as
    bytecode. Rows it decides in full come back as columns, with no
    `Txn`; the rest come back as `Txn`s and are evaluated in Python.
  - The aggregation code is shared, so it is order-independent: ties go to
    the (start, id)-first row, as before.
  - One subtlety: equal keys of different types (0, 0.0, False) share a
    group named by the first row inserted, so groups are built in
    (start, id) order when a key column holds floats or bools.
  - `stats` owners (`min_txn`/`max_txn`) are now `TxnRef`s
    (id, start, end, stream, type, `.ref`) on both engines, not `Txn`s.
  - `--by meta[K]` keeps the `Txn` path.
  - `gaps` (step 8) uses the same columns; its `prev`/`next` are
    `TxnRef`s (the CLI only reads `.ref`).
  - C values carry a bool tag, because `numeric(True)` is missing in
    Python. `true`/`false` compile to bools.

### Q4 timings (200k-txn trace, fresh process each, best of 2)

From `PYTHONPATH=python/src python benchmarks/query/bench.py big.trl`
(the trace is from `benchmarks/query/bigtrace.py OUT 200000`), after step
7, at a machine load of about 4. Absolute times move with load; the ratios
move less.

| Command | Python | C | Speed-up |
|---|---|---|---|
| `info` | 1.31 s, 145 MB | 0.09 s, 68 MB | 14.6x |
| `count (all)` | 1.47 s, 147 MB | 0.09 s, 69 MB | 16.3x |
| `count 'addr > 0x1000 && len < 4'` | 1.44 s, 142 MB | 0.09 s, 61 MB | 16.0x |
| `find 'len == 3' (first 20)` | 1.36 s, 142 MB | 0.09 s, 61 MB | 15.1x |
| `count --by stream` | 1.48 s, 147 MB | 0.12 s, 70 MB | 12.3x |
| `active @1us` | 0.43 s, 78 MB | 0.05 s, 38 MB | 8.6x |
| `stats dur --by stream` | 1.58 s, 151 MB | 0.18 s, 85 MB | 8.8x |
| `hist start --bin 100ns` | 1.57 s, 149 MB | 0.15 s, 83 MB | 10.5x |
| `gaps --min 1ns` | 1.60 s, 151 MB | 0.15 s, 84 MB | 10.7x |
| `match --a len==0 --b len==1 --on addr` | 1.45 s, 155 MB | 0.17 s, 65 MB | 8.5x |
| `show t:100000 (full graph)` | 1.54 s, 155 MB | 0.58 s, 173 MB | 2.7x |
| `export sqlite` | 1.92 s, 176 MB | 1.00 s, 176 MB | 1.9x |

- **Aggregates and selective queries** are 8-16x faster and use about
  half the memory of Python.
- **`gaps`** row: step 8, timed separately at load ~2 (before step 8:
  0.57 s, 173 MB on C).
- **`show`, `export`** still build a `Txn` per row (2-3x faster, ~20%
  more memory than Python): `show` needs the full graph, and `export`
  needs every field.
- **`match`** took 3.5 s on Python before it became a single pass.

### Next (candidates, not started)

- Streaming (constant memory) needs a format/writer guarantee about where
  TR_ATTR/TR_META/TR_LINK records may appear (Q5 discussion).
- The column-layout round-trip bug above (format decision needed).
- C support for column layout, once that bug is fixed.

## Where things are

| Path | What |
|------|------|
| `pyproject.toml` | Packages `python/src/trlog`; console script `trlog = "trlog.cli:main"`; extras `zstd`, `lz4`, `parquet` |
| `python/src/trlog/__main__.py` | `python -m trlog` |
| `python/src/trlog/cli.py` | Verbs, argparse, envelope, paging, `--max-bytes`, exit codes, help topics (incl. `help agent`), `batch` |
| `python/src/trlog/query/_expr.py` | Lexer, parser, name resolution, static checks with agent-oriented errors, closure compiler, pushdown extraction |
| `python/src/trlog/query/_engine.py` | `Trace`: catalog, joined scan, cached `graph()`, `find`/`count`/`show`/`tree`/`links`/`around`/`active`/`stream_summary`/`schema`/`info` |
| `python/src/trlog/query/_format.py` | JSON value encoding, text lines/tables, TTY styling |
| `python/src/trlog/query/_values.py` | `MISSING`, `EnumVal`, `LogicVal`, time units and parsing |
| `tests/query/corpus.py` | Deterministic query corpus (built at test time, not checked in) |
| `tests/query/golden_cases.py`, `tests/query/golden/` | 85 CLI golden outputs (a case may carry stdin, for `batch`); regenerate deliberately with `PYTHONPATH=python/src:. python -m tests.query.golden_cases` and review the diff |
| `tests/query/test_expr.py`, `test_engine.py`, `test_cli_modes.py` | Expression, API and output-mode tests |
| `tests/query/test_nav.py`, `test_cli_nav.py` | Q2: graph, nlinks/nchildren, tree, links, around, active; batch, help, CLI defaults |
| `python/src/trlog/query/_analysis.py` | Q3: `stats`, `hist`, `gaps`, `match` (engine-neutral; all but `match` over `Rows`) |
| `python/src/trlog/query/_export.py` | Q3: §4.2 tables; SQLite and Parquet writers |
| `tests/query/test_analysis.py` | Q3: API and CLI tests; `build_bus` req/rsp trace |
| `query_usability_q2.md` | §9 agent usability check after Q2: results and actions |
| `c/include/trl/trl_qscan.h`, `c/src/qscan.c` | Q4: C scan engine (decode, join, window, filter bytecode with keep/drop/defer) |
| `python/src/trlog/query/_cengine.py` | Q4: optional loader, AST→bytecode compiler, result unpacking |
| `tests/query/test_cengine.py` | Q4: no-library tests, differential tests (random traces × expressions) |
| `c/tests/test_qscan.c` | Q4: C harness for the qscan API (`ctest`: `c_qscan`) |
| `python/src/trlog/query/_rows.py` | Q4: `Rows` columns, `TxnRef`, `--by` key columns (shared by both engines) |
| `benchmarks/query/bigtrace.py`, `bench.py` | Q4: benchmark trace and Python-vs-C timing table |

## Semantics decided during Q1

- **`-t A..B`** selects transactions that overlap the window, inclusive at
  both ends. An open transaction overlaps every window after its start.
  (The C query scan of Q4 uses the same inclusive window; the SV
  cursor in `trl_query.h` keeps its own half-open `[from, to)`.)
- **Precedence** is Python's: bit operators bind tighter than comparisons,
  so `addr & 0xf == 0` means `(addr & 0xf) == 0`. `!`/`not` sit between
  `&&` and the comparisons, so `!x == 1` is `!(x == 1)`; `!` inside an
  arithmetic operand is a syntax error with a hint.
- **Parse errors come before name errors**: `a < b < c` reports the
  chained comparison, not the unknown names.
- **Enums** compare by label or by number; an unknown label is an
  `unknown_enum` error listing the valid ones. Bitvecs and 2-state logicvecs
  are integers; logicvecs with x/z bits never compare equal to anything.
- **`--nth N`** is 1-based. **`--sort -KEY`** is descending; missing values
  sort last in both directions. `--sort -dur` works (the CLI rejoins the
  dash value before argparse sees it).
- **JSON** is compact by default (`--pretty` to indent). Errors in JSON
  mode go to stdout as `{"error": ...}`; in text mode to stderr. Exit 2.
- **`count`** without `--by` prints just the number in text mode.
- **Default `--limit`**: 20 for `find`, 50 for `count` groups, 100 for
  `streams`. `summary.by_stream` is capped at 20 entries plus
  `by_stream_omitted`.
- **Files** are checked for a FILE_HDR block and magic before the reader
  opens them. A garbage file otherwise made the reader try to allocate a
  bogus block length (`MemoryError`).

## Engine behaviour and known limits (inputs for Q2/Q4/Q5)

- Pure Python: every selected txn is materialised. On a generated
  200k-txn, 8-stream trace (520 KB), `count` and a filtered `find` take
  about 1.4 s and 146 MB. Q4's C engine materialises only survivors
  (see the Q4 timings); streaming is still open.
- Pushdown: `-s`, `-T`, and top-level `stream ==`/`in`/`~` and
  `type ==`/`in` conjuncts restrict which txns are built. `-s` (and lifted
  stream conjuncts) also restrict which blocks are read.
- Upper time bound: a block whose declared start is after `B` is skipped
  unless its stream still has a txn open at `B`. This assumes a block's
  declared start is no later than the records inside it (true for the
  current writers). No lower-bound skipping, because long txns can start
  early.
- `--with links` reads every block and disables time skipping: a link
  record can live in any stream's block and at any later time. A test
  covers a link recorded in another stream's block.
- `parent.X` scans all streams so that the parent is available.
- `show`, `tree`, `links`, `around t:ID` and `--with children` do a full
  scan (no id→block index until Q5), cached per `Trace` since Q2. Ref time
  hints (`t:ID@T`) are accepted and ignored.
- Stream paths come from the scope tree (hierarchy name excluded).
  Stream instance ids are per hierarchy in the writer, so two hierarchies
  that both declare streams collide; `info` warns and keeps the first.
  This is a format/writer issue, not fixed here.

## Not done yet (deferred or dropped)

- `--cols` and `--raw` (§3.4/§3.5): not implemented yet.
- Verbs for later phases (Q5: index, diff, value) return `not_supported`
  with the phase.
- Usability findings still open (`query_usability_q2.md`): siblings of a txn
  in one call (`tree --siblings` or `show`), a warning when `around
  --window` covers the whole trace, `gaps --with fields` for the prev/next
  txns, and a `match` note when one side spans extra streams.
- `$prev` substitution in batch: out of v1 by design (§6).
- `around`/`active` accept `@TIME` only for times; `active t:ID` (at a
  txn's start) was not added.

## Other changes

- `python/src/trlog/_writer.py`: `TrlWriter.add_enum_type` built
  `EnumValue(name_id=..., numeric_value=...)`, which the dataclass does not
  accept, so any call raised `TypeError`. It now builds
  `EnumValue(integer_value=..., label_str_id=...)`. The query corpus is
  its first user.
- TR_ATTR records need `TxnAttr._field_type` set by the caller (the
  writer does not look up the txn's schema); the corpus does this.
  Worth fixing in the writer later.
