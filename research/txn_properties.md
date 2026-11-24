# Temporal properties over transactions: research notes

Status: **research / proposal, not reviewed** (2026-11-23).

## 1. The question

We want to define *properties* over the transactions in a `.trl` trace that:

- collect **coverage**: did a pattern happen at least once (existential), how
  often, and with which values;
- are **temporal**: they relate transactions over time (order, latency,
  overlap, rate), not just one transaction's fields;
- track **requirements**: each property is tied to a requirement ID, and the
  run reports covered / held / violated per requirement;
- can **assert**: report (and optionally stop on) a violation, with
  references to the transactions that show it.

Cover and assert are the two quantifiers of the same property:
`cover P` asks whether some match of P exists, and `assert P` asks whether
every attempt of P holds. Coverage counts witnesses. An assertion produces
counterexamples. One language and one engine should do both, as SVA and PSL
do for signals.

## 2. What is different about transactions

Signal-level languages (SVA, PSL) assume a clock and boolean samples at each
tick. Transactions differ in four ways, and the design has to handle each one.

| Property of the data | Consequence |
|---|---|
| **Intervals, not points.** A txn has `start` and `end`, and may be open. | Ordering is ambiguous ("a before b" by start? end?). Need Allen-style interval relations (before, meets, overlaps, during) and explicit anchors (`a.end`, `b.start`). |
| **No clock.** Events arrive irregularly on many streams. | Steps are counted in *transactions* (the next txn on a stream) or measured in *time* (`within 100ns`), never in cycles. This is metric temporal logic (MTL) over a timed event sequence. |
| **Data-carrying.** The interesting properties correlate by value (tag, id, addr). | We need *parametric* properties, where variables bind to values and one monitor instance runs per binding (trace slicing, as in JavaMOP and QEA). Plain propositional LTL/SVA can't do this without local-variable tricks. |
| **Structure.** Txns have parent/child and cause links. | Scoping a property to the children of each parent (each beat within its burst, each item within its sequence) is natural here and has no SVA equivalent. |

There's also **finite traces**: a trace ends, and some obligations are still
open (a request with no response yet). Verdicts need three values, as in
LTL3/LTLf: *holds*, *violated*, *pending at end*. Whether pending counts as a
failure should be set per property. For example, a hung request at end of
sim is usually a bug.

## 3. Prior art, and what to take from each

**Hardware assertion languages**
- *SVA* (IEEE 1800 ch.16): sequences (`##[m:n]`, repetition `[*]`,
  `intersect`, `throughout`), properties (`|->`, `|=>`, `until`,
  `s_eventually`), and the `assert` / `cover` / `assume` directives. Local
  variables give limited data binding. Take: the sequence/property split, the
  directive model and vacuity reporting. Leave: the cycle clock.
- *PSL* (IEEE 1850): SEREs plus the FL layer (LTL-style operators), with a
  defined *simple subset* that can be monitored in simulation. Take: the idea
  of declaring a monitorable fragment.
- *e / Specman temporal expressions*: events, `{a; b}` sequences, `=>`,
  `eventually`, `@sampling_event`, plus `expect` (assert) and `cover` on
  events. This is the closest existing match, since e was always
  transaction-centric and coverage-driven. Take: `expect` and `cover` share
  one temporal expression, and a `cover` on an event can carry item bins.
- *Transaction-level assertions* research (Ecker et al., TLM assertions for
  SystemC, about 2006-2010; Bombieri et al., PSL on TLM). These map
  transaction events (start/end) onto the PSL/SVA layer. The lesson: they
  worked, but forcing everything through a pseudo-clock was awkward.

**Runtime verification (RV)**
- *MTL / timed LTL*: time-bounded operators (`F[0,100ns] resp`). These map
  directly onto `within`.
- *Past-time LTL* (Havelund & Roşu): it can be monitored in O(1) state per
  formula with no look-ahead, which makes it ideal for *online* checking
  ("whenever a resp arrives, some matching req happened before").
- *Parametric / first-order monitoring*: JavaMOP (trace slicing), QEA
  (quantified event automata), MonPoly and DejaVu (first-order past-time
  MTL). Take: `forall tag:` quantification evaluated by slicing, with one
  small automaton per binding value, which is what makes value correlation
  cheap.
