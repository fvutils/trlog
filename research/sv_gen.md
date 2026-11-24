# Generating trlog recorders and property modeling classes from SV classes

Status: **proposal; review decisions recorded in §5** (2026-11-23). Revised the same day to
build on **sv-weave** (github.com/sv-weave) instead of a standalone pyslang
tool. Serves both `sv_api_design.md` (recording) and
`research/txn_props_sv.md` §4 (properties).

## 1. Idea

The testbench already has transaction classes (`axi_rd_item`, often a
`uvm_sequence_item`). From each one we want two things:

- a **recorder**: type registration, plus a `record` path that makes one
  Tier-1 DPI call (`sv_api_design.md` §3.2);
- a **modeling class** (`axi_rd_item_ph`): a property handle whose field
  accessors return expression objects, used as
  `p.trav(b, b.id().eq(a.id()))` (`txn_props_sv.md` §4.2).

Both come from one analysis of the class, so they can't disagree about which
fields exist, what kind each one is, or its index.

## 2. Why sv-weave rather than a standalone tool

sv-weave ("factory weaving for SystemVerilog class libraries") already
provides the parts a standalone generator would have to build:

| Need | sv-weave provides |
|---|---|
| Parse and elaborate SV | pyslang/slang, with the generated code elaborated *with the design* before files are written |
| Class and field model | `SvClass` (`ancestors`, `all_fields(upto=…)`, parameterized specializations), and `SvField` (`type`, `rand`, `static`, `visibility`, `attrs`, `loc`). `SvType`: integral(width, signed), enum, string, real, class_handle, array(fixed\|dynamic\|queue\|assoc), struct |
| Per-field annotations | SV attributes on members, read through `attrs` (`(* trl_skip *)` etc.) |
| Per-class code in a subclass | a *woven* subclass `Woven_<cls>_<profile>`, returned transparently by the library factory. It reaches `protected` members, which removes the `` `include `` hook from the earlier draft. |
| Standalone generated classes | `bus.on_design(...)` + `DesignCtx.add_class(pkg, …)` for whole-design emitters. This is where the modeling classes go. |
| Library neutrality | **bindings** map an abstract *feature* to a library hook (UVM `do_copy`, plain `copy()`, …) |
| Turning a feature on per build | **profiles** (e.g. `release = [core]`, `debug = [core, debug.*]`), selectable at build time or at runtime (`+weave_profile=`) |
| Flow | an existing DV Flow Manager task, `svweave.Weave`, with incremental, manifest-hashed builds |

What sv-weave doesn't have yet: any recording feature. Its UVM binding maps
`copy`, `compare` and `print` only, and has no `do_record`. That is the gap
trlog fills.

## 3. Design: a trlog weaver plugin

trlog ships an sv-weave plugin, registered through the `sv_weave.weavers`
entry point. It is an optional extra, `trlog[weave]`, and the core of trlog
does not depend on sv-weave. The plugin has two handlers.

```
 SV sources ──sv-weave (pyslang, SvClass model)──┬─ per-class handler ─▶ woven subclass: record hook (Tier-1 fast path)
                                                 │                       + shared layout (indices, schema hash)
                                                 └─ on_design handler ─▶ standalone classes: <cls>_trl (layout),
                                                                          <cls>_ph (modeling), type JSON
