"""Unit tests for the filter expression language (query_cli_design.md §5)."""

import pytest

from trlog.query import MISSING, EnumVal, LogicVal, QueryError, SchemaView, compile_filter
from trlog.query._expr import FieldSpec, tokenize

SCHEMA = SchemaView(
    timescale_exp=-12,
    types={
        "axi_rd": {"addr": FieldSpec("u64"), "len": FieldSpec("u8"),
                   "resp": FieldSpec("enum", {0: "OKAY", 2: "SLVERR"}),
                   "bus": FieldSpec("logicvec")},
        "msg": {"text": FieldSpec("str"), "len": FieldSpec("u32")},
    },
    stream_paths=["top.a.mon", "top.b.mon"],
)


class T:
    def __init__(self, **kw):
        self.id, self.start, self.end, self.parent = 1, 1000, 2000, None
        self.stream, self.type = "top.a.mon", "axi_rd"
        self.fields, self.meta = {}, {}
        self.__dict__.update(kw)


RD = T(fields={"addr": 0x4000, "len": 4, "resp": EnumVal(2, "SLVERR"),
               "bus": LogicVal(8, 0x0F, 0xF0)}, meta={"note": "hello world"})
MSG = T(id=2, type="msg", stream="top.b.mon", end=None, parent=1,
        fields={"text": "timeout here", "len": 12})


def m(expr, txn, schema=SCHEMA):
    return compile_filter(expr, schema).match(txn)


@pytest.mark.parametrize("expr,rd,msg", [
    ("addr == 0x4000", True, False),
    ("addr == 16384", True, False),
    ("addr == 32'h4000", True, False),
    ("addr == 'h4000", True, False),
    ("addr == 0b100000000000000", True, False),
    ("addr == 0x40_00", True, False),
    ("resp == 'SLVERR'", True, False),
    ("resp == 2", True, False),
    ("resp != 'OKAY'", True, False),
    ("resp in ('OKAY', 'SLVERR')", True, False),
    ("addr in 0x4000..0x4fff", True, False),
    ("len in (1..2, 4)", True, False),
    ("addr not in (1, 2)", True, False),
    ("(addr & 0xfff) == 0", True, False),
    ("addr & 0xfff == 0", True, False),          # Python precedence
    ("addr >> 12 == 4 && bits(addr, 15, 12) == 4 && bit(addr, 14) == 1", True, False),
    ("addr + 1 == 0x4001 && addr * 2 == 0x8000 && addr / 3 == 5461", True, False),
    ("dur > 999ps && dur == 1ns && dur < 0.002us", True, False),
    ("open", False, True),
    ("!open", True, False),
    ("not open and addr > 0", True, False),
    ("addr != 0", True, False),                  # missing addr on MSG -> false
    ("!has(addr)", False, True),
    ("has(dur)", True, False),
    ("len > 10", False, True),                   # field shared by two types
    ("axi_rd.len == 4", True, False),
    ("msg.len == 4", False, False),
    ("text =~ /time.ut/", False, True),
    ("text =~ 'here$'", False, True),
    ("text ~ 'time*'", False, True),
    ("stream ~ 'top.*.mon'", True, True),
    ("stream == 'top.b.mon'", False, True),
    ("type == 'axi_rd' || type == 'msg'", True, True),
    ("type in ('msg')", False, True),
    ("parent == 1", False, True),
    ("meta[\"note\"] == 'hello world'", True, False),
    ("meta['note'] =~ /world/", True, False),
    ("has(meta['note'])", True, False),
    ("len(text) == 12", False, True),
    ("abs(-len) == 4 && min(len, 2) == 2 && max(len, 9) == 9", True, False),
    ("-len < 0 && ~0 == -1 && +len == 4", True, False),
    ("bus == 0", False, False),                  # x/z value never compares equal
    ("has(bus)", True, False),
    ("id == 1 and start == 1ns and end == 2ns", True, False),
    ("true && !false", True, True),
    ("addr == 1 || addr == 0x4000 && len == 4", True, False),
    ("addr / 0 == 1", False, False),             # division by zero -> missing
])
def test_match(expr, rd, msg):
    assert m(expr, RD) is rd
    assert m(expr, MSG) is msg


