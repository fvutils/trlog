# Transaction properties: layered architecture

Status: **proposal; first review decisions recorded in §9** (2026-11-23). Builds on
`research/txn_properties.md` (semantics: PSS 3.1 behavioral coverage core
plus extensions for assert, time bounds, absence, structure).

## 1. The layers

Properties are specified at several levels. All of them meet at one
**property IR**. Front-ends only *build* IR, and back-ends only *consume*
it. No front-end talks to an engine directly, and no engine knows which
front-end a property came from.

```
 Front-ends (authoring)                          (each one only builds IR)
 ┌──────────────┬──────────────┬───────────────┬───────────────┬────────────┐
 │ Python DSL   │ text / YAML  │ PSS (Zuspec)  │ other langs   │ agents     │
 │ trlog.props  │ + CLI -e     │ adapter lives │ (SV, C++,     │ (text or   │
 │ .dsl         │              │ in Zuspec     │  Rust, ...)   │  JSON IR)  │
 └──────┬───────┴──────┬───────┴───────┬───────┴───────┬───────┴─────┬──────┘
        │ builder API (Python)          │      serialized IR (JSON)   │
        ▼                               ▼                             ▼
 ┌──────────────────────────────────────────────────────────────────────────┐
 │ L1  Builder API: constructs + structurally validates IR nodes            │
 ├──────────────────────────────────────────────────────────────────────────┤
 │ L0  Property IR (unbound): schema-independent, serializable, versioned   │
 └──────────────────────────────────┬───────────────────────────────────────┘
                                    │ bind: against a TraceSchema (.trl file,
                                    ▼       or a live writer's type registry)
 ┌──────────────────────────────────────────────────────────────────────────┐
 │ Bound IR: names → type/field/stream ids, time literals → ticks,         │
 │ expressions type-checked                                                │
 └──────────────────────────────────┬───────────────────────────────────────┘
                                    │ plan: selections pushed down to the
                                    ▼       index, predicates compiled
 ┌───────────────┬──────────────────┬────────────────────┬──────────────────┐
 │ reference     │ offline engine   │ C accelerator      │ online tap       │
 │ evaluator     │ (Python, .trl)   │ (optional, reads a │ (Python writer,  │
 │ (Annex F      │                  │  lowered binary    │  C L0 session)   │
 │  oracle)      │                  │  plan)             │                  │
 └───────────────┴──────────────────┴────────────────────┴──────────────────┘
                                    │
                                    ▼
                      Results model (shared): report, COVG block,
                      props.<id> virtual stream, coverage samples
```

The same rule the Zuspec/fw-hdl work follows applies here: the IR is the
hub, and engines are peers behind it. If one front-end or one engine
leaks into the IR (for example, IR nodes that only make sense to PSS,
or a shape dictated by the C engine), it becomes a second IR that every
other layer has to work around.

## 2. L0: the property IR

### 2.1 Requirements

- **Unbound and portable.** It refers to types, fields and streams *by
  name*, and keeps time literals *with units* (`200 ns`, not ticks).
  Properties are written before any trace exists and get applied to many
  traces. This differs from today's filter parser, which converts time to
  ticks at parse time using the trace's timescale (`_expr.py`, `_Parser.ts`).
- **Serializable, versioned, public early.** A JSON form with a published
  schema. This is how non-Python front-ends produce properties, and how a
  trace can carry its own properties (§6). It is a public interface from the
  first release, and expected to evolve (decision 2):
  - Each document carries `"trlog_props": "MAJOR.MINOR"`.
  - A minor bump is additive: new optional fields or new node kinds.
  - A major bump is breaking.
  - While it is `0.x`, a minor bump may break, with a changelog entry.
  - Readers ignore unknown *optional fields*. They reject an unknown *node
    kind* with an error naming the version that defines it.
  - Node kinds not yet settled are marked `experimental` in the schema.
  - Golden JSON fixtures in the tests catch accidental format changes.
- **Small and canonical.** Sugar is lowered by front-ends or the builder.
  `sequence {a; b}` becomes `concat {a; eventually b}`, as in Annex F. Each
  node keeps an `origin` note (`sugar`, `defined_in`) so messages can say
  "sequence", or name the base monitor a node came from.
