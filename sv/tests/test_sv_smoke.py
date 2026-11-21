"""Verilator smoke test for the L2 trlog_pkg (sv_api_design.md §6, phase 4).

Compiles sv/src/*.sv + sv/tests/top_smoke.sv with Verilator's --binary mode,
linking against the already-built c/CMakeLists.txt targets `trl_sv` and
`trl` (so build the C library first: `cmake --build build`), then runs the
resulting model and checks for the "SMOKE: PASS" marker top_smoke.sv prints
after round-tripping a tier-2 and a tier-1 transaction through a real DPI
boundary and reading them back with trlog_pkg::reader/cursor.

Skipped (not failed) when verilator isn't on PATH or the C libraries
haven't been built yet.
"""

from __future__ import annotations

import pathlib
import shutil
import subprocess

import pytest

REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
SV_SRC = pathlib.Path(__file__).resolve().parent.parent / "src"
TOP_SV = pathlib.Path(__file__).resolve().parent / "top_smoke.sv"
BUILD_C = REPO_ROOT / "build" / "c"

VERILATOR = shutil.which("verilator")

pytestmark = [
    pytest.mark.skipif(VERILATOR is None, reason="verilator not found in PATH"),
    pytest.mark.skipif(
        not (BUILD_C / "libtrl_sv.so").exists() or not (BUILD_C / "libtrl.so").exists(),
        reason="build/c/libtrl_sv.so / libtrl.so not found; run cmake --build build first",
    ),
]


def _compile(tmp_path: pathlib.Path) -> pathlib.Path:
    obj_dir = tmp_path / "obj"
    subprocess.check_call(
        [
            VERILATOR, "--binary", "--sv", "--timing",
            "-I" + str(SV_SRC),
            "--top-module", "top_smoke",
            "-o", "smoke",
            "--Mdir", str(obj_dir),
            "-LDFLAGS", f"-L{BUILD_C} -Wl,-rpath,{BUILD_C} -ltrl_sv -ltrl",
            str(SV_SRC / "trlog_dpi_pkg.sv"),
            str(SV_SRC / "trlog_pkg.sv"),
            str(SV_SRC / "trlog_final.sv"),
            str(TOP_SV),
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    return obj_dir / "smoke"


def test_dpi_round_trip(tmp_path):
    binary = _compile(tmp_path)
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    out = subprocess.run(
        [str(binary), "+trlog_file=smoke.trl"],
        cwd=str(run_dir),
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert "SMOKE: PASS" in out.stdout, out.stdout + out.stderr