- *Stream RV* (LOLA, RTLola, TeSSLa, Copilot): properties as derived streams
  computed from input streams. This fits trlog, which already has derived
  signals (`_derived.py`). A property's verdict can itself be a stream.
- *STL robustness*: a quantitative margin, not just pass/fail. For us this
  is the *latency slack* on a `within` bound, which is useful for coverage
  bins ("how close did we get to the limit?").

**Complex event processing / query languages**
- *SQL `MATCH_RECOGNIZE`* (SQL:2016), *SASE*, *Esper EPL*, *Flink CEP*:
  regex patterns over rows partitioned by key, with `WITHIN` windows, named
  pattern variables and measures computed over the match. Take: the surface
  syntax ideas (`PARTITION BY` = our `on`, named variables, `MEASURES` = the
  values a cover records). These are also the pattern languages LLM agents
  already know best.

**Coverage side**
- SV covergroups (bins, cross, `with`) and pyvsc covergroups. A temporal
  property should be able to *feed* a covergroup: each match supplies the
  values sampled into bins (e.g. `a.len` × latency bucket). That is "temporal
  coverage" in the e sense, and it also connects to pyvsc and Zuspec.
- *PSS 3.1 behavioral coverage* is the most directly relevant prior art. It
  gets its own section next.

## 4. PSS 3.1 behavioral coverage (Clause 16, Annex F)

References are to `PSS_3.1.md` line numbers.

### 4.1 Why it fits so well

PSS observes **action executions**. Each one is a half-open interval
`[start, end)` with a type and attribute values, and attributes are sampled
at the end (17496-17501, 17812). That is a trlog transaction: the action type
is the txn type, the attributes are the fields, and the component instance
maps to the stream. PSS already solved "temporal patterns over typed,
data-carrying intervals with no clock", which is our problem, and it has a
formal semantics (Annex F) we can implement directly.

### 4.2 The model

- **`monitor` / `cover`:** a `monitor` is a named, reusable scenario type: the
  coverage counterpart of an action. A `cover` statement instantiates one
  (`cover m;`) or inlines a body (`cover { ... }`) (17358-17400).
- **Realization:** an injective map from action *handles* to observed
  executions. Its start point is the minimum start of the bound executions
  and its match point is the maximum end (33412-33467).
- **Checkpoint:** the time a sub-scenario is matched from. **Attempt:** a
  scenario plus a checkpoint. Top-level attempts start at every action start
  time. An attempt succeeds if some realization starts exactly at its
  checkpoint (17488-17548).
- **Handle binding is "first qualifying occurrence":** `h with p` binds the
  *first* execution of h's type, at or after the checkpoint, that satisfies
  `p`. Executions of other types in between are ignored (17816-17824). This is
  like SVA's goto `[->1]`.
- **Covered:** at least one successful attempt. This is a pure existential
  boolean (33469-33477).

### 4.3 Operators

| PSS | Semantics | Note for us |
|---|---|---|
| `h with p` | first qualifying execution at or after the checkpoint | the basic binding |
| `concat { s1; s2 }` | s2 matched from s1's match point, again first-occurrence: "the next one" | other types may appear in between |
| `eventually s` | s matched from *any* later checkpoint | unbounded |
| `sequence { s1; s2 }` (`{...}`) | `≡ concat { s1; eventually s2 }`: any later s2 | gaps allowed |
| `select { s1; s2 }` | union of the alternatives | `select { a; {} }` = optional |
| `schedule { s1; s2 }` | each member eventually, any order, sharing no executions | |
| `overlap { s1; s2 }` | schedule plus a common instant: `max(begin) < min(end)` | strict; touching intervals don't overlap |
| `m with p` | traverse a sub-monitor, constrain its realization | composition |
| `s constraint { p }` | filter realizations *after* matching | see below |

**Inline vs. standalone constraints** is the most useful idea to take
(18989-19049, Ex. 233):
- An **inline** constraint (`r with core == 1`) filters *before* the
  first-occurrence choice, so it finds the next read with core 1.
- A **standalone** constraint (`constraint r.core == 1`) is checked *after*
  matching. Under `concat` it says "the *first* read after w must have
  core 1", and the match fails if it doesn't.

That second form is a property shape ("the next X after Y must satisfy P")
that most pattern languages can only express with explicit absence
operators.

