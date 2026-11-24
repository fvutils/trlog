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
| 1. Vendor zlib + lz4 sources via ivpm, link statically | not started |
| 2. Single version source | not started |
| 3. Python wheels (ivpm-build + cibuildwheel) | not started |
| 3a. Windows/MSVC port (required for first release) | not started |
| 4. Release gate + PyPI publish | not started |
| 5. Memory-buffer reader in libtrl | not started |
| 6. WASM build (`wasm/`) | not started |
| 7. TS package (`ts/`), read-only API | not started |
| 8. npm publish in lockstep with wheels | not started |
| 9. (later) Query engine from JS | deferred |
| 10. (later) Writing from JS | deferred |

Next action: step 1 (vendor zlib + lz4).

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
