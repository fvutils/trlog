"""Stream dependency catalog + resolver — Phase 2c (impl-plan §2.5/§4.4/§4.7).

Covers the DependencyGraph (topo order, reverse-dep finalize order, cycle
detection), the lazy + memoized Materializer, write-time cycle rejection, and
persistence/round-trip of the catalog through the type registry.
"""

from __future__ import annotations

import io

import pytest

from trlog._writer import TrlWriter
from trlog._reader import TrlReader
from trlog._deps import DependencyGraph, Materializer, DependencyError


# ---------------------------------------------------------------------------
# DependencyGraph
# ---------------------------------------------------------------------------

class TestDependencyGraph:
    def test_topo_and_finalize_order(self):
        g = DependencyGraph()
        g.add(3, [1, 2])   # 3 depends on 1 and 2
        g.add(2, [1])      # 2 depends on 1
        topo = g.topo_order()
        # dependencies must precede dependents
        assert topo.index(1) < topo.index(2) < topo.index(3)
        # finalize is the reverse: dependents first, depended-on last
        assert g.finalize_order() == list(reversed(topo))

    def test_no_cycle(self):
        g = DependencyGraph()
        g.add(2, [1]); g.add(3, [2])
        assert g.find_cycle() is None
        g.validate_acyclic()   # no raise

    def test_direct_cycle_detected(self):
        g = DependencyGraph()
        g.add(1, [2]); g.add(2, [1])
        cyc = g.find_cycle()
        assert cyc is not None and cyc[0] == cyc[-1]
        with pytest.raises(DependencyError):
            g.topo_order()

    def test_self_cycle_detected(self):
        g = DependencyGraph()
        g.add(1, [1])
        assert g.find_cycle() == [1, 1]

    def test_indirect_cycle_detected(self):
        g = DependencyGraph()
        g.add(1, [2]); g.add(2, [3]); g.add(3, [1])
        with pytest.raises(DependencyError):
            g.validate_acyclic()


# ---------------------------------------------------------------------------
# Materializer — lazy + memoized
# ---------------------------------------------------------------------------

class TestMaterializer:
    def test_inputs_first_each_computed_once(self):
        g = DependencyGraph()
        g.add(3, [1, 2]); g.add(2, [1])
        calls = []
        m = Materializer(g, lambda sid, inputs: (calls.append(sid), f"S{sid}")[1])
        result = m.materialize(3)
        assert result == "S3"
        # 1 before 2 before 3, each exactly once
        assert calls == [1, 2, 3]

    def test_memoized_not_recomputed(self):
        g = DependencyGraph()
        g.add(2, [1])
        calls = []
        m = Materializer(g, lambda sid, inputs: calls.append(sid))
        m.materialize(2)
        m.materialize(2)
        m.materialize(1)
        assert calls == [1, 2]   # second materialize(2) and materialize(1) cached

    def test_not_materialized_until_requested(self):
        g = DependencyGraph()
        g.add(2, [1]); g.add(3, [1])
        calls = []
        m = Materializer(g, lambda sid, inputs: calls.append(sid))
        m.materialize(2)         # only touches 1 and 2, never 3
        assert 3 not in calls
        assert m.is_materialized(2) and not m.is_materialized(3)

    def test_read_time_cycle_defended(self):
        # A hand-built cyclic graph (bypassing write-time checks) must error at
        # read rather than infinite-loop.
        g = DependencyGraph()
        g.add(1, [2]); g.add(2, [1])
        m = Materializer(g, lambda sid, inputs: sid)
        with pytest.raises(DependencyError):
            m.materialize(1)


# ---------------------------------------------------------------------------
# Persistence + write-time rejection through the writer/reader
# ---------------------------------------------------------------------------

class TestCatalogPersistence:
    def test_roundtrip_catalog(self):
        buf = io.BytesIO()
        with TrlWriter(buf, compress=False) as w:
            a = w.add_stream_type("A")
            b = w.add_stream_type("B")
            c = w.add_stream_type("C")
            w.declare_dependencies(b, [a])
            w.declare_dependencies(c, [a, b])
        buf.seek(0)
        with TrlReader(buf) as r:
            assert r.dependencies(a) == []
            assert r.dependencies(b) == [a]
            assert r.dependencies(c) == [a, b]
            assert r.dependency_catalog() == {b: [a], c: [a, b]}
            order = r.dependency_order()
            assert order.index(a) < order.index(b) < order.index(c)

    def test_write_time_cycle_rejected_and_not_persisted(self):
        buf = io.BytesIO()
        with TrlWriter(buf, compress=False) as w:
            a = w.add_stream_type("A")
            b = w.add_stream_type("B")
            w.declare_dependencies(b, [a])
            with pytest.raises(DependencyError):
                w.declare_dependencies(a, [b])   # a->b->a
            # the rejected edge must not have been persisted
            assert w._typereg.get_stream_decl(a).input_streams == []
        buf.seek(0)
        with TrlReader(buf) as r:
            assert r.dependencies(a) == []
            assert r.dependencies(b) == [a]

    def test_catalog_is_opt_in(self):
        buf = io.BytesIO()
        with TrlWriter(buf, compress=False) as w:
            w.add_stream_type("A")
        buf.seek(0)
        with TrlReader(buf) as r:
            assert r.dependency_catalog() == {}
            assert r.dependency_order() == []