**Data correlation** works by referring to already-bound handles in later
inline constraints (`r with addr == w.addr`). Annex F makes it a syntax
error to refer forward to unbound handles (33488-33495). Siblings in
`schedule` / `overlap` / `select` can only be correlated with a standalone
constraint.

**Covergroups in monitors** are sampled once per successful top-level
attempt, at its *first match*, using that realization's handle bindings
(19060-19066, Table 26). So the number of samples is well defined: one per
attempt, not one per realization. Per-instance coverage follows a path of
named handles (19205-19215). Inheritance (`monitor D : B`, `super;`),
extension and abstract monitors allow reusable libraries of scenarios
(19272-19316).

### 4.4 What PSS does not have

The subagent read the full clause and annex and confirmed that PSS 3.1
behavioral coverage has **none** of the following:
- **No assert side.** It is purely existential. A "failing attempt" is
  internal vocabulary and is never reported. It has no implication between
  scenarios, no violations and no disable/reset.
- **No time bounds.** `eventually` and `sequence` gaps are unbounded, and
  there is no `within` or `##[m:n]`.
- **No negation or absence.** The only absence-like effect is the
  concat-plus-standalone-constraint trick above.
- **No containment or structure.** `overlap` is intersection, not "during".
  There is no parent/child or cause relation.
- **No repetition** in monitor activities. You can't write `repeat`,
  `foreach` or `[n:m]`; only handle arrays.
- **No finite-trace story.** An action without an end time can't take part
  (33547-33549), and pending attempts at the end of the trace are not
  mentioned.
- **Tie non-determinism.** When several first-match realizations tie, the
  implementation picks one, so sampled values can differ between tools
  (19142-19149).

### 4.5 What this means for the design

1. **Adopt the PSS core as the cover semantics, as is.** That means
   realizations, checkpoints, attempts, first-qualifying-occurrence binding,
   `concat`/`sequence`/`eventually`/`select`/`schedule`/`overlap`, the
   inline/standalone constraint split, and one sample per attempt at first
   match. It is formally defined, it maps one-to-one onto txns, and its users
   (PSS, Zuspec) are ours. Make the tie-break deterministic: `(start, id)`.
   *(Later decision: binding is existential by default, and
   first-occurrence is the explicit `next(...)`; see §6.3.)*
2. **Add the assert side using PSS's own vocabulary.** An assertion is a
   trigger plus a scenario: *every* attempt whose checkpoint is set by a
   trigger txn must succeed. A failing attempt is a violation, reported with
   the trigger ref. This reuses the attempt machinery PSS already defines
   internally.
3. **Add the missing operators as extensions:** time bounds (`within W` on
   `eventually`/`sequence`/attempts), absence, `during` (containment),
   structural scoping (`children`, `linked`) and bounded repetition.
4. **Add finite-trace verdicts.** Attempts still open at the end of the trace,
   or bound to still-open txns, are `pending`, not silently unmatched.
5. **Interop opportunity:** a PSS `cover`/`monitor` (e.g. from
   `zuspec-fe-pss`) could compile *unchanged* into a trlog property and be
   measured post-run on a trace of the test's action executions. This needs
   a recording convention: action type → txn type, component path → stream,
   compound action → parent txn. That gives PSS behavioral-coverage closure
   without the PSS tool having to observe the run live.

## 5. What already exists in trlog

The query engine already covers a lot of the ground:

| Existing piece | Property it already is |
|---|---|
| Filter expression language (§5 of `query_cli_design.md`) | The *atom* layer: a predicate over one txn, with missing-value rules already defined. This is the `with` / `constraint` expression language. |
| `match --a --b --on --order --within --unmatched` | A one-off **response property**: `forall a: a -> b(on key) within W`, with `unmatched_a` as the counterexamples and the latency stats as the robustness margin. |
| `gaps --min W` | A **quiescence / liveness** check: never idle longer than W. |
| `active @T` | An **outstanding count** at one instant. |
| `links`, `tree`, `parent.FIELD` | Navigation for cause- and parent-scoped properties. |
| Ext blocks (`write_ext(b"COVG", ...)`) | A place to store coverage results inside the trace. |
| Derived signals | A place to store a verdict as a virtual stream. |

