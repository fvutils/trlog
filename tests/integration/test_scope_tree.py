"""Public scope-tree API on TrlReader (issue #24, Q4)."""
from trlog import TrlWriter, TrlReader, ScopeNode
from trlog._types import ScopeType, SignalEncoding


def _write(path):
    with TrlWriter(str(path)) as w:
        st = w.add_signal_type(SignalEncoding.SE_2STATE, 1)
        stype = w.add_stream_type("bus")
        with w.begin_hierarchy() as h:
            h.begin_scope(ScopeType.ST_MODULE, "top")
            h.add_var("clk", st)
            h.begin_scope(ScopeType.ST_MODULE, "u0")
            h.add_var("d", st)
            h.add_stream(stype, "s0")
            h.end_scope()
            h.begin_scope(ScopeType.ST_TASK, "t")
            h.end_scope()
            h.end_scope()
            h.add_var("glob", st)
        with w.begin_hierarchy(2, name="tb") as h:
            h.begin_scope(ScopeType.ST_MODULE, "env")
            h.end_scope()


def test_scope_tree_root_and_children(tmp_path):
    p = tmp_path / "s.trl"
    _write(p)
    with TrlReader(str(p)) as r:
        root = r.scope_tree(1)
        assert isinstance(root, ScopeNode)
        assert root.scope_type is None and root.path == "" and root.hier_id == 1
        names = lambda vs: [r.string_table.lookup(v.name_str_id) for v in vs]
        assert names(root.vars) == ["glob"]
        (top,) = root.children
        assert (top.name, top.scope_type, top.path) == ("top", ScopeType.ST_MODULE, "top")
        assert names(top.vars) == ["clk"]
        assert [c.name for c in top.children] == ["u0", "t"]
        u0 = top.children[0]
        assert u0.path == "top.u0"
        assert names(u0.vars) == ["d"]
        assert names(u0.streams) == ["s0"]
        assert top.children[1].scope_type == ScopeType.ST_TASK


def test_scopes_walks_all_hierarchies(tmp_path):
    p = tmp_path / "s.trl"
    _write(p)
    with TrlReader(str(p)) as r:
        assert [(s.hier_id, s.path) for s in r.scopes()] == [
            (1, "top"), (1, "top.u0"), (1, "top.t"), (2, "env")]
        assert [s.path for s in r.scopes(2)] == ["env"]
