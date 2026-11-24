# The `trlog` command

`trlog` searches, filters, navigates and summarises the transactions in a
`.trl` trace from the shell. Every command has the same shape:

```text
trlog VERB TRACE [options] [EXPR]
```

`VERB` says what to do (`find`, `count`, `tree`, ...), `TRACE` is the trace
file, the options narrow the selection or shape the output, and `EXPR` is an
optional filter expression. The command is a thin layer over the
{doc}`trlog.query <query>` Python API, so anything it does can also be done
from a script.

## Installing and running

Installing the package adds a `trlog` console script
(`trlog = trlog.cli:main` in `pyproject.toml`). From a source checkout,
`python -m trlog` runs the same command:

```bash
pip install .                       # installs the trlog command
PYTHONPATH=python/src python -m trlog info run.trl
```

`export parquet` needs `pyarrow`, installed with the `parquet` extra
(`pip install .[parquet]`). Nothing else is optional.

## The example trace

The examples on this page run against the query test corpus, a small
deterministic trace that the test suite builds. Generate it with:

```bash
PYTHONPATH=python/src:. python -c \
    "from tests.query.corpus import build; build('run.trl')"
```

It has four streams and 19 transactions, with times in ps:

```text
top.env.seqr      seq_item  t:1 wr_burst, t:2 rd_burst, t:3 err_test, t:4 hang (open)
top.env.axi0.mon  axi_wr t:10..13 (parent t:1), axi_rd t:20..23 (parent t:2)
top.env.axi1.mon  axi_wr t:30 SLVERR, axi_rd t:31 DECERR (parent t:3),
                  axi_rd t:32 open (parent t:4)
top.log           msg t:100..103; links t:3 -> t:30 -> t:101 (cause),
                  t:31 -> t:102 (cause), t:32 -> t:103 (related)
```

The `match` examples use a second trace with request and response streams,
written by `examples/query_bus.py` (`python examples/query_bus.py bus.trl`).

The outputs below are real. `{...}` and `...` mark trimmed JSON, and
absolute paths are shortened.

## Getting started

Start with `info`. It reports the time unit, the span, the counts, which
scan engine ran, and suggests the next commands to try:

```text
$ trlog info run.trl
file         run.trl
version      2.1
time unit    ps (10^-12 s)
span         1.000ns .. 70.000ns  (1000 .. 70000)
txns         19 (2 open)
streams      4
types        4
signals      0
hierarchies  design
blocks       8 txn, 0 vc
index        yes, query index: no
engine       c
meta tool    query_corpus
meta seed    1
try next:
  trlog streams run.trl
  trlog schema run.trl
  trlog count run.trl --by stream,type
  trlog find run.trl -T axi_rd --limit 5
  trlog find run.trl open
```

`streams` lists stream paths with their counts, types and time range.
`--tree` shows them as a hierarchy, and an optional `GLOB` argument
narrows the list:

```text
$ trlog streams run.trl
stream            decl     txns  open     first      last  types
top.env.axi0.mon  axi_mon     8     0   2.000ns  36.500ns  axi_wr,axi_rd
top.env.axi1.mon  axi_mon     3     1  42.000ns  62.000ns  axi_rd,axi_wr
top.env.seqr      seq         4     1   1.000ns  61.000ns  seq_item
top.log           log         4     0  19.000ns  70.000ns  msg
-- 4 streams

$ trlog streams run.trl --tree
top
  env
    axi0
      mon  8 txns  [axi_wr,axi_rd]
    axi1
      mon  3 txns, 1 open  [axi_rd,axi_wr]
    seqr  4 txns, 1 open  [seq_item]
  log  4 txns  [msg]
-- 4 streams
```

`schema` lists transaction types, their fields, kinds, radix and enum
values. `--sample N` reads up to N transactions per type and adds the
observed values, which shows what a realistic filter looks like:

```text
$ trlog schema run.trl axi_rd
axi_rd  (id 1)
  field  kind           radix
  addr   u64            hex
  len    u8             dec
  resp   enum axi_resp  -      {OKAY=0, EXOKAY=1, SLVERR=2, DECERR=3}
  data   bitvec         -
  bus    logicvec       -

$ trlog schema run.trl msg --sample 50
msg  (id 4)  sampled 4
  field  kind           radix
  text   str            -      top: "write burst done"(1) "slave error at 0x8000"(1) "decode error"(1) "timeout waiting for rsp"(1)
  sev    enum severity  -      {INFO=0, WARN=1, ERROR=2}  top: ERROR(2) INFO(1) WARN(1)
```

## Verbs

| Verb | What it does |
|------|--------------|
| `info` | file facts, counts and suggested next commands |
| `streams` | streams with txn counts, types and time range |
| `schema` | transaction types, fields, kinds and enum values (`--sample N`) |
| `find` | transactions matching a selection, with a summary |
| `show` | full records for `t:ID` refs: fields, meta, links, parent, children |
| `count` | match counts, optionally grouped with `--by` |
| `tree` | parent/child structure around a transaction |
| `links` | follow cause/related links from a transaction |
| `around` | what happened near a transaction or a time, across streams |
| `active` | transactions in progress at an instant |
| `stats` | n, min, p50/p90/p99, max and mean of a value |
| `hist` | histogram of a value (start time gives throughput) |
| `gaps` | idle intervals per stream |
| `match` | pair requests with responses; latency and unmatched ones |
| `export` | write the selection to SQLite or Parquet |
| `batch` | run one command per stdin line, opening the trace once |
| `help` | `help [agent\|expr\|refs\|json\|batch\|VERB]` |

`trlog help VERB` (or `trlog VERB --help`) prints the full option list
for a verb. `trlog help agent` prints a one-screen cheat sheet. The verbs
`index`, `diff` and `value` are reserved for a later phase and currently
fail with an error that says so.

## Selecting transactions

