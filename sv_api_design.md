# TRLOG SystemVerilog API: Design

Status: **draft for review, rev 3** (2026-11-21). All review questions (Q1–Q9) are decided. Q3's analysis is in
§11 (spill-coalesce block policy), and Q6's is in §12 (both forms ship).
Implementation progress is tracked in `sv_impl_status.md`.
Inputs: `sv_pkg_plan.md`, `c/include/trl/trl.h`, `c/include/trl/trl_codec.h`,
`c/src/*.c`, `docs/guide/format.md`, the Python reference writer/reader.

This document defines a class-based SystemVerilog API for recording and reading
transactions, implemented over the C library through DPI-C. The main design
goal is efficiency, which means three things:

1. Each recorded transaction should make as few DPI crossings as possible.
2. The C side should do no heap allocation and no string hashing for each transaction.
3. Disabled recording should cost close to nothing: no DPI calls, and no
   allocation of SV objects.

---

## 1. Summary

The design has four layers. Each layer is thin, and all state lives in C.

```
 L3  trlog_uvm_pkg      optional uvm_tr_database / uvm_recorder adapter
 L2  trl_pkg          SV classes: trl_db, trl_type, trl_stream, trl_txn, trl_reader, trl_cursor
 L1  trl_sv_dpi.svh +   1:1 DPI imports, `include`d into trl_pkg  <->  trl_sv.c (svdpi glue only)
     c/src/trl_sv.c
 L0  trl_rec.h / trl_query.h   NEW C session layer (simulator-independent,
     over trl.h                unit-testable from C and Python ctypes)
```

Main decisions (§9 lists the ones that still need a choice):

* **Handles are `chandle` pointers straight into C structs.** No ID→struct
  lookup happens on the hot path. A debug plusarg turns on a magic-number
  check.
* **Every name is resolved to an integer once, at setup time.** This covers
  field indices, string IDs and enum types. Hot-path calls take only `int`,
  `longint` and `chandle` arguments.
* **Three recording tiers**, from fastest to most flexible:
  1. `trl_stream.record(...)`: a complete transaction in **one** DPI call, with
     scalar fields passed in bulk through an open array.
  2. `open_txn` → set fields (one bulk call or one call per field) → `close`.
  3. Ad-hoc, field-by-name: a convenience path with optional caching.
* **Nested types are flattened at definition time.** A sub-struct field's flat
  index is `base + offset`, so walking a nested type in the hot path is
  integer addition. A struct-declaration type-registry entry keeps the tree so
  it can be introspected.
* **Ad-hoc streams use an append-only "open schema".** The type registry is
  written when the file closes, so the schema can keep growing. Records are
  already sparse (`field_idx, value` pairs), so an ad-hoc transaction costs the
  same as a typed one once its field indices are known.
* **The C side encodes on close.** When a transaction ends, its values are
  varint-encoded straight into the current block buffer. There is no
  `trl_attr_t` array and no per-record `malloc` (the current C writer does
  both).
* **Reading uses a pull cursor, not callbacks.** One DPI call returns the
  record header, and one bulk call returns the scalar fields. The cursor walks
  transactions in start-time order by doing a k-way merge across blocks.

---

## 2. Review of the current C API (gap analysis)

The C library today is a low-level, block-scoped *encoder*. The SV API needs a
*session* layer above it. What exists, and what is missing:

| Need (from `sv_pkg_plan.md`) | C today | Gap |
|---|---|---|
| Open, configure and close a database | `trl_writer_open/close` | No default/named DB registry. No auto-close. Not shared with `libtrlog_vpi` |
| Define a type | `trl_add_txn_schema` (one-shot, flat array) | No incremental build, no nested structs, no enum types (Python has `add_enum_type`), no `display_hint`/`enum_type_id` (C writes 0) |
| Look up a type by name / introspect it | — | No name maps, no introspection on either the write or read side |
| Create a stream (global or under a parent) | `trl_hier_add_stream` inside a one-shot hierarchy block | No dynamic stream creation after the hierarchy is flushed, no stream types (`add_stream_type` is Python-only), no lookup by path |
| Begin, set fields, end a transaction | `trl_txn_full` only (start and end known at the same time) | No open-transaction table, no `TR_BEGIN/ATTR/END`, no `TR_LINK` writer, no automatic block management, no txn-ID allocation |
| Associations | — | `TR_LINK` is defined in the format but C never writes it |
| Read transactions | `trl_iter_vc` (VC only, callback) | **No transaction reader in C at all.** No per-stream index (Python has one), no start-time ordering, no schema access |
| Check whether recording is enabled | — | — |

Defects found during the review. These should be fixed before or alongside
L0. **All five were fixed in phase 1** (see `sv_impl_status.md`):

1. **The field-type enums disagree between C and Python.** C has
   `FT_BOOL=10, FT_STRING=11, FT_ENUM=12`. Python (`_types.py`) has
   `FT_BOOL=0x0A, FT_STRING=0x0B, FT_BITVEC=0x0C, FT_BYTES=0x0D, FT_TIME=0x0E,
   FT_ENUM=0x0F`. A C-written `FT_ENUM` field therefore reads back in Python as
   `FT_BITVEC`. C also lacks `FT_BITVEC`, `FT_BYTES` and `FT_TIME`.
2. **The `FT_ENUM` value encoding disagrees.** C writes `le64`
   (`txn_data.c`); Python writes `uvarint` (`_codec.py`).
3. **String values can dangle.** `trl_txn_block_add_full` `memcpy`s each
   `trl_attr_t`, but `value.str` keeps the caller's pointer until block encode
   time. A DPI `string` argument is only valid for the duration of the call, so
   from SV this is a use-after-free.
4. `trl_txn_block_add_full` does a `malloc` for every record.
   `trl_txn_block_free` is called from `trl_txn_block_begin`, so the buffers
   are freed and reallocated for every block.
5. `trl_index_add_txn` does not record a stream ID, so the C writer cannot
   produce the per-stream index that the Python reader's fast path uses.

---

## 3. Efficiency principles

### 3.1 DPI cost model

Each crossing has a fixed cost: the call itself, argument marshalling, and on
some simulators a save/restore of the SV context. The marshalling cost depends
on the argument type:

| Argument kind | Cost | Use |
|---|---|---|
| `int`, `longint`, `chandle`, `real` | register-passed, ~free | everything hot |
| `string` (input) | simulator may copy or convert to `char*`, and C must hash or copy it | setup only. On the hot path, use an interned `int` ID |
| `input longint unsigned v[]` (open array) | one handle. `svGetArrayPtr` gives a direct pointer on the major simulators | bulk scalar transfer |
| `input bit [N-1:0]` (fixed packed) | by reference as `const svBitVecVal*`, but SV zero-extends the source to N | wide vectors, N = `TRL_MAX_BITS` |
| `output` scalar args | ~free | multiple results from one cursor call |
| `context` import | may disable simulator optimizations around the call site | **never on the hot path** |

Rules that follow from the table:

* No `context` imports except one-time init, and only if VPI is needed there.
* No DPI *exports* (C→SV callbacks) on any path. Reading is pull-based.
* No `pure` imports either. The results depend on C state, so marking them
  `pure` would be wrong.

### 3.2 Crossing budget per transaction

These are the targets the implementation will be benchmarked against
(`benchmarks/`):

| Scenario | DPI calls |
|---|---|
| Stream disabled (SV-side `en` bit) | **0** |
| Tier 1: `record()` complete txn, k scalar fields | **1** |
| Tier 1 with string fields given as interned IDs in the bulk array | **1** |
| Free-form strings (not interned) | Tier 2: 2 + s |
| Tier 2: `open_txn`, bulk set, `close` | 3 |
| Tier 2: `open_txn`, k × `set_int`, `close` | 2 + k |
| Tier 3: ad-hoc by name (SV assoc-array hit) | 2 + k (+ k SV hash lookups) |
| Tier 3: first use of a field name | + 1 string crossing + 1 C hash lookup |
| Read/replay: `next` + bulk get | 2 |

### 3.3 Cost model on the C side (L0)

* **Open-transaction slots** come from a free-list pool. Each slot holds
  `u64 vals[nfields]` and a present bitmap sized to the schema, stored inline
  (`nfields` ≤ 64) or in a pool chunk. Ad-hoc schemas grow the slot when
  needed.
* **Variable-length values** (string, bitvec, bytes) are copied into a
  per-slot arena that is reset when the slot is freed. This fixes defect 3.
