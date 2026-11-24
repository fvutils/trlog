"""The trlog-record sv-weave plugin (trlog.weave): recorder classes and type JSON."""
import json
import os
import pathlib
import textwrap

import pytest

pytest.importorskip("sv_weave")

from sv_weave import plugin                          # noqa: E402
from sv_weave.config import parse_config             # noqa: E402
from sv_weave.diag import Severity, SvWeaveError     # noqa: E402
from sv_weave.filelist import SourceSpec             # noqa: E402
from sv_weave.run import run                         # noqa: E402

from trlog.weave import RecordWeaver                 # noqa: E402

SV_SRC = pathlib.Path(__file__).resolve().parents[2] / "sv" / "src"

SRC = """
package pkt_pkg;
  typedef enum bit [1:0] {RD, WR, RMW} op_e;
  typedef struct packed { bit [3:0] a; bit [3:0] b; } hdr_t;

  class base_c;
    bit [31:0] addr;
    protected int hidden;
  endclass

  (* trl_record *)
  class pkt extends base_c;
    op_e op;
    hdr_t hdr;
    byte unsigned d[2];
    string tag;
    real r;
    bit flag;
    int signed_v;
    (* trl_name = "len" *) shortint unsigned length;
    (* trl_skip *) int scratch;
    bit [99:0] wide;
    static int count;
    byte unsigned q[$];
  endclass

  class other;
    int x;
  endclass
endpackage
"""


@pytest.fixture(autouse=True)
def _weaver():
    plugin.register_weaver(RecordWeaver)
    yield
    plugin.unregister(RecordWeaver.name)


def weave(tmp_path, src=SRC, options=None, profiles=None):
    body = {"name": "trlog-record"}
    if options:
        body["options"] = options
    yaml = "sv-weave:\n  weavers: [%s]\n" % json.dumps(body)
    if profiles:
        yaml += textwrap.indent(profiles, "  ")
    cfg = parse_config(yaml)
    spec = SourceSpec(files=[str(SV_SRC / "trl_pkg.sv")], incdirs=[str(SV_SRC)])
    return run(cfg, spec=spec, texts=[("p.sv", src)], out_dir=str(tmp_path))


def warnings(res):
    return [d.msg for d in res.diags if d.severity == Severity.WARNING]


def test_recorder_class_and_fields(tmp_path):
    res = weave(tmp_path)
    assert res.verify_status == "full"          # slang elaborated the woven output
    text = (tmp_path / "pkt_pkg_woven.sv").read_text()
    assert "class pkt_trl;" in text and "other_trl" not in text
    assert "import trl_pkg::*;" in text
    types = json.loads((tmp_path / "trl_types.json").read_text())
    assert types["trlog_types"] == "0.1"
    (t,) = types["types"]
    assert (t["name"], t["sv_class"], t["recorder"]) == ("pkt", "pkt_pkg::pkt",
                                                         "pkt_pkg_woven::pkt_trl")
    assert [(f["name"], f["kind"], f["width"]) for f in t["fields"]] == [
        ("addr", "U32", 32), ("op", "U8", 2), ("hdr", "U8", 8), ("d[0]", "U8", 8),
        ("d[1]", "U8", 8), ("tag", "STRING", 0), ("r", "F64", 0), ("flag", "BOOL", 1),
        ("signed_v", "I32", 32), ("len", "U16", 16)]
    assert "localparam longint unsigned SCHEMA_HASH = 64'h%s;" % t["schema_hash"] in text
    assert "localparam int NF = 10;" in text
    assert "localparam int F_d_1 = 4;" in text
    assert "x.set_int(F_d_1, obj.d[1]);" in text
    assert "x.set_str(F_tag, obj.tag);" in text
    assert "x.set_real(F_r, obj.r);" in text
    assert "x.set_int(F_len, obj.length);" in text
    assert "scratch" not in text and "count" not in text


def test_skipped_fields_warn(tmp_path):
    w = warnings(weave(tmp_path))
    assert any("pkt_pkg::base_c.hidden is protected" in m for m in w)
    assert any("pkt_pkg::pkt.wide is wider than 64 bits" in m for m in w)
    assert any("pkt_pkg::pkt.q is a queue" in m for m in w)
    assert not any("scratch" in m or "count" in m for m in w)


def test_types_json_is_an_artifact_not_compiled(tmp_path):
    res = weave(tmp_path)
    assert [(a.name, a.kind) for a in res.artifacts] == [("trl_types.json", "trlogTypes")]
    assert "trl_types.json" not in res.compile_order