Most verbs take the same selection options. They combine with AND.

| Option | Meaning |
|--------|---------|
| `-s GLOB`, `--stream GLOB` | stream path glob, matched against the full dotted path. Repeatable or comma-separated. |
| `-T NAME`, `--type NAME` | transaction type name (globs such as `axi_*` work). Repeatable or comma-separated. |
| `-t WINDOW`, `--time WINDOW` | keep transactions that overlap the window |
| `-w EXPR`, `--where EXPR` | filter expression (same as the positional `EXPR`) |
| `EXPR` | filter expression; see {ref}`cli-expressions` |

Prefer `-s`, `-T` and `-t` to the equivalent expression terms. The engine
uses them (and top-level `stream ==`, `stream in`, `stream ~`, `type ==`
and `type in` terms of the expression) to skip whole blocks or skip
building transactions it does not need.

A stream glob that matches nothing, or an unknown type, is an error with
suggestions:

```text
$ trlog find run.trl -s nosuch
trlog: error: no stream matches 'nosuch'
  hint: globs match the full dotted path; run `trlog streams TRACE` to list paths
```

### Time windows

`-t` takes one of these forms. Times are a number with a unit (`fs`, `ps`,
`ns`, `us`, `ms`, `s`) or a bare integer tick count in the trace's time
unit.

| Form | Window |
|------|--------|
| `A..B` | from A to B |
| `..B` | from the start of the trace to B |
| `A..` | from A to the end of the trace |
| `@T` | the single instant T |
| `@T±W` or `@T+-W` | from T-W to T+W |

Each time must be a whole number of ticks of the trace's time unit. In a
trace with a 1ps unit, `1.5ns` is 1500 ticks, but `1.5ps` is an error
(`bad_time`): it is neither truncated nor rounded.

A transaction is selected when it overlaps the window, and both ends are
inclusive: a transaction ending exactly at A, or starting exactly at B, is
selected. An open transaction (one that never ended) overlaps every window
after its start.

```text
$ trlog find run.trl -t 20ns..30ns
t:1    1.000ns  +19ns   top.env.seqr      seq_item  name=wr_burst prio=1
t:2   21.000ns  +19ns   top.env.seqr      seq_item  name=rd_burst prio=2
t:20  22.000ns  +2.5ns  top.env.axi0.mon  axi_rd    addr=0x4000 len=4 resp=OKAY data=0xa0a00000 bus=0xa0
t:21  26.000ns  +2.5ns  top.env.axi0.mon  axi_rd    addr=0x4010 len=4 resp=OKAY data=0xa0a00001 bus=0xa1
t:22  30.000ns  +2.5ns  top.env.axi0.mon  axi_rd    addr=0x4020 len=4 resp=OKAY data=0xa0a00002 bus=0xa2
-- 5 matched; 1.000ns .. 40.000ns
```

`t:1` (1 ns to 20 ns) is selected because it ends exactly at the lower
bound, and `t:22` because it starts exactly at the upper bound.

### References

Every result row carries a reference that later commands accept:

| Ref | Meaning |
|-----|---------|
| `t:1234` | a transaction by id; taken by `show`, `tree`, `links` and `around` |
| `t:1234@15200000` | the same, with a time hint (accepted and currently ignored) |
| `@15us` | a point in time, for `around` and `active` (`@15200000` is in ticks) |

A ref to an id that is not in the trace is a `not_found` error (exit 2) for
`tree`, `links` and `around`. `show` lists missing ids in its summary
instead, and exits 1 only when none of the refs were found.

(cli-expressions)=
## Filter expressions

An expression is evaluated once per transaction. Several words after the
trace are joined with spaces, so `trlog find run.trl addr == 0x4000` works
unquoted. Quote the expression when it holds characters the shell
interprets: `' " ( ) < > & | !`.

### Names

| Name | Value |
|------|-------|
| `id` | transaction id |
| `start`, `end`, `dur` | times in ticks; `end` and `dur` are missing while a transaction is open |
| `open` | true when the transaction never ended |
| `parent` | parent transaction id (missing when there is none) |
| `stream` | stream path, for example `'top.env.axi0.mon'` |
| `type` | transaction type name |
| `nlinks` | number of link edges touching the transaction, in both directions |
| `nchildren` | number of transactions whose parent is this one |
| `FIELD` | a field of the transaction's type, for example `addr` |
| `TYPE.FIELD` | a field of one type only, for example `axi_rd.addr` |
| `meta["KEY"]` | a per-transaction metadata string |
| `parent.NAME` | a name evaluated on the parent, for example `parent.type == 'seq_item'` |

`nlinks`, `nchildren` and `parent.NAME` need the whole trace and make the
query scan every block.

An unknown name is an error that suggests the closest match and lists the
known fields:

```text
$ trlog find run.trl "rsp == 'OKAY'"
trlog: error: unknown field 'rsp'
  rsp == 'OKAY'
  ^
  did you mean: resp
  hint: run `trlog schema TRACE` to list types and fields
```

With `-T`, unqualified field names resolve only against the selected
types:

```text
$ trlog find run.trl addr == 0x4000 -T msg
trlog: error: unknown field 'addr'
  addr == 0x4000
  ^
  hint: 'addr' exists in a type not selected by -T; widen -T or use TYPE.field
```

### Literals

| Kind | Examples |
|------|----------|
| integers | `42`, `0x4000`, `0b101`, `32'h4000`, `'h4000` |
| floats | `1.5` |
| strings | `'str'`, `"str"` |
| booleans | `true`, `false` |
| times | `10ns`, `1.5us` (converted to trace ticks) |
| regex | `/timeout\|err/` (right side of `=~` only) |

### Operators

Loosest binding first:

| Operators | Notes |
|-----------|-------|
| `\|\|`, `or` | |
| `&&`, `and` | |
| `!`, `not` | prefix |
| `==` `!=` `<` `<=` `>` `>=` `~` `=~` `in` `not in` | comparisons do not chain |
| `\|` `^` `&` `<<` `>>` `+` `-` `*` `/` `%` | Python precedence |
| unary `-` `+` `~` | |

Bit operators bind tighter than comparisons, as in Python, so
`addr & 0xf == 0` means `(addr & 0xf) == 0`. `!x == 1` means `!(x == 1)`.
A chained comparison is an error:

```text
$ trlog find run.trl "a < b < c"
trlog: error: comparisons cannot be chained
  a < b < c
        ^
  hint: combine them with &&, e.g. a < x && x < b
```

Membership and matching:

- `x in (a, b, c)` and `x not in (a, b)` test membership.
- `x in LO..HI` tests an inclusive range: `addr in 0x4000..0x4fff`.
- `x ~ 'glob'` matches the whole string against a glob, for example
  `stream ~ 'top.*.mon'`.
- `x =~ /re/` searches for a regular expression anywhere in the string
  (Python `re` syntax, no flags). A string literal also works on the right.

Functions:

| Function | Value |
|----------|-------|
| `has(f)` | true when the transaction has a value for `f` |
| `abs(x)`, `min(a, ...)`, `max(a, ...)` | as in Python |
| `bit(x, n)` | bit `n` of `x` |
| `bits(x, hi, lo)` | bits `hi` down to `lo` of `x` |
| `len(s)` | length of a string or bytes value |

### Missing values

A field the transaction lacks (another type, never set, or `dur` of an open
transaction) is missing. Every comparison against a missing value is false,
including `!=`. So `resp != 'OKAY'` does not match `t:32`, which has no
`resp`. Use `has(f)` and `!has(f)` to test presence:

```text
$ trlog find run.trl "nlinks > 0 && !has(resp)"
t:3    41.000ns  +19ns  top.env.seqr      seq_item  name=err_test prio=5
t:101  45.500ns  +0ps   top.log           msg       text="slave error at 0x8000" sev=ERROR
t:102  50.500ns  +0ps   top.log           msg       text="decode error" sev=ERROR
t:32   62.000ns  open   top.env.axi1.mon  axi_rd    addr=0xc000
t:103  70.000ns  +0ps   top.log           msg       text="timeout waiting for rsp" sev=WARN
-- 5 matched; 41.000ns .. 70.000ns
```

### Value kinds

- Unsigned and signed integers, bitvecs and 2-state logicvecs compare as
  integers.
- Enums compare by label (`resp == 'SLVERR'`) or by number (`resp == 2`).
  An unknown label is an error that lists the valid ones. Ordering
  comparisons against a label compare enum values: `resp >= 'SLVERR'` is
  the same as `resp >= 2`, and selects SLVERR (2) and DECERR (3).
- A logicvec with x or z bits never compares equal to anything, and
  arithmetic on it gives a missing value.
- Strings compare as strings.

```text
$ trlog find run.trl "resp == 'BAD'"
trlog: error: 'BAD' is not a value of enum field 'resp'
  resp == 'BAD'
          ^
  valid values: DECERR, EXOKAY, OKAY, SLVERR
```

### Expression examples

```text
$ trlog find run.trl "resp != 'OKAY'"
t:30  42.000ns  +3ns    top.env.axi1.mon  axi_wr  addr=0x8000 len=1 resp=SLVERR strb=0xff
t:31  47.000ns  +2.5ns  top.env.axi1.mon  axi_rd  addr=0x8004 len=1 resp=DECERR data=0x0 bus=0xzf
-- 2 matched; 42.000ns .. 49.500ns

$ trlog find run.trl addr in 0x4000..0x401f
t:10   2.000ns  +3ns    top.env.axi0.mon  axi_wr  addr=0x4000 len=1 resp=OKAY strb=0x0f
t:11   6.000ns  +3ns    top.env.axi0.mon  axi_wr  addr=0x4010 len=2 resp=OKAY strb=0x0f0f
t:20  22.000ns  +2.5ns  top.env.axi0.mon  axi_rd  addr=0x4000 len=4 resp=OKAY data=0xa0a00000 bus=0xa0
t:21  26.000ns  +2.5ns  top.env.axi0.mon  axi_rd  addr=0x4010 len=4 resp=OKAY data=0xa0a00001 bus=0xa1
-- 4 matched; 2.000ns .. 28.500ns

$ trlog find run.trl "text =~ /error/"
t:101  45.500ns  +0ps  top.log  msg  text="slave error at 0x8000" sev=ERROR
t:102  50.500ns  +0ps  top.log  msg  text="decode error" sev=ERROR
-- 2 matched; 45.500ns .. 50.500ns

$ trlog find run.trl "parent.name == 'err_test'"
t:30  42.000ns  +3ns    top.env.axi1.mon  axi_wr  addr=0x8000 len=1 resp=SLVERR strb=0xff
t:31  47.000ns  +2.5ns  top.env.axi1.mon  axi_rd  addr=0x8004 len=1 resp=DECERR data=0x0 bus=0xzf
-- 2 matched; 42.000ns .. 49.500ns

$ trlog find run.trl "bits(addr, 15, 12) == 8"
t:30  42.000ns  +3ns    top.env.axi1.mon  axi_wr  addr=0x8000 len=1 resp=SLVERR strb=0xff
t:31  47.000ns  +2.5ns  top.env.axi1.mon  axi_rd  addr=0x8004 len=1 resp=DECERR data=0x0 bus=0xzf
-- 2 matched; 42.000ns .. 49.500ns

$ trlog find run.trl 'meta["note"] =~ /error/'
t:30  42.000ns  +3ns  top.env.axi1.mon  axi_wr  addr=0x8000 len=1 resp=SLVERR strb=0xff
-- 1 matched; 42.000ns .. 45.000ns

$ trlog find run.trl "stream ~ 'top.env.*.mon' && dur > 2.6ns"
t:10   2.000ns  +3ns  top.env.axi0.mon  axi_wr  addr=0x4000 len=1 resp=OKAY strb=0x0f
t:11   6.000ns  +3ns  top.env.axi0.mon  axi_wr  addr=0x4010 len=2 resp=OKAY strb=0x0f0f
t:12  10.000ns  +3ns  top.env.axi0.mon  axi_wr  addr=0x4020 len=3 resp=OKAY strb=0x0f0f0f
t:13  14.000ns  +3ns  top.env.axi0.mon  axi_wr  addr=0x4030 len=4 resp=OKAY strb=0x0f0f0f0f
t:30  42.000ns  +3ns  top.env.axi1.mon  axi_wr  addr=0x8000 len=1 resp=SLVERR strb=0xff
-- 5 matched; 2.000ns .. 45.000ns
```