So properties are mostly a **sequence/temporal layer on top of the filter
language**, generalising `match`. They are not a new subsystem.

## 6. Design dimensions and recommendations

### 6.1 Surface syntax

| Option | For | Against |
|---|---|---|
| A. SVA-flavoured text (`a \|-> ##[..100ns] b`) | DV engineers know it | `##` means cycles. A half-SVA would mislead and get "fixed" into real SVA by people and LLMs. |
| B. Pattern text using the **PSS monitor vocabulary** (`sequence`, `concat`, `select`, `schedule`, `overlap`, `eventually`, `with`, `constraint`), plus extensions | Formal semantics come for free (Annex F). Familiar to PSS users, and one step from PSS source. Fits the filter language for atoms. | PSS's `{...}` block syntax is verbose for one-line CLI use. Needs a compact form. |
| C. Python builder API (`sequence(w, r.with_(...)).within(100*ns)`) | Composes with pyvsc/Zuspec. Testable. | Not usable from the CLI, or by agents without writing Python. |
| D. Explicit automata (QEA-style) | The most expressive | Hard to write and to read. |

> *Superseded (`txn_property_arch.md` decision 7):* the text form is now
> the builder API spelled as calls, with filter-language predicates
> (`concat(a:axi_rd, next(b:axi_rd_resp with b.id == a.id))`), rather than
> PSS-vocabulary blocks. The PSS operators still define the IR. The
> scenario strings in the YAML sketch below predate this.

**Recommendation: B, with C as a thin builder that produces the same AST.**
Keep D as an internal compilation target only. The PSS operators define the
AST and its semantics, and the extensions (§6.2) are additions to it. Allow
both a PSS-like block form (files) and a compact one-line form (`-e`, agents)
that parse to the same AST. Properties live in a YAML file next to the test,
each with an ID, a requirement tag, a kind (`assert`/`cover`) and the
expression.

Sketch (illustrative, not final). Handles are declared as `name: type`, and
an assert names its trigger handle.

```yaml
properties:
  - id: rd_resp_latency
    req: REQ-AXI-012
    assert:
      handles: {a: axi_rd, b: axi_rd_resp}
      trigger: a                       # one attempt per axi_rd
      scenario: "sequence { a; b with b.id == a.id } within 200ns"
    pending_at_end: fail               # a read with no response by end of sim is a violation

  - id: next_rd_after_wr_sees_data      # PSS standalone-constraint idiom, asserted
    req: REQ-MEM-004
    assert:
      handles: {w: mem_wr, r: mem_rd}
      trigger: w
      scenario: "concat { w; r with r.addr == w.addr }"
      constraint: "r.data == w.data"

  - id: no_write_to_ro
    req: REQ-REG-003
    assert: "never reg_wr with addr in 0x1000..0x10ff"

  - id: max_outstanding
    req: REQ-AXI-020
    assert: "outstanding(axi_rd) <= 8"

  - id: wr_rd_overlap_same_addr          # plain PSS cover
    req: REQ-COV-101
    cover:
      handles: {w: axi_wr, r: axi_rd}
      scenario: "overlap { w; r with r.addr == w.addr }"
    bins:                                # sampled once per attempt, at first match
      len: w.len
      skew: bucket(r.start - w.start, [0, 5ns, 20ns])

  - id: burst_beats
    req: REQ-AXI-007
    assert:
      handles: {p: axi_burst}
      trigger: p
      scenario: "count(children(p) with type == 'beat') == p.len + 1"
```

### 6.2 Operator set: PSS core plus extensions

**Core (PSS 3.1 semantics, Annex F):** `h with p`, `concat`, `sequence`,
`eventually`, `select`, `schedule`, `overlap`, monitor traversal with
constraints, standalone `constraint`, and named, reusable monitors with
inheritance.

**Extensions** (each appears in real protocol requirements, and each still
compiles to a small per-attempt automaton):