* **Encode on close.** `end` encodes the `TR_FULL` record straight into the
  block's payload buffer. This replaces the record array and fixes defect 4.
* **Blocks flush automatically** when a stream's encoded payload exceeds
  `+trlog_block_kb` (default 64 KiB), or under the spill rules in §11.4. Before encoding, records are sorted by
  start time inside the block (§7.3).
* **Transaction IDs** come from a monotonic counter per database, so the
  delta-encoded `txn_id` is 1 byte.
* **Long-lived transactions:** if a block flushes while a transaction is still
  open and that transaction began more than one block ago, C writes a
  `TR_BEGIN` (plus a `TR_ATTR` with the values so far) into the flushing
  block. It later writes `TR_ATTR`/`TR_END` instead of `TR_FULL`. This keeps
  each block's time range honest without paying for begin/end records on
  ordinary short transactions.

### 3.4 Fast paths on the SV side

* `trl_pkg::trl_enabled()` reads a `local static` bit set once at init
  inside `trl_db`. Each `trl_stream` has its own `en` bit, which applies the
  plusarg filter at creation time.
* Every L2 method checks `if (h == null) return;` *in SV* before it crosses.
  A disabled stream's `open_txn()` returns a `trl_txn` with a `null` handle
  without crossing.
* Recommended user idiom: `if (mon_s.en) begin ... end`. This also skips
  computing field values.
* Per-transaction SV objects are optional. `open_txn()` can rebind a
  caller-owned `trl_txn` object, and there is a raw-`chandle` API for code
  that wants no objects at all.

---

## 4. Data model mapping

### 4.1 Field kinds

The SV enum `trl_pkg::trl_field_type_e` mirrors the **corrected** `FT_*`
codes (Python numbering, which is authoritative):

| SV source type | `trl_field_type_e` | Wire `FT_*` | Setter |
|---|---|---|---|
| `bit` | `TRL_FT_BOOL` | `FT_BOOL` | `set_int` |
| `byte unsigned`, `bit[7:0]` | `TRL_FT_U8` | `FT_U8` | `set_int` |
| `shortint unsigned` | `TRL_FT_U16` | `FT_U16` | `set_int` |
| `int unsigned` | `TRL_FT_U32` | `FT_U32` | `set_int` |
| `longint unsigned`, `bit[63:0]` | `TRL_FT_U64` | `FT_U64` | `set_int` |
| `byte` / `shortint` / `int` / `longint` | `TRL_FT_I8…TRL_FT_I64` | `FT_I*` | `set_int` (C sign-extends by kind) |
| `shortreal` | `TRL_FT_F32` | `FT_F32` | `set_real` |
| `real` | `TRL_FT_F64` | `FT_F64` | `set_real` |
| `time` | `TRL_FT_TIME` | `FT_TIME` | `set_int` |
| `string` | `TRL_FT_STRING` | `FT_STRING` | `set_str` / `set_str_id` |
| SV `enum` | `TRL_FT_ENUM` (+ `enum_type`) | `FT_ENUM` | `set_int` |
| `bit [N-1:0]`, N > 64 | `TRL_FT_BITVEC` (+ width) | `FT_BITVEC` | `set_bits` |
| `logic [N-1:0]` (4-state) | `TRL_FT_LOGICVEC` (+ width) | **new** `FT_LOGICVEC = 0x10` (decided, Q1) | `set_logic` |
| `byte unsigned q[$]` / `[]` | `TRL_FT_BYTES` | `FT_BYTES` | `set_bytes` |

Every kind that is 64 bits or narrower goes through **one** setter,
`set_int(f, longint unsigned)`, plus the bulk variant. The schema already
knows the width and signedness, so SV never has to choose between setters,
and the bulk array has a single element type.

A 4-state `logic` value of 64 bits or less that is known to be free of X/Z can
also be declared as an integral kind. Only declare `TRL_FT_LOGICVEC` when you
need X/Z preserved.

`FT_LOGICVEC` value encoding (row layout), following the `FT_BITVEC` style:
`uvarint width`, then `ceil(width/8)` bytes of `aval`, then `ceil(width/8)`
bytes of `bval`, both big-endian like `FT_BITVEC`. A writer may emit an
`FT_LOGICVEC` field whose `bval` is all zero as-is. It does not quietly
downgrade the field to `FT_BITVEC`, because the schema type has to stay
fixed. Column layout does not support it, the same as the other
variable-length kinds.

### 4.2 Nested types (flattening)

`trl_type::add_struct("hdr", hdr_t)` copies `hdr_t`'s (already flat) field
list into the parent, with names prefixed `hdr.`. It returns the **base**
flat index. Field `len` of `hdr` then lives at
`t.index("hdr") + hdr_t.index("len")`, and also at `t.index("hdr.len")`.

* Arrays with a fixed size are flattened the same way:
  `add_array("d", TRL_FT_U32, 4)` creates `d[0]..d[3]` and returns the base
  index.
* Storage stays flat: the record is one `TR_FULL` with sparse
  `(field_idx, value)` pairs, and column layout keeps working.
* For introspection, a new length-prefixed type-registry entry
  `TRL_TYPE_TAG_STRUCT` (tag ≥ 0x05, so old readers skip it) records
  `(schema_id, struct_name, [(member_name, base_idx, sub_type_id | 0)])`. The
  reader rebuilds the tree from it. A reader that doesn't know the tag still
  sees the dotted names.
* A type is mutable until it is **sealed**: explicitly with `seal()`, or
  implicitly the first time a transaction uses it. After that, `add_*` is an
  error. This is what makes `base + offset` stable.

### 4.3 Ad-hoc transactions

A stream created without a type (or `adhoc_type()` on any stream) gets an
**open schema** named `<stream path>::adhoc`. `field("name", kind)` is
get-or-create. It returns the existing index if the name exists with a
compatible kind, appends a new field otherwise, and reports an error if the
name exists with an incompatible kind. Fields are only ever appended, so
earlier records stay valid. The schema is written out when the file closes,
like every other schema.

### 4.4 Type namespaces

Types live in a global namespace or a stream-scoped one.
`trl_type::find(name, scope)` searches the stream and its ancestors first, then
the global namespace. On disk, a stream-scoped type's schema name is
`"<stream path>::<name>"`, which needs no format change (§9 Q4).

### 4.5 Time

Times are an explicit `longint unsigned` on every L1 call, so C never calls
`vpi_get_time`. L2 defaults the time to `$time` evaluated inside `trl_pkg`,
which is compiled with