def test_select_option_and_profiles(tmp_path):
    res = weave(tmp_path, options={"select": "named(other)"},
                profiles="profiles: {release: [core], trace: [core]}\n")
    text = (tmp_path / "pkt_pkg_woven.sv").read_text()
    assert text.count("class pkt_trl;") == 1 and text.count("class other_trl;") == 1
    names = [t["name"] for t in json.loads((tmp_path / "trl_types.json").read_text())["types"]]
    assert sorted(names) == ["other", "pkt"]
    assert res.verify_status == "full"


def test_no_recorded_classes_writes_nothing(tmp_path):
    res = weave(tmp_path, src="package q; class c; int x; endclass endpackage\n")
    assert res.artifacts == []
    assert not (tmp_path / "trl_types.json").exists()


def test_type_name_clash(tmp_path):
    src = SRC + """
package b_pkg;
  (* trl_record *) class pkt; int y; endclass
endpackage
"""
    with pytest.raises(SvWeaveError, match="both recorded as type 'pkt'"):
        weave(tmp_path, src=src)


def test_trl_name_on_class(tmp_path):
    src = 'package q; (* trl_record, trl_name = "q_item" *) class c; int x; endclass endpackage\n'
    weave(tmp_path, src=src)
    (t,) = json.loads((tmp_path / "trl_types.json").read_text())["types"]
    assert t["name"] == "q_item"
    assert 'localparam string TYPE_NAME = "q_item";' in (tmp_path / "q_woven.sv").read_text()


def test_schema_hash_tracks_layout(tmp_path):
    def digest(src):
        weave(tmp_path, src=src)
        return json.loads((tmp_path / "trl_types.json").read_text())["types"][0]["schema_hash"]
    a = digest("package q; (* trl_record *) class c; int x; endclass endpackage\n")
    b = digest("package q; (* trl_record *) class c; int x; endclass endpackage\n// edit\n")
    c = digest("package q; (* trl_record *) class c; shortint x; endclass endpackage\n")
    assert a == b != c


# -- UVM: the woven do_record ----------------------------------------------------------

REPO = pathlib.Path(__file__).resolve().parents[2]
UVM_SRC = REPO / "packages" / "verilator" / "share" / "uvm" / "src"
UVM_BINDING = REPO / "packages" / "sv-weave" / "examples" / "uvm"

UVM_ITEM = """
`include "uvm_macros.svh"
package tx_pkg;
  import uvm_pkg::*;
  typedef enum bit [1:0] {RD, WR} op_e;
  (* trl_record *)
  class tx extends uvm_sequence_item;
    `uvm_object_utils(tx)
    bit [31:0] addr;
    op_e op;
    int signed_v;
    real r;
    string tag;
    time t;
    function new(string name = "tx"); super.new(name); endfunction
  endclass
endpackage
"""


def weave_uvm(tmp_path, src=UVM_ITEM):
    yaml = ("sv-weave:\n  binding: \"uvm_binding:UvmBinding\"\n"
            "  plugin-path: [%s]\n  packages: {include: [tx_pkg]}\n"
            "  weavers: [{name: trlog-record}]\n" % UVM_BINDING)
    spec = SourceSpec(files=[str(UVM_SRC / "uvm_pkg.sv"), str(SV_SRC / "trl_pkg.sv"),
                             str(SV_SRC / "trl_uvm_pkg.sv")],
                      incdirs=[str(UVM_SRC), str(SV_SRC)], defines={"UVM_NO_DPI": None})
    return run(parse_config(yaml), spec=spec, texts=[("tx.sv", src)], out_dir=str(tmp_path))


@pytest.mark.skipif(not UVM_SRC.is_dir(), reason="no UVM sources (packages/verilator)")
def test_uvm_do_record(tmp_path):
    res = weave_uvm(tmp_path)
    assert res.verify_status == "full"
    text = (tmp_path / "tx_pkg_woven.sv").read_text()
    assert text.index("typedef class tx_trl;") < text.index("class Woven_tx")
    body = text[text.index("function void do_record"):]
    body = body[:body.index("endfunction")]
    assert "__trl_r.open_typed(tx_trl::type_())" in body
    assert "tx_trl::set_fields(__trl_x, this);" in body
    # generic path: super first, then one call per field
    assert body.index("super.do_record(recorder);") < body.index('record_field_int("addr"')
    assert 'recorder.record_field_int("addr", addr, 32, uvm_pkg::UVM_NORADIX);' in body
    assert 'recorder.record_string("op", op.name());' in body
    assert 'recorder.record_field_int("signed_v", signed_v, 32, uvm_pkg::UVM_DEC);' in body
    assert 'recorder.record_field_real("r", r);' in body
    assert 'recorder.record_string("tag", tag);' in body
    assert 'recorder.record_time("t", t);' in body
    assert body.count("super.do_record") == 1
    (t,) = json.loads((tmp_path / "trl_types.json").read_text())["types"]
    assert [f["name"] for f in t["fields"]] == ["addr", "op", "signed_v", "r", "tag", "t"]