| Extension | Meaning | Typical requirement |
|---|---|---|
| `assert` with `trigger: h` | Every attempt checkpointed by a trigger txn must succeed. A failure is a violation. | latency, every request answered |
| `… within W` on `eventually` / `sequence` / a whole scenario | Time-bounded match. Measured from the trigger start, overridable with `of h.end`. | deadlines |
| `absent { s } within W`, `never SEL` | No realization of s / no matching txn | no retry after OKAY, illegal values |
| `during { outer; inner }` | Allen containment (PSS only has overlap) | a response inside its request window |
| `h[n:m]` repetition in `concat`/`sequence` | Bounded repetition (PSS needs handle arrays) | n beats, retries |
| `children(p)`, `linked(h, kind)` | Structural scoping over parent/child and cause links | beats in a burst, effect after cause |
| `count(SEL [in window W]) OP N` | Rate / bandwidth | ≤ N writes per µs |
| `outstanding(SEL) OP N` | Number of overlapping open intervals | max in-flight |

Deliberately left out at first: unbounded `until` nesting, arbitrary
negation of sequences, and future operators nested inside past ones. Those
are where monitoring cost and semantic surprises grow (the PSL simple-subset
lesson).

### 6.3 Semantics

- **Core:** Annex F's model, with one change to binding (decision 6 in
  `txn_property_arch.md` §9). Realizations are handle→txn maps. The match
  point is the maximum end, and a txn's attributes are its fields.
  - **Binding is existential by default:** a traversal may bind *any*
    qualifying txn at or after its checkpoint, and an attempt succeeds if
    some realization exists. PSS's first-qualifying-occurrence rule is the
    explicit `next(h with p)` (`bind: next`), which never reconsiders its
    choice; with a standalone constraint it expresses "the next X must
    satisfy P".
  - **The witness** is the earliest realization by `(match end, start, id)`,
    which closes the PSS tie non-determinism.

  §4.5 item 1 ("adopt the PSS core as is") is superseded on this point.
- **Attempts:**
  - A `cover` attempt starts at each txn start, as in PSS.
  - An `assert` attempt starts at each trigger txn.
  - Coverage counts **successful attempts**, and bins are sampled once per
    attempt at first match, as in PSS. Reports also give the number of
    distinct witnesses.
- **Verdicts:** per assert attempt, `holds | violated | pending`. An attempt
  is decided as soon as a `within` bound passes or the scenario succeeds. At
  the end of the trace, `pending` resolves through
  `pending_at_end: fail | pass | report`. Txns that are still open can bind,
  but only as *pending*. This extends PSS, which excludes them.
- **Vacuity:** every `assert` implicitly covers its trigger. If the trigger
  never occurred, the assertion is reported as *vacuous*, not as passed. This
  is the main reason to unify cover and assert, because it catches "the check
  passed because nothing happened".
- **Missing values:** inherited from the filter language. An absent field
  makes a comparison false.
- **Robustness:** `within` properties record the slack (`W - lat`) on each
  attempt. Coverage can bin it, and reports show the worst case.

### 6.4 Evaluation: offline and online, one engine

- **Reference evaluator first:** a direct, deliberately simple implementation
  of the Annex F `realizations(s, t, R)` function plus the extensions. It
  defines the semantics and serves as the test oracle.
- **Offline engine:** runs over a `.trl` file.
  - Handle types and inline constraints push down to the existing `-s/-T/-t`
    index selections, so only the relevant streams are scanned.
  - The engine merges those streams in `(start, id)` order (as `around`
    does) and advances live attempts incrementally.
  - `next` bindings need O(1) state per bound handle. `any` bindings fan
    out, but the planner commits them like `next` wherever that provably
    gives the same verdict, so most attempts still need O(1) state.
    `within` bounds keep the remaining fan-out finite.
  - This is a generalisation of `match`'s code.
- **Online (later):** the same monitors attached to the writer, through a
  tap on txn end in the Python writer and on the L0 recording session in C,
  so SV/DPI runs can fire assertions during simulation and stop on the first
  violation. Only properties whose future obligations are time-bounded
  qualify. Unbounded `eventually` can only report at end of run.
- Per the existing rule for the scan engine, any C implementation is an
  **accelerator with a pure-Python fallback**, and the Python engine defines
  the semantics.

### 6.5 Results, and where they live

- **Report:** per property and per requirement: attempts, holds, violations,
  pending, vacuous flag, cover count, worst slack, and the first N
  counterexample/witness refs (`t:1234`), so `show` / `around` can drill in
  directly. JSON envelope as in `query_cli_design.md` §3.3.
