# trlog deployment plan

Two release artifacts from one tag:

- **Python wheels** on PyPI (`trlog`), built the way **dv-solve** builds them.
- **A JavaScript/WebAssembly package** on npm, built the way **pssparser** builds
  its `@psstools/pssparser` package. Target consumers are JavaScript (browser,
  Worker, Node). Python/Pyodide is not a goal; Pyodide users get the pure-Python
  wheel.

Reference repos: `~/projects/fvutils/dv-solve` (pyproject.toml,
`.github/workflows/wheels.yml`), `~/projects/psstools/pssparser` (`wasm/`, `ts/`,
`scripts/wasm-env.sh`, `ivpm.yaml` dep-sets `wasm-build`/`ts-build`,
`.github/workflows/ci.yml` jobs `ts` and `publish`).

## Status

| Step | State |
|------|-------|
| 0. Fix existing CI (lz4) | done locally (not pushed): `liblz4-dev` added; clean C build + ctest 9/9 + pytest 1020 passed / 34 skipped |
| 1. Vendor zlib + lz4 sources via ivpm, link statically | done locally: `c/cmake/trl_deps.cmake` (zlib v1.3.1, lz4 v1.10.0 from `packages/`, static PIC, lz4 symbols not exported), `TRL_SYSTEM_DEPS=ON` keeps system libs; ivpm dep-set `default`; CI clones the two tags instead of apt. Checked: vendored + system builds ctest 9/9, standalone `-S c` build, pytest 1020/34 skipped (byte-golden + parity incl.). CI not run. |
| 2. Single version source | done locally: `python/src/trlog/__version__.py` (BASE/SUFFIX), `setup.py` exec()s it, `pyproject.toml` version dynamic, `trlog.__version__`. `uv build` gives trlog-0.1.0 wheel + sdist. |
| 3. Python wheels (ivpm-build + cibuildwheel) | done locally (Linux x86_64 only; CI/macOS/Windows not run): loader `_libpath.py`; `TRL_BUILD_TESTS` option (default from env, cibuildwheel sets OFF); pyproject `ivpm_build.backend` + `package-dir` + extra-data (libtrl, libtrl_sv, libtrlog_vpi, `share/include/trl`, `share/sv` via a CMake `install(DIRECTORY sv/src/ ...)`) + cibuildwheel config; setup.py `build_ext` override; `MANIFEST.in` ships c/, vpi/, sv/src and the zlib/lz4 sources so the sdist builds alone; `get_libdirs/get_incdirs/get_svdirs` + `trlog config --libdir|--incdir|--svdir|--version`; `tests/wheel_smoke.py`; `.github/workflows/wheels.yml` (no publish). `uv build` → `trlog-0.1.0-py3-none-linux_x86_64.whl`, smoke test PASS in a clean venv. Since 3a.1 the wheel also carries `libtrlog_vpi`. |
| 3a. Windows/MSVC port (required for first release) | 3a.1 done locally: IEEE 1800-2023 `vpi_user.h` vendored as `vpi/ext/vpi_user.h` (copied from Verilator's vltstd; SPDX `LicenseRef-IEEE-1800`, Annex K interface material, same basis Verilator/Icarus ship it on); VPI routines looked up at runtime (`vpi/src/trlog_vpi_fn.{h,c}`: dlsym(RTLD_DEFAULT)/dlopen(NULL), Windows GetProcAddress + EnumProcessModules) from `trlog_vpi_register`; Linux link uses `-Wl,--no-undefined`; `$ORIGIN`/`@loader_path` install rpath so wheel libs find libtrl; smoke test requires and loads trlog_vpi. Checked: ctest 9/9, VPI mock unit test PASS, Icarus VPI tests pass, pytest 1025/34, wheel smoke PASS. 3a.2 done locally (Windows not compiled, only read): `c/include/trl/trl_api.h` TRL_API (dllexport/dllimport on Windows, visibility default elsewhere) on every public function; trl, trl_sv (TRL_SV_API) and trlog_vpi (TRLOG_VPI_API in trlog_vpi.h) build with hidden visibility so Linux exercises the marks (libtrl exports 121 symbols, trl_sv 56, trlog_vpi 2); sources build as OBJECT lib `trl_obj` so test_qscan can still link internals; `c/src/trl_portable.h`: int64 overflow helpers (match GCC builtins), `trl_fseek64`, `trl_strdup`, `trl_glob_match` (replaces fnmatch on all platforms; agrees with glibc fnmatch on 272 cases); MSVC `.CRT$XCU` initializer for core-codec registration; test_codec no longer uses mkstemp; `build-and-test-windows` job in ci.yml (continue-on-error until first green). Checked: ctest 9/9, VPI mock PASS, pytest 1025/38 incl. Icarus, native pytest (known flaky only), Verilator links trl_sv DPI imports, wheel smoke PASS. |
| 4. Release gate + PyPI publish | done locally (CI not run; nothing tagged): `.github/workflows/wheels.yml` gains dv-solve's `gate` (dvkit.org release-authority switch, fails closed on tags) and `publish` (`needs: [gate, build_wheels, build_wheels_macos_intel, build_sdist]`, so windows-AMD64 and macOS Intel block a release; only on `refs/tags/v*` with authority `github`). Publish checks tag == `BASE`, `SUFFIX` empty, every wheel and the sdist named `trlog-<BASE>`, at least 7 wheels; then `twine check` + `twine upload --skip-existing` of wheels **and sdist** with org secret `PYPI_API_TOKEN`. Plain twine (not trusted publishing) per this plan and the dvkit-infra skill. Checked: YAML parses; the Forgejo shadow's "must not gain a publisher" check passes against it; the version-check script passes/fails correctly on fake dist/ trees; `twine check` passes on a local wheel+sdist (warns: no long_description, repo has no README). |
| 5. Memory-buffer reader in libtrl | done locally: `trl_reader_open_mem(buf, len)` (trl.h) and `trl_rdb_open_mem(name, buf, len)` (trl_query.h, so JS can read transactions too), both TRL_API. The buffer is **borrowed**, not copied: it must outlive close (the JS wrapper will own the heap copy). `reader.c` reads through `trl_reader_src_read/seek` (FILE* or memory cursor; no fmemopen); `query.c` `read_block_at` uses them. Also: a reader with no file-header block now fails to open (TRL_ERR_MAGIC) instead of opening as empty. `trl_qscan_new` still takes a path (step 9). Test `c/tests/test_open_mem.c` (ctest `c_open_mem`): all 19 golden fixtures give identical value changes, streams and transactions from file and memory; junk/truncated/NULL rejected. Checked: ctest 10/10, pytest 1025/34 (plain and TRL_LIBRARY_PATH), `nm -D` shows both symbols. No Python wrapper yet. |
| 6. WASM build (`wasm/`) | done locally: `wasm/CMakeLists.txt` (emcmake only) compiles libtrl's sources + zlib/lz4 (via `c/cmake/trl_deps.cmake`) + `wasm/bindings.cpp` (embind `Trace(bytes)`: owns the heap copy the borrowed-buffer readers need; `valueChanges()`, `streams()`, `transactions(t0,t1)`, `schema(id)`, `close()`; 64-bit values as BigInt; provisional until step 7's API design) into `trlog.{js,wasm}` (122 KB wasm) with pssparser's link flags; `-DENABLE_ASAN=ON` diagnostic build into its build dir. `fopen` in writer/qscan links under `-sFILESYSTEM=0`. ivpm dep-sets `wasm-build` (emsdk 6.0.9, pinned hash) + `ts-build`; `scripts/wasm-env.sh`. Checked with pssparser's emsdk 6.0.9: release + ASan builds, `node wasm/smoke.mjs` reads all 19 golden fixtures with counts equal to the native library, junk rejected; native ctest + pytest unchanged. Not run: `ivpm update -d ts-build` fetch. |
| 7. TS package (`ts/`), read-only API | done (vitest vs Python reader; libtrl gaps: column-layout txn, bytesplit codec) |
| 8. npm publish in lockstep with wheels | done locally (CI not run; nothing tagged): `wheels.yml` job `build_npm` (ubuntu, node 22): `ivpm update -d ts-build` (emsdk + zlib/lz4), on tags `npm version` from the tag, `npm ci` (its `prepare` builds wasm + TS), `npm test`, `npm pack` (asserts `dist/wasm/trlog.wasm` in it) as artifact `npm`. `publish` now also needs `build_npm`, checks `npm-dist/fvutils-trlog-<BASE>.tgz` with the wheels, uploads PyPI first, then `npm publish <tgz> --access public` with org secret `NPM_TOKEN` (setup-node registry-url). Checked: YAML parses; Forgejo shadow check OK (note: it does not look for `npm publish`); version check passes/fails on fake trees; simulated pack step gives fvutils-trlog-0.1.0.tgz with the wasm. Not run: `ivpm update -d ts-build` fetch in CI (pulls default-dev, incl. verilator). |
| 9. (later) Query engine from JS | deferred |
| 10. (later) Writing from JS | deferred |