```

### 3.1 Per-class handler: recording

- **Subscription:** `bus.on(extends_root() & has_attr("trl_record") | <config match>, self.weave)`.
  Which classes are recorded is chosen in `sv-weave.yaml`, e.g.
  `match: "extends(uvm_sequence_item)"`, or class by class with
  `(* trl_record *)`.
- **Feature:** a new `record` feature.
  - **UVM binding:** `record` → `do_record(uvm_recorder r)`. The woven
    `do_record` tries `$cast` to `trl_uvm_recorder`. On success it takes the
    **Tier-1 fast path**: it packs the fields into the bulk buffer and makes
    one DPI call. Otherwise it falls back to generic `r.record_field(...)`
    calls. Standard UVM recording (`begin_tr`/`end_tr`, `uvm_object::record`)
    then goes through trlog at Tier-1 cost, with no field macros. For now
    the trlog plugin carries this binding extension itself (decision 2), so
    `sv-weave-uvm` is unchanged.
  - **Plain binding:** `record` → `trl_record(trl_stream s, t0, t1)`, for
    non-UVM items.
- **Fields:** `w.cls.all_fields(upto=w.binding_root)`, skipping `static` and
  `(* trl_skip *)`. `local` members of ancestors are unreachable, so
  sv-weave's rule applies: warn and skip by default, fail under
  `strict: true`.
- **Kind mapping:** `SvType` → trlog field kind, using the table in
  `sv_api_design.md` §4.1, implemented once in the plugin:
  - integral ≤ 64 bits → int kinds;
  - wider integrals → `BITVEC`, or `LOGICVEC` with `(* trl_kind="logicvec" *)`;
  - enums → `ENUM`, with labels registered;
  - byte queues and dynamic arrays → `BYTES`;
  - structs and fixed arrays flatten to dotted and indexed names (§4.2 of
    that doc);
  - class handles flatten if the target is itself recorded, and are skipped
    with a warning otherwise;
  - associative arrays and other dynamic containers are skipped with a
    warning.
- **Profiles:** recording goes in an extension set, e.g. `trace`. A
  `release` profile without it has *no* recording code woven in, and a
  `trace` profile adds it. This gives zero cost when recording is off, on
  top of trlog's `en`-bit fast path.

### 3.2 Design-level handler: layout and modeling classes

`on_design` runs once per profile after the per-class handlers and emits
standalone classes with `DesignCtx.add_class`:

- **`<cls>_trl` (layout):** field indices as `localparam`s, `NF`, a
  `SCHEMA_HASH` over names, kinds, widths and enum labels, and a cached,
  sealed `type_()`. The woven recorder and the modeling class both
  reference it. The hash is written into the trace's type registry and
  checked at bind, so a property set built against a stale generation fails
  with "regenerate" instead of silently mis-binding.
- **`<cls>_ph` (modeling):** a `trl_ph` subclass with one accessor per field.
  - Enum fields return `trl_pe_enum#(E)`.
  - Flattened members get nested accessors (`pkt.hdr().len()`).
  - Accessors that collide with `trl_ph` methods or SV keywords get a
    trailing `_`, reported through sv-weave's diagnostics.
  - These classes are emitted for every recorded class regardless of
    profile, since property code has to compile in every profile.
- **Type JSON:** the trlog type-definition file for the same types, so
  Python DSL and YAML property authors get early field-name checks without
  needing a trace (`txn_property_arch.md` §4). It is emitted with
  sv-weave's `DesignCtx.add_artifact(..., kind="trlogTypes")` (§6).

### 3.3 Attributes

These follow sv-weave's convention of attributes on SV members:

| Attribute | Effect |
|---|---|
| `(* trl_record *)` on a class | record this class (alternative to a config match) |
| `(* trl_skip *)` | leave the member out |
| `(* trl_name = "araddr" *)` | recorded and accessor name |
| `(* trl_kind = "logicvec" *)` | keep 4-state |
| `(* trl_intern *)` | a string member passed as an interned id in the bulk array |
| `(* trl_start *)`, `(* trl_end *)` | members holding the txn's begin and end time |

For read-only VIP sources, use the `sv-weave.yaml` match predicates. A
separate side file isn't needed.

## 4. Phasing

Implementation status: `research/sv_gen_impl.md`.

1. **Recording:** the plugin's per-class handler plus the `<cls>_trl` layout
   class, with the plain binding first. Then the plugin's own UVM
   binding extension (`record` → `do_record`), with the `trl_uvm_recorder`
   fast path. This is
   useful for recording on its own, before any property work exists, and it
   also delivers the UVM adapter (L3) that `sv_api_design.md` Q8 deferred.
2. **Modeling classes**, once the SV typed builder (`trl_ph`, `trl_pe`)
   lands (`txn_props_sv.md` §7 step 4).
3. **Type JSON** for the Python and YAML front-ends.
4. **Tests:** golden woven output for representative classes (enums,
   nested, arrays, inherited, `local`/`protected`, attributes,
   parameterized). These are elaborated by sv-weave and simulated under
   Verilator/xezim, then the trace is read back in Python to check names,
   kinds and the hash.

## 5. Decisions and open questions

Decisions (review of 2026-11-23):

1. **The plugin lives in trlog**, as an optional extra (`trlog[weave]`,
   registered on the `sv_weave.weavers` entry point). trlog's core does not
   import sv-weave.
2. **No generic `record` feature in sv-weave-uvm yet.** The trlog plugin
   provides its own binding extension that maps `record` → `do_record` for
   UVM classes, plus `trl_record()` for plain classes. A generic feature can
   be upstreamed later, once a second recording weaver exists.
3. **Non-SV side artifacts:** sv-weave didn't support them, so
   `DesignCtx.add_artifact` was added to it (§6.2).