```systemverilog
`ifndef TRL_TIMEUNIT
`define TRL_TIMEUNIT 1ps
`endif
timeunit `TRL_TIMEUNIT; timeprecision `TRL_TIMEUNIT;
```

The trace's `timescale_exp` is derived from the same define and passed to C at
open time. The package does not declare a precision finer than the design's,
because doing so would make the whole simulation run at the finer precision.
Users whose design runs at fs set `+define+TRL_TIMEUNIT=1fs`. Any call also
accepts an explicit time.

---

## 5. Initialization and configuration

### 5.1 Plusargs

The SV side reads all plusargs with `$value$plusargs`, which is portable,
including to Verilator without `--vpi`. It passes the values to
`trl_sv_db_open`. The VPI side keeps reading its own plusargs through
`vpi_get_vlog_info`. Both sides use **one vocabulary**: rev 1's `+trlog=<path>`
is dropped in favour of the `+trlog_file` that `trlog_vpi.c` already uses, so
one plusarg turns on a single combined file.

| Plusarg | Meaning | Default |
|---|---|---|
| `+trlog_file=<path>` | turn recording on, writing to `<path>`. **Shared with `libtrlog_vpi`** (§5.4) | off |
| `+trlog_off` | force off (wins over `+trlog_file`) | — |
| `+trlog_compress=none\|zlib` | block compression (existing VPI plusarg, same meaning) | `zlib` |
| `+trlog_signals=<globs>\|none` | VPI signal dumping (existing). `none` records transactions only | all signals, when VPI is active |
| `+trlog_block_kb=<n>` | target block payload size | `64` |
| `+trlog_buf_mb=<n>` | memory cap across all streams' unflushed buffers (§11.4) | `16` |
| `+trlog_max_age=<t>` | flush any stream buffer whose oldest record is older than `<t>` sim-time units. Bounds what a crash can lose | `0` = off |
| `+trlog_streams=<glob>[,<glob>…]` | only these stream paths are enabled | all |
| `+trlog_streams_off=<glob>[,…]` | disable these stream paths (applied after the include list) | none |
| `+trlog_read=<path>` | default read database | none |
| `+trlog_read_<name>=<path>` | named read database `<name>` | none |
| `+trlog_check` | validate handles (magic + generation), and check field kind on each set | off |
| `+trlog_verbose` | print open/close/flush statistics | off |

### 5.2 Lifecycle

* **Lazy init.** The first call to `trl_pkg::trl_db::get()` (or anything
  that needs it) reads the plusargs and opens the file. Packages cannot hold
  `initial` blocks, and calling DPI from a static initializer behaves
  differently across simulators, so init does not happen at elaboration.
* `trl_pkg::trl_enabled()` is an SV function that reads a `local static`
  bit inside `trl_db` (after forcing init). No DPI call.
* **Close**, in order of preference:
  1. `trl_pkg::trl_shutdown()` called by the user (for example from
     `uvm_component::final_phase`).
  2. `module trl_final; final trl_pkg::trl_shutdown(); endmodule`, shipped
     with the package. Instantiate it or bind it at top.
  3. A C `atexit` handler as a fallback. It does nothing if the file is
     already closed.
* Transactions still open at close are written with `end = last time seen`
  and the metadata `trlog.truncated=1`.
* **Sharing with the VPI dumper:** see §5.4.

### 5.3 Threads

Verilator `--threads` can call DPI from several threads at once. L0 does not
lock by default. A `TRL_REC_THREADSAFE` build option adds a mutex for each
database. Stream-local state lives in the stream, so the lock covers only
txn-ID allocation and block flushes.

### 5.4 Packaging: one library, VPI optional (Q9, decided)

Decision: there is one shared library, `libtrlog_sv.so`, containing L0, the
DPI glue (`trl_sv.c`) and the VPI signal dumper (`vpi/src/*`). The constraint
is that **loading or linking it must never require VPI.**

What makes this hard today. `trlog_vpi.c` calls `vpi_register_cb`,
`vpi_get_vlog_info`, `vpi_iterate` and others directly. Each call is an
undefined symbol in the `.so`, so:

* **Verilator without `--vpi`** links the library into the model executable,
  and `ld` rejects undefined symbols in a shared library when it links an
  executable (`--no-allow-shlib-undefined` is the default). The build fails.
* **Event simulators** usually `dlopen` with lazy binding, but any of them
  that uses `RTLD_NOW`, or a user environment that sets `LD_BIND_NOW`, fails at
  load time.

The design:

1. **No direct `vpi_*` references.** Every VPI call goes through a
   function-pointer table, `trl_vpi_api_t`, with one pointer per VPI routine
   used. The table is filled the first time it is needed, with
   `dlsym(RTLD_DEFAULT, "vpi_…")`. If `vpi_register_cb` is missing, VPI is
   treated as absent: the dumper turns itself off, and `$trlog_dump*` cannot
   exist anyway. The mock-VPI unit tests fill the table directly instead of
   relying on `-include` symbol interposition. (Weak undefined symbols would
   also work on ELF, but `dlsym` behaves the same on ELF and Mach-O, and it
   makes "is VPI present?" an explicit check.)
2. **The VPI entry point does nothing unless asked.** `vlog_startup_routines`
   and `trlog_vpi_register` stay exported. They only run when the simulator
   loads the library as a VPI module (`-pli`, `-load`, `-vpi`, or Verilator
   calling `VerilatedVpi` startup). The DPI path never calls them. Loading the
   library only with `-sv_lib` gives transaction recording with no VPI.
3. **One database registry.** `trlog_vpi_state.c` gets its writer from
   `trl_db_open("default", …)` instead of opening its own, so whichever side
   starts first opens the file and the other attaches to it.
4. **Time units.** A file has a single `timescale_exp`. If VPI is active, the
   database uses the simulator's precision (`vpi_get(vpiTimePrecision)`),
   which is the finest unit in the design. The SV side declares its unit when
   it attaches (`TRL_TIMEUNIT` exponent), and L0 multiplies SV times by the
   integer `10^(sv_exp - db_exp)`, which is ≥ 1, so no precision is lost. If
   VPI is absent, the database uses the SV unit and the multiplier is 1.
   Sharing a database where the SV unit is *finer* than the database unit is an
   error at attach time.
5. **Build.** CMake builds `libtrlog_sv.so` with `-Wl,--no-undefined` (minus
   libc/libdl), which proves at build time that the library has no VPI or DPI
   link-time dependency. `svdpi` entry points (`svGetArrayPtr` and others) are
   needed only by `trl_sv.c`. Every DPI simulator provides them, but they get
   the same `dlsym` treatment, so the library can also be used from plain C or
   Python ctypes. A separate `libtrlog_vpi.so` is no longer needed. Keeping it
   as a symlink for existing `-pli` command lines costs nothing.

---

## 6. SystemVerilog API

Naming and packaging rule: one class lives in its own `.svh` file, `include`d
into `trl_pkg`. Because users normally `import trl_pkg::*;`, every
package-scope name (class, enum, enum literal, function) is prefixed
`trl_`/`TRL_` to stay clear of user and UVM names. The DPI-C imports are
`include`d directly into `trl_pkg` rather than kept in a separate package,
since `trl_pkg` is useless without them.

### 6.1 L1: DPI-C imports (`trl_sv_dpi.svh`, included into `trl_pkg`)

All of these are non-`context`. Handles are `chandle`. Status values are `int`
(0 = OK).

```systemverilog
// trl_sv_dpi.svh — included directly into trl_pkg (not a standalone package)
// ---- database ----
import "DPI-C" function chandle trl_sv_db_open(string name, string path,
                  int timescale_exp, int compress, int block_kb, int flags);
import "DPI-C" function chandle trl_sv_db_find(string name);
import "DPI-C" function int     trl_sv_db_close(chandle db);
import "DPI-C" function int     trl_sv_intern(chandle db, string s);
import "DPI-C" function string  trl_sv_last_error();

// ---- types ----
import "DPI-C" function chandle trl_sv_type_new(chandle db, string name, chandle scope);
import "DPI-C" function chandle trl_sv_type_find(chandle db, string name, chandle scope);
import "DPI-C" function int     trl_sv_type_add_field(chandle t, string name, int kind,
                                                      int width, chandle enum_t);
import "DPI-C" function int     trl_sv_type_add_struct(chandle t, string name, chandle sub);
import "DPI-C" function int     trl_sv_type_add_array(chandle t, string name, int kind,
                                                      int width, int n);
import "DPI-C" function int     trl_sv_type_seal(chandle t);
import "DPI-C" function int     trl_sv_type_num_fields(chandle t);
import "DPI-C" function int     trl_sv_type_field_index(chandle t, string path); // -1 if absent
import "DPI-C" function void    trl_sv_type_field_info(chandle t, int f,
                                  output string name, output int kind,
                                  output int width, output chandle enum_t);
import "DPI-C" function string  trl_sv_type_name(chandle t);
import "DPI-C" function int     trl_sv_type_num_members(chandle t);   // nested view
import "DPI-C" function void    trl_sv_type_member(chandle t, int m,
                                  output string name, output int base,
                                  output chandle sub);
import "DPI-C" function chandle trl_sv_enum_new(chandle db, string name);
import "DPI-C" function void    trl_sv_enum_add(chandle e, string label, longint v);

// ---- streams ----
// Returns null if recording is disabled for this path; *en is filled in either way.
import "DPI-C" function chandle trl_sv_stream_new(chandle db, chandle parent, string name,
                                  string kind, chandle dflt_type, output bit en);
import "DPI-C" function chandle trl_sv_stream_find(chandle db, string path);
import "DPI-C" function chandle trl_sv_stream_adhoc_type(chandle s);
import "DPI-C" function int     trl_sv_field(chandle t, string name, int kind, int width);

// ---- recording: hot path ----
import "DPI-C" function longint unsigned trl_sv_record(chandle s, chandle t,
                   longint unsigned start, longint unsigned end_t,
                   longint unsigned parent, input longint unsigned v[]);
import "DPI-C" function chandle trl_sv_begin(chandle s, chandle t,
                   longint unsigned start, longint unsigned parent);
import "DPI-C" function longint unsigned trl_sv_txn_id(chandle x);
import "DPI-C" function void trl_sv_set_int  (chandle x, int f, longint unsigned v);
import "DPI-C" function void trl_sv_set_ints (chandle x, int f0, input longint unsigned v[]);
import "DPI-C" function void trl_sv_set_real (chandle x, int f, real v);
import "DPI-C" function void trl_sv_set_str  (chandle x, int f, string v);
import "DPI-C" function void trl_sv_set_str_id(chandle x, int f, int sid);
import "DPI-C" function void trl_sv_set_bits (chandle x, int f, int width,
                   input bit [`TRL_MAX_BITS-1:0] v);
import "DPI-C" function void trl_sv_set_logic(chandle x, int f, int width,
                   input logic [`TRL_MAX_BITS-1:0] v);
import "DPI-C" function void trl_sv_set_bytes(chandle x, int f, input byte unsigned v[]);
import "DPI-C" function void trl_sv_link (chandle x, longint unsigned tgt, int ltype, int label_sid);
import "DPI-C" function void trl_sv_link_id(chandle db, longint unsigned src,
                   longint unsigned tgt, int ltype, int label_sid);
import "DPI-C" function void trl_sv_meta (chandle x, int key_sid, int val_sid);
import "DPI-C" function void trl_sv_end  (chandle x, longint unsigned end_t);

// ---- reading ----
import "DPI-C" function chandle trl_sv_rdb_open(string name, string path);
import "DPI-C" function chandle trl_sv_rdb_find(string name);   // "default" = +trlog_read
import "DPI-C" function int     trl_sv_rdb_num_streams(chandle r);
import "DPI-C" function chandle trl_sv_rdb_stream_at(chandle r, int i);
import "DPI-C" function chandle trl_sv_rdb_stream_find(chandle r, string path);
import "DPI-C" function chandle trl_sv_rdb_type_find(chandle r, string name, chandle scope);
import "DPI-C" function string  trl_sv_rstream_path(chandle rs);
// Reader-side types reuse the trl_sv_type_* introspection calls above.
import "DPI-C" function chandle trl_sv_cursor_new(chandle r, chandle rs /*null=all*/,
                   longint unsigned t_from, longint unsigned t_to);