## Finding and inspecting

### find

`find` lists the matching transactions in `start,id` order. Each text row
is `ref  start  +dur|open  stream  type  fields`. The footer gives the
match count and the span of the whole matched set, and says how to get the
next page when the output is cut.

```text
$ trlog find run.trl open
t:4   61.000ns  open  top.env.seqr      seq_item  name=hang prio=0
t:32  62.000ns  open  top.env.axi1.mon  axi_rd    addr=0xc000
-- 2 matched; 61.000ns .. 62.000ns
```

Paging options (also on `active`, and partly on other verbs):

| Option | Meaning |
|--------|---------|
| `--limit N` | at most N rows (default 20 for `find`); `0` prints only the summary |
| `--offset N` | skip the first N rows |
| `--sort KEY[,-KEY]` | sort keys; `-` for descending. Missing values sort last either way. Default `start,id`. |
| `--first`, `--last`, `--nth N` | one row: the first, the last, or the Nth (1-based) |
| `--count` | print only the match count, like `count` |

```text
$ trlog find run.trl -T axi_rd --sort -dur --limit 2
t:20  22.000ns  +2.5ns  top.env.axi0.mon  axi_rd  addr=0x4000 len=4 resp=OKAY data=0xa0a00000 bus=0xa0
t:21  26.000ns  +2.5ns  top.env.axi0.mon  axi_rd  addr=0x4010 len=4 resp=OKAY data=0xa0a00001 bus=0xa1
-- 6 matched, showing 2 (--offset 2 for more); 22.000ns .. 62.000ns

$ trlog find run.trl -T msg --where "sev == 'ERROR'" --last
t:102  50.500ns  +0ps  top.log  msg  text="decode error" sev=ERROR
-- 2 matched, showing 1 from offset 1; 45.500ns .. 50.500ns

$ trlog find run.trl -T axi_wr --count
5
```

Projection options control what each row carries:

| Option | Meaning |
|--------|---------|
| `--with links,meta,parent,end,children` | extra sections per record |
| `--fields F1,F2` | only these fields |
| `--full-paths` | JSON: keep full stream paths (see `stream_prefix` below) |
| `--max-bytes N` | JSON: cap the output size; rows are dropped from the end, but one row is always kept |
| `--max-field-bytes N` | abbreviate `bytes` fields beyond N bytes (default 64) |

```text
$ trlog find run.trl -T axi_wr --fields addr,resp --limit 2
t:10  2.000ns  +3ns  top.env.axi0.mon  axi_wr  addr=0x4000 resp=OKAY
t:11  6.000ns  +3ns  top.env.axi0.mon  axi_wr  addr=0x4010 resp=OKAY
-- 5 matched, showing 2 (--offset 2 for more); 2.000ns .. 45.000ns

$ trlog find run.trl "resp == 'SLVERR'" --first --with links,meta,parent
t:30  42.000ns  +3ns  top.env.axi1.mon  axi_wr  addr=0x8000 len=1 resp=SLVERR strb=0xff
      parent t:3
      link in cause t:3 (stimulus)
      link out cause t:101 (resp)
      meta note="injected error"
-- 1 matched; 42.000ns .. 45.000ns
```

### show

`show` prints full records for one or more refs, including the parent,
children, metadata and links in both directions:

```text
$ trlog show run.trl t:31
t:31  axi_rd
  stream   top.env.axi1.mon
  start    47.000ns  (47000)
  end      49.500ns  (49500)
  dur      2.5ns
  parent   t:3
  children -
  fields
    addr  0x8004
    len   1
    resp  DECERR
    data  0x0
    bus   0xzf
  links
    out cause        t:102  resp
```

### count

`count` prints the number of matches. `--by` groups them by one or more
keys: `stream`, `type`, `open`, `parent`, `meta[KEY]` or a field name.
Groups are listed largest first. Transactions without a value for a key
form their own group, shown as `-` (`null` in JSON). `--limit` (default
50) caps the number of groups.

```text
$ trlog count run.trl --by stream,type
stream            type      count
top.env.axi0.mon  axi_rd        4
top.env.axi0.mon  axi_wr        4
top.env.seqr      seq_item      4
top.log           msg           4
top.env.axi1.mon  axi_rd        2
top.env.axi1.mon  axi_wr        1
-- 19 matched in 6 groups

$ trlog count run.trl -T axi_rd,axi_wr --by resp
resp    count
OKAY        8
-           1
DECERR      1
SLVERR      1
-- 11 matched in 4 groups
```

## Navigating

`show`, `tree`, `links`, `around t:ID` and `--with children` need the
whole-trace graph of transactions, links and children. The first of them
builds it with one full scan. Later commands on the same open trace (in a
`batch` run, or a script using the Python API) reuse it, and their JSON
`scan` section says `"cached": true`.

### tree

`tree REF` shows the parent/child structure around a transaction: its
ancestors up to the root, the transaction itself (marked `>`), and its
descendants.

