"""Storage comparison benchmark: VCD vs FST vs TRLOG.

For each (mix, scale) combination this test:
  1. Runs Verilator VCD binary if available        → trace.vcd
  2. Runs Verilator FST binary if available        → trace_sim.fst
  3. Runs VCS VCD binary if available              → trace_vcs.vcd
  4. Runs VCS FSDB binary if available             → trace_vcs.fsdb
  5. Picks the first available VCD as the baseline for conversions
  6. Converts baseline VCD → FST via vcd2fst       → trace_conv.fst
  7. Converts baseline VCD → FSDB via vcd2fsdb     → trace_conv.fsdb
  8. Writes the same signal activity via TrlWriter → raw and compressed
  9. gzip-compresses the baseline VCD             → vcd.gz

File sizes are printed as an aligned table and recorded as pytest properties
so they appear in JUnit XML output.
"""

import gzip
import os
import shutil
import subprocess
import sys

import pytest

sys.path.insert(0, os.path.dirname(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "../../python/src"))

from _bench_utils import (
    MIXES,
    SCALES,
    VCD2FST,
    VCD2FSDB,
    gzip_file,
    run_simulation,
    run_simulation_fsdb,
    write_trl,
)

# ---------------------------------------------------------------------------
# Parametrize over all (mix, scale) combinations
# ---------------------------------------------------------------------------

WORKLOADS = [
    pytest.param(("clk_only", "small"),   id="clk_only-small"),
    pytest.param(("clk_only", "medium"),  id="clk_only-medium"),
    pytest.param(("clk_only", "large"),   id="clk_only-large"),
    pytest.param(("bus_mix",  "small"),   id="bus_mix-small"),
    pytest.param(("bus_mix",  "medium"),  id="bus_mix-medium"),
    pytest.param(("bus_mix",  "large"),   id="bus_mix-large"),
    pytest.param(("dense",    "small"),   id="dense-small"),
    pytest.param(("dense",    "medium"),  id="dense-medium"),
    pytest.param(("dense",    "large"),   id="dense-large"),
]