def test_missing_makes_ne_false():
    assert m("addr != 5", MSG) is False
    assert m("!(addr == 5)", MSG) is True


def test_parent_lookup():
    f = compile_filter("parent.type == 'axi_rd' && parent.addr == 0x4000", SCHEMA)
    assert f.uses_parent
    f.set_parent_lookup({1: RD}.get)
    assert f.match(MSG) is True
    assert f.match(RD) is False


def test_pushdown():
    f = compile_filter("type == 'msg' && stream ~ 'top.*' && len > 1", SCHEMA)
    assert f.pushdown == [("type_eq", "msg"), ("stream_glob", "top.*")]
    f = compile_filter("type == 'msg' || stream ~ 'top.*'", SCHEMA)
    assert f.pushdown == []
    f = compile_filter("stream in ('top.a.mon', 'top.b.mon')", SCHEMA)
    assert f.pushdown == [("stream_in", ("top.a.mon", "top.b.mon"))]


def test_selected_types_restrict_fields():
    sv = SchemaView(SCHEMA.timescale_exp, SCHEMA.types, SCHEMA.stream_paths, {"msg"})
    with pytest.raises(QueryError) as e:
        compile_filter("addr == 1", sv)
    assert e.value.info["error"] == "unknown_field"
    assert "not selected by -T" in e.value.info["hint"]
    assert compile_filter("axi_rd.addr == 1", sv)   # qualified still works


@pytest.mark.parametrize("expr,code,extra", [
    ("adr == 1", "unknown_field", {"did_you_mean": ["addr", "dur"]}),
    ("resp == 'OKY'", "unknown_enum", {"did_you_mean": ["OKAY"]}),
    ("resp in ('OKAY', 'BAD')", "unknown_enum", {}),
    ("addr == 'x'", "type_error", {}),
    ("text == 3", "type_error", {}),
    ("stream == 1", "type_error", {}),
    ("type == 'axi_r'", "unknown_type", {"did_you_mean": ["axi_rd"]}),
    ("stream == 'top.*'", "unknown_stream", {}),
    ("a < b < c", "syntax", {}),             # parse errors come before name checks
    ("len < 1 < 2", "syntax", {}),
    ("addr = 1", "syntax", {}),
    ("foo(addr)", "unknown_function", {}),
    ("has(1)", "syntax", {}),
    ("bits(addr, 1)", "syntax", {}),
    ("axi_rd", "syntax", {}),
    ("axi_wr.addr == 1", "unknown_type", {}),
    ("axi_rd.text == 'x'", "unknown_field", {}),
    ("10qq", "syntax", {}),
    ("", "syntax", {}),
    ("(addr == 1", "syntax", {}),
    ("text == 'abc", "syntax", {}),
    ("text =~ /abc", "syntax", {}),
    ("text =~ /a(/", "syntax", {}),
    ("text ~ 3", "syntax", {}),
    ("meta == 'x'", "syntax", {}),
    ("meta[text] == 'x'", "syntax", {}),
    ("addr == 4'hx", "syntax", {}),
    ("text + 1 == 2", "type_error", {}),
    ("!addr == 1 + !len", "syntax", {}),
])
def test_errors(expr, code, extra):
    with pytest.raises(QueryError) as e:
        compile_filter(expr, SCHEMA)
    info = e.value.info
    assert info["error"] == code, info
    assert info["message"]
    for k, v in extra.items():
        assert info[k] == v


def test_error_position_points_at_token():
    with pytest.raises(QueryError) as e:
        compile_filter("len == 1 && adr == 2", SCHEMA)
    assert e.value.info["pos"] == 12


def test_tokenize_ranges_and_strings():
    kinds = [(t.kind, t.value) for t in tokenize("1.5..2 'hello' 'h40' 'h40 10us")]
    assert kinds[0] == ("float", 1.5)
    assert kinds[1] == ("op", "..")
    assert kinds[2] == ("num", 2)
    assert kinds[3] == ("str", "hello")
    assert kinds[4] == ("str", "h40")
    assert kinds[5] == ("num", 0x40)
    assert kinds[6][0] == "time"