- **Source locations everywhere.** Every node carries an optional
  `loc: {file, line, col, frontend}`. Errors from binding and violations
  in reports point back to the user's source: the DSL line, the YAML line or
  the PSS line.
- **No inheritance or overrides in the IR** (decision 4). Front-ends resolve
  their own extension, inheritance and override rules and emit flattened
  monitors. `origin.defined_in` keeps the name of the defining monitor or
  class.
  Annex F makes the same simplification (it assumes all handles are hoisted
  to the top level). Named monitors *do* stay in the IR as reusable
  definitions, because reports and per-instance coverage need the names.

### 2.2 Node families

**Declarations**

| Node | Fields |
|---|---|
| `PropertySet` | `trlog_props` (version), `monitors[]`, `properties[]`, `requirements[]` (id → title/source URI) |
| `Monitor` | `name`, `handles[]`, `scenario`, `constraints[]`, `covergroups[]` |
| `Handle` | `name`, `txn_type` (name, possibly qualified), `subtypes: bool` (also match recorded subtypes, using the class hierarchy in the type registry; see `sv_gen.md` §5 decision 4), optional `streams` (glob list), optional `array` dim |
| `Property` | `id`, `kind: cover \| assert`, `reqs[]`, `handles[]`, `trigger?`, `scenario \| checker`, `constraints[]`, `covergroups[]`, `options` (`pending_at_end`, `anchor`, `stop_on_fail`, ...) |

**Scenarios** (PSS core first, then extensions)

| Node | Source |
|---|---|
| `Traverse(handle, pred?, bind: any \| next)` | `bind: any` (default): any qualifying txn at or after the checkpoint (decision 6). `bind: next`: PSS `h with p`, the first qualifying txn, no backtracking |
| `Concat(items[])`, `Eventually(s, within?)`, `Select(alts[])`, `Schedule(items[])`, `Overlap(items[])` | PSS |
| `MonitorRef(name, bind{}, pred?)` | PSS monitor traversal |
| `Constrain(s, pred)` | PSS standalone constraint |
| `Within(s, dur, anchor)` | extension: time bound |
| `Absent(s, within)` | extension: absence |
| `During(outer, inner)` | extension: containment |
| `Repeat(s, min, max)` | extension: bounded repetition |
| `Scope(kind: children \| linked(kind), of: handle, s)` | extension: structure |

**Checkers** (not scenarios, because they aggregate over many txns)

`Never(handle, pred)`, `Outstanding(handle, pred, op, n)`,
`CountIn(handle, pred, window, op, n)`. Each is a property body in its own
right.

**Coverage** (decision 3: bins and crosses live in the IR)

This is a subset of SV and PSS covergroups. It is big enough that
`trlog cover` can compute coverage and report holes on its own, without
pyvsc or Zuspec.

| Node | Fields |
|---|---|
| `Covergroup` | `name`, `coverpoints[]`, `crosses[]`, `sample_on: holds \| violated \| all` (default `holds`), `options` (`per_instance`, `at_least`, `goal`, `weight`) |
| `Coverpoint` | `name`, `expr` (expression IR over bound handles), `iff?`, `bins[]`, `options` (`auto_bin_max`, `at_least`) |
| `Bins` | `name`, `kind: bins \| ignore \| illegal`, `values[]` (`Lit` / `Range`), `split?` (one bin per value, or N equal bins), `default?` |
| `AutoBins` | for enums (one bin per enum value, resolved at bind) and small fixed-width ints. Unbounded values such as latency need explicit ranges. |
| `Buckets` | `edges[]`: sugar for contiguous ranges, e.g. latency `[0, 5ns, 20ns, 100ns]`. Edges carry units and become ticks at bind. |
| `Cross` | `name`, `coverpoints[]`, `bins[]` whose `select` is a predicate over the coverpoint values (a simplified `binsof … intersect`), with `kind` as above |

