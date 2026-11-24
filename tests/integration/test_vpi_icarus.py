"""VPI library under Icarus Verilog: dump limit and callback value format.

Issue #20: ``$trlog_dumplimit`` estimated the file size from a change count
that was reset at every block flush, so a limit larger than one block's
worth of changes never triggered.

Issue #21: Icarus printed "value format 12 not supported" for every traced
change because the value-change callbacks asked for ``vpiObjTypeVal``.

Skipped when ``iverilog``/``vvp`` or the built libraries are not available.
"""

import os
import shutil
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
BUILD = REPO / "build"
LIB_VPI = BUILD / "vpi" / "libtrlog_vpi.so"
LIB_TRL = BUILD / "c" / "libtrl.so"
SMOKE_SV = REPO / "vpi" / "tests" / "sv" / "smoke_dut.sv"


def _find_tool(name):
    path = shutil.which(name)
    if path:
        return path
    for cand in sorted(Path("/tools/iverilog").glob(f"*/bin/{name}")):
        return str(cand)
    return None


IVERILOG = _find_tool("iverilog")
VVP = _find_tool("vvp")

pytestmark = pytest.mark.skipif(
    not (IVERILOG and VVP and LIB_VPI.exists() and LIB_TRL.exists()),
    reason="iverilog/vvp or libtrlog_vpi.so/libtrl.so not available",
)


LIMIT_SV = """\
module limit_dut;
    logic        clk      = 1'b0;
    logic [31:0] word_bus = 32'h0;
    always #5 clk = ~clk;
    always @(posedge clk) word_bus <= word_bus + 32'd1;
    integer i;
    initial begin
        $trlog_dumpfile("limit.trl");
        $trlog_dumplimit(4000);
        $trlog_dumpvars(0, limit_dut);
        for (i = 0; i < 400; i++) @(posedge clk);
        $finish;
    end
endmodule
"""


def _run(tmp_path, sv_file, plusargs=()):
    (tmp_path / "trlog_vpi.vpi").symlink_to(LIB_VPI)
    vvp_file = tmp_path / "sim.vvp"
    subprocess.check_call([IVERILOG, "-g2012", "-o", str(vvp_file), str(sv_file)],
                          cwd=tmp_path)
    env = dict(os.environ)
    env["LD_LIBRARY_PATH"] = f"{LIB_TRL.parent}:{env.get('LD_LIBRARY_PATH', '')}"
    proc = subprocess.run([VVP, "-M.", "-mtrlog_vpi", str(vvp_file), *plusargs],
                          cwd=tmp_path, env=env, capture_output=True, text=True,
                          timeout=120)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    return proc.stdout + proc.stderr


def _total_changes(path):
    from trlog import TrlReader
    r = TrlReader(str(path))
    try:
        return sum(len(blk) for blk in r.iter_vc_blocks())
    finally:
        r.close()


def test_no_value_format_warning(tmp_path):
    out = _run(tmp_path, SMOKE_SV)
    assert "value format" not in out
    assert _total_changes(tmp_path / "smoke.trl") > 0


def test_dumplimit_triggers_across_block_flushes(tmp_path):
    sv = tmp_path / "limit_dut.sv"
    sv.write_text(LIMIT_SV)
    # Flush every 10 changes: one block holds far less than the limit.
    out = _run(tmp_path, sv, ["+trlog_flush_interval=10"])
    assert "dump limit reached" in out
    # 400 cycles x 3 changes would be ~1200 changes without the limit.
    n = _total_changes(tmp_path / "limit.trl")
    assert 0 < n < 600