CI (2026-11-24): pushed to main (3edd461); first GitHub run: Linux CI, sdist, macOS x86_64 green; fixes pushed for macos-arm64 (`macos-14` runner retired → `macos-15`), Windows pytest (Windows paths in `batch` shlex and JSON golden normalization), npm job (ivpm `lz4` name clash with the pypi package → C source renamed `lz4-src`; `ts-build` uses `default`, not `default-dev`).

Next action: get every wheels.yml job green (workflow_dispatch on main), set org secrets `PYPI_API_TOKEN` and `NPM_TOKEN`, then tag `v0.1.0`. Steps 9-10 deferred.

## Current state (2026-11-24)

- `.github/workflows/ci.yml`: one ubuntu job (Python 3.12) — CMake build of
  `c/`, `ctest`, `pytest` from the source tree. No matrix, no packaging, no
  publish. **Likely broken:** `c/CMakeLists.txt` requires liblz4
  (`find_library(LZ4_LIBRARY ... REQUIRED)`), the workflow installs only
  `zlib1g-dev`.
- `.forgejo/workflows/ci.yml`: guard only (internal-identifier check; fails if a
  GitHub workflow gains a publish step not gated on `refs/tags/v` *and* the
  release `authority`). dv-solve's `gate` + `publish` shape satisfies it.