Rules:
- **Sampling** follows PSS: once per successful attempt, at first match,
  using that realization's bindings. Ties are broken by `(start, id)`, which
  closes PSS's tie non-determinism. On an assert, `sample_on` picks whether
  holding attempts, violating attempts or all attempts sample.
- **Illegal bins are violations.** An `illegal` hit is reported like an
  assert failure, with the attempt's witness refs. This ties coverage
  directly into requirement status.
- **No transition bins** (`(a => b)`). Temporal order is what scenarios are
  for. Write the transition as a scenario, and bin its values.
- **Front-ends lower their own coverage models** to these nodes: PSS
  covergroups in monitors, the DSL's covergroup classes, YAML `bins:`. pyvsc
  interop (importing a pyvsc covergroup, exporting results) is an adapter,
  not a dependency.

**Expressions**

One expression IR, shared with the query filter language. Its nodes are:
`Lit(value, kind, unit?)`, `Field(handle?, name)`,
`Builtin(handle?, start|end|dur|stream|type|id|open)`, `Meta(handle?, key)`,
`Un`, `Bin`, `In`, `Range`, `Match(glob|regex)`, `Call(fn, args)`.

`handle?` is empty in the filter language, where there is one implicit
txn. In properties it names a bound handle. This means refactoring
`_expr.py`'s tuple AST into these nodes, with unit-carrying time literals.
The filter language then becomes "an expression over one implicit handle",
and both features share the parser, the type checker and the predicate
compiler (`_Compiler`).

### 2.3 Example (JSON form)

```json
{"version": 1,
 "properties": [{
   "id": "rd_resp_latency", "kind": "assert", "reqs": ["REQ-AXI-012"],
   "handles": [{"name": "a", "txn_type": "axi_rd"},
               {"name": "b", "txn_type": "axi_rd_resp"}],
   "trigger": "a",
   "scenario": {"op": "concat", "items": [
     {"op": "traverse", "handle": "a"},
     {"op": "eventually", "within": {"value": 200, "unit": "ns"},
      "s": {"op": "traverse", "handle": "b",
            "pred": {"op": "==", "l": {"field": "id", "h": "b"},
                                 "r": {"field": "id", "h": "a"}}}}]},
   "options": {"pending_at_end": "fail"},
   "loc": {"file": "props.yaml", "line": 3, "frontend": "yaml"}}]}
```

## 3. L1: the builder API

This is the programmatic layer. Front-ends written in Python call it.
Everything else serializes to the JSON in §2.3, which deserializes to the
same objects.

- **Immutable node classes** (frozen dataclasses) in `trlog.props.ir`, with
  `to_json()` and `from_json()`.
- **Thin constructors** in `trlog.props.build`. They convert plain Python
  values (`"200ns"`, ints, strings) into nodes and attach `loc`.