import "DPI-C" function int     trl_sv_cursor_add_stream(chandle c, chandle rs);
import "DPI-C" function int     trl_sv_cursor_next(chandle c,
                   output longint unsigned id, output longint unsigned start,
                   output longint unsigned end_t, output longint unsigned parent,
                   output chandle rs, output chandle t);         // 0 = exhausted
import "DPI-C" function int     trl_sv_cursor_seek(chandle c, longint unsigned t);
import "DPI-C" function int     trl_sv_cur_has  (chandle c, int f);
import "DPI-C" function longint unsigned trl_sv_cur_int(chandle c, int f);
import "DPI-C" function void    trl_sv_cur_ints (chandle c, int f0, output longint unsigned v[]);
import "DPI-C" function real    trl_sv_cur_real (chandle c, int f);
import "DPI-C" function string  trl_sv_cur_str  (chandle c, int f);
import "DPI-C" function void    trl_sv_cur_bits (chandle c, int f,
                   output bit [`TRL_MAX_BITS-1:0] v);
import "DPI-C" function void    trl_sv_cur_logic(chandle c, int f,
                   output logic [`TRL_MAX_BITS-1:0] v);
import "DPI-C" function void    trl_sv_cur_bytes(chandle c, int f, output byte unsigned v[]);
import "DPI-C" function int     trl_sv_cur_num_links(chandle c);
import "DPI-C" function void    trl_sv_cur_link (chandle c, int i, output longint unsigned tgt,
                   output int ltype, output string label);
