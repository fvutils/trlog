"""conftest.py for benchmarks/tests.

Provides a session-scoped fixture that compiles simulation binaries for each
signal mix.  Verilator and VCS failures are handled gracefully so that whichever
simulator is available drives the run.
"""

import os
import subprocess
import sys
import warnings

import pytest

sys.path.insert(0, os.path.dirname(__file__))
from _bench_utils import (
    MIXES,
    SCALES,
    VERILATOR,
    VCS,
    SV_FILE,
    compile_vcs,
)


@pytest.fixture(scope="session")
def compiled_sims(tmp_path_factory):
    """Compile simulation binaries for each signal mix.

    Tries Verilator VCD + FST first; compile failures set those keys to
    ``None`` (non-fatal) rather than aborting the session, so VCS-only runs
    still work.  When VCS is available, also compiles VCS VCD and VCS FSDB
    (Verdi) binaries.  The test is skipped only when *no* simulator compiles.

    Returns dict:  mix_name → {
        "vcd_bin":      Path | None,  # Verilator VCD
        "fst_bin":      Path | None,  # Verilator FST
        "vcs_vcd_bin":  Path | None,  # VCS VCD
        "vcs_fsdb_bin": Path | None,  # VCS FSDB (Verdi)
    }
    """
    if VERILATOR is None and VCS is None:
        pytest.skip("neither verilator nor vcs found in PATH")

    base = tmp_path_factory.mktemp("compiled")
    result = {}

    for mix_name, mix_id in MIXES.items():
        mix_dir = base / mix_name
        mix_dir.mkdir()
        entry = {
            "vcd_bin":      None,
            "fst_bin":      None,
            "vcs_vcd_bin":  None,
            "vcs_fsdb_bin": None,
        }
        if VERILATOR is not None:
            try:
                entry["vcd_bin"] = _compile_verilator(mix_dir, mix_id, "vcd", "--trace-vcd")
                entry["fst_bin"] = _compile_verilator(mix_dir, mix_id, "fst", "--trace-fst")
            except Exception as exc:
                warnings.warn(f"Verilator compile failed for {mix_name}: {exc}")
        if VCS is not None:
            vcs_dir = mix_dir / "vcs"
            vcs_dir.mkdir()
            try:
                entry["vcs_vcd_bin"]  = compile_vcs(vcs_dir, mix_id, "vcd")
                entry["vcs_fsdb_bin"] = compile_vcs(vcs_dir, mix_id, "fsdb")
            except Exception as exc:
                warnings.warn(f"VCS compile failed for {mix_name}: {exc}")
        result[mix_name] = entry

    return result


def _compile_verilator(base_dir, mix_id, fmt, trace_flag):
    obj_dir  = base_dir / f"obj_{fmt}"
    bin_path = obj_dir / f"simv_{fmt}"
    subprocess.check_call(
        [
            VERILATOR, "--binary", "--sv",
            f"-GSIGNAL_MIX={mix_id}",
            trace_flag,
            "-o", f"simv_{fmt}",
            "--Mdir", str(obj_dir),
            SV_FILE,
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    return bin_path