- **Structural validation at build time**, with no schema needed. These are
  the rules Annex F and the extensions impose:
  - Every handle is declared.
  - The inline predicate of a `bind: next` traversal only references handles
    already bound to its left (Annex F's `supp(p) ⊆ dom(r) ∪ {h}`), because
    "first qualifying" must be decidable when the txn is seen. A `bind: any`
    predicate may reference any handle in the scenario, siblings in
    `schedule`/`overlap` included (decision 6).
  - A named handle is traversed once per scope.
  - A `cover` scenario must not be degenerate (it must not admit an empty
    realization).
  - An assert's `trigger` is the first handle the scenario traverses.
  - `Within` and `Absent` need a bound.
- **Schema validation at bind time** (§5): types and fields exist, the
  expression types check, and enum names are known. Errors use the query
  engine's agent-friendly structured form (`did_you_mean`, `known_fields`),
  plus the `loc` from the front-end.

```python
from trlog.props import build as B

a, b = B.handle("a", "axi_rd"), B.handle("b", "axi_rd_resp")
prop = B.assert_("rd_resp_latency", reqs=["REQ-AXI-012"], trigger=a,
    scenario=B.concat(a, B.eventually(b.with_(b.f.id == a.f.id), within="200ns")),
    pending_at_end="fail")
pset = B.property_set(prop)
pset.to_json()
```

**Predicates are Python expressions only, never strings** (decision 7). The
`b.f.id == a.f.id` form above uses operator overloading, and it builds the
*same* expression IR nodes (§2.2, Expressions) that the filter-language
parser produces. So there is one type checker, one predicate compiler, one
C pushdown and one missing-value rule. Python and the text form (§4.2)
differ only in spelling.

Operator overloading has traps, and agents fall into them, so the builder
guards against each one:

| Trap | Guard |
|---|---|
| `and` / `or` / `not` can't be overloaded; `p and q` silently keeps one side | The expression's `__bool__` raises: "use `&`, `\|`, `~`, or `B.all_(…)` / `B.any_(…)`" |
| `&` binds tighter than `==`, so `a.f.x == 1 & b.f.y == 2` parses as `a.f.x == (1 & b.f.y) == 2` | Chained comparisons also go through `__bool__` and raise. Docs and `help props` use `B.all_(p, q)` / `B.any_(p, q)` in every example, not `&` / `\|` |
| `x in (…)` can't return an expression | `.in_(…)` and `.between(lo, hi)`, the pandas/SQLAlchemy spelling |
| Glob and regex matches | `.glob('top.*')`, `.re('timeout\|err')` |
| Time literals | `200 * B.ns`, or the string `"200ns"` where a *duration* is expected (a literal, not an expression) |

The Python DSL (§4.1) parses method bodies with `ast` and never executes
them, so plain `and`, `in range(…)` and chained comparisons work there.
It lowers to the same expression IR.

## 4. Front-ends

### 4.1 Python DSL (`trlog.props.dsl`)

This is meant to be the friendliest way to write properties. It should
follow the **zuspec-dataclasses** idioms, so the two read alike and code
moves between them:
- a decorated class;
- typed fields;
- decorated methods whose bodies are **parsed from source with `ast`**
  (as `zuspec.dataclasses.api.visitor` does), not executed.

Parsing the source lets bodies use plain Python (`and`, `in range(...)`,
`if`/`else`) instead of overloaded operators, and gives exact line
numbers for `loc`.

```python
import trlog.props.dsl as tp

@tp.monitor
class WrThenRd:
    w: tp.Handle["mem_wr"]
    r: tp.Handle["mem_rd"]

    @tp.activity
    def body(self):
        with tp.concat():                       # "the next read after w"
            self.w
            self.r.with_(self.r.addr == self.w.addr)

    @tp.constraint
    def same_data(self):                        # standalone: checked after matching
        self.r.data == self.w.data

    @tp.covergroup
    def cov(self):                              # sampled at first match
        len_cp  = tp.coverpoint(self.w.len, bins={"one": 1, "short": range(2, 5), "long": range(5, 17)})
        core_cp = tp.coverpoint(self.r.core)    # enum: auto bins
        lat_cp  = tp.coverpoint(self.r.end - self.w.start,
                                buckets=[0, 5 * tp.ns, 20 * tp.ns, 100 * tp.ns])
        tp.cross(len_cp, core_cp)

@tp.assert_(req="REQ-MEM-004", trigger="w", pending_at_end="fail")
class RdSeesWr(WrThenRd):                       # inheritance resolved here, flattened in IR
    pass

@tp.assert_(req="REQ-AXI-012", trigger="a")
class RdRespLatency:
    a: tp.Handle["axi_rd"]
    b: tp.Handle["axi_rd_resp"]

    @tp.activity
    def body(self):
        self.a
        with tp.eventually(within=200 * tp.ns):
            self.b.with_(self.b.id == self.a.id)

pset = tp.collect(RdSeesWr, RdRespLatency)      # -> PropertySet (L0)
```

The rules for statements inside `@tp.activity`:
- A bare statement sequence means `sequence`.
- `with tp.concat()`, `tp.select()`, `tp.schedule()`, `tp.overlap()`,
  `tp.eventually(within=...)`, `tp.during(outer)` and
  `tp.repeat(min, max)` open the corresponding nodes.
- Handle types can be a type-name string, or a trlog record type class,
  where one is defined in Python. The class gives the DSL field names for
  early checking.

### 4.2 Text and YAML (`trlog.props.text`)

The text form is **the builder API spelled as calls, with filter-language
predicates** (decision 7). It is not a separate language:
- The combinator names, argument order and keyword names are the builder's
  (§3), and map one-to-one to IR nodes.
- Predicates and values are the existing filter language (`_expr.py`), with
  handle-qualified names (`b.id`) as `match --on` already uses them.
- It replaces the PSS-vocabulary block/compact text that was planned here.
  PSS users come in through Zuspec (§4.3), and since decision 6 trlog's
  defaults are not PSS's anyway.

```
trlog check TRACE -e 'assert(concat(a:axi_rd, eventually(next(b:axi_rd_resp with b.id == a.id), within=200ns)), id=rd_resp_latency)'
```

The equivalent builder call:

```python
a, b = B.handle("a", "axi_rd"), B.handle("b", "axi_rd_resp")
B.assert_(B.concat(a, B.eventually(B.next(b.with_(b.f.id == a.f.id)), within="200ns")), id="rd_resp_latency")
```

**The whole grammar on top of the filter language:**

```
prop    := scen
scen    := NAME '(' [arg (',' arg)*] ')'       # a combinator from the name table
         | handle ['with' expr]               # Traverse, bind: any
         | IDENT                              # Traverse of an already declared handle
handle  := IDENT ':' TYPE                     # declares the handle at first use
arg     := scen | expr | IDENT '=' value      # positional kind comes from the name table
value   := literal | IDENT                    # 200ns, 8, 'fail', true, a
```

- **No scenario-level operators.** There is no infix `;`, `->` or `within`,
  so there is no precedence to learn. The only new tokens are `:`, `=` and
  the `with` keyword.
- **Combinators are table rows, not grammar.** Each row gives the name, the
  IR node, the kind (scenario or expression) of each positional argument,
  and the allowed keywords:
  `assert`, `cover`, `concat`, `eventually`, `next`, `select`, `schedule`,
  `overlap`, `during`, `repeat`, `absent`, `constrain`, `scope`,
  `never`, `outstanding`, `count_in`, `handle` (long form:
  `handle(a, axi_rd, streams='top.*.mon', subtypes=true)`).
  Adding an IR node adds a row.
- **Defaults keep one-liners short.** An assert's `trigger` defaults to the
  first traversed handle (the rule in §3 makes that the only legal choice
  anyway), and `id` defaults to a hash of the text.
- **Errors:** the parser reuses the filter language's lexer, literals
  (`200ns`, `0x…`, `32'h…`) and structured errors. Unknown combinators and
  keywords get `did_you_mean` from the name table, and `loc` is a column.
- **Size:** roughly 150–250 lines of parser over the existing lexer, plus
  the table. All validation stays in the builder, which this front-end
  calls.

YAML property files carry ids, requirement tags, options and covergroups
around call-form scenarios (sketch in `txn_properties.md` §6.1; its
scenario strings predate this form). Covergroups are YAML structure with
filter-language expressions. They are not written in the call form.

### 4.3 PSS through Zuspec (outside trlog)

- **The dependency points one way:** Zuspec depends on trlog's IR (or just
  on its JSON schema). trlog never imports Zuspec.