import "DPI-C" function string  trl_sv_cur_meta (chandle c, string key);
import "DPI-C" function void    trl_sv_cursor_free(chandle c);
```

Notes:

* Rev 3: calls that take a transaction `chandle x` also take an `int unsigned gen`
  argument, the slot generation from §12.3. The listing above leaves it out
  for readability.
* `TRL_MAX_BITS` defaults to 1024. UVM users set it to `UVM_MAX_STREAMBITS`
  (4096). Values wider than `TRL_MAX_BITS` are an error. The `width`
  argument tells C how many bits of the fixed vector are significant.
* `trl_sv_cur_bytes` uses an `output` open array, so the caller must size the
  dynamic array first. L2 hides this by calling `trl_sv_cur_len(c, f)` first
  (not shown).
* SV `string` returned from DPI is copied by the simulator. C returns pointers
  into the cursor's decoded block, which stay valid until the next `next()`.

### 6.2 L2: `trl_pkg` (classes)

The listing shows signatures and only the bodies that matter for cost.

```systemverilog
package trl_pkg;
  `include "trl_macros.svh"   // TRL_TIMEUNIT, TRL_MAX_BITS, TRL_IF
  `include "trl_sv_dpi.svh"     // DPI-C imports, included rather than a separate package

  typedef enum int {
    TRL_FT_U8=0, TRL_FT_U16, TRL_FT_U32, TRL_FT_U64, TRL_FT_I8, TRL_FT_I16, TRL_FT_I32, TRL_FT_I64,
    TRL_FT_F32, TRL_FT_F64, TRL_FT_BOOL, TRL_FT_STRING, TRL_FT_BITVEC /*FT_BITVEC*/, TRL_FT_BYTES, TRL_FT_TIME,
    TRL_FT_ENUM, TRL_FT_LOGICVEC /*new FT_LOGICVEC*/
  } trl_field_type_e;

  typedef enum int { TRL_LINK_CAUSE=0, TRL_LINK_PARENT_CHILD=1, TRL_LINK_RELATED=2, TRL_LINK_CUSTOM=255 } trl_link_e;

  function automatic bit trl_enabled();  return trl_db::enabled(); endfunction
  function automatic longint unsigned trl_now(); return $time; endfunction
  function automatic void trl_shutdown(); trl_db::close_all(); endfunction

  // ------------------------------------------------------------------ trl_db (trl_db.svh)
  class trl_db;
    chandle h;
    string  name;
    local static trl_db m_dbs[string];
    local static bit    m_enabled;      // set once by get()
    local static bit    m_inited;

    static function trl_db get(string name = "default");   // lazy: plusargs → trl_sv_db_open
    static function trl_db open(string name, string path, int compress = 1); // explicit, extra DBs
    static function bit enabled();      // forces lazy init, then returns m_enabled
    static function void close_all();
    function int intern(string s);   // → string id, for set_str_id / meta / link labels
  endclass

  // ------------------------------------------------------------------ trl_enum (not yet implemented)
  class trl_enum;
    chandle h;
    static function trl_enum create(string name, trl_db d = null);
    function void add(string label, longint value);
  endclass

  // Fills a trl_enum from any SV enum type; one-time cost at setup.
  // `TRL_ENUM(my_enum_t) returns a trl_enum.

  // ------------------------------------------------------------------ trl_type (trl_type.svh)
  class trl_type;
    chandle h;
    static function trl_type create(string name, trl_stream scope = null, trl_db d = null);
    static function trl_type find  (string name, trl_stream scope = null, trl_db d = null); // null if absent
    // definition — each returns the field's flat index
    function int add_field (string name, trl_field_type_e k, int width = 0, trl_enum e = null);
    function int add_struct(string name, trl_type sub);   // returns base index
    function int add_array (string name, trl_field_type_e k, int n, int width = 0); // base index
    function void seal();
    // introspection (works on writer and reader types)
    function string name();
    function int    num_fields();
    function int    index(string path);                 // "hdr.len", "d[2]"; -1 if absent
    function void   field_info(int f, output string nm, output trl_field_type_e k,
                               output int width, output trl_enum e);
    function int    num_members();                      // nested view, top level
    function void   member(int m, output string nm, output int base, output trl_type sub);
  endclass

  // ------------------------------------------------------------------ trl_txn (trl_txn.svh)
  // Thin handle. Reusable: trl_stream::begin(..., .into(t)) rebinds an existing object.
  class trl_txn;
    chandle h;            // null => disabled/no-op
    local static trl_txn m_null;                       // shared inert object
    static function trl_txn null_txn(); if (m_null == null) m_null = new; return m_null; endfunction
    function trl_txn clear(); h = null; return this; endfunction
    function longint unsigned id();
      return (h == null) ? 0 : trl_sv_txn_id(h);
    endfunction
    function void set_int (int f, longint unsigned v); if (h != null) trl_sv_set_int(h, f, v); endfunction
    function void set_ints(int f0, const ref longint unsigned v[]);
      if (h != null) trl_sv_set_ints(h, f0, v);
    endfunction
    function void set_real (int f, real v);   if (h != null) trl_sv_set_real(h, f, v);   endfunction
    function void set_str  (int f, string v); if (h != null) trl_sv_set_str(h, f, v);    endfunction
    function void set_str_id(int f, int sid); if (h != null) trl_sv_set_str_id(h, f, sid); endfunction
    function void set_bits (int f, int w, bit   [`TRL_MAX_BITS-1:0] v); if (h != null) trl_sv_set_bits(h, f, w, v);  endfunction
    function void set_logic(int f, int w, logic [`TRL_MAX_BITS-1:0] v); if (h != null) trl_sv_set_logic(h, f, w, v); endfunction
    function void set_bytes(int f, const ref byte unsigned v[]); if (h != null) trl_sv_set_bytes(h, f, v); endfunction
    function void link(longint unsigned tgt, trl_link_e k = TRL_LINK_RELATED, int label_sid = 0);
      if (h != null) trl_sv_link(h, tgt, k, label_sid);
    endfunction
    function void meta(int key_sid, int val_sid); if (h != null) trl_sv_meta(h, key_sid, val_sid); endfunction
    function void close(longint unsigned end_t = trl_now());
      if (h != null) begin trl_sv_end(h, end_t); h = null; end
    endfunction
  endclass

  // ------------------------------------------------------------------ trl_stream (trl_stream.svh)
  class trl_stream;
    chandle  h;            // null when disabled
    bit      en;           // read this before doing any work for the stream
    string   path;
    trl_type dflt_type;

    static function trl_stream create(string name, trl_stream parent = null,
                                  trl_type dflt = null, string kind = "", trl_db d = null);
    static function trl_stream find(string path, trl_db d = null);

    // Tier 1: one DPI call. Returns the txn id (0 when disabled).
    function longint unsigned record(longint unsigned start, longint unsigned end_t,
                                     const ref longint unsigned v[],
                                     trl_type t = null, longint unsigned parent = 0);
      if (!en) return 0;
      return trl_sv_record(h, (t == null) ? dflt_type.h : t.h, start, end_t, parent, v);
      // (dflt_type == null && t == null is a setup error, reported at create())
    endfunction

    // Tier 2
    // (`begin` is an SV keyword, hence open_txn.)
    function trl_txn open_txn(trl_type t = null, longint unsigned parent = 0,
                          longint unsigned start = trl_now(), trl_txn into = null);
      trl_txn x;
      if (!en) return (into != null) ? into.clear() : trl_txn::null_txn();  // no allocation
      x = (into != null) ? into : new;
      x.h = trl_sv_begin(h, (t == null) ? dflt_type.h : t.h, start, parent);
      return x;
    endfunction
    function trl_txn open_child(trl_txn parent, trl_type t = null, longint unsigned start = trl_now(),
                            trl_txn into = null);
      return open_txn(t, parent.id(), start, into);
    endfunction

    // Tier 3: ad-hoc
    local int m_fidx[string];                         // SV-side name→index cache
    function trl_type adhoc_type();                   // the open schema
    // get-or-create index; a cache hit costs no DPI call
    function int field(string name, trl_field_type_e k, int width = 0);
      if (!m_fidx.exists(name)) m_fidx[name] = trl_sv_field(adhoc_type().h, name, k, width);
      return m_fidx[name];
    endfunction
  endclass

  // ------------------------------------------------------------------ reading
  // trl_rstream (trl_rstream.svh)
  class trl_rstream;
    chandle h;
    function string path();
  endclass

  // trl_schema (trl_schema.svh) — read-side introspection; see trl_pkg.sv's header
  // comment for why this is a separate class from the write-side trl_type.
  class trl_schema;
    chandle h;
    function string name();
    function int    num_fields();
    function int    index(string name);
    function void   field_info(int f, output string nm, output trl_field_type_e k);
  endclass

  // trl_reader (trl_reader.svh)
  class trl_reader;
    chandle h;
    static function trl_reader get(string name = "default");   // +trlog_read / +trlog_read_<name>
    static function trl_reader open(string name, string path);
    function int        num_streams();
    function trl_rstream stream_at(int i);
    function trl_rstream find_stream(string path);
    function trl_schema  find_schema(string name);
  endclass

  // trl_cursor (trl_cursor.svh)
  class trl_cursor;
    chandle h;
    // Current record header; filled in by next() in one crossing.
    longint unsigned id, start, end_t, parent;
    chandle          rs_h, t_h;

    static function trl_cursor create(trl_reader r, trl_rstream s = null,
                                  longint unsigned t_from = 0,
                                  longint unsigned t_to = 64'hFFFF_FFFF_FFFF_FFFF);
    function void add_stream(trl_rstream s);        // merge several streams by start time
    function bit  next();
      return trl_sv_cursor_next(h, id, start, end_t, parent, rs_h, t_h) != 0;
    endfunction
    function void seek(longint unsigned t);
    // field access: same index space as trl_type
    function bit              has (int f);
    function longint unsigned get_int(int f);
    function void             get_ints(int f0, ref longint unsigned v[]);   // v pre-sized
    function real             get_real(int f);
    function string           get_str(int f);
    function void             get_bits(int f, output bit [`TRL_MAX_BITS-1:0] v);
    function void             get_logic(int f, output logic [`TRL_MAX_BITS-1:0] v);
    function void             get_bytes(int f, ref byte unsigned v[]);
    function int              num_links();
    function void             link(int i, output longint unsigned tgt, output trl_link_e k,
                                   output string label);
    function string           meta(string key);
    // Replay helper: wait until the current record's start time.
    task wait_start();
      if (start > $time) #(start - $time);
    endtask
  endclass
endpackage
```

Design points in the listing:

* `record()` and `trl_txn.set_ints()` take `const ref` open arrays, so SV does not copy
  the array at the call. The DPI layer then passes a handle, not a copy.
* `trl_type` objects are created once per type. `trl_txn` objects can be reused
  through `open_txn(..., .into(x))`. On the Tier 1 path, recording allocates no SV objects at all.
* `trl_cursor` caches the header in member variables, so reading
  `c.start`/`c.end_t` costs nothing after `next()`.
* Reader-side types are `trl_type` too. Introspection works the same way on both
  sides.

### 6.3 Macros (`trl_macros.svh`)

```systemverilog
`define TRL_IF(s) if ((s) != null && (s).en)

// Build a trl_enum from an SV enum type once, by iterating first()/next().
`define TRL_ENUM(ET) trl_pkg::trl_enum_helper#(ET)::get()

```

There is no field-index macro. `trl_stream::field()` keeps its own SV-side
`string→int` cache, so the ad-hoc path never crosses DPI for a name it has
already seen. Hot code should still store the index in an `int`, as in §8.

The macros cover only what SV cannot express concisely. There is no macro
layer for defining whole types: these schemas are meant to be built by code,
and a UVM adapter or a generator can build them from `uvm_object` field
automation or a struct.

---

## 7. C session layer (L0)

These are new public headers. They are plain C, so they can be called from
Python ctypes and C unit tests without a simulator. `trl_sv.c` is a thin
mapping: `svOpenArrayHandle` → pointer/length, and `svBitVecVal*` /
`svLogicVecVal*` → byte buffers.

### 7.1 `trl/trl_rec.h`: recording session

```c
typedef struct trl_db     trl_db_t;
typedef struct trl_rtype  trl_rtype_t;    /* schema (writer or reader side) */
typedef struct trl_renum  trl_renum_t;
typedef struct trl_stream trl_stream_t;
typedef struct trl_txn    trl_txn_t;      /* open-transaction slot */

typedef struct {
    const char *path;
    int         timescale_exp;
    trl_compress_t compress;
    uint32_t    block_bytes;              /* auto-flush threshold */
    const char *stream_include;           /* comma-separated globs, NULL = all */
    const char *stream_exclude;
    uint32_t    flags;                    /* TRL_DB_CHECK, TRL_DB_VERBOSE */
} trl_db_cfg_t;

/* Process-wide registry; "default" is shared with libtrlog_vpi. */
trl_db_t     *trl_db_open(const char *name, const trl_db_cfg_t *cfg);
trl_db_t     *trl_db_find(const char *name);
trl_status_t  trl_db_close(trl_db_t *);                 /* also run from atexit */
trl_writer_t *trl_db_writer(trl_db_t *);                /* for VPI / VC sharing */
uint32_t      trl_db_intern(trl_db_t *, const char *s);
const char   *trl_last_error(void);

/* Types */
trl_rtype_t *trl_type_new  (trl_db_t *, const char *name, trl_stream_t *scope);
trl_rtype_t *trl_type_find (trl_db_t *, const char *name, trl_stream_t *scope);
int32_t      trl_type_add_field (trl_rtype_t *, const char *name, trl_field_type_t,
                                 uint32_t width, trl_renum_t *);
int32_t      trl_type_add_struct(trl_rtype_t *, const char *name, trl_rtype_t *sub);
int32_t      trl_type_add_array (trl_rtype_t *, const char *name, trl_field_type_t,
                                 uint32_t width, uint32_t n);
int32_t      trl_type_field_get_or_add(trl_rtype_t *, const char *name,
                                       trl_field_type_t, uint32_t width);  /* ad-hoc */
trl_status_t trl_type_seal(trl_rtype_t *);
/* introspection: shared with trl_query.h */
const char  *trl_type_name(const trl_rtype_t *);
uint32_t     trl_type_num_fields(const trl_rtype_t *);
int32_t      trl_type_field_index(const trl_rtype_t *, const char *path);
const char  *trl_type_field_name(const trl_rtype_t *, uint32_t f);
trl_field_type_t trl_type_field_type(const trl_rtype_t *, uint32_t f);
uint32_t     trl_type_field_width(const trl_rtype_t *, uint32_t f);
uint32_t     trl_type_num_members(const trl_rtype_t *);
const char  *trl_type_member(const trl_rtype_t *, uint32_t m, uint32_t *base,
                             const trl_rtype_t **sub);

trl_renum_t *trl_enum_new(trl_db_t *, const char *name);
trl_status_t trl_enum_add(trl_renum_t *, const char *label, int64_t value);

/* Streams */
trl_stream_t *trl_stream_new(trl_db_t *, trl_stream_t *parent, const char *name,
                             const char *kind, trl_rtype_t *dflt, int *enabled);
trl_stream_t *trl_stream_find(trl_db_t *, const char *path);
trl_rtype_t  *trl_stream_adhoc_type(trl_stream_t *);

/* Recording */
uint64_t   trl_rec_full (trl_stream_t *, trl_rtype_t *, uint64_t start, uint64_t end,
                         uint64_t parent, const uint64_t *vals, uint32_t nvals);
trl_txn_t *trl_rec_begin(trl_stream_t *, trl_rtype_t *, uint64_t start, uint64_t parent);
uint64_t   trl_rec_id   (const trl_txn_t *);
void trl_rec_set_u64  (trl_txn_t *, uint32_t f, uint64_t v);
void trl_rec_set_u64v (trl_txn_t *, uint32_t f0, const uint64_t *v, uint32_t n);
void trl_rec_set_f64  (trl_txn_t *, uint32_t f, double v);
void trl_rec_set_str  (trl_txn_t *, uint32_t f, const char *s);          /* copied */
void trl_rec_set_str_id(trl_txn_t *, uint32_t f, uint32_t sid);
void trl_rec_set_bits (trl_txn_t *, uint32_t f, uint32_t width, const uint32_t *words);
void trl_rec_set_logic(trl_txn_t *, uint32_t f, uint32_t width,
                       const uint32_t *aval, const uint32_t *bval);
void trl_rec_set_bytes(trl_txn_t *, uint32_t f, const uint8_t *p, uint32_t n);
void trl_rec_link     (trl_txn_t *, uint64_t tgt, uint8_t ltype, uint32_t label_sid);
void trl_rec_link_ids (trl_db_t *, uint64_t src, uint64_t tgt, uint8_t ltype, uint32_t label_sid);
void trl_rec_meta     (trl_txn_t *, uint32_t key_sid, uint32_t val_sid);
void trl_rec_end      (trl_txn_t *, uint64_t end);
```

How `trl_rec_full` and `trl_rec_set_u64v` treat their arrays:

* `vals[i]` maps to field `i` (or `f0 + i`) of the flat schema.
* Floats are passed as their bit pattern; L2 fills those slots with
  `$realtobits`. A `TRL_FT_STRING` slot carries an interned string ID (`trl_db.intern`),
  which keeps strings from a fixed set on the one-call path. Bit and logic
  vectors up to 64 bits take the value (no X/Z). Wider vectors and bytes are
  skipped (reported by `trl_last_error`) and need their own setter.
* With `nvals` < `num_fields`, the trailing fields are left unset, which gives
  sparse records.

### 7.2 Writer internals

* Each stream has its own block buffer, and transactions are encoded into it
  when they close (§3.3). Buffers are flushed under the **spill-coalesce**
  policy from §11: a full buffer becomes a block of its own, and when the
  database-wide memory cap or the max-age limit is hit, small buffers are
  written together as one multi-stream block. The record layout starts as
  row-delta. Column layout comes later, as one column set per (stream, schema).
* Streams are declared lazily. New streams are collected and written as a new
  hierarchy block (`HK_BEHAVIORAL`, with parent streams as scopes) just
  **before** the first transaction block that references them, and at close.
  The format already allows several hierarchy blocks. If the simulation
  crashes, every flushed transaction block's streams are still resolvable.
* The index records `(start, end, offset, stream_id | 0xFFFFFFFF)`, matching
  Python's per-stream index (defect 5), plus a new `min_start` per block for
  the cursor merge. A multi-stream block also needs a **stream-set entry**: a
  new index section, sentinel `0x05`, holding `uvarint offset_idx,
  uvarint n, n × uvarint stream_id`. Old readers skip the section and treat the
  block as mixed (`0xFFFFFFFF`), which is correct, just slower.

### 7.3 `trl/trl_query.h`: reading

```c
typedef struct trl_rdb     trl_rdb_t;
typedef struct trl_rstream trl_rstream_t;
typedef struct trl_cursor  trl_cursor_t;

trl_rdb_t     *trl_rdb_open(const char *name, const char *path);
trl_rdb_t     *trl_rdb_find(const char *name);
uint32_t       trl_rdb_num_streams(trl_rdb_t *);
trl_rstream_t *trl_rdb_stream_at(trl_rdb_t *, uint32_t i);
trl_rstream_t *trl_rdb_stream_find(trl_rdb_t *, const char *path);
const trl_rtype_t *trl_rdb_type_find(trl_rdb_t *, const char *name, trl_rstream_t *scope);

trl_cursor_t *trl_cursor_new(trl_rdb_t *, trl_rstream_t *s /*NULL=all*/,
                             uint64_t t_from, uint64_t t_to);
trl_status_t  trl_cursor_add_stream(trl_cursor_t *, trl_rstream_t *);
int           trl_cursor_next(trl_cursor_t *, trl_txn_hdr_t *out);   /* 0 = end */
trl_status_t  trl_cursor_seek(trl_cursor_t *, uint64_t t);
/* field accessors mirror trl_rec_set_*: has/u64/u64v/f64/str/bits/logic/bytes,
 * links, meta. Pointers stay valid until the next trl_cursor_next. */
void          trl_cursor_free(trl_cursor_t *);
```

**Start-time order.** Records are written when they close, so a long
transaction can sit in a later block than shorter transactions that started
after it. The cursor handles this as follows:

1. Inside each block, the writer sorts records by `(start, txn_id)` before
   encoding.
2. The cursor keeps a min-heap of per-block sub-cursors. Blocks are opened in
   `min_start` order (from the index). A block is decoded only when its
   `min_start` ≤ the smallest head currently in the heap.
3. Memory use is bounded by the number of blocks whose time ranges overlap,
   which is normally one or two. Blocks for streams outside the cursor are
   skipped using the per-stream index.
4. `TR_BEGIN`/`TR_END` pairs that span blocks are joined before they are
   emitted. A `TR_BEGIN` waits in the heap until its `TR_END` (or `TR_ATTR`)
   is found by a bounded forward scan, using the index to skip.

---

## 8. Usage examples

### 8.1 Typed monitor, Tier 1 (one DPI call per transaction)

```systemverilog
import trl_pkg::*;

class axi_mon;
  trl_stream s;  trl_type t;
  int f_addr, f_len, f_data;              // flat indices, resolved once
  longint unsigned v[3];                  // reused bulk buffer

  function void build();
    t      = trl_type::create("axi_rd");
    f_addr = t.add_field("addr", TRL_FT_U64);
    f_len  = t.add_field("len",  TRL_FT_U8);
    f_data = t.add_field("data", TRL_FT_U32);
    s      = trl_stream::create("axi_rd", null, t, "axi");   // path "axi_rd"
  endfunction

  task run();
    forever begin
      longint unsigned t0;
      @(posedge vif.arvalid); t0 = $time;
      @(posedge vif.rlast);
      `TRL_IF(s) begin
        v[f_addr] = vif.araddr; v[f_len] = vif.arlen; v[f_data] = vif.rdata;
        void'(s.record(t0, $time, v));
      end
    end
  endtask
endclass
```

### 8.2 Nested type and incremental fields, Tier 2

```systemverilog
trl_type hdr = trl_type::create("pkt_hdr");
int h_len  = hdr.add_field("len", TRL_FT_U16);
int h_kind = hdr.add_field("kind", TRL_FT_ENUM, 0, `TRL_ENUM(pkt_kind_e));

