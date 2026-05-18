"""Storage comparison benchmark: VCD vs FST vs TRLOG.

For each (mix, scale) combination this test:
  1. Runs the Verilator VCD-instrumented binary  → trace.out  (VCD data)
  2. Runs the Verilator FST-instrumented binary  → trace.out  (FST data)
  3. Converts the VCD output to FST via vcd2fst  → converted.fst
  4. Writes the same signal activity via TrlWriter → raw and compressed
  5. gzip-compresses the VCD                      → vcd.gz

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
    gzip_file,
    run_simulation,
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

    # ---- 1. Run VCD simulation -----------------------------------------
    vcd_run = tmp_path / "vcd_run"
    vcd_trace = run_simulation(sims["vcd_bin"], steps, vcd_run)
    vcd_path = tmp_path / "trace.vcd"
    vcd_trace.rename(vcd_path)

    # ---- 2. Run FST simulation -----------------------------------------
    fst_run = tmp_path / "fst_run"
    fst_trace = run_simulation(sims["fst_bin"], steps, fst_run)
    fst_sim_path = tmp_path / "trace_sim.fst"
    fst_trace.rename(fst_sim_path)

    # ---- 3. Convert VCD → FST via vcd2fst -------------------------------
    fst_conv_path = tmp_path / "trace_conv.fst"
    if VCD2FST:
        subprocess.check_call(
            [VCD2FST, str(vcd_path), str(fst_conv_path)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

    # ---- 4. Write TRLOG (raw, domain-compressed, and whole-block compressed) ---
    trl_raw_path  = tmp_path / "trace_raw.trl"
    trl_dom_path  = tmp_path / "trace_domain.trl"
    trl_cmp_path  = tmp_path / "trace_compressed.trl"
    write_trl(trl_raw_path,  mix_name, steps, compress=False)
    write_trl(trl_dom_path,  mix_name, steps, compress=False,
              compress_time_table=True, compress_waves=True)
    write_trl(trl_cmp_path,  mix_name, steps, compress=True)

    # ---- 5. gzip the VCD -----------------------------------------------
    vcd_gz_path = tmp_path / "trace.vcd.gz"
    gzip_file(vcd_path, vcd_gz_path)

    # ---- Collect sizes --------------------------------------------------
    def sz(p):
        return p.stat().st_size if p.exists() else None

    sizes = {
        "vcd":            sz(vcd_path),
        "vcd_gz":         sz(vcd_gz_path),
        "fst_sim":        sz(fst_sim_path),
        "fst_conv":       sz(fst_conv_path) if VCD2FST else None,
        "trl_raw":        sz(trl_raw_path),
        "trl_domain":     sz(trl_dom_path),
        "trl_compressed": sz(trl_cmp_path),
    }

    vcd_size = sizes["vcd"]

    # ---- Print table ----------------------------------------------------
    label = f"{mix_name}/{scale_name} ({steps:,} cycles)"
    print(f"\n{'='*65}")
    print(f"  {label}")
    print(f"{'='*65}")
    print(f"  {'Format':<20} {'Bytes':>14}  {'% of VCD':>8}  {'vs prev':>8}")
    print(f"  {'-'*20}  {'-'*14}  {'-'*8}  {'-'*8}")

    rows = [
        ("vcd",            "VCD (Verilator)"),
        ("vcd_gz",         "VCD.gz (level 6)"),
        ("fst_sim",        "FST (Verilator)"),
        ("fst_conv",       "FST (vcd2fst)"),
        ("trl_raw",        "TRLOG (raw)"),
        ("trl_domain",     "TRLOG (time+wave compr)"),
        ("trl_compressed", "TRLOG (compressed)"),
    ]

    prev = None
    for key, label_col in rows:
        v = sizes[key]
        if v is None:
            print(f"  {label_col:<20}  {'N/A':>14}")
            continue
        pct   = f"{v / vcd_size * 100:.1f}%"
        vs_p  = f"{v / prev * 100:.1f}%" if prev is not None else "—"
        print(f"  {label_col:<20}  {v:>14,}  {pct:>8}  {vs_p:>8}")
        prev = v

    # ---- Record as pytest properties (appear in JUnit XML) --------------
    tag = f"{mix_name}_{scale_name}"
    for key, v in sizes.items():
        if v is not None:
            record_property(f"{tag}_{key}_bytes", v)

    # ---- Sanity checks --------------------------------------------------
    assert sizes["vcd"]     > 0,   "VCD file is empty"
    assert sizes["fst_sim"] > 0,   "FST file is empty"
    assert sizes["fst_sim"] < sizes["vcd"], \
        f"FST ({sizes['fst_sim']:,}) should be smaller than VCD ({sizes['vcd']:,})"
