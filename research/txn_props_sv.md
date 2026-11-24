# Transaction properties in SystemVerilog: in-simulation checking

Status: **proposal, not reviewed** (2026-11-23). Revised the same day: SV
predicates are built from generated, typed classes instead of strings (§4). Builds on
`research/txn_property_arch.md` (layers, IR) and `sv_api_design.md` (L0 C
session, DPI cost model, `trl_pkg`).

## 1. The use case

A testbench records transactions through `trl_pkg`. It defines properties
*in SV* (or loads them from a file). While the simulation runs, the engine
checks each recorded txn against those properties. It flags violations to
the testbench (`$error`, UVM report, optional stop), and records them in the
trace next to the transactions that caused them. Coverage for the same
properties is collected at the same time.

Post-run checking (`trlog check`) stays available and remains the reference.
In-sim checking adds two things: **failing early**, with sim context still
live, and **zero extra steps** in the flow.

## 2. Where the engine runs

Online checking happens inside the **L0 C recording session**: at
`trl_rec_end` / `trl_rec_full`, and for the deadline timers described in
§5.3. There is no Python inside a simulation, so for in-sim checking the C
engine is *required*.

This is consistent with the rule that C is an optional accelerator:
- A build without the C property engine still records.
- The properties are written into the trace (`PROP` block), and
  `trlog check` evaluates them post-run in Python.
- The Python implementation stays the semantic reference.

What C has to implement:

```
 SV typed builder (DPI, one call per property) ─┐
 JSON IR file ──────────────────────────────────┴─▶ C IR ─▶ bind (live session types) ─▶ plan ─▶ online engine
                                                   (structs)                                    │
                                                                    violations → trace + SV poll ◀┤
                                                                    coverage   → COVG block     ◀┘
```

- **A JSON IR reader.** The IR is public (decision 2), so C reads the same
  JSON as every other consumer. YAML and the Python DSL compile to JSON
  ahead of time: `trlog props compile props.yaml -o props.json`.
- **No expression parser.** Both inputs arrive already structured: the
  JSON IR, and the expression trees built by the generated SV classes
  (§4). The parser stays in Python only.
- **Bind against the live session.** It resolves type and field names to
  `trl_rtype_t` and field indices, converts time units to ticks, and does it
  **lazily**: a property whose types aren't registered yet binds when the
  type is first created. SV code often registers types on first use.
- **Plan + engine** for the online subset (§5). Predicates compile to the
  **same bytecode as the query scanner** (`trl_qscan.h`). That means one
  predicate VM in C, reading either packed records from a file or live
  open-transaction slots.

Two implementations (Python and C) of bind, plan and evaluate is the real
cost of this feature. Three things keep them aligned:
- a shared golden suite of JSON IR fixtures and tiny traces;
- every trace that was checked in-sim embeds its `PROP` set and the online
  results, so `trlog check TRACE --compare-online` re-runs the check in
  Python and diffs the two. This runs in CI on every SV test;
- the Python reference evaluator as the final authority on semantics.

## 3. SV reporting without exports

`sv_api_design.md` §3.1 rules out DPI exports (C→SV callbacks) and `context`
imports on the hot path. Violations therefore reach SV by **polling**, and
the common case costs no extra crossing:

- **The flag comes back with the record call.** When properties are
  attached to the database, the L2 `record()` / `close()` methods use import
  variants with an extra `output int nviol` argument. Output scalars are
  about free (§3.1 of the SV design). If it is non-zero, SV calls
  `trl_props::report()`, which drains the violation queue:
  `trl_prop_next_violation(output ...)` per entry, then `$error` or UVM.
  With no violations the cost is zero extra crossings, which keeps the
  §3.2 budget.
- **A background poll for deadlines.** A hung DUT produces *no* record calls,
  so a `within` deadline can expire with nobody asking. `trl_props::start()`
  forks one SV process that calls `trl_prop_poll(now)` every
  `+trl_props_poll=<time>` (default: off, with a check only at
  end of sim), or on a user-supplied event. The poll advances the engine's
  clock, fires expired deadlines and returns the count.
- **End of sim:** `trl_db_close` resolves pending attempts per
  `pending_at_end`, reports the remaining violations, and writes `PROP` and
  `COVG`.
- **Reactions:** each property has a `severity: info|warning|error|fatal`
  (default `error`) and `on_fail: continue|stop|finish` (default
  `continue`).
  - `trl_pkg` maps these to `$info/$warning/$error/$fatal` and
    `$stop/$finish`.
  - The optional `trl_uvm` layer maps them to `uvm_report_*`, with the
    property id as the message id, so UVM verbosity, catchers and
    `+UVM_MAX_QUIT_COUNT` work as usual.
  - The message carries the requirement, the trigger and witness refs, and
    a ready-to-run `trlog show TRACE t:…` hint.

`severity` and `on_fail` become IR `options`. They mean the same thing to
the Python online tap and are ignored post-run.

## 4. Authoring properties in SV

### 4.1 Approach: generated, typed modeling classes