trl_type pkt  = trl_type::create("pkt");
int p_hdr   = pkt.add_struct("hdr", hdr);          // base index
int p_pay   = pkt.add_field("payload", TRL_FT_BYTES);

trl_txn x = new;                                     // reused
...
s.open_txn(pkt, .into(x));
x.set_int(p_hdr + h_len,  p.len);                   // nested access = integer add
x.set_int(p_hdr + h_kind, p.kind);
x.set_bytes(p_pay, p.data);
x.close();
```

### 8.3 Ad-hoc

```systemverilog
trl_stream dbg = trl_stream::create("dbg", env_s);   // no type → open schema
...
`TRL_IF(dbg) begin
  trl_txn x = dbg.open_txn();
  x.set_str(dbg.field("msg", TRL_FT_STRING), msg);    // SV cache hit after first use
  x.set_int(dbg.field("lvl", TRL_FT_U8), lvl);
  x.close();
end
```

### 8.4 Replay

```systemverilog
trl_reader  r  = trl_reader::get();                  // +trlog_read=golden.trl
trl_rstream rs = r.find_stream("axi_rd");
trl_type    t  = r.find_type("axi_rd");
int f_addr = t.index("addr"), f_data = t.index("data");
trl_cursor  c  = trl_cursor::create(r, rs);
while (c.next()) begin
  c.wait_start();
  drive_read(c.get_int(f_addr), c.get_int(f_data));
end
```