- The adapter lowers Zuspec IR `monitor`/`cover` to trlog IR. It resolves
  PSS inheritance, extension and overrides, and maps PSS covergroups in
  monitors directly to IR `Covergroup`s.
- **PSSC/Zuspec owns the recording of action executions as transactions**
  (decision 5): action type → txn type, component instance path → stream,
  compound action → parent txn. Because the same side owns both the
  recording and the adapter, the names in the IR and in the trace come from
  one place. trlog only needs to provide the recording API it already has,
  and to document what a front-end may assume about stream and type names.

### 4.4 Other front-ends

- SV / C++ / Rust tools, or a requirements-management export, emit the JSON
  IR. No C builder API is planned at first. A C++ front-end can write JSON.
- **LLM agents** use the call form (`-e`) for one-offs, and JSON or the
  builder when they generate many properties. Structured bind errors
  carrying `loc` keep a wrong guess to one retry. Syntax is the easy half,
  though. The usual agent failure is a result that means less than it
  seems, so `check`/`cover` report:
  - **Vacuity and selectivity:** per handle, how many txns its predicate
    selected, and per property how many attempts started. "Holds, 0
    attempts" is a warning (and a distinct exit code), not a pass. A
    misspelled enum value or the wrong stream glob produces exactly this.
  - **Violation explanations:** for each failing attempt, the deepest
    partial realization: the step that found no candidate, the nearest
    candidate (`t:…` ref), and the conjunct of its predicate that was
    false.
  - **`--plan`:** binds and plans without the full scan. It shows per-handle
    counts and the `any`/`next` choice of each traversal (including the
    planner's own commits, decision 6), so an agent can check what its
    property selects before running it.
  - **`help props`:** a one-page, example-led cheat sheet like `help agent`.
    It shows the call form, the Python builder equivalents and an
    `any`-vs-`next` example, which is the semantic trap most likely to
    catch an agent.

## 5. Binding and planning

- **Bind** (`trlog.props.bind`): `PropertySet × TraceSchema → BoundSet`.
  `TraceSchema` comes from a `.trl` reader *or* from a live writer's type
  registry, and that is what lets offline and online share everything below
  this point. Binding does the following:
  - resolves type, field, stream and enum names, with handle stream globs
    selecting stream sets;
  - converts time literals to ticks using the trace timescale;
  - type-checks expressions;
  - flags properties whose types are absent from the trace. These are
    reported as `unbound` (not vacuous), since a missing type is usually a
    naming or convention mismatch.
- **Plan** (`trlog.props.plan`): it does three things.
  - It lifts each handle's type, stream set and pushdown-able inline
    predicate into scan selections (reusing the query planner, §7.2 of
    `query_cli_design.md`).
  - It compiles predicates with the shared expression compiler.
  - It builds per-property attempt machines: commit for `bind: next`,
    fan-out for `bind: any`, and deadlines for `within`. Where it can prove
    that committing to the first candidate gives the same verdict, it commits
    a `bind: any` traversal too (decision 6), so fan-out is only paid where
    the property needs it.
- **Lowered plan for C:** a compact binary form of the plan (predicate
  bytecode plus attempt-machine tables), like the derived-signal bytecode
  in `_derived.py`. The C accelerator consumes only this, never the IR, so
  the C side stays small and the IR can evolve without changing C. The
  pure-Python engine runs the same plan and is the fallback.

## 6. Back-ends, results and storage

- **Back-end contract:** each back-end declares the subset it supports. For
  example, online needs bounded future obligations. Each back-end must agree
  with the **reference evaluator** on the shared golden suite (the PSS
  Clause 16 examples plus extension tests). The reference evaluator
  interprets bound IR directly from the Annex F definitions. It is the
  semantics, not a fast path.
- **Results model:** shared by all back-ends.
  - Per property: attempts, holds, violated, pending, vacuous or unbound,
    worst slack, and witness and violation refs.
  - Per covergroup: hit count per bin and cross bin, the coverage
    percentage, holes (bins below `at_least`), and illegal-bin hits as
    violations. Each bin keeps its first N witness refs, so an agent can ask
    "which txn hit this bin?" and go straight to `show`.
  - Per requirement: a roll-up.
  - Each result entry carries the property's `loc`, so a violation points to
    the DSL or YAML line that defined it.
- **In the trace:** an optional `PROP` ext block holds the serialized
  `PropertySet` the run was checked against, and a `COVG` block holds the
  results (status counts and bin hit counts).
- **Merging across runs:** status counts and bin hits add up, and `violated`
  is sticky. That gives regression-level coverage per requirement, with no
  external coverage database. UCIS export is a possible later adapter. A trace is then self-describing ("what was checked, what held"),
  and `trlog check TRACE` with no property file re-runs the embedded set.

## 7. Package layout (proposal)

```
python/src/trlog/props/
  ir.py        L0 node classes, to_json/from_json, JSON schema
  build.py     L1 builder + structural validation
  dsl.py       Python DSL front-end (ast-based, zuspec-dataclasses idioms)
  text.py      call-form text (`-e`) and YAML front-end (decision 7)
  bind.py      PropertySet x TraceSchema -> BoundSet
  plan.py      execution plans; lowering to the C plan format
  refeval.py   Annex F reference evaluator (oracle)
  engine.py    offline engine over .trl
  online.py    writer tap
  results.py   results model, report, COVG/props.<id> write-back
python/src/trlog/query/_expr.py   refactored: shared expression IR + parser
c/src/pscan.c (later)             optional accelerator for lowered plans
```

## 8. Phasing (replaces `txn_properties.md` §7)

1. **IR + builder + JSON**:
   - `ir.py` and `build.py`, with structural validation, a JSON round trip,
     the published JSON schema (`0.1`) and golden JSON fixtures.
   - Refactor the expression IR out of `_expr.py` (unit-carrying time
     literals) with no change to query behavior, checked by the existing
     query tests.
2. **Bind + reference evaluator**, using the PSS Clause 16 examples as
   golden tests built with the builder API. This includes covergroup
   sampling at first match.
3. **Python DSL** (trlog's own, decision 1), and the **offline engine**:
   - The engine covers the assert side, `within`, `never` and `outstanding`,
     checked against the reference evaluator.
   - **Coverage computation**: bins, crosses, illegal bins as violations,
     and holes.
4. **Call-form text/YAML front-end + `trlog check/cover`** (decision 7),
   the results model (including vacuity, selectivity and violation
   explanations, §4.4), `--plan`, `help props`, `PROP`/`COVG` storage, and
   cross-run merge.
5. **Remaining extensions** and the **online tap**, then the lowered plan
   and the C accelerator.
   In-simulation checking from SV (C engine inside the L0 session, SV
   builder, poll-based reporting, violations written into the trace) is
   designed in `research/txn_props_sv.md`, which splits this step up further.
6. **Zuspec adapter and action recording**, both in PSSC/Zuspec
   (decision 5). trlog's part is to keep the JSON IR documented and stable
   enough to target.

## 9. Decisions and open questions

Decisions (review of 2026-11-23):

1. **trlog has its own Python DSL**, following zuspec-dataclasses idioms
   but with no Zuspec dependency (§4.1).
2. **The JSON IR goes public early** and will evolve. That calls for the
   versioning and compatibility rules in §2.1.
3. **Bins and crosses are part of the IR** (§2.2, Coverage). trlog
   computes coverage and holes itself, and pyvsc is interop only.
4. **Front-ends flatten inheritance, extension and overrides.** To keep
   provenance, each node's `origin` records the monitor or class that
   defined it (`origin: {sugar: "sequence", defined_in: "WrThenRd"}`), and
   `loc` points to that definition. Reports show "from base monitor
   `WrThenRd`" from this.
5. **PSSC/Zuspec records action executions as transactions**, and owns the
   adapter from PSS monitors and covers to the IR (§4.3).
6. **Matching: a scenario holds when *some* realization satisfies it;
   first-occurrence is an explicit operator** (review of 2026-11-23). trlog is
   not a PSS tool, so PSS's binding rule is not the default.
   - **Verdict:** existential over realizations, within the time bounds, as
     for SVA sequences and as regex match/no-match. A traversal's default is
     `bind: any`.
   - **Witness:** when several realizations exist, the earliest by
     `(match end, start, id)` is the witness. Coverage samples it once per
     attempt, so sampling stays deterministic (PSS's "first match").
   - **Commit:** `bind: next` (DSL `next(h with p)`) binds the first
     qualifying txn and never reconsiders it. Followed by a `Constrain`, it
     says "the *next* X after Y must satisfy P": ordering, no reordering, "the
     next response is for this request". Under `bind: any` that would weaken
     to "*some* later X satisfies P".
   - **Why not first-occurrence everywhere:** in `a; b; c with c.tag == b.tag`
     over `a b1 b2 c(tag=b2.tag)`, committing `b := b1` fails the attempt
     although a realization exists. On an assert that is a false violation.
     It is also why PSS has to forbid forward references and sibling
     references in inline predicates.
   - **Cost:** the planner commits `bind: any` traversals itself when a
     liveness check shows that the handle's values are not referenced after
     it binds, or only through an equality key that the next step is indexed
     on. Most real properties pass, and run as a per-key automaton. The rest
     fan out, merged by (state, live bound values), and `within` keeps that
     finite.
   - **PSS lowering:** the Zuspec adapter maps PSS `h with p` to
     `bind: next`. PSS `concat` vs `sequence` (`concat {s1; eventually s2}`)
     then keeps its meaning exactly.
   - **Later, if needed:** strict contiguity ("no other txn of this type in
     between") as another `bind` value. CEP engines expose the same three
     choices: Flink's `next()` / `followedBy()` / `followedByAny()`.
7. **No new language: the text form is the builder spelled as calls**
   (review of 2026-11-23).
   - **Python:** the builder takes predicates only as Python expressions,
     never strings. Overloaded operators build the shared expression IR, and
     `__bool__` raises, so `and`/`or`/`not` and chained comparisons fail
     loudly (§3).
   - **Text (`-e`, YAML scenarios):** `NAME(args, kw=value)` calls whose names,
     argument order and keywords are the builder's, plus `a:TYPE` handle
     declarations and `with` predicates in the existing filter language
     (§4.2). There are no scenario operators and no precedence. It is
     roughly 200 lines of parser over the existing lexer.
   - **Why not Python everywhere:** agents work mostly through the CLI (the
     query CLI's primary audience), and `check`/an MCP server must not
     execute agent-written code. **Why not JSON on the command line:** it
     is hard to quote in a shell, and agents unbalance nested JSON more often
     than nested calls. JSON stays the machine form.
   - **Replaces** the PSS-vocabulary block/compact text (`txn_properties.md`
     §6.1, option B).

Still open, with recommendations (not yet decided):

1. **`sample_on` default for asserts.**
   **Recommended: `holds` by default, with `all` available plus an
   implicit `verdict` coverpoint.**
   - Coverage should mean "this requirement was exercised *and satisfied*".
     If violating attempts sampled by default, a regression that merges
     runs would count failing behavior as coverage.
   - Violations are already reported in full, with witness refs.
   - With `sample_on: all`, the builtin `verdict` (`holds`/`violated`) can be
     used as a coverpoint or crossed, for "did we ever see the error case?".
   - Pending and vacuous attempts never sample, because they have no first
     match, so an untriggered assert shows up as holes.
2. **Cross selection.**
   **Recommended: a predicate over *bin names*, built from the shared
   expression IR.**
   - Inside a cross `select`, each coverpoint name evaluates to the name of
     the bin it hit, and `value(cp)` gives the raw sampled value. For
     example: `select: "len_cp == 'long' && core_cp in ('c0','c1')"`.
   - That covers what `binsof(cp.bin)`, `binsof(cp) intersect {…}` and `&&`
     / `||` combinations are used for in practice, without adding a second
     selection language.
   - The default is the full product of the coverpoints' bins, minus ignore
     and illegal bins. A `select` is only needed for named, ignore or illegal
     cross bins.
   - SV `with` filters and set algebra are deferred. Because the IR is public
     and versioned, adding them later is a minor (additive) change.
3. **Per-instance coverage.**
   **Recommended: an `instance_by` key expression, defaulting to the
   stream of the trigger handle (asserts) or the first traversed handle
   (covers). Off unless `per_instance` is set.**
   - In a trace, the stream path is the component instance. With PSSC/Zuspec
     recording component path → stream (decision 5), this matches PSS's
     per-component meaning. The PSS adapter maps PSS per-instance coverage to
     `instance_by: <handle>.stream`.
   - PSS's named-handle path describes the static model tree, which a trace
     doesn't contain, so it doesn't carry over.
   - Making it an expression also covers keys like `a.port` or `a.master_id`
     for free.
   - Reports show each instance and the merged total, with holes per
     instance. Cross-run merge keys on the instance value; stream paths are
     stable across runs, so this works.