SV has no operator overloading. Rather than predicates in strings, a
generator turns the testbench's existing SV transaction types into
**modeling classes**. Their field accessors return expression objects, so
predicates are built from SV calls that the SV compiler checks:

```systemverilog
p.trav(b, b.id().eq(a.id()))
```

The generator is a **trlog plugin for sv-weave** (pyslang-based class
weaving); `research/sv_gen.md` has the design. From each transaction class
it produces three pieces:

1. **Type registration:** `axi_rd_trl::type_()` builds the `trl_type` once,
   with field indices as `localparam`s. This replaces the hand-written
   `add_field` bookkeeping in `sv_api_design.md` §8.1.
2. **Recording glue:** `axi_rd_trl::record(s, t0, t1, axi_rd_s item)` packs
   the struct into the bulk buffer and makes the single Tier-1 DPI call.
3. **The property handle class:** `axi_rd_ph extends trl_ph`, with one
   accessor per field, returning an expression bound to the field *index*.

Recording and properties come from the same generated package, so they
always agree on names and indices. Binding by name can't fail for generated
types, and a mistyped field is an SV elaboration error, not a bind-time
error.

The input is the testbench's SV classes, read through sv-weave's class
model. The recorder is woven into a factory subclass, and the layout and
modeling classes are emitted as standalone classes (`sv_gen.md` §3).

### 4.2 Expression objects

- `trl_pe` is a plain SV class tree: op, children, and a literal or field
  ref. Building one makes **no DPI calls**. When a property is registered,
  its whole IR (scenario plus expressions) is flattened to post-order node
  arrays plus a small string table, and sent in **one** DPI call with open
  arrays. The "strings only at setup" rule (SV design §3.1) holds.
- **Method chaining** is the primary style, because it reads left to right:
  `b.id().eq(a.id()).and_(b.resp().eq(OKAY))`. The same operations exist
  as package functions (`trl_px::eq(x, y)`) for those who prefer prefix
  form.
- **Naming around SV keywords and fields:**
  - `and`, `or`, `not`, `inside` and `end` are SV keywords, so the methods
    are `and_`, `or_`, `not_`, `inside_(lo, hi)`, `in_('{…})`.
  - Built-ins carry a `t_` prefix (`t_start()`, `t_end()`, `t_dur()`,
    `t_stream()`), so they can't collide with a field called `start`.
  - The generator renames any field that collides with a method, and
    reports it.
- **Light typing where it pays:**
  - Enum fields return `trl_pe_enum #(E)`, whose `eq(E v)` takes a real SV
    enum literal (`b.resp().eq(OKAY)`), checked by the compiler.
  - Other fields return the generic `trl_pe`.
  - Literals: `lit(longint)`, `lit_s(string)`, and time literals
    `p.ns(200)`, `p.us(1)`, which carry units into the IR.
- **Ad-hoc / runtime-defined types** fall back to `h.field("name")`, which
  is checked at bind, the same as JSON.

### 4.3 Example

```systemverilog
import axi_trl_pkg::*;          // generated by the sv-weave trlog plugin (svweave.Weave)

trl_props    p = db.props();
axi_rd_ph    a = axi_rd_ph::create(p, "a");
axi_rresp_ph b = axi_rresp_ph::create(p, "b");

`trl_assert(p, "rd_resp_latency")              // macro adds `__FILE__/`__LINE__ to loc
   .req("REQ-AXI-012")
   .trigger(a)
   .scenario(p.concat('{ p.trav(a),
                         p.eventually(p.trav(b, b.id().eq(a.id())
                                                 .and_(b.resp().eq(OKAY))),
                                      p.ns(200)) }))
   .pending_at_end(TRL_PROP_FAIL);

// Checker shorthands
p.response("wr_resp", a_wr, b_bresp, b_bresp.id().eq(a_wr.id()), p.ns(500));
p.never   ("no_ro_write", w, w.addr().inside_(32'h1000, 32'h10ff));
p.max_outstanding("rd_inflight", a, 8);

