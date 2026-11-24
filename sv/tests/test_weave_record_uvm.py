"""End to end for UVM recording through trlog: weave a uvm_sequence_item with the
trlog-record plugin (and sv-weave's example UVM binding), record it with
begin_tr/end_tr into trl_uvm_pkg under Verilator, and read the trace back.

Checks the typed fast path (woven do_record -> one typed transaction), the generic
path (a `uvm_field_*` class -> the stream's ad-hoc type), begin_tr's label as
metadata, and a parent/child link.

Skipped when sv-weave, verilator, Verilator's UVM sources or the C libraries are
missing. Compiling UVM takes a few minutes.
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
from trlog._types import (TxnAttrRecord, TxnBegin, TxnEnd, TxnFull,  # noqa: E402
                          TxnLink, TxnMeta, LinkType)
from trlog.weave import RecordWeaver                 # noqa: E402

REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
SV_SRC = REPO_ROOT / "sv" / "src"
BUILD_C = REPO_ROOT / "build" / "c"
UVM_SRC = REPO_ROOT / "packages" / "verilator" / "share" / "uvm" / "src"
UVM_BINDING = REPO_ROOT / "packages" / "sv-weave" / "examples" / "uvm"
VERILATOR = shutil.which("verilator")

pytestmark = [
    pytest.mark.skipif(VERILATOR is None, reason="verilator not found in PATH"),
    pytest.mark.skipif(not UVM_SRC.is_dir(), reason="no UVM sources (packages/verilator)"),
    pytest.mark.skipif(
        not (BUILD_C / "libtrl_sv.so").exists() or not (BUILD_C / "libtrl.so").exists(),
        reason="build/c/libtrl_sv.so / libtrl.so not found; run cmake --build build first",
    ),
]

TX_SV = """
`include "uvm_macros.svh"
package tx_pkg;
  import uvm_pkg::*;
  typedef enum bit [1:0] {RD, WR, RMW} op_e;

  (* trl_record *)
  class tx extends uvm_sequence_item;
    `uvm_object_utils(tx)
    bit [31:0] addr;
    op_e op;
    int signed_v;
    string tag;
    function new(string name = "tx"); super.new(name); endfunction
  endclass

  // Not woven: recorded through uvm_field_* and the generic path.
  class gen_item extends uvm_sequence_item;
    bit [15:0] x;
    string s;
    `uvm_object_utils_begin(gen_item)
      `uvm_field_int(x, UVM_ALL_ON)
      `uvm_field_string(s, UVM_ALL_ON)
    `uvm_object_utils_end
    function new(string name = "gen_item"); super.new(name); endfunction
  endclass

  class mon extends uvm_component;
    `uvm_component_utils(mon)
    function new(string name, uvm_component parent); super.new(name, parent); endfunction
    task run_phase(uvm_phase phase);
      tx a, b;
      gen_item g;
      int ph;
      phase.raise_objection(this);
      #10;
      a = tx::type_id::create("a");
      a.addr = 32'h1000; a.op = WR; a.signed_v = -3; a.tag = "parent";
      ph = begin_tr(a, "main", "lbl");
      #5;
      b = tx::type_id::create("b");
      b.addr = 32'h2000; b.op = RMW; b.signed_v = 7; b.tag = "child";
      void'(begin_tr(b, "main", "", "", 0, ph));
      #5;
      end_tr(b);
      #5;
      end_tr(a);
      g = gen_item::type_id::create("g");
      g.x = 16'h55; g.s = "gen";
      void'(begin_tr(g, "main"));
      #3;
      end_tr(g);
      phase.drop_objection(this);
    endtask
  endclass

  class test extends uvm_test;
    `uvm_component_utils(test)
    mon m;
    function new(string name, uvm_component parent); super.new(name, parent); endfunction
    function void build_phase(uvm_phase phase);
      void'(trl_uvm_pkg::trl_uvm_tr_database::install());
      m = mon::type_id::create("m", this);
    endfunction
    function void final_phase(uvm_phase phase);
      trl_pkg::trl_shutdown();
    endfunction
  endclass
endpackage
"""

TB_SV = """
module tb;
  import uvm_pkg::*;
  sv_weave_init_m u_init();     // registers the woven classes with the factory
  initial begin
    uvm_config_db#(int)::set(null, "*", "recording_detail", UVM_FULL);
    run_test("test");
  end
