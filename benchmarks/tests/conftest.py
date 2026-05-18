"""conftest.py for benchmarks/tests.

Provides a session-scoped fixture that compiles Verilator VCD and FST binaries
once per signal mix.
"""

import os
import subprocess
import sys

import pytest

sys.path.insert(0, os.path.dirname(__file__))
from _bench_utils import MIXES, SCALES, VERILATOR, SV_FILE


@pytest.fixture(scope="session")
def compiled_sims(tmp_path_factory):
    """Compile VCD and FST Verilator binaries for each signal mix.

    Returns dict:  mix_name → {"vcd_bin": Path, "fst_bin": Path}
    """
    if VERILATOR is None:
        pytest.skip("verilator not found in PATH")

    base = tmp_path_factory.mktemp("compiled")
    result = {}

    for mix_name, mix_id in MIXES.items():
        mix_dir = base / mix_name
        mix_dir.mkdir()
        result[mix_name] = {
            "vcd_bin": _compile(mix_dir, mix_id, "vcd", "--trace-vcd"),
            "fst_bin": _compile(mix_dir, mix_id, "fst", "--trace-fst"),
        }

    return result


def _compile(base_dir, mix_id, fmt, trace_flag):
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
