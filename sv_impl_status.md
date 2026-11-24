# SV API implementation: plan and status

Design: [`sv_api_design.md`](sv_api_design.md) (rev 3, all review questions decided).
The phases come from §10 of the design. This file is updated as each step finishes.

## Phase 1: C fixes and C↔Python parity (done, 2026-11-21)

| # | Step | Status |
|---|---|---|
| 1.1 | Defect 1: C `trl_field_type_t` now uses the Python numbering (`BITVEC=0x0C`, `BYTES=0x0D`, `TIME=0x0E`, `ENUM=0x0F`). `FT_LOGICVEC=0x10` is added on both sides. `trl_attr_t` gains a `vec {data,len,width}` union member. | done |
| 1.2 | Defect 2: C encodes `FT_ENUM` as a uvarint. | done |
| 1.3 | Defect 3: records are encoded into the block buffer inside `trl_txn_full`, so no caller pointer is kept. | done |
| 1.4 | Defect 4: no `malloc` per record. A single `recs` buffer is reused from block to block, and a failed add rolls back to the last complete record. | done |
| 1.5 | Defect 5: index entries carry a stream ID. The new `trl_txn_begin_block_stream()` writes a single-stream block, and C encodes and decodes index section 0x02. A record from a different stream returns the new `TRL_ERR_INVALID`. | done |
| 1.6 | Python §11.5: `find_txn_blocks_for_stream` always includes mixed blocks. | done |
| 1.7 | Golden test: the `txn_field_kinds` corpus entry and fixture cover all 17 `FT_*` kinds plus a mixed block and a per-stream block. Three checks use it: the C `test_golden` build must match the fixture byte-for-byte, the Python reader decodes it, and the ctypes writer must match it. | done |

### Files changed

- C: `c/include/trl/trl.h`, `c/src/trl_internal.h`, `c/src/txn_data.c`,
  `c/src/index.c`, `c/src/writer.c`, `c/tests/test_golden.c`
- Python: `python/src/trlog/_types.py`, `_codec.py`, `_index.py`, `_native.py`
- Tests: `tests/golden/corpus.py`, `tests/golden/fixtures/txn_field_kinds.trl` (new),
  `tests/integration/test_field_kinds.py` (new),
  `tests/integration/test_multi_stream_txn.py`, `tests/unit/test_index_stream.py`

### Verification

Logs are in `logs/p1_*.log`. `logs/` is untracked and not in `.gitignore`.

- `ctest` (build/c): 2/2 pass. `test_golden` now requires a byte match for
  `empty`, `txn_row`, `txn_schema_attrs` and `txn_field_kinds`. All four match.
  The VC and hierarchy entries still diverge (informational, pre-existing).
- valgrind on `test_golden`: 0 errors, no leaks.
- pytest (pure Python, with libtrl.so available): 412 passed, 33 skipped
  (baseline was 404 passed / 33 skipped).
- pytest with `TRL_USE_CTYPES_NATIVE=1`: 48 failures, against 45 at HEAD. The 3
  extra are new tests that use APIs the ctypes binding has never had
  (`_CtypesReader.iter_txn_blocks`, `_CtypesWriter.add_stream_type`). Nothing
  that passed before regressed.
- The two new index regression tests fail on the old `_index.py` and pass on
  the fix (checked).

### Notes found along the way

- `trl_txn_meta` is declared in `trl.h` but has no implementation. Calling it
  would fail at link time. It will be implemented in phase 2 (TR_META).
- The multi-stream index section 0x05 (Q3, spill-coalesce) is phase 2 work,
  since the C writer does not yet produce multi-stream blocks.
  `find_txn_blocks_for_stream` will need to consult it then.

## Phase 2: L0 recording (`trl_rec.h`) (done, 2026-11-21)

See design §7.1–7.2 and §10. `trl_rec.h` is a session layer over the
existing block-level writer: it owns the db/type/stream registry, string
interning passthrough, lazy stream declaration, and open-transaction
handles, so a caller never deals with schema ids, stream ids or block
boundaries directly.

