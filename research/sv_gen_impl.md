# sv_gen implementation: plan and status

Design: `research/sv_gen.md`. This file tracks the implementation so a fresh
session can pick it up.

## Scope of this pass (phase 1a: plain recording)

The trlog sv-weave plugin, `trlog.weave`, weaver name `trlog-record`:

- **Selection:** classes marked `(* trl_record *)`, or matched by the
  weaver's `select` option (an sv-weave match expression, e.g.
  `"extends(uvm_sequence_item) & in_package(axi_*)"`). The design handler
  selects from the whole model, so a class needs no binding and no factory.
- **Per recorded class `C` in package `p`:** `p_woven` gets `C_trl`, holding:
  - a `localparam int F_<field>` for each field index, plus `NF`,
    `TYPE_NAME` and `SCHEMA_HASH`;
  - `type_()`, which builds and seals the `trl_type` once and caches it;
  - `static function longint unsigned record(trl_stream s, C obj, start, end_t, parent)`,
    which makes one `open_txn`, one `set_*` per field and one `close`. It
    reuses a static `trl_txn`.

  No woven class references `C_trl`. So this works in every profile, for
  classes no binding claims, and costs nothing until it is called.
- **Type JSON:** the artifact `trl_types.json`, kind `trlogTypes`, is one file
  per Weave, listing every recorded type.

## Phase 1b: UVM `do_record` and `trl_uvm_pkg`

- **`C_trl::set_fields(x, obj)`** sets every field of an open `trl_txn`;
  `record()` and the woven `do_record` both use it.
- **Per-class handler** (`bus.on(..., needs_binding=False)`), for woven classes
  that derive from `uvm_object`: it overrides `do_record(recorder)` and replaces
  the `super` call.
  - With a `trl_uvm_pkg::trl_uvm_recorder`, it calls
    `open_typed(C_trl::type_())` and then `C_trl::set_fields(x, this)`, giving
    one typed transaction with no field-by-name calls. It does *not* call
    `super`, so `uvm_transaction`'s `accept_time`/`initiator` stay out of the
    typed layout.
  - With any other recorder, or when `open_typed` returns null (a nested
    object, or already open), it calls `super.do_record`, then one generic
    `record_*` call per field. Enums go through `record_string(.name())`,
    signed values through `UVM_DEC`, and `time` through `record_time`.

  So a woven class records in any UVM recorder without field macros.