- `pyproject.toml`: plain setuptools, pure-Python, static `version = "0.1.0"`.
  The `lz4` dep in `ivpm.yaml` is the PyPI Python package, not C sources.
  (The leftover `zuspec-trace` names in `ivpm.yaml` and the top-level
  `CMakeLists.txt` were renamed to `trlog` on 2026-11-24.)
- Native loading (ctypes):
  - `python/src/trlog/query/_cengine.py` already searches the package dir and
    picks the per-platform library name.
  - `python/src/trlog/_native.py` only searches repo build dirs and hardcodes
    `libtrl.so` — must be fixed for wheels and macOS/Windows.
- Native libs: `trl` (core), `trl_sv` (DPI glue), `trlog_vpi` (VPI). External
  deps: zlib, lz4. `qscan.c` has a `_WIN32` branch; MSVC build never tried.
- No Emscripten / JS anything yet.

## Shared prerequisites

### 0. Fix existing CI
Add `liblz4-dev` to the apt install in `.github/workflows/ci.yml`. Superseded by
step 1 once deps are vendored, but CI should be green first.

### 1. Vendor zlib + lz4
Fetch C sources via `ivpm.yaml` (as dv-solve fetches CaDiCaL), build them as
static libraries from `c/CMakeLists.txt`, link into `trl`. Option
`TRL_SYSTEM_DEPS=ON` keeps the `find_package` path for distro builds.
Why: removes system-library requirements for manylinux/macOS/Windows wheels
(no auditwheel/delocate bundling) and is required for the Emscripten build
(Emscripten ships zlib via `-sUSE_ZLIB=1` but not lz4).
Check: byte-golden + cross-impl parity tests still pass (vc_data.c relies on
zlib's deterministic output).

### 2. Single version source
Add `python/src/trlog/__version__.py` (BASE + SUFFIX, same shape as dv-solve /
pssparser) and a `setup.py` that exec()s it, per dv-solve's `setup.py` rationale
(the package is not importable under the ivpm_build backend). Release checks
compare the tag against it and against built artifacts.

## Python wheels (dv-solve pattern)

### 3. Wheels
- `build-backend = "ivpm_build.backend"`, `[tool.ivpm-build] cmake = true`.
- `[[tool.ivpm-build.extra-data]]`:
  - `{libpref}trl{dllext}`, `{libpref}trl_sv{dllext}`, `{libpref}trlog_vpi{dllext}`
    → package root (next to `__init__.py`).
  - `c/include/trl` → `share/include`.
  - `sv/src` → `share/sv`.
- Python helpers like dv-solve's: `get_libdirs()`, `get_incdirs()`,
  `get_svdirs()`, and a CLI form (`trlog config --libdir` etc.) for simulator
  command lines.
- Fix `_native.py` to share `_cengine._candidates()` (package dir first,
  per-platform name).
- Wheels are `py3-none-<plat>` (ctypes, no CPython ABI); cibuildwheel
  `build = "cp311-*"`, skip musllinux.
- CMake option to skip C tests/benchmarks in wheel builds
  (dv-solve: `DV_SOLVE_BUILD_TOOLS=OFF`).
- `tests/wheel_smoke.py`: import from the installed wheel, assert the native
  lib loads (`_native._LIB`, `_cengine.load()`), round-trip a small trace.
- `.github/workflows/wheels.yml` matrix: manylinux_2_34 + _2_28 × x86_64 +
  aarch64 (QEMU), macos-14 arm64, windows-latest AMD64 (MSVC; delvewheel only if
  a C++ runtime gets pulled in — libtrl is C, so probably not); macOS Intel on
  tags/manual only. Plus sdist.
- Every platform wheel ships all three native libs (`trl`, `trl_sv`,
  `trlog_vpi`), Windows included.
- **Windows is in the first release** (decided 2026-11-24), so it is a
  required matrix entry and `publish` needs it. Work this implies (step 3a):
  - MSVC pass over `c/src`, `vpi/src` (C11 features, `<unistd.h>`/POSIX calls,
    `__attribute__`, VLAs).
  - `TRL_API` export macro on the public headers (`__declspec(dllexport)` when
    building, `dllimport` for consumers); same for `trl_sv` and `trlog_vpi`.
  - **VPI functions are looked up at runtime, not linked** (decided
    2026-11-24). Windows DLLs cannot leave `vpi_*` unresolved the way a Linux
    `.so` can, and a per-simulator import lib would mean per-simulator builds.
    So `trlog_vpi` calls VPI through a function-pointer table filled at load:
    - POSIX: `dlsym(RTLD_DEFAULT, "vpi_...")` (falling back to
      `dlopen(NULL)`); the simulator has already loaded its VPI exports.
    - Windows: try `GetProcAddress(GetModuleHandle(NULL), ...)`, then walk
      `EnumProcessModules` and take the first module exporting the symbol
      (the simulator's VPI may live in a DLL, not the .exe).
    - Fill the table in the first `vlog_startup_routines` entry, before any
      other VPI call; fail with a clear message naming the missing symbol.
    - The same path on every platform, so Linux/macOS exercise the code Windows
      depends on. Current use is ~13 functions (`vpi_scan`, `vpi_iterate`,
      `vpi_printf`, `vpi_control`, `vpi_get_value`, `vpi_get`,
      `vpi_register_cb`, `vpi_get_str`, `vpi_handle`, `vpi_get_time`,
      `vpi_register_systf`, `vpi_get_vlog_info`).
    - `vpi/CMakeLists.txt` currently fails without a system `vpi_user.h`.
      Vendor the IEEE `vpi_user.h` (types/constants only) so the wheel build
      has no simulator dependency; check the license of the copy taken.
    - Only `vlog_startup_routines` (and any explicit bootstrap entry) must be
      exported from the DLL.
  - `_native.py`/`_cengine.py` already pick `trl.dll` by platform; verify with
    `os.add_dll_directory` for the package dir (trl_sv/trlog_vpi depend on trl).
  - Add a windows-latest job to the regular test CI (not just wheels) so
    breakage shows up per-PR.

### 4. Release
Copy dv-solve's `gate` job (dvkit.org release-authority switch, fail closed on
tags) and `publish` job (needs every build job; tag == `__version__` == every
wheel name; `twine upload`). Uses the org-level `PYPI_API_TOKEN` secret
(already set on the `fvutils` GitHub org). Consult the
`dvkit-infra` skill before adding the publisher.

## JavaScript / WebAssembly (pssparser pattern)

### 5. Memory-buffer I/O in libtrl
Browsers hand over bytes (`File`, `ArrayBuffer`, `fetch`), not paths, and the
module is built with `-sFILESYSTEM=0`.
- `trl_reader_open_mem(const void *buf, size_t len)` — reader over a caller-owned
  buffer (refactor `reader.c` off `FILE*` onto a small read/seek interface).
- `trl_qscan_new` takes a path too; give it the same treatment when step 9 comes.
- Writer-to-buffer is deferred with step 10.
Useful natively as well (pipes, archive members); expose in Python later.

### 6. `wasm/CMakeLists.txt`
- Separate configuration, `emcmake` only (FATAL_ERROR otherwise).
- Compile `c/src/*.c` + vendored zlib/lz4 (or `-sUSE_ZLIB=1`) + `wasm/bindings.cpp`
  (embind wrapper over the C API) into one module.
- Link flags as pssparser: `-lembind -sMODULARIZE=1 -sEXPORT_ES6=1
  -sEXPORT_NAME=createTrlogModule -sALLOW_MEMORY_GROWTH=1 -sFILESYSTEM=0
  -sENVIRONMENT=web,worker,node`, no pthreads, explicit `STACK_SIZE`.
- Output into `ts/src/wasm/trlog.{js,wasm}`.
- `ENABLE_ASAN` diagnostic build into its own dir.
- Toolchain: `ivpm.yaml` dep-set `wasm-build` (pinned emsdk tarball, cached) and
  `ts-build` = `default-dev` + `wasm-build`; `scripts/wasm-env.sh` copied from
  pssparser.

### 7. `ts/` package — read-only API first
- npm package `@fvutils/trlog`, ESM, ships `dist/` incl. the `.wasm`.
- `prepare` hook (`scripts/prepare.mjs`) builds wasm + tsc for source consumers;
  registry consumers never need Emscripten. `SKIP_TRLOG_PREPARE` escape hatch.
- API sketch (to be designed before bindings):
  - `openTrace(bytes: Uint8Array | ArrayBuffer): Promise<Trace>`
  - hierarchy/scopes, signals, streams, schemas — plain JS objects
  - value changes for a signal over `[t0, t1)` as typed arrays
  - transaction iteration over a time window (iterator of JS objects)
  - explicit `close()` / `Symbol.dispose` for WASM memory
- Use the index for time-window access; avoid decoding whole files. Later
  option: lazy block reads from a `Blob` / HTTP range requests rather than
  copying the whole file into the heap (4 GB WASM limit).
- Tests: vitest, including reading the golden fixtures in `tests/golden/` and
  comparing against Python-produced expectations.
- CI job `ts` (every push): `ivpm update -d ts-build`, `npm install`,
  typecheck, `npm test`.

### 8. npm publish
Extend the step-4 `publish` job as pssparser does: one tag stamps the version
into wheels and `package.json`; pack a real tarball, assert the `.wasm` is in it,
load it and open a fixture; `npm publish --access public` (`--tag rc` for
pre-releases). Uses the org-level `NPM_TOKEN` secret (already set on the
`fvutils` GitHub org).

### 9–10. Deferred
- Query engine from JS (qscan over a buffer).
- Writing from JS (writer to a growable buffer, returned as `Uint8Array`).

## Decisions (2026-11-24)
- PyPI and ivpm package name: `trlog`.
- npm package: `@fvutils/trlog`.
- All platform wheels ship the SV/VPI libraries (`trl_sv`, `trlog_vpi`).
- Windows is a first-release target.
- WASM serves JavaScript consumers only; Pyodide uses the pure-Python wheel.

- `PYPI_API_TOKEN` is an org-level secret on the `fvutils` GitHub org.
- `NPM_TOKEN` is an org-level secret on the `fvutils` GitHub org; the publish
  job reads `secrets.NPM_TOKEN` with no per-repo setup.