endmodule
"""


@pytest.fixture(autouse=True)
def _weaver():
    plugin.register_weaver(RecordWeaver)
    yield
    plugin.unregister(RecordWeaver.name)


def _read(path):
    txns, links, meta = {}, [], {}
    with TrlReader(str(path)) as r:
        st = r.string_table
        for block in r.iter_txn_blocks():
            for rec in block:
                if isinstance(rec, TxnFull):
                    txns[rec.txn_id] = [rec.txn_type_id, rec.start_time, rec.end_time,
                                        rec.parent_txn_id,
                                        {a.field_idx: a.value for a in rec.attrs}]
                elif isinstance(rec, TxnBegin):
                    txns[rec.txn_id] = [rec.txn_type_id, rec.start_time, None,
                                        rec.parent_txn_id, {}]
                elif isinstance(rec, TxnAttrRecord):
                    txns[rec.txn_id][4].update({a.field_idx: a.value for a in rec.attrs})
                elif isinstance(rec, TxnEnd):
                    txns[rec.txn_id][2] = rec.end_time
                elif isinstance(rec, TxnLink):
                    links.append((rec.link_type, rec.source_txn_id, rec.target_txn_id))
                elif isinstance(rec, TxnMeta):
                    meta.setdefault(rec.txn_id, {})[st.lookup(rec.key_str_id)] = \
                        st.lookup(rec.value_str_id)
        out = {}
        for tid, (ty, t0, t1, parent, attrs) in txns.items():
            s = r.type_registry.get_txn_schema(ty)
            names = [st.lookup(f.name_str_id) for f in s.fields]
            out[tid] = dict(type=st.lookup(s.name_str_id), start=t0, end=t1, parent=parent,
                            fields={names[i]: v for i, v in attrs.items()})
    return out, links, meta


def test_uvm_record_round_trip(tmp_path):
    src = tmp_path / "tx_pkg.sv"
    src.write_text(TX_SV)
    (tmp_path / "tb.sv").write_text(TB_SV)
    woven = tmp_path / "woven"
    yaml = ("sv-weave:\n  binding: \"uvm_binding:UvmBinding\"\n"
            "  plugin-path: [%s]\n  packages: {include: [tx_pkg]}\n"
            "  weavers: [{name: trlog-record}]\n" % UVM_BINDING)
    lib = [str(UVM_SRC / "uvm_pkg.sv"), str(SV_SRC / "trl_pkg.sv"), str(SV_SRC / "trl_uvm_pkg.sv")]
    res = run(parse_config(yaml),
              spec=SourceSpec(files=lib + [str(src)], incdirs=[str(UVM_SRC), str(SV_SRC)],
                              defines={"UVM_NO_DPI": None}),
              out_dir=str(woven))
    assert res.verify_status == "full"
    (t,) = json.loads((woven / "trl_types.json").read_text())["types"]

    files = [lib[0]] + list(res.runtime_files) + lib[1:] + [str(src)] + res.woven_files + \
        [str(tmp_path / "tb.sv")]
    obj_dir = tmp_path / "obj"
    log = tmp_path / "build.log"
    with open(log, "w") as fp:
        rc = subprocess.call(
            [VERILATOR, "--binary", "--sv", "--timing", "-Wno-fatal", "-Wno-lint",
             "-Wno-style", "+define+UVM_NO_DPI", "-I" + str(UVM_SRC), "-I" + str(SV_SRC),
             "--top-module", "tb", "-o", "sim", "--Mdir", str(obj_dir), "-j", "0",
             "-LDFLAGS", f"-L{BUILD_C} -Wl,-rpath,{BUILD_C} -ltrl_sv -ltrl"] + files,
            stdout=fp, stderr=subprocess.STDOUT, timeout=1500)
    assert rc == 0, "verilator failed; see %s" % log
    out = subprocess.run([str(obj_dir / "sim"), "+trlog_file=uvm.trl"], cwd=str(tmp_path),
                         capture_output=True, text=True, timeout=120)
    (tmp_path / "sim.log").write_text(out.stdout + out.stderr)
    assert out.returncode == 0 and "UVM_ERROR :    0" in out.stdout, "see %s" % (tmp_path / "sim.log")

    txns, links, meta = _read(tmp_path / "uvm.trl")
    typed = {v["fields"]["tag"]: (k, v) for k, v in txns.items() if v["type"] == "tx"}
    (pid, p), (cid, c) = typed["parent"], typed["child"]
    # typed fast path: the woven layout, nothing from uvm_transaction
    assert list(p["fields"]) == [f["name"] for f in t["fields"]]
    assert p["fields"] == {"addr": 0x1000, "op": 1, "signed_v": -3, "tag": "parent"}
    assert c["fields"] == {"addr": 0x2000, "op": 2, "signed_v": 7, "tag": "child"}
    assert (p["start"], p["end"], c["start"], c["end"]) == (10, 25, 15, 20)
    # the child closed first: the parent links to it when it opens
    assert (LinkType.LT_PARENT_CHILD, pid, cid) in links or c["parent"] == pid
    assert meta.get(pid, {}).get("label") == "lbl"
    # generic path
    (g,) = [v for v in txns.values() if v["type"] != "tx"]
    assert g["fields"].get("x") == 0x55 and g["fields"].get("s") == "gen"
    assert (g["start"], g["end"]) == (25, 28)