- **`sv/src/trl_uvm_pkg.sv`** (L3 adapter, `sv_api_design.md` Q8):
  - `trl_uvm_tr_database::install(d)` sets the coreservice default.
  - Streams nest under per-component scope streams (`uvm_test_top` → `env` →
    `mon` → `main`).
  - `trl_uvm_recorder` opens its trace txn late, once it knows the type:
    - at `open_typed`;
    - at close for generic records, with fields on the stream's ad-hoc type,
      named `obj.field` for nested objects.
  - Values recorded before a typed open (`begin_tr`'s `label`/`desc`) become
    txn metadata.
  - Parent/child: if the parent is already open, the child gets it as
    `parent`; otherwise the parent links to the child (`TRL_LINK_PARENT_CHILD`)
    when it opens. Related links are added at close if the target has an ID.
- **sv-weave changes** (packages/sv-weave):
  - design-handler classes get forward `typedef class` declarations ahead of
    the woven classes, because woven code renders first and must name
    `C_trl`;
  - `bus.on(..., needs_binding=False)`, so the weaver still runs with no
    binding configured.

## Deviations from sv_gen.md, and why

- **No `record` feature on the woven class yet.** Features override virtual
  methods. A plain class has no `trl_record` for a woven subclass to override,
  and user code holds base-class handles. So a static recorder in `C_trl` is
  what user code can actually call. Virtual dispatch can come later through
  the UVM `do_record` override (phase 1b).
- **Kinds limited to what `trl_pkg` exports today:**
  - integral ≤ 64 bits (including packed structs and enums) uses `set_int`;
  - `real`/`shortreal` uses `set_real`;
  - `string` uses `set_str`;
  - 1-D fixed arrays of these flatten to `name[i]`.

  These are skipped with a warning, because `trl_pkg` has no setter for them
  yet:
  - more than 64 bits, `trl_kind="logicvec"`, byte queues and dynamic arrays
    (`set_bits`/`set_logic`/`set_bytes`);
  - unpacked structs (sv-weave's `SvType` has no member list);
  - class handles.

  Enums are recorded as unsigned ints (`TRL_FT_ENUM` needs enum-type
  registration, which trl_pkg doesn't export).
- **`SCHEMA_HASH` isn't written into the trace yet.** There is no SV/C API
  for type-level metadata. It is in `C_trl` and in the type JSON.
- **`local` and `protected` fields** are unreachable from `C_trl`, so they
  are skipped with a warning, or rejected as an error with the `strict`
  option.
- **`trl_start` / `trl_end` attributes:** deferred.

## Status (2026-11-23)

| Step | State |
|---|---|
| `python/src/trlog/weave/` (`_record.py`): kind mapping, design handler, `C_trl` emit, `trl_types.json` | done |
| `pyproject.toml`: extra `weave = ["sv-weave"]`, entry point `sv_weave.weavers: trlog-record` | done |
| `tests/unit/test_weave_record.py` (8 tests: layout, warnings, artifact, `select` + profiles, no-op, type-name clash, class `trl_name`, hash stability); slang verifies the woven output | done, passing |
| `sv/tests/test_weave_record.py`: weave → Verilator → record 2 objects → read back in Python, checking names, kinds, times and values against `trl_types.json` | done, passing |
| Phase 1b: `set_fields`, woven UVM `do_record` (typed + generic), `trl_uvm_pkg` (database/stream/recorder); sv-weave forward typedefs + `needs_binding=False` | done; unit test (slang + UVM) passing |
| `sv/tests/test_weave_record_uvm.py`: Verilator + UVM, `begin_tr`/`end_tr` → typed txns, generic `uvm_field_*` txn, label metadata, parent/child | done, passing (~22 s) |
| C fix: `trl_rec_begin` registers the schema of a field-less ad-hoc type (it used to record schema id 0); covered by `c/tests/test_rec.c` + `tests/integration/test_rec_session.py` | done |
| `trl_pkg` gaps that limit kinds: `set_bits`/`set_logic`/`set_bytes`, enum-type registration, type-level metadata for `SCHEMA_HASH` | todo (C/SV side) |
| Phase 2: `<cls>_ph` modeling classes (needs `trl_ph`/`trl_pe`) | todo |

Phase 1b notes:
- The testbench must instantiate `sv_weave_init_m` (or call the init package), or
  the factory never hands out the woven class, and recording silently falls back
  to UVM's empty `do_record`.
- Ad-hoc fields must be declared before the txn opens: a txn takes the ad-hoc
  type's schema version as of its open. `trl_uvm_recorder` buffers generic values
  for this reason.
- Fields set by a user-written `do_record` in a class between the recorded class
  and `uvm_object` are skipped on the typed path (`super` is not called). Fields
  declared in those classes are still recorded, if they are in user packages.

Run the tests:
`PATH=$PWD/packages/verilator/bin:$PATH PYTHONPATH=python/src packages/python/bin/python -m pytest tests/unit/test_weave_record.py sv/tests/test_weave_record.py sv/tests/test_weave_record_uvm.py`
(the sim test needs `build/c/libtrl_sv.so`).

Notes:
- slang accepted `longint unsigned'(x)`, but Verilator rejected it, and
  rightly: a cast type must be one word. The generated code now relies on
  `set_int`'s implicit conversion instead. Slang's verification alone isn't
  enough, so keep the Verilator test.
- `sv/tests/test_sv_smoke.py::test_dpi_round_trip` fails in this working
  tree because of the IMPLICITSTATIC lint at `top_smoke.sv:45`. It is
  unrelated to this work.