// Coverage on a property
trl_pcg cg = p.find("rd_resp_latency").covergroup("lat_cov");
cg.coverpoint("lat", b.t_end().sub(a.t_start()))
  .buckets('{ p.ns(0), p.ns(20), p.ns(50), p.ns(100), p.ns(200) });
cg.coverpoint("len", a.len()).auto();
cg.cross("len_x_lat", '{"len", "lat"});

// Properties compiled from YAML / the Python DSL
p.load("props.json");
```

Notes:
- Builder errors (an unknown handle, a forward reference in a `with`, a
  degenerate cover) are raised as `$error` at registration. They carry the
  `loc` from the `` `trl_assert ``/`` `trl_cover `` macro, and bind errors
  carry the same `loc`.
- Properties can be added at any time. Attempts start from the moment a
  property is added, and the report notes when a property was added
  mid-run.
- The verbosity is real but bounded. Most SV properties should be checker
  shorthands. Long scenarios are better written in YAML or the Python DSL
  and loaded.

## 5. Online semantics: the hard parts

### 5.1 Arrival order vs. start order

A `bind: next` traversal binds the "first qualifying occurrence *by start
time*" (`txn_property_arch.md` §9, decision 6). Online, txns arrive at `end`
(Tier 1 `record()` delivers a whole txn after the fact, with `start` in the
past). Field values are only final at end. So the engine can only bind a
`next` handle to candidate *c* once it knows that **no txn of that type with
an earlier start can still arrive**. A `bind: any` handle can bind each
candidate as it arrives, but a failure, and the choice of the earliest
witness, wait on the same condition.

- Each stream keeps a **watermark**: a start time before which no further
  txn will arrive on that stream.
  - On streams recorded with begin/end, the watermark is exact: the start
    of the oldest open txn, or *now* if none is open.
  - On `record()`-only streams, it is `now - max_dur`. `max_dur` is declared
    per stream or type (`s.set_max_dur("1us")`). If not declared, it is the
    largest duration seen so far on that stream (adaptive).
- An attempt is decided only once the watermarks of all streams its pending
  handles can bind to have passed the decision point.
- A txn that arrives *behind* the watermark is counted as a **late
  arrival**. The engine warns once per stream, and the property's online
  result is marked `inexact`. The post-run Python check gives the exact
  answer, and `--compare-online` shows whether the late arrival made a
  difference.

Most monitors record in order per stream, so in practice this rarely
triggers. The declared `max_dur` is the fix when it does.

### 5.2 What qualifies for online checking

- **Online-checkable:** every property whose future obligations are
  time-bounded (`within`), plus all checkers.
- **Unbounded `eventually`/`sequence`:** in an assert these can only fail at
  end of sim. They still run online, but they are reported at close.
- The builder warns when an online assert has no time bound, since it can
  never fire during the run.
- Attempts are capped per property (`max_attempts`, default 64k), with a
  warning on overflow. This keeps an unbounded cover from holding memory
  for the whole run.

### 5.3 Deadlines and time

- The engine keeps a min-heap of `within` deadlines. Every engine entry
  (record call or poll) fires expired ones first.
- A violation is **timestamped at the deadline**, not at detection, so
  reports and the trace show *when it actually failed*, however late the
  next record call or poll came.

### 5.4 Per-transaction cost

- Each type keeps a list of the handle slots that reference it. A txn whose
  type no property mentions costs one branch.
- Otherwise the cost is O(live attempts waiting on that type) predicate
  evaluations, running on the qscan VM over the slot's `vals[]`.
- Coverage sampling happens only at first match.
- There are **zero extra DPI crossings** per txn (§3).

## 6. Violations in the trace

- Each violation, and optionally each cover match, is written by the same L0
  writer as a txn on the virtual stream `props.<id>`:
  - `start` = attempt start; `end` = deadline or detection time;
  - fields: `verdict`, `slack`, `req`;
  - `cause` links to the trigger and to every bound txn.

  All the query verbs, and a waveform viewer, then show the failure next to
  its cause: `trlog around t:<violation>` lands right on it.
- At close, the writer stores the `PROP` block (the bound property set as
  JSON IR, including properties built in SV) and the `COVG` block (status
  counts, bin hits, `inexact` flags).
- `trlog check TRACE` with no property file re-runs the embedded set. Adding
  `--compare-online` diffs the result against the stored online results.

## 7. Phasing

This slots into `txn_property_arch.md` §8 as step 5's "online tap", split
into these steps:

1. **C IR + JSON reader + bind (lazy) + plan**, reusing the qscan predicate
   bytecode. Tested against the shared JSON golden suite.
2. **C online engine for checkers and `trigger … within` asserts**, with
   watermarks, the deadline heap and `PROP`/`props.<id>`/`COVG` write-back,
   plus `--compare-online` in Python.
3. **SV**: `trl_props` loading (`p.load`), the poll and report path (the
   `nviol` output variants, the background poll), and severity mapping, with
   `trl_uvm` mapping as an option.
4. **The sv-weave trlog plugin's modeling classes** (`sv_gen.md` §4 step 2) and the **SV typed builder**: `trl_pe`
   trees, one-call registration, checker shorthands and covergroups.
5. **The rest of the PSS core online** (`schedule`, `overlap`, `select`) and
   the remaining extensions.

Steps 1 to 3 already give "define in YAML/Python, enforce in SV". Step 4
adds "define in SV".

## 8. Open questions

1. **Generator:** settled on sv-weave (`research/sv_gen.md`). Its open
   questions are tracked there.
2. **Late recording.** Do your monitors use `record()` for transactions whose
   start is far in the past, such as pipelined monitors that emit out of
   order? If so, declaring `max_dur` should probably be required rather than
   adaptive.
3. **Defaults for reactions.** Is `severity: error, on_fail: continue`
   (count and report, never stop) the right default, with the background
   poll off?
4. **UVM.** Should `trl_uvm` mapping be on by default when UVM is present,
   or opt-in?