---

## 9. Open questions for review

| # | Question | Recommendation |
|---|---|---|
| Q1 | 4-state values wider than 64 bits | **Decided:** add `FT_LOGICVEC = 0x10` (§4.1). |
| Q2 | C/Python `FT_*` numbering and `FT_ENUM` encoding | **Decided:** Python is the reference. C is fixed in phase 1 (§10), with a C↔Python golden test for every `FT_*` kind, including `FT_LOGICVEC`. |
| Q3 | Blocks per database (mixed) or per stream? | **Decided:** per-stream blocks with spill-coalescing (§11.4), plus the stream-set index section (§7.2). |
| Q4 | How to encode stream-scoped type namespaces: qualified schema name `path::name`, or a new type-registry entry? | **Decided:** qualified name. No format change, and Python can parse it. |
| Q5 | Dynamic arrays of non-byte elements (`int q[$]`): `FT_BYTES` with an element-width hint, a new `FT_ARRAY`, or not supported? | **Decided (for now):** not supported in v1. Users can flatten into fixed arrays or pack into bytes. Revisit once there is a use case. |
| Q6 | Make L2 `trl_txn` objects optional for Tier 2 (raw-`chandle` functions at package scope)? | **Decided:** ship both. `trl_txn` classes are the convenient default; generation-checked `trl_txn_h` handles are the efficient form, for pipelined and non-class code (§12). |
| Q7 | Transactions still open at close: write them as truncated, or drop them? | **Decided:** write them with `trlog.truncated=1` metadata. |
| Q8 | Ship the UVM adapter (L3) in this phase? | **Decided:** a follow-up phase. `trlog_uvm_recorder::do_record_field` maps to Tier 3 with an SV-side `string→int` cache per recorder, and `uvm_bitstream_t` maps to `set_bits` with `TRL_MAX_BITS=UVM_MAX_STREAMBITS`. |
| Q9 | One `.so` for VPI and DPI? | **Decided:** yes, as long as VPI stays optional. How that is guaranteed is in §5.4. |

---

## 10. Implementation phases

1. **C fixes** (defects 1–5, and the Python reader bug in §11.5) plus golden tests for C↔Python parity on every
   `FT_*` kind.
2. **L0 recording** (`trl_rec.h`): database registry, types (flat, nested,
   enum, ad-hoc), streams with lazy hierarchy emission, slot pool,
   encode-on-close, auto-flush, `TR_BEGIN/ATTR/END/LINK/META`. Test from C and
   ctypes, and read back with the Python reader.
3. **L0 reading** (`trl_query.h`): name lookup, introspection, cursor with
   start-order merge, per-stream index use. Cross-check against the Python
   reader on the golden corpus.
4. **L1 + L2** (`trl_sv.c`, `trl_sv_dpi.svh`, `trl_pkg.sv`,
   `trl_macros.svh`, `trl_final.sv`). Smoke tests on Verilator and xezim
   (both are in `packages/`).
5. **Benchmarks:** extend `benchmarks/` with an SV transaction DUT. Measure
   per-transaction cost on Tiers 1, 2 and 3 against the budget in §3.2, and
   compare against a no-recording baseline and a disabled-stream run (the
   target is a difference within noise).
6. **L3 UVM adapter** (Q8).

---

## 11. Q3 analysis: block layout

### 11.1 The question

A `BLK_TXN_DATA` block can hold transactions from every stream (*mixed*) or
from just one (*per-stream*). The format already supports both, because index
entries carry `stream_id | 0xFFFFFFFF`. The choice affects six things:

| Axis | Mixed | Per-stream |
|---|---|---|
| Reading one stream (replay) | Decodes **every** block and filters | Decodes only that stream's blocks |
| Reading all streams in time order | One pass, plus the long-txn merge | Merge across S streams (heap, O(log S) per record). Up to S blocks in memory at once |
| Writer memory | One block buffer | S buffers. **Quiet streams never fill one**, so their records stay in memory until close |
| Crash / live-view latency | Bounded by one block | A quiet stream's records can stay unwritten for the whole run |
| Block count and overhead | Fixed: total / block size | Grows with S if buffers are flushed early. Each block costs about 75 B (header, payload header, zlib framing, index entry) and compresses worse when small |
| Compression | Interleaved schemas and values | Similar records next to each other, and column layout stays usable with mixed schemas. Txn-ID deltas grow (IDs are global), which costs a fraction of a byte per record |

So per-stream is better on the axis that matters most for this package
(replay), and worse on writer memory and latency. The question is whether a
flush policy can win the reading benefit back without paying for it in
memory or block count.

### 11.2 Experiment

The scripts are `benchmarks/block_layout/q3_policies.py` and
`q3_spill_coalesce.py`, and the raw results are in
`benchmarks/block_layout/results_2026-11-21.jsonl`. They use the Python
reference writer and reader: 200k transactions, block target 2000 records,
memory cap 16k records (8 blocks). Each record has four fields:
`addr` (sequential per stream), `data` (random 32-bit), `len` and `kind`.
Records are emitted in end-time order (write-on-close) and sorted by start
time inside each block. Stream rates follow a Zipf distribution.

* **S1**: 8 streams, uniform rates.
* **S2**: 64 streams, Zipf 1.2, 4 schemas.
* **S3**: 1000 streams, Zipf 1.2, 8 schemas.

Policies compared:

* **mixed**: one buffer for all streams.
* **per_stream**: unbounded buffer per stream.
* **per_stream_cap**: per stream; when the cap is hit, flush the largest
  buffer.
* **pooled**: streams share a mixed buffer until they make up ≥10% of a
  flushed block, then get a dedicated buffer.
* **spill_coalesce**: per stream; when the cap is hit, flush the largest
  buffer if it holds ≥25% of a block. Otherwise write *all* buffers together
  as multi-stream blocks, with the stream set recorded in the index.

Results, all with `adaptive` layout. File size is relative to mixed. "Peak
buf" is the largest number of records held in memory at once. "Rd" is the
number of blocks decoded to read one stream.