# ---------------------------------------------------------------------------
# Test
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("workload", WORKLOADS)
def test_storage_sizes(workload, compiled_sims, tmp_path, record_property):
    mix_name, scale_name = workload
    steps = SCALES[scale_name]
    sims  = compiled_sims[mix_name]

    # ---- 1. Verilator VCD (optional) ------------------------------------
    vcd_path = tmp_path / "trace.vcd"
    if sims.get("vcd_bin") is not None:
        run_simulation(sims["vcd_bin"], steps, tmp_path / "vcd_run").rename(vcd_path)

    # ---- 2. Verilator FST (optional) ------------------------------------
    fst_sim_path = tmp_path / "trace_sim.fst"
    if sims.get("fst_bin") is not None:
        run_simulation(sims["fst_bin"], steps, tmp_path / "fst_run").rename(fst_sim_path)

    # ---- 3. VCS VCD (optional) ------------------------------------------
    vcs_vcd_path = tmp_path / "trace_vcs.vcd"
    if sims.get("vcs_vcd_bin") is not None:
        run_simulation(sims["vcs_vcd_bin"], steps, tmp_path / "vcs_vcd_run").rename(vcs_vcd_path)

    # ---- 4. VCS FSDB (optional) -----------------------------------------
    vcs_fsdb_path = tmp_path / "trace_vcs.fsdb"
    if sims.get("vcs_fsdb_bin") is not None:
        run_simulation_fsdb(
            sims["vcs_fsdb_bin"], steps, tmp_path / "vcs_fsdb_run"
        ).rename(vcs_fsdb_path)

    # ---- 5. Choose baseline VCD for conversions and percentages ----------
    # Prefer Verilator VCD; fall back to VCS VCD; skip if neither exists.
    if vcd_path.exists():
        baseline_vcd = vcd_path
        baseline_label = "VCD (Verilator)"
    elif vcs_vcd_path.exists():
        baseline_vcd = vcs_vcd_path
        baseline_label = "VCD (VCS)"
    else:
        pytest.skip("no VCD baseline available (all simulator compiles failed)")

    # ---- 6. Convert baseline VCD → FST and FSDB -------------------------
    fst_conv_path  = tmp_path / "trace_conv.fst"
    fsdb_conv_path = tmp_path / "trace_conv.fsdb"
    if VCD2FST:
        subprocess.check_call(
            [VCD2FST, str(baseline_vcd), str(fst_conv_path)],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
    if VCD2FSDB:
        subprocess.check_call(
            [VCD2FSDB, str(baseline_vcd), "-o", str(fsdb_conv_path)],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )

    # ---- 7. gzip the baseline VCD ---------------------------------------
    vcd_gz_path = tmp_path / "trace.vcd.gz"
    gzip_file(baseline_vcd, vcd_gz_path)

    # ---- 8. Write TRLOG in three compression modes -----------------------
    # raw:      XOR-delta + RLE encoding, no further compression
    # wave_lz4: per-signal LZ4 on top of XOR-delta + RLE
    # zlib:     whole-block zlib on top of XOR-delta + RLE
    trl_raw_path  = tmp_path / "trace_raw.trl"
    trl_lz4_path  = tmp_path / "trace_lz4.trl"
    trl_cmp_path  = tmp_path / "trace_zlib.trl"
    trl_wzlib_path = tmp_path / "trace_wave_zlib.trl"
    trl_scope_path = tmp_path / "trace_scope.trl"
    write_trl(trl_raw_path,  mix_name, steps, compress=False, compress_waves=False)
    write_trl(trl_lz4_path,  mix_name, steps, compress=False, compress_waves=True)
    write_trl(trl_cmp_path,  mix_name, steps, compress=True)
    write_trl(trl_wzlib_path, mix_name, steps, compress=False, compress_waves=True,
              compress_waves_alg="zlib", seekable=True)
    write_trl(trl_scope_path, mix_name, steps, compress=False, compress_waves=True,
              compress_waves_alg="zlib", seekable=True, scope_grouped=True)

    # ---- Collect sizes --------------------------------------------------
    def sz(p):
        return p.stat().st_size if p.exists() else None

    sizes = {
        "vcd":            sz(vcd_path) if vcd_path.exists() else None,
        "vcd_gz":         sz(vcd_gz_path),
        "fst_sim":        sz(fst_sim_path) if fst_sim_path.exists() else None,
        "fst_conv":       sz(fst_conv_path) if fst_conv_path.exists() else None,
        "fsdb_conv":      sz(fsdb_conv_path) if VCD2FSDB else None,
        "vcs_vcd":        sz(vcs_vcd_path) if vcs_vcd_path.exists() else None,
        "vcs_fsdb":       sz(vcs_fsdb_path) if vcs_fsdb_path.exists() else None,
        "trl_raw":        sz(trl_raw_path),
        "trl_lz4":        sz(trl_lz4_path),
        "trl_compressed": sz(trl_cmp_path),
        "trl_wave_zlib":  sz(trl_wzlib_path),
        "trl_scope":      sz(trl_scope_path),
    }

    # Baseline for percentage column: whichever VCD we used for conversions.
    vcd_size = baseline_vcd.stat().st_size

    # ---- Print table ----------------------------------------------------
    label = f"{mix_name}/{scale_name} ({steps:,} cycles)"
    print(f"\n{'='*65}")
    print(f"  {label}")
    print(f"{'='*65}")
    print(f"  {'Format':<24} {'Bytes':>14}  {'% of base':>9}  {'vs prev':>8}")
    print(f"  {'(baseline: ' + baseline_label + ')':<24}")
    print(f"  {'-'*24}  {'-'*14}  {'-'*8}  {'-'*8}")

    rows = [
        ("vcd",            "VCD (Verilator)"),
        ("vcd_gz",         "VCD.gz (level 6)"),
        ("fst_sim",        "FST (Verilator)"),
        ("fst_conv",       "FST (vcd2fst)"),
        ("fsdb_conv",      "FSDB (vcd2fsdb)"),
        ("vcs_vcd",        "VCD (VCS)"),
        ("vcs_fsdb",       "FSDB (VCS native)"),
        ("trl_raw",        "TRLOG (raw)"),
        ("trl_lz4",        "TRLOG (wave LZ4)"),
        ("trl_compressed", "TRLOG (block zlib)"),
        ("trl_wave_zlib",  "TRLOG (wave zlib)"),
        ("trl_scope",      "TRLOG (scope grp)"),
    ]

    prev = None
    for key, label_col in rows:
        v = sizes[key]
        if v is None:
            print(f"  {label_col:<24}  {'N/A':>14}")
            continue
        pct   = f"{v / vcd_size * 100:.1f}%"
        vs_p  = f"{v / prev * 100:.1f}%" if prev is not None else "—"
        print(f"  {label_col:<24}  {v:>14,}  {pct:>9}  {vs_p:>8}")
        prev = v

    # ---- Record as pytest properties (appear in JUnit XML) --------------
    tag = f"{mix_name}_{scale_name}"
    for k, v in sizes.items():
        if v is not None:
            record_property(f"{tag}_{k}_bytes", v)

    # ---- Sanity checks --------------------------------------------------
    if sizes["fst_sim"] is not None:
        assert sizes["fst_sim"] > 0, "FST file is empty"
        assert sizes["fst_sim"] < vcd_size, (
            f"FST ({sizes['fst_sim']:,}) should be < baseline VCD ({vcd_size:,})"
        )
    if sizes["fsdb_conv"] is not None:
        assert sizes["fsdb_conv"] < vcd_size, (
            f"FSDB-conv ({sizes['fsdb_conv']:,}) should be < baseline VCD ({vcd_size:,})"
        )
    if sizes["vcs_fsdb"] is not None:
        assert sizes["vcs_fsdb"] < vcd_size, (
            f"VCS FSDB ({sizes['vcs_fsdb']:,}) should be < baseline VCD ({vcd_size:,})"
        )
