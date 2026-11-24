"""End to end for the trlog-record sv-weave plugin: weave a class, record objects of it
through the generated ``<cls>_trl::record`` under Verilator, read the trace back in
Python and check names, kinds and values against ``trl_types.json``.

Skipped when sv-weave or verilator is missing or the C libraries are not built
(``cmake --build build``).
"""
from __future__ import annotations

import json
import pathlib
import shutil
import subprocess

import pytest

pytest.importorskip("sv_weave")

from sv_weave import plugin                          # noqa: E402
from sv_weave.config import parse_config             # noqa: E402
from sv_weave.filelist import SourceSpec             # noqa: E402
from sv_weave.run import run                         # noqa: E402

from trlog import TrlReader                          # noqa: E402
from trlog._types import TxnAttrRecord, TxnBegin, TxnEnd, TxnFull   # noqa: E402
from trlog.weave import RecordWeaver                 # noqa: E402

REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
SV_SRC = REPO_ROOT / "sv" / "src"
BUILD_C = REPO_ROOT / "build" / "c"
VERILATOR = shutil.which("verilator")

pytestmark = [
    pytest.mark.skipif(VERILATOR is None, reason="verilator not found in PATH"),
    pytest.mark.skipif(
        not (BUILD_C / "libtrl_sv.so").exists() or not (BUILD_C / "libtrl.so").exists(),
        reason="build/c/libtrl_sv.so / libtrl.so not found; run cmake --build build first",
    ),
]

PKT_SV = """
package pkt_pkg;
  typedef enum bit [1:0] {RD, WR, RMW} op_e;

  class base_c;
    bit [31:0] addr;
  endclass

  (* trl_record *)
  class pkt extends base_c;
    op_e op;
    byte unsigned d[2];
    string tag;
    real r;
    bit flag;
    int signed_v;
    (* trl_name = "len" *) shortint unsigned length;
  endclass
endpackage
"""

TB_SV = """
module tb;
  import trl_pkg::*;
  import pkt_pkg::*;
  import pkt_pkg_woven::*;
  initial begin
    trl_stream s = trl_stream::create("mon", null, null, "bus");
    pkt p = new;
    p.addr = 32'h1000; p.op = WR; p.d[0] = 8'h11; p.d[1] = 8'h22; p.tag = "first";
    p.r = 1.5; p.flag = 1; p.signed_v = -3; p.length = 16'd64;
    if (pkt_trl::record(s, p, 10, 20) == 0) $display("WR_TEST: FAIL record 1");
    p.addr = 32'h2000; p.op = RMW; p.tag = "second"; p.flag = 0; p.signed_v = 7;
    if (pkt_trl::record(s, p, 30, 45) == 0) $display("WR_TEST: FAIL record 2");
    if (pkt_trl::record(null, p) != 0) $display("WR_TEST: FAIL null stream");
    trl_shutdown();
    $display("WR_TEST: DONE");
    $finish;
  end
endmodule
"""


@pytest.fixture(autouse=True)
def _weaver():
    plugin.register_weaver(RecordWeaver)
    yield
    plugin.unregister(RecordWeaver.name)


def _txns(path):
    """{txn_id: (type_id, start, end, {field_idx: value})} from any record form."""
    out = {}
    with TrlReader(str(path)) as r:
        for block in r.iter_txn_blocks():
            for rec in block:
                if isinstance(rec, TxnFull):
                    out[rec.txn_id] = [rec.txn_type_id, rec.start_time, rec.end_time,
                                       {a.field_idx: a.value for a in rec.attrs}]
                elif isinstance(rec, TxnBegin):
                    out[rec.txn_id] = [rec.txn_type_id, rec.start_time, None, {}]
                elif isinstance(rec, TxnAttrRecord):
                    out[rec.txn_id][3].update({a.field_idx: a.value for a in rec.attrs})
                elif isinstance(rec, TxnEnd):
                    out[rec.txn_id][2] = rec.end_time
        st = r.string_table
        schemas = {}
        for tid in {v[0] for v in out.values()}:
            s = r.type_registry.get_txn_schema(tid)
            schemas[tid] = (st.lookup(s.name_str_id),
                            [(st.lookup(f.name_str_id), f.field_type.name) for f in s.fields])
    return out, schemas


def test_weave_record_round_trip(tmp_path):
    src = tmp_path / "pkt_pkg.sv"
    src.write_text(PKT_SV)
    (tmp_path / "tb.sv").write_text(TB_SV)
    woven = tmp_path / "woven"
    cfg = parse_config("sv-weave:\n  weavers: [{name: trlog-record}]\n")
    res = run(cfg, spec=SourceSpec(files=[str(SV_SRC / "trl_pkg.sv"), str(src)],
                                   incdirs=[str(SV_SRC)]), out_dir=str(woven))
    assert res.verify_status == "full"
    (t,) = json.loads((woven / "trl_types.json").read_text())["types"]

    obj_dir = tmp_path / "obj"
    log = tmp_path / "build.log"
    with open(log, "w") as fp:
        rc = subprocess.call(
            [VERILATOR, "--binary", "--sv", "--timing", "-Wno-fatal", "-I" + str(SV_SRC),
             "--top-module", "tb", "-o", "sim", "--Mdir", str(obj_dir),
             "-LDFLAGS", f"-L{BUILD_C} -Wl,-rpath,{BUILD_C} -ltrl_sv -ltrl",
             str(SV_SRC / "trl_pkg.sv"), str(src), str(woven / "pkt_pkg_woven.sv"),
             str(tmp_path / "tb.sv")],
            stdout=fp, stderr=subprocess.STDOUT)
    assert rc == 0, "verilator failed; see %s" % log
    out = subprocess.run([str(obj_dir / "sim"), "+trlog_file=wr.trl"], cwd=str(tmp_path),
                         capture_output=True, text=True, timeout=60)
    assert "WR_TEST: DONE" in out.stdout and "FAIL" not in out.stdout, out.stdout + out.stderr

    txns, schemas = _txns(tmp_path / "wr.trl")
    assert len(txns) == 2
    ((tname, fields),) = schemas.values()
    assert tname == t["name"] == "pkt"
    assert fields == [(f["name"], "FT_" + f["kind"]) for f in t["fields"]]
    names = [n for n, _ in fields]
    by_start = sorted(txns.values(), key=lambda v: v[1])
    vals = [{names[i]: v for i, v in x[3].items()} for x in by_start]
    assert [(x[1], x[2]) for x in by_start] == [(10, 20), (30, 45)]
    assert vals[0] == {"addr": 0x1000, "op": 1, "d[0]": 0x11, "d[1]": 0x22, "tag": "first",
                       "r": 1.5, "flag": True, "signed_v": -3, "len": 64}
    assert (vals[1]["addr"], vals[1]["op"], vals[1]["tag"], vals[1]["flag"],
            vals[1]["signed_v"]) == (0x2000, 2, "second", False, 7)