| Option | Meaning |
|--------|---------|
| `--up N\|all` | ancestor levels (default `all`) |
| `--down N\|all` | descendant levels (default 1) |
| `--limit N` | at most N nodes (default 200). Descendants are taken breadth first, so a cut drops the deepest levels rather than later siblings. |

```text
$ trlog tree run.trl t:3
> t:3     41.000ns  +19ns   top.env.seqr      seq_item  name=err_test prio=5
    t:30  42.000ns  +3ns    top.env.axi1.mon  axi_wr    addr=0x8000 len=1 resp=SLVERR strb=0xff
    t:31  47.000ns  +2.5ns  top.env.axi1.mon  axi_rd    addr=0x8004 len=1 resp=DECERR data=0x0 bus=0xzf
-- 3 nodes; root t:3

$ trlog tree run.trl t:30 --down all
  t:3     41.000ns  +19ns  top.env.seqr      seq_item  name=err_test prio=5
>   t:30  42.000ns  +3ns   top.env.axi1.mon  axi_wr    addr=0x8000 len=1 resp=SLVERR strb=0xff
-- 2 nodes; root t:3
```

### links

`links REF` follows link edges transitively. *Up* walks against the edge
direction, towards causes; *down* walks along it, towards effects. Each row
starts with its signed distance from the focus. The `edges` section lists
the edges between the returned nodes, from the furthest cause down.

| Option | Meaning |
|--------|---------|
| `--kind K` | edge kinds to follow: `cause`, `parent_child`, `related`, `custom` (repeatable or comma-separated; default all) |
| `--up N\|all` | levels towards causes (default 1, or 0 when only `--down` is given) |
| `--down N\|all` | levels towards effects (default 1, or 0 when only `--up` is given) |
| `--limit N` | at most N nodes (default 200) |

`links` exits 1 when the transaction has no edges.

```text
$ trlog links run.trl t:101 --up all --kind cause
-2 t:3    41.000ns  +19ns  top.env.seqr      seq_item  name=err_test prio=5
-1 t:30   42.000ns  +3ns   top.env.axi1.mon  axi_wr    addr=0x8000 len=1 resp=SLVERR strb=0xff
 0 t:101  45.500ns  +0ps   top.log           msg       text="slave error at 0x8000" sev=ERROR
edges
  t:3   ->  t:30   cause  stimulus
  t:30  ->  t:101  cause  resp
-- 3 nodes, 2 edges (- = causes/up, + = effects/down)
```

### around

`around REF|@TIME` shows what happened near a transaction or a time,
merged across all streams in start order. Each row starts with its start
time relative to the anchor.

- `-n N` (default 5) gives the N transactions starting before the anchor
  and the N starting after it. A transaction anchor is always shown.
- `--window W` instead gives every transaction overlapping the anchor
  extended by W on each side, paged with `--limit` (default 50). Keep W
  well below the trace span that `info` reports.
- `--same-stream` keeps only the anchor transaction's stream (needs a
  `t:ID` anchor; with `@TIME`, use `-s`).
- `-s`, `-T` and `EXPR` filter the neighbours. `-t` is not accepted,
  because the anchor sets the time.

```text
$ trlog around run.trl t:30 -n 2
    -8ns t:23   34.000ns  +2.5ns  top.env.axi0.mon  axi_rd    addr=0x4030 len=4 resp=OKAY data=0xa0a00003 bus=0xa3
    -1ns t:3    41.000ns  +19ns   top.env.seqr      seq_item  name=err_test prio=5
>      0 t:30   42.000ns  +3ns    top.env.axi1.mon  axi_wr    addr=0x8000 len=1 resp=SLVERR strb=0xff
  +3.5ns t:101  45.500ns  +0ps    top.log           msg       text="slave error at 0x8000" sev=ERROR
    +5ns t:31   47.000ns  +2.5ns  top.env.axi1.mon  axi_rd    addr=0x8004 len=1 resp=DECERR data=0x0 bus=0xzf
-- 2 before, 2 after t:30; more before and after (-n 4)

$ trlog around run.trl @45ns --window 2ns
   -4ns t:3    41.000ns  +19ns   top.env.seqr      seq_item  name=err_test prio=5
   -3ns t:30   42.000ns  +3ns    top.env.axi1.mon  axi_wr    addr=0x8000 len=1 resp=SLVERR strb=0xff
 +500ps t:101  45.500ns  +0ps    top.log           msg       text="slave error at 0x8000" sev=ERROR
   +2ns t:31   47.000ns  +2.5ns  top.env.axi1.mon  axi_rd    addr=0x8004 len=1 resp=DECERR data=0x0 bus=0xzf
-- 4 overlapping 43.000ns .. 47.000ns
```

### active

`active @TIME` lists the transactions in progress at an instant: started at
or before T, and either open or ending after T. A transaction that ends
exactly at T is not active. It takes the selection (except `-t`), paging
and projection options of `find`.

```text
$ trlog active run.trl @48ns
t:3   41.000ns  +19ns   top.env.seqr      seq_item  name=err_test prio=5
t:31  47.000ns  +2.5ns  top.env.axi1.mon  axi_rd    addr=0x8004 len=1 resp=DECERR data=0x0 bus=0xzf
-- 2 matched; 41.000ns .. 60.000ns
```

## Analysing

### stats

`stats VALUE` gives n, min, p50, p90, p99, max and mean of a numeric value
over the matching transactions, with refs to the transactions holding the
min and max. `VALUE` is `dur`, `start`, `end`, a field, or any numeric
expression such as `'addr & 0xfff'`. `--by` groups as in `count`.

- For a bare field name, transactions whose type lacks the field are
  skipped. `missing` counts those that have the field but no usable value
  (unset, x/z bits, or `dur` of an open transaction).
- Percentiles use the nearest rank, so they are always values that occur
  in the data.
- Times print with units in text mode; JSON gives ticks and
  `value_is_time`.