| # | Step | Status |
|---|---|---|
| 2.1 | `trl_db_t`: named registry (`trl_db_open/find/close`), wraps one `trl_writer_t`, `trl_db_writer()` for VC/VPI sharing, `trl_db_intern()`. | done |
| 2.2 | Types: `trl_rtype_t` with flat/struct/array fields (struct and array flatten into the flat list at call time, e.g. `hdr.len`, `tag[1]`), `trl_type_seal` registers the schema. Ad-hoc types via `trl_type_field_get_or_add`, which grows and re-registers the schema on demand. | done |
| 2.3 | Streams: `trl_stream_t` with a dotted qualified path (`env.axi`), an immediately-assigned `stream_inst_id`, and `stream_include`/`stream_exclude` glob filtering (`fnmatch`), gated by the parent's enabled state too. | done |
| 2.4 | Lazy hierarchy declaration: each stream's `H_STREAM` entry is deferred to a flush right before that stream's first record, so a crash never leaves a referenced stream undeclared. Flushes use an explicit, db-owned `stream_inst_id` (new `trl_hierarchy_add_stream_explicit`) so ids stay stable across the several hierarchy blocks a long session produces. | done |
| 2.5 | Per-stream buffers and auto-flush: each stream owns its own `trl_txn_block_t`, flushed as its own single-stream block (phase 1's `trl_txn_begin_block_stream`) at a configurable byte threshold and at close. Streams no longer contend over one shared active block. | done |
| 2.6 | New wire-level record kinds in `txn_data.c`: `trl_txn_block_add_begin/attr/end/link/meta`, each delta-encoded against the block's running `(prev_txn_id, prev_time)` state exactly as `tests/golden/../_txn_data.py`'s `_advance_state`/`_encode_record` do. `TR_ATTR` carries an inline field-type byte (Python's `_encode_attr_value_raw`), unlike `TR_FULL`'s schema-resolved attrs. | done |
| 2.7 | Recording API: `trl_rec_full` (one-shot Tier 1, bit-pattern `vals[]`) and `trl_rec_begin`/`set_u64/i64/f64/str/str_id/bits/logic/bytes`/`link`/`meta`/`end` (incremental Tier 2), plus `trl_rec_link_ids` for a source-less link, written into a small db-level MIXED buffer. `trl_txn_t` handles are pooled (a freelist on `trl_db_t`), not malloc'd per transaction. | done |
| 2.8 | Closed a phase-1 gap along the way: the low-level `trl_txn_meta` (declared in `trl.h`, never implemented) now writes a `TR_META` record into the writer's active block. | done |

### Scope trimmed from the design (see the note at the top of `trl_rec.h`)

- Enum labels (`trl_renum_t`) are in-memory only; no `TYPE_TAG_ENUM` wire
  entry yet. `FT_ENUM` values are still plain integers, as in phase 1.
- A stream's `kind` string and parent nesting are not written to the
  hierarchy; streams are declared flat (one `HK_BEHAVIORAL` tree per db,
  `H_STREAM`'s `stream_type_id` is always 0). `trl_stream_find` and
  qualified type names still use the dotted path, so this only affects the
  on-disk hierarchy shape, not lookups. `find_streams(kind=...)` is
  therefore not usable against phase-2-written files.
- `trl_type_num_members`/`trl_type_member` (nested-type introspection) are
  not implemented; the flat `field_index`/`name`/`type`/`width` accessors
  cover everything `trl_rec.h` itself needs, and the design's planned
  readers (trl_query.h, phase 3) can recover the nested view from the
  `outer.inner`/`name[i]` field-name convention.
- The §7.2 stream-set index section (0x05), which coalesces several small
  per-stream buffers into one multi-stream block, is not implemented. Each
  stream's buffer is flushed as its own single-stream block instead —
  correct (and already covered by phase 1's per-stream index), just less
  dense on disk for many low-traffic streams. `find_txn_blocks_for_stream`
  does not need to change for this.

### Files changed

- C: `c/include/trl/trl_rec.h` (new), `c/src/rec.c` (new), `c/src/trl_internal.h`,
  `c/src/txn_data.c`, `c/src/hierarchy.c`, `c/src/writer.c`, `c/CMakeLists.txt`
- Tests: `c/tests/test_rec.c` (new), `c/tests/CMakeLists.txt`,
  `tests/integration/test_rec_session.py` (new)

### Verification

Logs are in `logs/p2_*.log` (untracked, as `logs/p1_*.log` were).

- `ctest` (build/c): 3/3 pass (`c_golden`, `c_rec`, `c_codec`).
- `test_golden` byte match is unchanged from phase 1: `empty`, `txn_row`,
  `txn_schema_attrs`, `txn_field_kinds` MATCH; the pre-existing VC/hierarchy
  DIVERGE entries are unaffected.
- valgrind on `test_rec`: 0 errors, no leaks.
- pytest: 417 passed, 33 skipped (412 passed before; the 5 new tests in
  `test_rec_session.py` account for the difference, no regressions).
  `test_rec_session.py` reads back the file `c/tests/test_rec.c` writes
  during `ctest` and is skipped if that file isn't present yet.

### Notes for phase 3

- `trl_query.h` (L0 reading) needs the start-order cursor merge (§7.3) and
  will be the first place `TR_BEGIN`/`TR_ATTR`/`TR_END` pairs that span
  blocks actually get joined back into one transaction — phase 2's writer
  already supports a transaction opened in one block and closed in a later
  one (nothing stops a stream's buffer from flushing between `trl_rec_begin`
  and `trl_rec_end`), but nothing joins them back on read yet. The existing
  Python `TrlReader.iter_txn_blocks` likewise yields raw per-tag records,
  not joined transactions, which is what `tests/integration/test_rec_session.py`
  works with directly.
- If stream "kind" and nested scope placement end up mattering for L1/L2
  (UVM recorder kind filtering, scope-based browsing), revisit the two
  hierarchy simplifications above together, since both touch how `rec.c`
  calls into `hierarchy.c`.

## Phase 3: L0 reading (`trl_query.h`) (done, 2026-11-21)

See design §7.3 and §10. `trl_query.h` is the read-side counterpart to
`trl_rec.h`: name lookup for streams and transaction schemas, and a cursor
that joins `TR_BEGIN`/`TR_ATTR`/`TR_END` (and attaches `TR_LINK`/`TR_META`)
back into one transaction per `txn_id`, emitted in `(start, txn_id)` order.

| # | Step | Status |
|---|---|---|
| 3.1 | Reader infrastructure gap closed along the way: `trl_reader_t` never decoded `BLK_STR_TABLE` (reader.c had no case for it at all), so no string could be resolved. Added the `strtab` field and its decode/free. | done |
| 3.2 | Hierarchy decode gap closed: `trl_hierarchy_decode_payload` discarded `H_STREAM` entries. Added `trl_hierarchy_decode_streams`, which collects `{stream_inst_id, stream_type_id, name_id}` instead, driven by the index's `hier_offsets` (no extra linear scan). | done |
| 3.3 | `trl_rdb_t`: named registry (`trl_rdb_open/find/close`), wraps a `trl_reader_t`. Builds the stream table from every `H_STREAM` entry (path already qualified, per `trl_rec.h`'s phase-2 trim) and the schema table from every `BLK_TYPE_REG` entry (including superseded ad-hoc versions). | done |
| 3.4 | Name lookup and introspection: `trl_rdb_stream_find`/`stream_at`, `trl_rdb_schema_find`/`schema_by_id`, and `trl_rschema_num_fields`/`field_index`/`field_name`/`field_type`. | done |
| 3.5 | Transaction-record decode in `query.c`, the mirror image of `txn_data.c`'s encoder: full attr-value decode for every `FT_*` kind, and the delta-state walk for `TR_FULL/BEGIN/ATTR/END/LINK/META`. | done |
| 3.6 | `trl_cursor_t`: merges the selected per-stream (and always-included mixed) index entries in file-offset order, joins `TR_BEGIN`/`TR_ATTR`/`TR_END` by `txn_id` (so a transaction can span several of one stream's auto-flushed blocks), attaches `TR_LINK`/`TR_META`, and emits in `(start, txn_id)` order via `trl_cursor_next`. | done |
| 3.7 | **Bug fix in `trl_rec.h` (phase 2), found by this phase's own test:** `trl_rec_set_*`, `trl_rec_link`, `trl_rec_meta` and `trl_rec_end` wrote into `txn->stream->blk` without first calling `ensure_stream_active`. If the stream's block auto-flushed between a `trl_rec_begin` and a later call on the same open `txn_t` (always possible once `block_bytes` is small enough, or a transaction's own calls are large enough), those later writes landed in a stale, already-encoded buffer instead of a fresh block. Fixed by calling `ensure_stream_active(txn->stream, txn->start)` (a new `start` field on `trl_txn_t`, captured at `trl_rec_begin`) before each of those four writes, matching what `trl_rec_full`/`trl_rec_begin` already did. | done |

### Scope trimmed from the design (see the note at the top of `trl_query.h`)

- The read-side schema type is `trl_rschema_t`, not `trl_rtype_t`. The design
  sketch reuses `trl_rtype_t` for both recording and reading, but
  `trl_rec.h`'s `trl_rtype_t` is a distinct, non-interchangeable write-side
  struct; a different name keeps a write-side handle from silently compiling
  into a read-side accessor.
- `trl_cursor_new`/`trl_cursor_add_stream` materialize the whole joined
  transaction list in one pass over the selected blocks, rather than the
  design's incremental, bounded-memory min-heap of per-block sub-cursors.
  Correct for the traces this phase's tests exercise; the open-transaction
  lookup during the join is also linear, not hashed. Revisit both for very
  large per-stream traces.
- `trl_cursor_seek(time)` is not implemented; iterate with `trl_cursor_next`
  and compare `start` instead.
- A stream's default/declared type cannot be resolved from the hierarchy
  (`trl_rec.h`'s phase-2 trim leaves `H_STREAM.stream_type_id` always 0);
  use the cursor's per-transaction `txn_type_id` with `trl_rdb_schema_by_id`
  instead of a stream-level accessor.
- A transaction left open at file close (no `TR_END` seen) is reported with
  `end == UINT64_MAX`. `trl_rec.h` does not write the `trlog.truncated=1`
  metadata the design calls for (§9 Q7) — tracking which `trl_txn_t` handles
  are still open at `trl_db_close` would need its own live-handle list, not
  just the existing ended-transaction freelist — so that key is never
  present to check. Noted as a gap for a future pass over `trl_rec.h`, not
  implemented here.
- Only the row-layout, delta-encoded `BLK_TXN_DATA` payload (what the C
  writer always produces) is decoded; the column-oriented layout
  (`TRL_FLAG_TXN_COLUMN`) is not supported, since nothing in this codebase
  writes it yet.

### Files changed

- C: `c/include/trl/trl_query.h` (new), `c/src/query.c` (new),
  `c/src/trl_internal.h`, `c/src/hierarchy.c`, `c/src/reader.c`,
  `c/src/rec.c` (the `ensure_stream_active` fix above), `c/CMakeLists.txt`
- Tests: `c/tests/test_query.c` (new), `c/tests/CMakeLists.txt`,
  `tests/integration/test_query_cursor.py` (new)

### Verification

Logs are in `logs/p3_*.log` (untracked, as the earlier phases' were).

- `ctest` (build/c): 4/4 pass (`c_golden`, `c_rec`, `c_query`, `c_codec`).
- `test_golden` byte match is unchanged from phase 1/2: `empty`, `txn_row`,
  `txn_schema_attrs`, `txn_field_kinds` MATCH; the pre-existing VC/hierarchy
  DIVERGE entries are unaffected.
- valgrind: 0 errors, no leaks on `test_query`, and re-run on `test_rec` and
  `test_golden` after the `rec.c` fix — still clean.
- pytest: 419 passed, 33 skipped (417 passed before; the 2 new tests in
  `test_query_cursor.py` account for the difference, no regressions).
  `test_query_cursor.py` independently re-derives the joined transactions
  from the raw per-tag records the pure-Python reader returns (there is no
  reference cursor on the Python side to diff against — phase 2 already
  noted `iter_txn_blocks` does no joining) and checks the C cursor's answer
  against that from-first-principles reconstruction, including the
  deliberately cross-block-split transaction.
- `c/tests/test_query.c` uses `block_bytes = 1` specifically to force a
  stream's buffer to auto-flush between a transaction's `begin` and its
  later `set`/`link`/`meta`/`end` calls, which is what exposed the 3.7 bug
  above; the resulting trace is exactly the kind of case the design's
  "Start-time order" note (§7.3) describes.

### Notes for phase 4

- `trl_sv.c`/`trlog_dpi_pkg.sv` (L1) will be the first caller of
  `trl_query.h` from outside a C test, so its DPI surface should mirror
  `trl_cursor_next`'s `trl_txn_hdr_t` shape rather than re-deriving its own.
- The §9 Q7 "truncated" gap noted above is worth closing before relying on
  open-at-close transactions in SV-level tests; right now they are simply
  reported as open (`end == UINT64_MAX`) with no `trlog.truncated` marker.

## Phase 4: L1 + L2 (`trl_sv.c`, `trlog_dpi_pkg.sv`, `trlog_pkg.sv`, `trlog_macros.svh`, `trlog_final.sv`) (done, 2026-11-21)

This is the SV-facing layer: a thin, non-`context` DPI-C glue file over
`trl_rec.h`/`trl_query.h` (L1), and the class-based `trlog_pkg` (L2) that
turns those raw imports into the db/type_h/stream/txn/reader/cursor API
`sv_pkg_plan.md` asked for. Verified against a real DPI boundary with
Verilator, not just linted.

| # | Step | Outcome |
|---|---|---|
| 4.1 | `c/src/trl_sv.c` | New. Plain-C ABI (`chandle`→`void*`, `string`→`char*`), one function per `trl_rec.h`/`trl_query.h` entry point this phase keeps. No `svdpi.h` dependency at all — every argument type maps without it — so the file (and its `trl_sv` CMake target) builds on any machine, not just one with a DPI simulator installed. |
| 4.2 | `trl_rdb_string_lookup` | New, small addition to `trl_query.h`/`query.c`. Needed to resolve `TR_META`/`TR_LINK` string ids on the read side (`trl_sv_cur_meta_at`/`trl_sv_cur_link` return ids; `trl_sv_string_lookup` resolves them) — a one-function gap phase 3 left because nothing needed it yet. |
| 4.3 | `c/tests/test_sv.c` (`ctest c_sv`) | New. Calls the `trl_sv_*` exports directly with their plain-C signatures (no simulator), covering write→close→reopen→cursor the same way `test_rec.c`/`test_query.c` do for L0. |
| 4.4 | `sv/src/trlog_dpi_pkg.sv` | New. 1:1 imports onto every `trl_sv_*` export. |
| 4.5 | `sv/src/trlog_pkg.sv` | New. Classes `db`, `type_h` (write-side schema), `schema_h` (read-side introspection, deliberately not shared with `type_h` — see 4.7), `txn`, `stream`, `rstream`, `reader`, `cursor`, plus package-level `enabled()`/`now()`/`shutdown()`. |
| 4.6 | `sv/src/trlog_macros.svh`, `sv/src/trlog_final.sv` | New. `TRLOG_TIMEUNIT`, `` `TRLOG_IF ``, and the `final`-block shutdown module from §5.2 option 2. |
| 4.7 | A real bug found in `c/src/txn_data.c` | `trl_txn_block_encode`'s `TRL_COMPRESS_ZLIB` path zlib-compressed the row payload directly, with no length prefix — correct, and it matches Python's own `_txn_data.py` convention for this one block type (`zlib.compress(payload)`/`zlib.decompress(payload)`, no prefix, unlike `BLK_HIERARCHY` and the other metadata blocks, which do prepend one). But `query.c`'s `cursor_build` (phase 3) assumed the hierarchy-style 8-byte prefix for every compressed block, including `BLK_TXN_DATA`. The mismatch meant any cursor read of a trace written with `compress=zlib` for its txn blocks silently returned zero transactions — found via the Verilator smoke test's end-to-end run (the SV smoke test calls `db::get()`, whose lazy-init plusarg path defaults to zlib, unlike every prior C test which explicitly used `TRL_COMPRESS_NONE`). Fixed `cursor_build` to decompress the same way `trl_vc_iter_payload` already does for `BLK_VC_DATA`: grow-and-retry with `uncompress()` until the output buffer is big enough, since the true output size isn't known up front for this block type. |
| 4.8 | `sv/tests/top_smoke.sv`, `sv/tests/test_sv_smoke.py` | New. Compiles with `verilator --binary --sv --timing`, linking `-ltrl_sv -ltrl` from `build/c`; writes one tier-2 (`open_txn`/`set_int`/`meta`/`close`) and one tier-1 (`record()`) transaction, closes, reopens read-only, and checks the cursor's joined output, printing a `SMOKE: PASS`/`FAIL` marker the pytest wrapper greps for. |

### Scope trimmed from the design

- No enum types (`enum_h`), no nested-struct/array field declaration
  (`type_h::add_struct/add_array`), no wide-vector fields
  (`set_bits`/`set_logic`/`set_bytes` and the matching cursor
  `get_bits`/`get_logic`/`get_bytes`). `trl_rec.h` already supports the
  first two; none of this phase's call sites needed them, and the vector
  setters need an SV open-array/packed-vector argument that's easy to get
  wrong without more interactive simulator testing than this phase did.
- **Tier 1 `stream::record()` is not one DPI call on Verilator.** The
  design's `trl_sv_record(..., input longint unsigned v[], int n)` exists
  in `trl_sv.c`/`trlog_dpi_pkg.sv` and is exercised by `test_sv.c`'s
  plain-C-ABI check, but Verilator 5.041 cannot pass a *dynamic* SV array
  through a DPI open-array argument (`V3Task.cpp:1105: Passing dynamic
  array or queue as actual argument to DPI open array is not yet
  supported`), and an unsized `v[]` formal parameter becomes a dynamic
  array the moment a caller's actual array is bound to it. `trlog_pkg.sv`'s
  `stream::record()` therefore composes `open_txn`/`set_int`/`close`
  instead (k+2 crossings, not 1) — correct, just not the design's budget on
  this simulator. Revisit if a future Verilator release lifts the
  limitation, or expose a fixed-max-width array variant.
- `cursor::seek` is not implemented (neither is `trl_cursor_seek` in
  `trl_query.h`, phase 3's trim).
- Lazy init (`db::get()`) only reads `+trlog_file`, `+trlog_off`,
  `+trlog_compress`, `+trlog_block_kb`, `+trlog_read[_<name>]`. Deferred:
  `+trlog_streams[_off]` (`trl_sv_db_open`'s fixed argument list, which
  matches the design's own §6.1 signature, has no slot for stream globs),
  `+trlog_buf_mb`, `+trlog_max_age`, `+trlog_check` (none of `trl_rec.h`'s
  memory cap / max-age flush / handle-checking exist yet), and
  `+trlog_verbose`/`+trlog_signals` (VPI signal dumping is a separate,
  untouched subsystem this phase).
- No VPI sharing (§5.4): `trl_sv_db_open` goes through `trl_db_open`'s
  existing process-wide name registry, which is already enough for DPI and
  VPI to share one database by name, but nothing here `dlsym`s `vpi_*` or
  touches `trlog_vpi_state.c`'s registry directly.
- `type_h::num_members`/`member` (nested-type introspection) are not
  exposed, matching `trl_rec.h`'s own phase-2 trim.

### Files changed

- New: `c/src/trl_sv.c`, `c/tests/test_sv.c`, `sv/src/trlog_dpi_pkg.sv`,
  `sv/src/trlog_pkg.sv`, `sv/src/trlog_macros.svh`, `sv/src/trlog_final.sv`,
  `sv/tests/top_smoke.sv`, `sv/tests/test_sv_smoke.py`
- Modified: `c/CMakeLists.txt` (new `trl_sv` target), `c/tests/CMakeLists.txt`
  (new `c_sv` test), `c/include/trl/trl_query.h`/`c/src/query.c`
  (`trl_rdb_string_lookup`), `c/src/txn_data.c` is unchanged — the fix in
  4.7 is entirely in `c/src/query.c`'s `cursor_build`.

### Verification

- `ctest` (build/c): 5/5 pass (`c_golden`, `c_rec`, `c_query`, `c_sv`,
  `c_codec`).
- `test_golden` byte match is unchanged from phases 1-3: `empty`,
  `txn_row`, `txn_schema_attrs`, `txn_field_kinds` MATCH; the pre-existing
  VC/hierarchy DIVERGE entries are unaffected.
- valgrind: 0 errors, no leaks on `test_sv`; re-run on `test_golden`,
  `test_rec`, `test_query` after the `query.c` fix — still clean.
- pytest (`python/src` + `TRL_LIBRARY_PATH`): 419 passed, 33 skipped —
  unchanged from phase 3 (this phase added no new C-reference Python
  tests; `sv/tests/test_sv_smoke.py` is a separate pytest file outside
  `tests/`, run directly: `packages/python/bin/python -m pytest
  sv/tests/test_sv_smoke.py`).
- `sv/tests/test_sv_smoke.py`: 1 passed. Compiles and runs
  `sv/tests/top_smoke.sv` through Verilator 5.041 against the real
  `libtrl_sv.so`/`libtrl.so`, i.e. an actual DPI crossing, not a mock —
  this is what caught 4.7's bug, which none of the plain-C tests could
  have (they all used `TRL_COMPRESS_NONE` explicitly; only `db::get()`'s
  lazy-init default of zlib exercised the broken path).

### Notes for phase 5 (if any) / general

- The design's L3 UVM adapter now exists as `sv/src/trl_uvm_pkg.sv`
  (`trl_uvm_tr_database`/`trl_uvm_tr_stream`/`trl_uvm_recorder`). It was built
  with the sv-weave recorder plugin; see `research/sv_gen_impl.md` (phase 1b).
  Tier-3 `stream::field()` is exercised by its generic path. Fix found on
  the way: a txn opened on a field-less ad-hoc type recorded schema id 0
  (`trl_rec_begin` now registers the empty schema).
- Before using `stream::field()` (tier 3 ad-hoc) or wide-vector fields in
  anything beyond a smoke test, close the trims listed above first,
  especially the tier-1 `record()` Verilator limitation if a benchmark
  depends on its 1-DPI-call budget.
- xezim smoke-testing (mentioned in the original phase-4 scope) was not
  attempted: no xezim installation was available in this session's
  environment, and the Verilator smoke test already exercises the same
  DPI surface end-to-end. Revisit if xezim-specific DPI behavior needs
  checking (e.g. a different `svdpi` ABI quirk).

## Status notes

- 2026-11-21: phase 1 complete. Nothing has been committed (the user handles commits).
- 2026-11-21: phase 2 complete. Nothing has been committed (the user handles commits).
- 2026-11-21: phase 3 complete, including a phase-2 bug fix (see 3.7). Nothing has been committed (the user handles commits).
- 2026-11-21: phase 4 complete, including a phase-3 bug fix found by the new Verilator smoke test (see 4.7). Nothing has been committed (the user handles commits).

## Phase 4b: package restructure (2026-11-23)

Reworked the SV package for how SV users actually consume it (`import trl_pkg::*;`):

- **Renamed to the C API's `trl_` convention**: `trlog_pkg`→`trl_pkg`
  (`sv/src/trl_pkg.sv`), `trlog_macros.svh`→`trl_macros.svh`,
  `trlog_final`→`trl_final`, macros `TRLOG_IF`/`TRLOG_TIMEUNIT`→`TRL_IF`/`TRL_TIMEUNIT`.
  Runtime plusargs keep the product name (`+trlog_file`, `+trlog_off`, ...).

- **One class per `.svh`**, each `include`d into `sv/src/trl_pkg.sv`:
  `trl_db.svh`, `trl_type.svh`, `trl_txn.svh`, `trl_stream.svh`,
  `trl_rstream.svh`, `trl_schema.svh`, `trl_reader.svh`, `trl_cursor.svh`.
- **Every package-scope name is prefixed**, so a wildcard import can't
  collide with user/UVM names: classes `db`→`trl_db`, `type_h`→`trl_type`,
  `schema_h`→`trl_schema`, `txn`→`trl_txn`, `stream`→`trl_stream`,
  `rstream`→`trl_rstream`, `reader`→`trl_reader`, `cursor`→`trl_cursor`;
  enums `kind_e`→`trl_field_type_e` (literals `TRL_FT_*`, mirroring trl.h's
  `trl_field_type_t`, so `K_BITS`→`TRL_FT_BITVEC`, `K_LOGIC`→`TRL_FT_LOGICVEC`),
  `link_e`→`trl_link_e` (`TRL_LINK_*`); functions `enabled`/`now`/`shutdown`
  →`trl_enabled`/`trl_now`/`trl_shutdown`. The package globals
  `g_enabled`/`g_inited` became `local static` members of `trl_db`
  (with `trl_db::enabled()`), so they no longer leak into the importer's scope.
  Class method names are unchanged (they're class-scoped).
- **No separate DPI package.** `trlog_dpi_pkg.sv` became `trl_sv_dpi.svh`
  (git mv), included directly into `trl_pkg` — the package is useless
  without DPI, so an extra package bought nothing. The `trl_sv_*` import
  names were already prefixed.
- `sv/tests/top_smoke.sv` now uses unqualified names after
  `import trl_pkg::*;` to exercise the wildcard import; the pytest wrapper
  compiles `trl_pkg.sv` + `trl_final.sv` + the test only.
- `sv_api_design.md` §6/§8 updated to the new names.

Verification: `sv/tests/test_sv_smoke.py` 1 passed (Verilator compile + DPI
round-trip). No C changes.

## Phase 4c: SV documentation (2026-11-23)

- Every public class, function, property, enum, macro and the `trl_final`
  module in `sv/src` has a doc comment in sphinx-systemverilog's `native`
  dialect (RST). Maintainer notes that used to sit at the top of
  `trl_pkg.sv` moved inside the package so they don't become its doc.
- `docs/conf.py` loads `sphinx_systemverilog` with `sv_build_units` =
  `trl_pkg.sv` + `trl_final.sv` (not `sv_source_dirs`: the `.svh` files
  are only valid inside the package). New pages: `docs/guide/systemverilog.md`
  (user guide), `docs/api/sv.rst` (class reference), `docs/api/sv_dpi.rst`
  (raw DPI imports).
- Build: `packages/python/bin/python -m sphinx -b html docs docs/_build/html`.
  All 153 public SV objects are documented; the 7 left out are `local`
  members and the include guard. The only warnings left are the 5 that
  existed before (stale Python `ZstWriter`/`ZstReader` autodoc names,
  missing `codec-registry`/`writing-a-codec` pages, no `_static` dir).
- Comment-writing rules forced by the `native` parser (sphinx-systemverilog
  0.6.2): a `:param:`/`:returns:` field must fit on one comment line (wrapped
  lines fall out of the field), and no comment line may *start* with a role
  such as `:sv:func:` (even indented, it is taken as a new field). Bare
  member refs (`:sv:func:\`get\``) are ambiguous across classes; write
  `:sv:func:\`get <trl_db::get>\``.
- Known gap found while documenting: `trl_stream::open_txn(null)` on a stream
  with no default type dereferences a null `dflt_type` (design §8.3's ad-hoc
  example does exactly this). The guide passes `adhoc_type()` explicitly.
