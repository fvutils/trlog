"""Shared utilities for benchmark tests: workload definitions, TRLOG writer helper,
and gzip helper.  Imported by both conftest.py and test modules.
"""

from __future__ import annotations

import gzip
import os
import shutil
import subprocess
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "../../python/src"))

from trlog import TrlWriter
from trlog._types import SignalEncoding, ScopeType, VarDir

# ---------------------------------------------------------------------------
# Tool paths
# ---------------------------------------------------------------------------

VERILATOR = shutil.which("verilator")
VCD2FST   = shutil.which("vcd2fst")
VCS       = shutil.which("vcs")
VCD2FSDB  = shutil.which("vcd2fsdb")
FSDB2VCD  = shutil.which("fsdb2vcd")
SV_FILE   = os.path.join(os.path.dirname(__file__), "../sv/bench_dut.sv")
SV_FILE   = os.path.abspath(SV_FILE)

# VERDI_HOME must be set for VCS+FSDB compilation (-debug_access links the
# Verdi runtime automatically in VCS 2024.09+).
VERDI_HOME = os.environ.get("VERDI_HOME", "") or None

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
    """Run *binary* with +steps=N in *run_dir*; returns path to the trace file.

    For VCD/FST binaries the trace is ``trace.out``.
    For VCS FSDB binaries Verdi always appends ``.fsdb``; call
    ``run_simulation_fsdb`` for those instead.
    """
    import subprocess
    run_dir.mkdir(parents=True, exist_ok=True)
    subprocess.check_call(
        [str(binary), f"+steps={steps}"],
        cwd=str(run_dir),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    return run_dir / "trace.out"


def run_simulation_fsdb(binary, steps, run_dir):
    """Like ``run_simulation`` but for VCS FSDB binaries.

    Verdi appends ``.fsdb`` when the dump file base-name has no extension,
    so ``$fsdbDumpfile("trace")`` → ``trace.fsdb``.
    """
    import subprocess
    run_dir.mkdir(parents=True, exist_ok=True)
    subprocess.check_call(
        [str(binary), f"+steps={steps}"],
        cwd=str(run_dir),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    return run_dir / "trace.fsdb"

# ---------------------------------------------------------------------------
# VCS compiler
# ---------------------------------------------------------------------------

def compile_vcs(base_dir, mix_id, fmt):
    """Compile *SV_FILE* with VCS for *fmt* ('vcd' or 'fsdb') output.

    Returns path to the produced simulation binary, or raises on failure.
    Intermediate VCS build artifacts are isolated inside *base_dir*.
    """
    bin_path = base_dir / f"simv_vcs_{fmt}"
    cmd = [
        VCS, "-sverilog", "-full64",
        # VCS uses -pvalue+<hier_path>=<value> for parameter override;
        # -G<param>=<value> (Verilator syntax) is silently ignored by VCS.
        f"-pvalue+bench_dut.SIGNAL_MIX={mix_id}",
        "-o", str(bin_path),
        # Keep intermediate files inside base_dir (csrc/, simv.daidir/, etc.)
        f"-Mdir={base_dir}/csrc_vcs_{fmt}",
    ]
    if fmt == "fsdb":
        if VERDI_HOME is None:
            raise RuntimeError("VERDI_HOME not set; cannot compile VCS FSDB binary")
        # VCS 2024.09+: -debug_access replaces the deprecated -P novas.tab pli.a
        # approach; VERDI_HOME in the environment is sufficient for linking.
        cmd += ["+define+DUMP_FSDB", "-debug_access"]
    cmd.append(SV_FILE)
    subprocess.check_call(
        cmd,
        cwd=str(base_dir),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    return bin_path


# ---------------------------------------------------------------------------
# TRLOG writer  (replicates bench_dut signal pattern in Python)
# ---------------------------------------------------------------------------

def write_trl(path, mix_name, steps, compress,
              compress_time_table=False, compress_waves=False,
              compress_waves_alg="lz4", seekable=False,
              scope_grouped=False):
    """Write a TRLOG file mirroring bench_dut signal activity.

    Clock half-period is 5 ns; other signals change at each posedge.
    """
    mix_id = MIXES[mix_name]

    with TrlWriter(str(path), timescale_exp=-9, compress=compress,
                   compress_time_table=compress_time_table,
                   compress_waves=compress_waves,
                   compress_waves_alg=compress_waves_alg,
                   seekable=seekable,
                   scope_grouped=scope_grouped) as w:
        clk_t  = w.add_signal_type(SignalEncoding.SE_2STATE, 1)
        byte_t = w.add_signal_type(SignalEncoding.SE_2STATE, 8)
        word_t = w.add_signal_type(SignalEncoding.SE_2STATE, 32)
        wide_t = w.add_signal_type(SignalEncoding.SE_2STATE, 64)

        with w.begin_hierarchy() as h:
            if scope_grouped:
                # Two sub-scopes for exercising scope-grouped blocks
                h.begin_scope(ScopeType.ST_MODULE, "bench_dut")
                h.begin_scope(ScopeType.ST_MODULE, "clocking")
                clk_v  = h.add_var("clk", clk_t, VarDir.VD_IN)
                h.end_scope()
                h.begin_scope(ScopeType.ST_MODULE, "datapath")
                byte_v = h.add_var("byte_bus", byte_t, VarDir.VD_IN)
                word_v = h.add_var("word_bus", word_t, VarDir.VD_IN)
                wide_v = h.add_var("wide_bus", wide_t, VarDir.VD_IN)
                den_v  = h.add_var("dense",    wide_t, VarDir.VD_IN)
                h.end_scope()
                h.end_scope()
            else:
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