```text
$ trlog stats run.trl dur -T axi_rd,axi_wr --by stream
stream            n  missing    min    p50  p90  p99  max    mean  min_ref  max_ref
top.env.axi0.mon  8        0  2.5ns  2.5ns  3ns  3ns  3ns  2.75ns  t:20     t:10
top.env.axi1.mon  2        1  2.5ns  2.5ns  3ns  3ns  3ns  2.75ns  t:31     t:30
-- dur: 10 values from 11 matched (1 without a value), 2 groups

$ trlog stats run.trl "addr & 0xfff" -T axi_wr
n  missing  min  p50  p90  p99  max  mean  min_ref  max_ref
5        0    0   16   48   48   48  19.2  t:10     t:13
-- addr & 0xfff: 5 values from 5 matched
```

Enum, string and bytes values are an error that points to `count --by`:

```text
$ trlog stats run.trl text
trlog: error: 'text' is str, not a number
  hint: use `trlog count --by FIELD` for the value counts of enum and string fields
```

### hist

`hist VALUE` counts values in bins of equal width. Bins are left-closed,
`[lo, lo+W)`, and empty bins between the min and the max are kept. With
`--bin W` (times take units: `--bin 1us`) the width is fixed. Without it,
the width is the smallest 1, 2 or 5 times a power of ten that fits in
`--bins N` bins (default 20). More than 10000 bins is an error.
`hist start` shows throughput over time.

```text
$ trlog hist run.trl start --bin 10ns
  lo    hi  count
 0ps  10ns      3  ##############################
10ns  20ns      3  ##############################
20ns  30ns      3  ##############################
30ns  40ns      2  ####################
40ns  50ns      4  ########################################
50ns  60ns      1  ##########
60ns  70ns      2  ####################
70ns  80ns      1  ##########
-- start: 19 values in 8 bins of 10ns  ▆▆▆▄█▂▄▂
```

### gaps

`gaps` finds idle intervals per stream: the parts of the range where no
selected transaction is in progress. The range is the `-t` window, or the
whole trace span, so leading and trailing silence count. Overlapping
transactions merge, and an open transaction covers to the end.

- `--min W` keeps only gaps longer than W.
- `--sort` takes `dur`, `start`, `end` or `stream` (default `-dur`,
  longest first).
- `prev` is the transaction whose end opened the gap and `next` the one
  whose start closed it; `(start)` and `(end)` mark the edges of the range.
- Selected streams with no matching transaction at all are reported as
  silent streams, not as gaps.

```text
$ trlog gaps run.trl --min 5ns
stream                from        to    idle  prev     next
top.env.axi1.mon   1.000ns  42.000ns    41ns  (start)  t:30
top.env.axi0.mon  36.500ns  70.000ns  33.5ns  t:23     (end)
top.log           19.000ns  45.500ns  26.5ns  t:100    t:101
top.log           50.500ns  70.000ns  19.5ns  t:102    t:103
top.log            1.000ns  19.000ns    18ns  (start)  t:100
top.env.axi1.mon  49.500ns  62.000ns  12.5ns  t:31     t:32
-- 6 gaps over 5ns in 1.000ns .. 70.000ns
```

### match

`match` pairs B transactions (for example responses) one-to-one with A
transactions (requests) and reports the latency `lat = b.end - a.start`
of each pair.

| Option | Meaning |
|--------|---------|
| `--a EXPR`, `--b EXPR` | expressions selecting each side (required) |
| `--on KEY` | `a.X == b.Y` terms joined by `&&` (X and Y are any expressions), or bare field names (`tag`, `tag,addr`) for `a.F == b.F`. Without a key, pairs follow order. |
| `--order fifo\|any` | `fifo` (default): the oldest unmatched A with an equal key that started at or before B. `any`: the unmatched A whose start is nearest B's, before or after. |
| `--within W` | pair only when `\|b.start - a.start\| <= W`. In `fifo` order an A that is too old for the current B is dropped as unmatched. |
| `--unmatched a\|b` | list the unmatched A or B transactions as full records instead of the pairs |
| `--sort start\|lat\|-lat` | order of the pairs (default `start`); open pairs sort last |

`-s` and `-t` apply to both sides. There is no `-T`; put `type == '...'`
in `--a` and `--b`. A transaction never pairs with itself. A transaction
whose key value is missing is counted as "without a key", not as
unmatched. `lat` is empty (null in JSON) when B is still open.

```text
$ trlog match bus.trl --a "type=='req'" --b "type=='rsp'" --on tag -s top.bus.req,top.bus.rsp
a    b     a.start   lat  tag
t:1  t:12      0ns  11ns  1
t:2  t:11  2.000ns   7ns  2
t:3  t:13  4.000ns  10ns  1
-- 3 pairs; unmatched: 2 a, 2 b; without a key: 0 a, 1 b; lat p50 10ns, p99 11ns, max 11ns
-- unmatched a: t:4 t:5 (--unmatched a to list)
-- unmatched b: t:14 t:15 (--unmatched b to list)

$ trlog match bus.trl --a "type=='req'" --b "type=='rsp'" --on tag -s top.bus.req,top.bus.rsp --unmatched b
t:14  15.000ns  +1ns  top.bus.rsp  rsp  tag=5 q=1.0
t:15  19.000ns  open  top.bus.rsp  rsp  tag=4
-- 3 pairs; unmatched: 2 a, 2 b; without a key: 0 a, 1 b; lat p50 10ns, p99 11ns, max 11ns
```

(cli-export)=
## Exporting

`export FORMAT -o OUT` writes the selected transactions to SQLite (one
file) or Parquet (a directory of `<table>.parquet` files; needs
`pyarrow`), for analysis in SQL, pandas or other tools.