- **Back into the trace (optional):** write matches and violations as
  transactions on a virtual stream (`props.<id>`), spanning from the start
  point to the match point, with `cause` links to the bound txns. Then every
  existing verb (`find`, `count --by`, `hist`, waveform viewers) works on
  property results for free. A compact summary goes in a `COVG` ext block.
- **Coverage merge:** summaries from many runs merge per requirement (counts
  add, `violated` is sticky). That gives a regression-level requirements
  view.
- **Covergroups:** bins and crosses are part of the property IR, and are
  sampled per attempt at first match, as PSS samples monitor covergroups
  (decided; see `txn_property_arch.md` §2.2).

### 6.6 CLI surface (sketch)

```
trlog check TRACE props.yaml [--id ID] [--req REQ] [--json] [--write-back]
trlog cover TRACE props.yaml ...        # same engine, report sorted by coverage holes
trlog check TRACE -e 'assert a:axi_rd -> sequence { a; b:axi_rd_resp with b.id==a.id } within 200ns'
```

(The `-e` text above predates decision 7. In the call form it is
`assert(concat(a:axi_rd, eventually(next(b:axi_rd_resp with b.id == a.id), within=200ns)))`;
see `txn_property_arch.md` §4.2. §4.4 there adds `--plan`, vacuity and
selectivity reporting, and violation explanations.)

Exit code non-zero on any violation, which makes it usable as a regression
gate. `-e` gives agents one-shot ad-hoc properties without a file.

## 7. Suggested phasing

> Superseded by the layered plan in `research/txn_property_arch.md` §8.

1. **P0, reference evaluator + AST:**
   - Implement Annex F's realization function in Python, plus three-valued
     verdicts, attempts and vacuity, with a builder API (option C).
   - Use PSS Clause 16's examples (Ex. 214-238, and the F.4.3 worked
     example) as golden tests: hand-build tiny traces and check
     covered/not-covered and sampled values. That gives a ready-made
     conformance suite.
2. **P1, assert side and the most common extensions:**
   - `trigger`, `within`, `never`, `outstanding` and `count in window`.
   - Re-express `match` and `gaps` on the new engine as a consistency check.
3. **P2, text syntax + `trlog check/cover`:**
   - The PSS-vocabulary parser in block and compact forms, built on the
     existing filter expression parser.
   - YAML property files with requirement tags, and a JSON report.
4. **P3, remaining extensions and results:**
   - Repetition, `during`, `absent`, `children` and `linked`.
   - Coverage bins, write-back to a virtual stream plus a `COVG` block, and
     cross-run merge.
5. **P4, online:** a writer tap in Python, then the C L0 session tap for SV
   runs, with stop-on-first-violation.
6. **P5 (if wanted), PSS interop:**
   - A recording convention for PSS action executions.
   - Lowering from `zuspec-fe-pss` monitors and covers to the trlog
     property AST.

## 8. Open questions

1. **Where should properties be authored?** In a standalone YAML file (tool
   neutral, agent friendly), embedded in trace metadata at record time, or
   generated from Zuspec/PSS models? This decides whether the text syntax or
   the builder API is primary.
2. **PSS interop as a goal?** Should a PSS `monitor`/`cover` lower unchanged
   into a trlog property (P5)? If yes, the core must stay a strict superset
   of Annex F, and the action-recording convention needs defining early. If
   no, PSS is only inspiration, and the syntax can drift toward something
   more compact.
3. **Online vs. offline priority.** Is stopping the sim on a violation a
   real need soon, or is post-run checking enough for the first version?
4. **Coverage ownership.** Should the bins / cross model live here, or be
   pyvsc's job, with trlog only producing the sampled values per attempt?
   (Recommended: pyvsc.)
   **Decided 2026-11-23: bins and crosses are in the IR; trlog computes
   coverage itself, and pyvsc is interop only. See
   `txn_property_arch.md` §9.**
5. **Default anchor.** For `within W`, measure from the trigger's start
   (latency, matching `match`) or from its end (completion to next)?
6. **Cover counting.** Use PSS's "successful attempts" (one per start time)
   as the headline count, or the number of distinct realizations? They
   differ when matches overlap.
7. **Requirement IDs.** Free-form strings, or a link to an external
   requirements source (a ReqIF / doc ID scheme) that should round-trip into
   reports?
