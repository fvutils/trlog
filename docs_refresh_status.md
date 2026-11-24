# Docs refresh: plan and status (2026-11-23)

Goal: bring `docs/` up to date with the code (it still described the old
`ZstWriter`/`ZstReader` API and the "zuspec-trace" name) and rebuild it
cleanly. Build: `packages/python/bin/python -m sphinx -b html docs docs/_build/html > logs/docs_build.log 2>&1`.

| Area | Pages | Status |
|---|---|---|
| SystemVerilog | `guide/systemverilog.md`, `api/sv.rst`, `api/sv_dpi.rst` | done (Phase 4c, `sv_impl_status.md`) |
| Core Python (writer/reader, hierarchies, transactions, derived, ext) | `guide/quickstart.md`, `guide/hierarchies.md`, `guide/transactions.md`, `guide/py-*.md`, `api/writer.rst`, `api/reader.rst`, `api/python-*.rst` | done |
| Codecs + format spec | `codec-registry.md`, `guide/writing-a-codec.md`, `guide/format.md`, `api/codec.rst` | done; many code issues reported |
| Query API + `trlog` CLI | `guide/cli.md`, `guide/query.md`, `api/query.rst` | done; 8 code issues reported |
| C library (+ VPI dumper) | `guide/c-api.md`, `guide/vpi.md`, `api/c.rst` | done; 8 code issues reported (see final summary) |
| `conf.py` / `index.rst` | project name, toctrees | done |

Result: clean full build (`-E`), 0 warnings, 0 errors. Every example in the
guides was run against the current code. Source edits were docstring/comment
only (Python docstrings converted to Google style; C header comments).

## Code issues found while documenting (fixed by code_fixes_plan.md; see tags)

The docs describe current behaviour and warn where it bites.

### Correctness (data written or read wrong)
1. C `vc_data.c`: 2-state values wider than 64 bits are truncated
   (`trl_chg_to_u64` keeps 8 bytes); confirmed with a 72-bit value. — fixed (item 1)
2. C: 4-state and 9-state values are truncated to 1 bit. — fixed (item 1)
3. Python `_pack_4state`: a multi-bit 4-state value that isn't an `int`
   (e.g. `"01xz"`) is stored as all X. — fixed (item 2)
4. Python 9-state: characters `'0'`/`'1'` are stored as 0 (`_9STATE_CODES`
   uses integer keys for 0 and 1). — fixed (item 2)
5. Python `write_attr()` (`TR_ATTR`) ignores the schema and writes every value
   as `FT_U64`; string values raise `struct.error`, floats are wrong. — fixed (item 3)
6. Python transaction column layout decodes optional attributes wrongly
   when some records omit them. — fixed (item 4)
7. Variable IDs and stream instance IDs restart at 1 per hierarchy block, but
   value-change data references variables by ID only, so two hierarchies
   that both declare variables collide. — fixed (item 5)

### Codecs
8. `TrlReader.read_signal(var, start=...)` decodes every block with the
   default codec and crashes on bytesplit/custom-codec blocks (reproduced). — fixed (item 6)
9. The C reader returns `TRL_ERR_CORRUPT` on codec-produced VC blocks instead
   of skipping them (reproduced). — fixed (item 6)
10. The C writer calls a codec's `finalize`/`close` without NULL checks;
    `c/tests/test_codec.c`'s vtable leaves them NULL. — fixed (item 7)
11. Python per-block codec selection calls `open_writer` once per block and
    never calls `finalize`/`close`. — fixed (item 7)
12. The codec bridge: the C writer writes a bridged codec's bare payload as a
    complete block, so bridged files are malformed (the test only compares
    payloads). — fixed (item 7)
13. There is no public `TrlWriter` API for stream-type codecs
    (`TypeRegistry.set_stream_codec` only), and readers never use them. — fixed (item 8)
14. `trl_codec.h` comments are wrong: `encode_block` output is not compressed
    by the core, registration only rejects NULL (not invalid IDs), and
    `trl_register_core_codecs` is not declared in any header. — fixed (item 8)

### C library / VPI
15. `trl_hier_add_attr` is declared in `trl.h` but never defined (link error). — fixed (item 9)
16. `trl.h`, `trl_rec.h` and `trl_query.h` lack `extern "C"` guards. — fixed (item 9)
17. Hierarchy stream scan reads too few `H_SCOPE` fields. Hierarchy and
    type-registry readers don't skip unknown records (`H_ATTR2`, enum/stream
    declarations). The writer ignores `timezero`. — fixed (item 10)
18. `trl_rec_full` silently drops `BITVEC`/`LOGICVEC`/`BYTES` fields. — fixed (item 9)
19. `trl_rec.h` doesn't suppress records on a disabled stream; callers must
    check `enabled`. — fixed (item 9)
20. VPI `$trlog_dumplimit` estimates size from a change count that resets
    on every block write, so limits above ~1.6 MB never trigger. — fixed (item 11)
21. VPI under Verilator: the build fails (tests skip). Icarus prints "value
    format 12 not supported" per traced signal (`vpiObjTypeVal` in
    callbacks); values are still recorded. — Icarus warning fixed (item 11); Verilator VPI build still open

### Python API
22. `TrlWriter(compressor=...)` is ignored; zstd blocks are never written. — fixed (item 12)
23. `TRL_USE_CTYPES_NATIVE=1` swaps in a `TrlReader` that can only
    open/close. `_native.py` searches `build/libtrl.so` and
    `c/build/libtrl.so`, not `build/c/libtrl.so` where CMake puts it. — fixed (item 12)
24. No public API to walk the scope tree (the query engine uses private
    `hier._root_children`). — fixed (item 12)

### Query / CLI
25. Enum ordering compares labels as strings: `resp >= 'SLVERR'` misses
    DECERR (value 3); `resp >= 2` works. Both engines. — fixed (item 13)
26. `--jsonl` rows carry stream paths with the common prefix stripped, but
    the prefix is only in the trailing summary line. — fixed (item 14)
27. Every verb's `--help` cites "(§3.3)" from the design doc. — fixed (item 14)
28. A `--max-bytes` smaller than one row returns `results: []` with
    `"next": "--offset 0"` (no progress). — fixed (item 13)
29. Export `trace` table mixes trace metadata keys with reserved keys
    (`source`, `query`, `time_unit`, `timescale_exp`). — fixed (item 14)
30. `parse_window` silently truncates fractional ticks (`1.5ps` -> 1). — fixed (item 13)
31. Plural grammar in footers ("1 pairs", "1 gaps"). — fixed (item 14)
32. The `match` examples' data generator (`build_bus`) lives in
    `tests/query/test_analysis.py`; an examples/ generator would be cleaner. — fixed (item 14)

### SV
33. `trl_stream::open_txn(null)` on a stream with no default type dereferences
    a null `dflt_type`. — fixed (item 15)