| Table | Columns |
|-------|---------|
| `txn` | `id, stream_id, type_id, start, end, dur, parent, open` |
| `attr_<type>` | `id` plus one column per field of that type (exists for every type, even when empty) |
| `link` | `src, tgt, kind, label`: every edge that touches a selected transaction |
| `meta` | `id, key, value` |
| `stream` | `id, path, type, first, last, count, metadata` (first, last and count over the selection) |
| `type` | `id, name, fields` (JSON) |
| `trace` | `key, value`: export info only: `source` path, `time_unit`, `timescale_exp` and the `query` (JSON) |
| `trace_meta` | `key, value`: the trace's own metadata (`add_trace_metadata`); non-string values as JSON |

Every `id` is the transaction id, so `attr_<type>.id = txn.id`. SQLite also
gets a `txn_<type>` view per type that joins `txn`, the stream path and
the fields, and indexes on `start`, `(stream_id, start)`, the link ends and
`meta.id`. Integers outside the signed 64-bit range and 4-state values with
x/z bits are stored as hex text, enums as labels and bytes as blobs.

An existing `OUT` is an error unless `--force` is given. Output is written
to a temporary file or directory next to `OUT` and then renamed into place.

```text
$ trlog export run.trl sqlite -o run.db
wrote 19 txns to /work/run.db (sqlite)
-- tables: txn 19, attr_axi_rd 6, attr_axi_wr 5, attr_seq_item 4, attr_msg 4, link 4, meta 3, stream 4, type 4, trace 6
-- views: txn_axi_rd, txn_axi_wr, txn_msg, txn_seq_item

$ trlog export run.trl parquet -o run_pq
trlog: error: export parquet needs pyarrow, which is not installed
  hint: pip install pyarrow  (or `pip install trlog[parquet]`); `trlog export sqlite` needs nothing extra
```

Python's `sqlite3` module reads the result, with no `sqlite3` command
needed:

```python
import sqlite3
db = sqlite3.connect("run.db")
db.execute("SELECT id, stream, start, dur, addr, resp FROM txn_axi_wr "
           "ORDER BY start").fetchall()
# [(10, 'top.env.axi0.mon', 2000, 3000, 16384, 'OKAY'), ...,
#  (30, 'top.env.axi1.mon', 42000, 3000, 32768, 'SLVERR')]
```

## Running many commands: batch

`batch TRACE` opens the trace once and runs one command per line of stdin.
A line is `VERB [options] [EXPR]` without the trace path; a leading `trlog`
and a repeated trace path are tolerated. Blank lines and `#` comments are
skipped, and a nested `batch` is an error. The whole-trace graph built by
`show`, `tree` or `links` is shared by later lines.

In text mode each command's output follows a `== [n] line` header, and
errors print in place on stdout so the order is kept:

```text
$ printf '%s\n' '# triage' 'count --by type' 'find open' 'links t:30 --down all' | trlog batch run.trl
== [1] count --by type
type      count
axi_rd        6
axi_wr        5
msg           4
seq_item      4
-- 19 matched in 4 groups
== [2] find open
t:4   61.000ns  open  top.env.seqr      seq_item  name=hang prio=0
t:32  62.000ns  open  top.env.axi1.mon  axi_rd    addr=0xc000
-- 2 matched; 61.000ns .. 62.000ns
== [3] links t:30 --down all
 0 t:30   42.000ns  +3ns  top.env.axi1.mon  axi_wr  addr=0x8000 len=1 resp=SLVERR strb=0xff
+1 t:101  45.500ns  +0ps  top.log           msg     text="slave error at 0x8000" sev=ERROR
edges
  t:30  ->  t:101  cause  resp
-- 2 nodes, 1 edge (- = causes/up, + = effects/down)
```

With `--json`, every line runs with `--json` (`--jsonl` and `--pretty`
are dropped) and prints one compact envelope, with `seq` and `exit` first:

```text
$ printf '%s\n' 'count -T axi_wr' 'find nosuchfield == 1' | trlog batch run.trl --json
{"seq":1,"exit":0,"cmd":"count","query":{"types":["axi_wr"]},"time_unit":"ps","summary":{"matched":5,"groups":1,"returned":1,"truncated":false},"keys":[],"results":[{"count":5}],"next":null,"scan":{...}}
{"seq":2,"exit":2,"error":"unknown_field","message":"unknown field 'nosuchfield'","name":"nosuchfield","pos":0,"did_you_mean":["nchildren"],...}
```

`batch` exits 2 if any command failed, else 0 if any matched, else 1.

## Output formats

### Text

Text is the default. On a terminal it is formatted (colour, terminal
width); when piped it is plain. `--color auto|always|never` overrides the
choice. Times print with units; footers starting with `--` summarise the
result and say how to get more.

(cli-json)=
### JSON

`--json` prints one envelope per command. `--pretty` indents it.

```text
$ trlog find run.trl -T axi_wr --limit 1 --json --pretty
{
  "cmd": "find",
  "query": {
    "types": [
      "axi_wr"
    ]
  },
  "time_unit": "ps",
  "summary": {
    "matched": 5,
    "returned": 1,
    "truncated": true,
    "span": [
      2000,
      45000
    ],
    "by_stream": {
      "top.env.axi0.mon": 4,
      "top.env.axi1.mon": 1
    }
  },
  "results": [
    {
      "ref": "t:10",
      "stream": "top.env.axi0.mon",
      "type": "axi_wr",
      "start": 2000,
      "dur": 3000,
      "fields": {
        "addr": 16384,
        "len": 1,
        "resp": "OKAY",
        "strb": "0x0f"
      }
    }
  ],
  "next": "--offset 1",
  "scan": {
    "blocks_read": 8,
    "blocks_skipped": 0,
    "ms": 0.4,
    "index": true,
    "qindex": false
  }
}
```

The envelope fields:

| Key | Meaning |
|-----|---------|
| `cmd`, `query` | the verb and the resolved selection |
| `time_unit` | unit of every time in the envelope; times are integers in this unit |
| `summary` | covers the whole matched set, not only the page: `matched`, `returned`, `truncated`, `span`, `by_stream` (capped at 20 streams, plus `by_stream_omitted`) |
| `stream_prefix` | a common prefix removed from each row's `stream`, when present; `--full-paths` turns this off (never used with `--jsonl`) |
| `results` | the rows. A transaction row has `ref`, `stream`, `type`, `start`, `dur` or `"open": true`, and `fields`, plus any `--with` sections |
| `next` | the option that fetches the next page, or null |
| `scan` | `blocks_read`, `blocks_skipped`, `ms`, `index`, `qindex`, and `cached` when the graph was reused |

Each verb adds its own keys: `info` puts its facts under `info` and its
suggestions under `try`; `count` adds `keys`; `match` adds `results_are`
and `unmatched`; `--max-bytes` adds `max_bytes_hit` when it dropped rows.

Value encoding: integers wider than 2^53, bitvecs and logicvecs are hex
strings (with `x`/`z` digits for 4-state values), enums are labels, and
bytes are hex. Integers that fit are JSON numbers.

`--max-bytes N` keeps the envelope under N bytes by dropping rows from the
end, and `next` then points at the first dropped row. At least one row is
always returned, even when it alone is larger than N, so paging with `next`
always moves on:

```text
$ trlog find run.trl -T axi_rd --fields addr --sort -addr --limit 3 --json --max-bytes 600
{"cmd":"find","query":{"types":["axi_rd"]},"time_unit":"ps","summary":{"matched":6,"returned":2,"truncated":true,"span":[22000,62000],"by_stream":{"top.env.axi0.mon":4,"top.env.axi1.mon":2}},"stream_prefix":"top.env.","results":[{"ref":"t:32","stream":"axi1.mon","type":"axi_rd","start":62000,"open":true,"fields":{"addr":49152}},{"ref":"t:31","stream":"axi1.mon","type":"axi_rd","start":47000,"dur":2500,"fields":{"addr":32772}}],"next":"--offset 2","scan":{...},"max_bytes_hit":true}
```

### JSON lines

`--jsonl` prints one JSON record per line and then the envelope without
`results` as the last line:

```text
$ trlog find run.trl -T axi_wr --limit 2 --jsonl
{"ref":"t:10","stream":"top.env.axi0.mon","type":"axi_wr","start":2000,"dur":3000,"fields":{"addr":16384,"len":1,"resp":"OKAY","strb":"0x0f"}}
{"ref":"t:11","stream":"top.env.axi0.mon","type":"axi_wr","start":6000,"dur":3000,"fields":{"addr":16400,"len":2,"resp":"OKAY","strb":"0x0f0f"}}
{"cmd":"find","query":{"types":["axi_wr"]},"time_unit":"ps","summary":{...},"next":"--offset 2","scan":{...}}
```

Each line stands alone: rows always carry full stream paths, and there is
no `stream_prefix` in the last line (`--full-paths` makes no difference).

### Errors

In text mode errors go to stderr as `trlog: error: ...`, with a caret
under the failing position of an expression and any suggestions. With
`--json` they go to stdout as an object:

```text
$ trlog find run.trl rsp == 1 --json
{"error":"unknown_field","message":"unknown field 'rsp'","name":"rsp","pos":0,"did_you_mean":["resp"],"known_fields":{"axi_rd":["addr","len","resp","data","bus"],"axi_wr":["addr","len","resp","strb"],"seq_item":["name","prio"],"msg":["text","sev"]},"hint":"run `trlog schema TRACE` to list types and fields","expr":"rsp == 1"}
```

Every error has `error` (a code) and `message`, and may have `pos`,
`did_you_mean`, `valid_values`, `known_types`, `known_fields` and `hint`.
Bad options are `usage` errors that point to `trlog help VERB`.

## Exit status

| Status | Meaning |
|--------|---------|
| 0 | the command ran and matched something |
| 1 | the command ran and matched nothing |
| 2 | usage, expression, file or other error |

This follows `grep`, so `trlog find run.trl "resp == 'SLVERR'" --count >/dev/null && echo found`
works in scripts.

(cli-engines)=
## Scan engines

Queries run on one of two engines with identical results:

- the native engine in `libtrl` (`trl_qscan_*`, declared in
  `c/include/trl/trl_qscan.h`), which decodes, joins and filters blocks in
  C and is 8 to 16 times faster on large traces;
- the pure-Python engine, which is the reference and always works.

The C engine is an optional accelerator. By default (`auto`) `trlog` uses
it when the library loads, and otherwise runs in Python with no other
change. `info` shows which engine ran. Set `TRLOG_ENGINE` to choose:

| `TRLOG_ENGINE` | Engine |
|----------------|--------|
| `auto` (default) | C when the library loads, else Python |
| `python` | always Python |
| `c` | C; an error when the library is not available |

The library is looked for in `$TRL_LIBRARY_PATH` (a file path), next to
the installed `trlog` package, and then in `build/c`, `c/build` and
`build` of a source checkout. A library built for a different qscan ABI
version is not used.

Some inputs always run in Python, even with `TRLOG_ENGINE=c`: traces
without an index, column-layout transaction blocks, zstd-compressed
blocks, and time bounds outside the 64-bit range. The C engine hands such
scans back to Python, which reruns them, so the output does not change.

```text
$ TRLOG_ENGINE=python trlog info run.trl | grep engine
engine       python
```

## Limits

- Each command scans the blocks it needs, and `show`, `tree`, `links`,
  `around t:ID` and `--with children` scan the whole trace once per
  process. There is no persistent query index yet (`qindex` is always
  false). Use `batch` or the Python API to run many queries on one scan.
- Time hints in refs (`t:ID@T`) are accepted but not used.
- `--with links` reads every block, because a link record can be stored in
  any stream's block.
