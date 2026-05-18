"""Shared utilities for benchmark tests: workload definitions, TRLOG writer helper,
and gzip helper.  Imported by both conftest.py and test modules.
"""

import gzip
import os
import shutil
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "../../python/src"))

from trlog import TrlWriter
from trlog._types import SignalEncoding, ScopeType, VarDir

# ---------------------------------------------------------------------------
# Tool paths
# ---------------------------------------------------------------------------

VERILATOR = shutil.which("verilator")
VCD2FST   = shutil.which("vcd2fst")
SV_FILE   = os.path.join(os.path.dirname(__file__), "../sv/bench_dut.sv")

# ---------------------------------------------------------------------------
# Workload definitions
# ---------------------------------------------------------------------------

#: Maps mix name → SIGNAL_MIX parameter value
MIXES: dict[str, int] = {
    "clk_only": 0,
    "bus_mix":  1,
    "dense":    2,
}

#: Maps scale name → number of simulation steps (clock cycles)
SCALES: dict[str, int] = {
    "small":  10_000,
    "medium": 100_000,
    "large":  1_000_000,
}

# ---------------------------------------------------------------------------
# Simulation runner
# ---------------------------------------------------------------------------

def run_simulation(binary, steps, run_dir):
    """Run *binary* with +steps=N in *run_dir*; returns path to trace.out."""
    import subprocess
    run_dir.mkdir(parents=True, exist_ok=True)
    subprocess.check_call(
        [str(binary), f"+steps={steps}"],
        cwd=str(run_dir),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    return run_dir / "trace.out"


# ---------------------------------------------------------------------------
# TRLOG writer  (replicates bench_dut signal pattern in Python)
# ---------------------------------------------------------------------------

def write_trl(path, mix_name, steps, compress,
              compress_time_table=False, compress_waves=False):
    """Write a TRLOG file mirroring bench_dut signal activity.

    Clock half-period is 5 ns; other signals change at each posedge.
    """
    mix_id = MIXES[mix_name]

    with TrlWriter(str(path), timescale_exp=-9, compress=compress,
                   compress_time_table=compress_time_table,
                   compress_waves=compress_waves) as w:
        clk_t  = w.add_signal_type(SignalEncoding.SE_2STATE, 1)
        byte_t = w.add_signal_type(SignalEncoding.SE_2STATE, 8)
        word_t = w.add_signal_type(SignalEncoding.SE_2STATE, 32)
        wide_t = w.add_signal_type(SignalEncoding.SE_2STATE, 64)

        with w.begin_hierarchy() as h:
            h.begin_scope(ScopeType.ST_MODULE, "bench_dut")
            clk_v  = h.add_var("clk",      clk_t,  VarDir.VD_IN)
            byte_v = h.add_var("byte_bus", byte_t, VarDir.VD_IN)
            word_v = h.add_var("word_bus", word_t, VarDir.VD_IN)
            wide_v = h.add_var("wide_bus", wide_t, VarDir.VD_IN)
            den_v  = h.add_var("dense",    wide_t, VarDir.VD_IN)
            h.end_scope()

        with w.begin_vc_block(0) as vc:
            dense_val = 0
            for step in range(steps):
                t_fall = step * 10
                t_rise = step * 10 + 5
                vc.add_change(clk_v, t_fall, 0)
                vc.add_change(clk_v, t_rise, 1)

                if mix_id >= 1:
                    t = t_rise + 1
                    vc.add_change(byte_v, t, step & 0xFF)
                    vc.add_change(word_v, t, step & 0xFFFF_FFFF)
                    wide_val = ((step & 0xFFFF_FFFF) << 32) | (step & 0xFFFF_FFFF)
                    vc.add_change(wide_v, t, wide_val)

                if mix_id >= 2:
                    dense_val ^= 0xDEAD_BEEF_CAFE_BABE
                    vc.add_change(den_v, t_rise + 1, dense_val)


# ---------------------------------------------------------------------------
# gzip helper
# ---------------------------------------------------------------------------

def gzip_file(src, dst):
    """Compress *src* to *dst* with gzip level 6."""
    with open(src, "rb") as f_in, gzip.open(dst, "wb", compresslevel=6) as f_out:
        shutil.copyfileobj(f_in, f_out)
