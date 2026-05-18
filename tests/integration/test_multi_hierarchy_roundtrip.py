"""Integration tests for multi-hierarchy files."""

import pytest
from trlog import TrlWriter, TrlReader, SignalEncoding, ScopeType, VarDir
from trlog._types import HierKind, StreamDeclEntry


class TestMultiHierarchyRoundtrip:
    def test_two_hierarchies(self, tmp_path):
        """Write design + SW hierarchy; reader exposes both."""
        p = tmp_path / "t.trl"
        with TrlWriter(str(p), compress=False) as w:
            sig_t = w.add_signal_type(SignalEncoding.SE_2STATE, 1)
            with w.begin_hierarchy(hier_id=1, kind=HierKind.HK_DESIGN, name="design") as h:
                h.begin_scope(ScopeType.ST_MODULE, "top")
                clk = h.add_var("clk", sig_t)
                h.end_scope()
            with w.begin_hierarchy(hier_id=2, kind=HierKind.HK_SW, name="sw") as h:
                h.begin_scope(ScopeType.ST_TASK, "main")
                h.end_scope()

        with TrlReader(str(p)) as r:
            assert set(r.hierarchies.keys()) == {1, 2}
            h1 = r.hierarchies[1]
            h2 = r.hierarchies[2]
            assert h1.header.kind == HierKind.HK_DESIGN
            assert h2.header.kind == HierKind.HK_SW

    def test_independent_var_ids(self, tmp_path):
        """Vars in each hierarchy have independent var_ids; no collision."""
        p = tmp_path / "t.trl"
        with TrlWriter(str(p), compress=False) as w:
            sig_t = w.add_signal_type(SignalEncoding.SE_2STATE, 8)
            with w.begin_hierarchy(hier_id=1, kind=HierKind.HK_DESIGN, name="design") as h:
                h.begin_scope(ScopeType.ST_MODULE, "top")
                v1 = h.add_var("a", sig_t)
                v2 = h.add_var("b", sig_t)
                h.end_scope()
            with w.begin_hierarchy(hier_id=2, kind=HierKind.HK_SW, name="sw") as h:
                h.begin_scope(ScopeType.ST_TASK, "task1")
                v3 = h.add_var("x", sig_t)
                h.end_scope()

        with TrlReader(str(p)) as r:
            h1_vars = list(r.hierarchies[1].vars.keys())
            h2_vars = list(r.hierarchies[2].vars.keys())
            # Both hierarchies should have vars
            assert len(h1_vars) == 2
            assert len(h2_vars) == 1

    def test_vc_data_with_two_hierarchies(self, tmp_path):
        """VC data block works when file has two hierarchies."""
        p = tmp_path / "t.trl"
        clk_id = None
        with TrlWriter(str(p), compress=False) as w:
            sig_t = w.add_signal_type(SignalEncoding.SE_2STATE, 1)
            with w.begin_hierarchy(hier_id=1, kind=HierKind.HK_DESIGN, name="design") as h:
                h.begin_scope(ScopeType.ST_MODULE, "top")
                clk_id = h.add_var("clk", sig_t)
                h.end_scope()
            with w.begin_hierarchy(hier_id=2, kind=HierKind.HK_SW, name="sw") as h:
                h.begin_scope(ScopeType.ST_TASK, "t")
                h.end_scope()
            with w.begin_vc_block(0) as vc:
                for i in range(20):
                    vc.add_change(clk_id, i * 10, i % 2)

        with TrlReader(str(p)) as r:
            changes = r.read_signal(clk_id)
            assert len(changes) == 20