| Scenario | Policy | File size | Blocks | Peak buf | Rd heavy | Rd median | Rd quiet |
|---|---|---|---|---|---|---|---|
| S1 (8 × uniform) | mixed | 1.94 MB | 100 | 2 000 | 100 | — | 100 |
| | per_stream | **±0%** | 104 | 15 367 | 13 | — | 13 |
| S2 (64 × Zipf) | mixed | 2.08 MB | 100 | 2 000 | 100 | 100 | 100 |
| | per_stream | −1.2% | 137 | **54 026** | 30 | — | 1 |
| | per_stream_cap | +3.7% | 384 | 16 000 | 89 | 2 | 1 |
| | pooled | −0.7% | 104 | 11 862 | 71 | — | 41 |
| | **spill_coalesce** | +2.2% | 243 | 16 000 | 54 | 3 | 3 |
| S3 (1000 × Zipf) | mixed | 2.23 MB | 100 | 2 000 | 100 | 100 | 100 |
| | per_stream | +5.4% | 1 056 | **90 600** | 24 | — | 1 |
| | per_stream_cap | **+33%** | **4 118** | 16 000 | 681 | 1 | 1 |
| | pooled | +0.5% | 102 | 7 252 | 86 | — | 62 |
| | **spill_coalesce** | +4.7% | 759 | 16 000 | 40 | 5 | 3 |

In S2 the heavy stream holds 30% of all transactions; in S3 it holds 23%.
Decode time in Python tracked the number of decoded records. For example,
reading the quiet stream in S3 took about 1.07 s mixed, 0.02 s with
spill_coalesce, and 0.001 s with per_stream. Write time was within noise
across policies. Row layout showed the same ordering, with files about 13%
larger.

### 11.3 What the numbers say

1. **Compression is not the deciding factor.** Unbounded per-stream ranges
   from −1% to +5% on file size against mixed. The cost of larger txn-ID
   deltas roughly cancels the gain from grouping similar records. Note that
   these schemas all have the same field layout, which favours mixed column
   encoding. With really heterogeneous schemas, per-stream should do better.
2. **Single-stream reads are the big win:** 2–3× fewer blocks for heavy
   streams, and 30–100× fewer for median or quiet streams. For a quiet stream,
   mixed costs as much as reading the whole file.
3. **Unbounded per-stream is not acceptable for memory.** With many quiet
   streams, 27–45% of the whole trace sat in writer memory, and it is lost if
   the simulation crashes.
4. **Capping by flushing the largest buffer breaks down when there are many
   streams.** With 1000 streams it produced 4118 mostly tiny blocks: +33% file
   size, and 681 blocks to read the heavy stream.
5. **Pooling gives up the benefit** where it matters most: quiet streams live
   in shared blocks, so reading one still touches 41–62 blocks.
6. **spill_coalesce holds most of the benefit at bounded cost.** File size is
   within 5% of mixed, the block count is moderate, memory is capped, and a
   quiet or median stream reads 3–5 blocks. Heavy streams read about 1.5–2×
   more blocks than with unbounded per-stream, because spills sometimes catch
   a heavy buffer while it is small.

### 11.4 Decision

**Per-stream blocks with spill-coalescing** (accepted 2026-11-21). The policy:

* Each stream has its own buffer. A buffer that reaches `+trlog_block_kb`
  becomes a single-stream block.
* When total buffered bytes exceed `+trlog_buf_mb`:
  * if the largest buffer is ≥25% of a block, flush it on its own;
  * otherwise write all buffers together as multi-stream blocks, each up to
    the block size.
* `+trlog_max_age`: a buffer whose oldest record is older than the limit
  joins the next spill. This bounds what a crash can lose and how stale a
  live view can be. It is off by default.
* `trl_pkg::trl_db::flush()` forces a full spill. Use it at checkpoints.

Format cost: one additive index section, the stream-set entry (sentinel
`0x05`, §7.2), so multi-stream blocks can be skipped by readers that don't
need them. No change to block payloads.

Things still open for when L0 is implemented in C (they don't change the
decision):

* Tune the 25% threshold and the cap defaults on a realistic UVM-style trace
  instead of synthetic random `data`.
* Measure the reader-side cost of merging all streams in time order, which
  the Python experiment did not model.
* Check whether per-stream txn-ID deltas are worth encoding against the
  stream's previous ID. That would need a flag bit in the block.

### 11.5 Bug found along the way

`IndexBlock.find_txn_blocks_for_stream` (`python/src/trlog/_index.py`) skips
`0xFFFFFFFF` (mixed) entries as soon as *any* entry has a real stream ID. A
file that contains both per-stream and mixed blocks therefore silently drops
records when one stream is read. Today's public writer API can already produce
such a file, by calling `begin_txn_block` both with and without
`stream_inst_id`. The fix is to always include mixed entries, and, after this
design, to include multi-stream entries whose stream set contains the queried
ID. This goes in phase 1.

---

## 12. Q6 analysis: `trl_txn` objects or plain handles

### 12.1 The two forms

```systemverilog
// (a) object form (§6.2)
trl_txn x = s.open_txn(t);
x.set_int(f_addr, a);
x.close();

// (b) handle form: package-scope functions on a value-type handle
trl_pkg::trl_txn_h x = trl_pkg::open_txn(s, t);
trl_pkg::set_int(x, f_addr, a);
trl_pkg::close_txn(x);
```

### 12.2 Tradeoffs

| Concern | `trl_txn` object | Plain handle |
|---|---|---|
| **Cost per transaction** | One class allocation per `open_txn`, unless the caller reuses an object (`.into(x)`). The simulator's allocator and its GC or refcounting both run on every transaction. Verilator's class references are refcounted, and the count is atomic under `--threads`. | None. The handle is a pointer-sized value |
| Cost per call | A non-virtual method call plus the `h != null` check. Negligible next to the DPI crossing | The same check inside the package function |
| **Disabled path** | Needs care: an allocating `open_txn` breaks the "no SV objects when disabled" rule. Fixed in rev 3 by returning a shared inert object (§6.2) | Naturally free: the handle is just `null` |
| **Stale-handle safety** | `close()` nulls *this object's* handle, so a second `close()` or `set_*` on the same object does nothing. Other references to the same object see the nulled handle too | A copied handle still points at the C slot after `close_txn`. The slot is reused by the next `open_txn`, so a late `set_int` through the old copy **silently writes into some other transaction**. This is the main risk of the handle form |
| Outstanding / pipelined transactions | `trl_txn pending[int]`: works, and each entry is an allocation | `trl_txn_h pending[int]`: works, no allocation |
| Where it can be used | Anywhere classes are allowed (that includes modules and interfaces) | Anywhere, including in structs, packed into queues with other data, and in functions declared outside classes |
| Readability | Methods (`x.set_int`), which matches UVM style | Free functions (`trl_pkg::set_int(x, …)`), wordier unless the package is imported with `::*` |
| Leaks (never closed) | Same in both forms: SV has no destructors, so nothing closes the C slot. Q7 writes leftover transactions as truncated at close | Same |
| API surface | — | A second set of about 12 functions to document and test. They are one-line wrappers of L1 |

The reuse pattern (`.into(x)`) already makes the object form allocation-free
for the common "one transaction outstanding at a time" monitor. Where
the object form really costs something is code with **many outstanding
transactions** (pipelined buses, out-of-order completion), where reuse needs a
user-managed pool.

### 12.3 Making the handle form safe

Generation-checked handles fix the stale-handle problem at almost no cost:

* `trl_txn_h` is an unpacked struct `{ chandle slot; int unsigned gen; }`, not a
  bare `chandle`. Passing it by value copies 12–16 bytes. Making it a distinct
  type also stops a stream or type `chandle` from being passed by mistake,
  which a bare `chandle` API cannot catch.
* Each C slot carries a generation counter, incremented when the slot is
  freed. Every L1 call that takes a transaction also takes `gen`, and C
  compares it with the slot's counter: one integer compare and a predictable
  branch. On a mismatch the call does nothing and counts the event. Under
  `+trlog_check` it also reports the event.
* This makes a late write through a stale copy a counted no-op rather than
  silent corruption. It costs one extra `int` argument per call, which is
  passed in a register.

The object form uses the same generation check internally, so both forms
behave the same on misuse.

### 12.4 Decision

Accepted 2026-11-21: ship both. Classes trade some efficiency for convenience,
and handles trade convenience for efficiency. The object form is the
documented default:

* `trl_txn` stays the primary API. It is idiomatic, and with `.into(x)` reuse
  plus the shared inert object it allocates nothing on the common paths.
* The handle form (`trl_txn_h` plus package functions) is documented as the
  zero-allocation API for pipelined or high-outstanding-count code and for
  non-class code.
* Both sit on generation-checked L1 calls, so neither form can corrupt another
  transaction through a stale reference.

The only thing given up is the extra API surface, about 12 one-line wrappers.
Before committing to that surface, the per-transaction cost of allocating a
`trl_txn` can be measured on Verilator and xezim (phase 5 benchmarks). If the
measured cost turns out negligible, the handle form can be dropped without
affecting anything else.