4. **Polymorphic handles: yes.** A property handle can match "this type and
   its subtypes". This needs:
   - the class hierarchy recorded in the trace's type registry, as a new
     length-prefixed tag that old readers skip. The plugin gets the hierarchy
     from sv-weave's `subclasses_of`;
   - `Handle.txn_type` in the IR plus a `subtypes: bool` flag (or an
     explicit type set). It resolves at bind to the set of concrete types
     present;
   - inline predicates may only use fields of the *declared* type. Fields
     inherited from the declared type are flattened first, so they share
     names and kinds in every subtype. Field indices may differ per subtype,
     so the plan keeps a per-subtype index map.
   - The modeling class for a base type then works as a polymorphic handle:
     `axi_item_ph::create(p, "a", .subtypes(1))`.
5. **Don't pin sv-weave yet.** It is being co-developed, so track its
   development head, and change the trlog plugin and sv-weave together where
   needed.

## 6. Investigation: non-SV side artifacts in sv-weave

First read in `/packets/sv-weave/sv-weave-setup` (0.1.0). Re-checked and
implemented against `packages/sv-weave` (0.3.0 plus unreleased changes),
where the findings below still held.

### 6.1 Findings

- **No supported way exists today.** Neither `DesignCtx`
  (`weave/design.py:35-128`) nor `WeaveCtx` (`weave/ctx.py:131-264`) can
  emit a file. Their `add_item`/`add_class` only add SV text to
  `<pkg>_woven`.
- **The only file hook is `Binding.init_files()`** (`plugin.py:112-114`,
  `run.py:300-301`). It writes any text into the output directory, but it
  also puts the file into `compile_order`. A JSON file there would land in
  `sv_weave.f` and in the `systemVerilogSource` FileSet, and be handed to
  the simulator. It is also binding-only, not available to weavers.
- **The `svweave.Weave` dfm task** (`dfm/tasks.py:414-455`) outputs exactly
  two FileSets: `systemVerilogSource` (`compile_order`) and `jsonManifest`
  (`weave-manifest.json`).
- **Profiles:** `on_design` handlers run once per profile (`bus.py:71-94`),
  so an artifact added there needs content-checked deduplication, as
  `add_item` already does (`design.py:97`). The types are the same in every
  profile, so one type JSON per run is correct.
- **Modeling classes need no change.** `add_class` puts them in the
  `<pkg>_woven` package, and identical text from different profiles is
  merged. Generated code imports that package (§3.2), not a separate
  `axi_trl_pkg`.

### 6.2 Implemented in `packages/sv-weave` (2026-11-23, uncommitted)

This follows decision 3 and is recorded under *Unreleased* in sv-weave's
CHANGELOG, matching how `add_package_include` was added, with no API version
bump yet.

- **`DesignCtx.add_artifact(name, text, kind)`** (`weave/design.py`):
  - `name` is a plain file name.
  - `kind` is a dv-flow filetype name. The SV kinds and `jsonManifest` are
    refused: SV belongs in `add_file`, and `svweave.Init` reads every
    `jsonManifest`.
  - Adding the same name with the same text and kind is a no-op, which
    covers the per-profile `on_design` calls. A different text or kind is
    an error.
- **Run** (`run.py`):
  - Artifacts are collected through `bus.dispatch` into
    `RunResult.artifacts`, sorted by name.
  - A name that clashes with any generated file, including `add_file`
    files, is an error.
  - They are written with the other outputs and included in `output_hash`,
    but are **not** in `compile_order`/`sv_weave.f` and are not verified.
  - Stale ones are removed through `files.generated`.
- **Manifest and report** (`report.py`): `files.artifacts` holds
  `[{file, kind, producer}]`, and `weave-report.md` lists them.
- **dfm** (`dfm/tasks.py`, `flow.dv`): `svweave.Weave`'s output is built by
  a new `weave_filesets()` helper. It emits one `FileSet(filetype=kind)` per
  kind (kinds and files sorted) between the package-include FileSet and the
  manifest.
- **Docs:** `docs/authoring/weavers.rst`, `docs/user/dvflow.rst`, CHANGELOG.
- **Tests:** 17 new unit tests in `tests/unit/test_design.py`. They cover the
  file being written but not compiled, the manifest and report entries,
  dedup across profiles, stale removal, the hash, every error case, and the
  FileSet grouping. sv-weave unit tests: 280 passed. Sim tests (dfm +
  Verilator): 29 passed, 3 skipped.

### 6.3 Consequence for trlog

The trlog plugin's `on_design` handler emits the type JSON directly with
`d.add_artifact("<pkg>_trl_types.json", ..., "trlogTypes")`. A downstream
dfm task (Python DSL checks, `trlog props compile`) depends on the
`trlogTypes` FileSet. No `trlog gen types` workaround on sv-weave's internal
model API is needed.
