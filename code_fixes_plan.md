# Code fixes from the docs refresh: plan and status (2026-11-23)

Source: the 33 issues in `docs_refresh_status.md`, "Code issues found while
documenting". `#N` below is the issue number there; read it for the detail.
The wpk outline (`.work-packet/state/outline.md`) mirrors this table.

## Ground rules

- For each task, first write a test that fails on the current code. Then fix
  the code, check the test passes, and run the regression checks.
- Do not `git commit` or `git push`. The branch (`mballance/svpkg`) already has
  a large uncommitted diff, and the user handles all commits.
- Send build and test output to `logs/fix_<task>_*.log`, never to the
  terminal. Show only pass/fail counts or a grep for failures.
- When a fix removes a limitation the docs warn about, update that page in
  `docs/` too, so the docs keep matching the code.
- The file format must stay readable by the current readers wherever
  possible. When a fix needs a format change, make it in both the C and the
  Python writer/reader and update `docs/guide/format.md`.
- The C engine stays optional: every Python fix must also work in pure
  Python (memory: c-engine-optional).

## Checks

| Name | Command |
|---|---|
| C | `cmake --build build > logs/fix_build.log 2>&1 && (cd build/c && ctest --output-on-failure) > logs/fix_ctest.log 2>&1` |
| Py | `PYTHONPATH=python/src packages/python/bin/python -m pytest tests -q > logs/fix_pytest.log 2>&1` |
| SV | `PYTHONPATH=python/src packages/python/bin/python -m pytest sv/tests -q > logs/fix_sv.log 2>&1` |
| Docs | `rm -rf docs/_build && packages/python/bin/python -m sphinx -E -b html docs docs/_build/html > logs/fix_docs.log 2>&1` (must give 0 warnings) |

Baseline (2026-11-23):
- C: ctest 6/6.
- Py: core 402 passed and 50 skipped; query 445 passed and 1 skipped; codec
  132 passed and 3 skipped.
- SV: the smoke test passes.
- Docs: 0 warnings.

## Tasks

| Id | Task | Issues | Status |
|---|---|---|---|
| 1 | C value encoding: 2-state values wider than 64 bits, and 4-state and 9-state values, are stored in full | #1 #2 | done |
| 2 | Python value encoding: multi-bit 4-state strings, and `'0'`/`'1'` in 9-state | #3 #4 | done |
| 3 | `write_attr()` encodes each value by its schema field type | #5 | done |
| 4 | Transaction column layout with optional attributes (first check `logs/column_fix_verify.log` and `logs/column_layout_repro.py` for earlier work) | #6 | done |
| 5 | Variable and stream-instance IDs unique across hierarchy blocks (Q1) | #7 | done |
| 6 | Codec read path: `read_signal(start=)` uses each block's codec; the C reader skips or decodes codec blocks | #8 #9 | done |
| 7 | Codec write path: NULL-safe `finalize`/`close` in C; Python per-block codec lifecycle; bridge writes complete blocks, and its test reads the file back | #10 #11 #12 | done |
| 8 | Codec API: public `TrlWriter` stream-codec API, readers honour it (Q2); fix the `trl_codec.h` comments; declare `trl_register_core_codecs` | #13 #14 | done |
| 9 | C API hygiene: define `trl_hier_add_attr`; `extern "C"` guards; `trl_rec_full` supports BITVEC/LOGICVEC/BYTES; `trl_rec.h` suppresses records on disabled streams | #15 #16 #18 #19 | done |
| 10 | C reader robustness: read every `H_SCOPE` field; skip unknown hierarchy/type-registry records; the writer honours `timezero` | #17 | done |
| 11 | VPI: fix the `$trlog_dumplimit` size estimate; Icarus value-format warning; Verilator build (Q3) | #20 #21 | done (Verilator build: not fixed, see Q3) |
| 12 | Python API: `compressor=` honoured (zstd); fix or remove the `TRL_USE_CTYPES_NATIVE` reader swap and the `_native.py` search path; public scope-tree API (Q4) | #22 #23 #24 | done |
| 13 | Query semantics: enum ordering by value; `--max-bytes` progress; reject or round fractional ticks | #25 #28 #30 | done |
| 14 | CLI polish: `--jsonl` prefix; drop the "(§3.3)" help text; separate trace metadata in export; plurals; examples/ generator for `match` | #26 #27 #29 #31 #32 | done |
| 15 | SV: `open_txn(null)` with no default type gives an error, not a null dereference | #33 | done |
| 16 | Final: full regression (C, Py, SV, Docs); remove the doc warnings for fixed issues; update `docs_refresh_status.md` to mark each issue fixed | all | done |

## Questions (recommended answers; go ahead on them unless told otherwise)

- **Q1 (task 5).** How to make IDs unique across hierarchies?
  - Recommended: the writers (C and Python) continue the numbering across
    hierarchy blocks instead of restarting at 1. Old files still read;
    the format is unchanged.
  - Alternative: qualify IDs by hierarchy, which changes the format.
- **Q2 (task 8).** Shape of the public stream-codec API.
  - Recommended: `TrlWriter.set_stream_codec(stream_type_id, codec_id)`,
    a thin wrapper over `TypeRegistry.set_stream_codec`.
  - Readers then pick the decoder from the codec ID that each block
    records.
- **Q3 (task 11).** The Verilator VPI build fails.
  - Recommended: diagnose it and fix it if the fix is small.
  - Otherwise mark it `[-]` with the reason and leave the tests skipped.
- **Q4 (task 12).** Public scope-tree API.
  - Recommended: `TrlReader.scopes()`, which yields scope objects with
    `name`, `type`, `children` and `vars`.
  - Then switch the query engine from `hier._root_children` to it.

## Status log

- 2026-11-23: plan written; handed to the wpk runner.
