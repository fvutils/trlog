"""Derived (virtual) signals — Phase 4 (impl-plan §4, design §5.2).

Covers the expression engine, identity aliasing (one opcode, zero value data),
end-to-end evaluation through the writer/reader, lazy + memoized evaluation,
derived-on-derived dependency chains, materialized-mode parity, and read-time
cycle defense.
"""

from __future__ import annotations

import io

import pytest

from trlog._writer import TrlWriter
from trlog._reader import TrlReader
from trlog._types import SignalEncoding, ScopeType, VcChange
from trlog._derived import (
    Input, Const, compile_expr, eval_bytecode, evaluate_changes, DerivedDef,
    DERIVED_AUX_OWNER,
)
from trlog._deps import DependencyError


# ---------------------------------------------------------------------------
# Expression engine
# ---------------------------------------------------------------------------

class TestExpressionEngine:
    def test_identity_is_one_opcode(self):
        bc = compile_expr(Input(0))
        assert len(bc) == 2          # OP_PUSH_INPUT + uvarint(0)

    @pytest.mark.parametrize("expr,inp,width,expected", [
        (Input(0) & Input(1), [0b1100, 0b1010], 8, 0b1000),
        (Input(0) | Input(1), [0b1100, 0b1010], 8, 0b1110),
        (Input(0) ^ Input(1), [0b1100, 0b1010], 8, 0b0110),
        (~Input(0), [0x0F], 8, 0xF0),
        (Input(0).eq(Input(1)), [5, 5], 1, 1),
        (Input(0).ne(Input(1)), [5, 6], 1, 1),
        (Input(0).shl(4), [0x3], 8, 0x30),
        (Input(0).shr(2), [0b1100], 8, 0b11),
        (Input(0).reduce_or(4), [0b0010], 1, 1),
        (Input(0).reduce_and(4), [0b1110], 1, 0),
        (Input(0).reduce_and(4), [0b1111], 1, 1),
        (Input(0).slice_(2, 3), [0b11100], 3, 0b111),
        (Input(0).concat(Input(1), 4), [0xA, 0x5], 8, 0xA5),
        (Input(0) & Const(0x0F), [0xAB], 8, 0x0B),
    ])
    def test_eval(self, expr, inp, width, expected):
        assert eval_bytecode(compile_expr(expr), inp, width) == expected

    def test_def_roundtrip(self):
        d = DerivedDef(bit_width=8, input_var_ids=[10, 11],
                       bytecode=compile_expr(Input(0) ^ Input(1)))
        d2 = DerivedDef.decode(d.encode())
        assert d2.bit_width == 8 and d2.input_var_ids == [10, 11]
        assert d2.bytecode == d.bytecode

    def test_evaluate_changes_union_and_dedup(self):
        a = [(0, 0), (10, 1), (30, 0)]
        b = [(0, 0), (20, 1)]
        defn = DerivedDef(1, [0, 1], compile_expr(Input(0) & Input(1)))
        assert evaluate_changes(defn, [a, b]) == [(0, 0), (20, 1), (30, 0)]


# ---------------------------------------------------------------------------
# End-to-end through writer / reader
# ---------------------------------------------------------------------------

def _trace_with_ab():
    buf = io.BytesIO()
    w = TrlWriter(buf, compress=False)
    bit = w.add_signal_type(SignalEncoding.SE_2STATE, 1)
    with w.begin_hierarchy() as h:
        h.begin_scope(ScopeType.ST_MODULE, "top")
        a = h.add_var("a", bit)
        b = h.add_var("b", bit)
        h.end_scope()
    with w.begin_vc_block(0) as vc:
        vc.add_change(a, 0, 0); vc.add_change(a, 10, 1); vc.add_change(a, 30, 0)
        vc.add_change(b, 0, 0); vc.add_change(b, 20, 1)
    return w, buf, a, b


class TestEndToEnd:
    def test_and_and_alias(self):
        w, buf, a, b = _trace_with_ab()
        c = w.add_derived_signal("c", Input(0) & Input(1), inputs=[a, b], bit_width=1)
        d = w.add_derived_signal("d", Input(0), inputs=[a], bit_width=1)
        w.close()
        buf.seek(0)
        with TrlReader(buf) as r:
            sigs = r.derived_signals()
            assert sigs[c]["name"] == "c" and sigs[c]["inputs"] == [a, b]
            assert sigs[d]["name"] == "d"
            assert [(x.time, x.value) for x in r.read_derived(c)] == [(0, 0), (20, 1), (30, 0)]
            assert [(x.time, x.value) for x in r.read_derived(d)] == [(0, 0), (10, 1), (30, 0)]

    def test_derived_on_derived(self):
        w, buf, a, b = _trace_with_ab()
        c = w.add_derived_signal("c", Input(0) & Input(1), inputs=[a, b], bit_width=1)
        # e = !c  (derived over a derived)
        e = w.add_derived_signal("e", Input(0).lnot(), inputs=[c], bit_width=1)
        w.close()
        buf.seek(0)
        with TrlReader(buf) as r:
            c_changes = [(x.time, x.value) for x in r.read_derived(c)]
            e_changes = [(x.time, x.value) for x in r.read_derived(e)]
            assert c_changes == [(0, 0), (20, 1), (30, 0)]
            # !c: 1 when c==0 -> (0,1) then 0 at (20) then 1 at (30)
            assert e_changes == [(0, 1), (20, 0), (30, 1)]

    def test_lazy_not_evaluated_until_read(self):
        w, buf, a, b = _trace_with_ab()
        c = w.add_derived_signal("c", Input(0) | Input(1), inputs=[a, b], bit_width=1)
        w.close()
        buf.seek(0)
        with TrlReader(buf) as r:
            calls = []
            orig = r.read_signal
            r.read_signal = lambda v, *aa, **kk: (calls.append(v), orig(v, *aa, **kk))[1]
            list(r.iter_vc_blocks())          # iterating base data must not eval derived
            assert calls == []
            r.read_derived(c)                 # now inputs get read
            assert set(calls) == {a, b}


# ---------------------------------------------------------------------------
# Materialized mode
# ---------------------------------------------------------------------------

class TestMaterialized:
    def test_materialized_parity_with_lazy(self):
        buf = io.BytesIO()
        ac = [(0, 0), (10, 1), (30, 0)]
        bc = [(0, 0), (20, 1)]
        with TrlWriter(buf, compress=False) as w:
            bit = w.add_signal_type(SignalEncoding.SE_2STATE, 1)
            with w.begin_hierarchy() as h:
                h.begin_scope(ScopeType.ST_MODULE, "top")
                a = h.add_var("a", bit); b = h.add_var("b", bit)
                h.end_scope()
            with w.begin_vc_block(0) as vc:
                for t, v in ac: vc.add_change(a, t, v)
                for t, v in bc: vc.add_change(b, t, v)
            c = w.add_derived_signal("c", Input(0) & Input(1), inputs=[a, b],
                                     bit_width=1, materialized=True,
                                     input_changes={a: ac, b: bc})
        buf.seek(0)
        with TrlReader(buf) as r:
            assert r.derived_signals()[c]["materialized"] is True
            stored = [(x.time, x.value) for x in r.read_signal(c)]   # the written VC block
            lazy = [(x.time, x.value) for x in r.read_derived(c)]     # recompute
            assert stored == lazy == [(0, 0), (20, 1), (30, 0)]

    def test_materialized_requires_input_changes(self):
        w, buf, a, b = _trace_with_ab()
        with pytest.raises(ValueError):
            w.add_derived_signal("c", Input(0), inputs=[a], bit_width=1,
                                 materialized=True)
        w.close()


# ---------------------------------------------------------------------------
# Cycle defense
# ---------------------------------------------------------------------------

class TestCycles:
    def test_write_time_cycle_guarded(self):
        """The append-only id API can't express a cycle, but the write-time
        guard still rejects one if the internal graph is forced cyclic."""
        w, buf, a, b = _trace_with_ab()
        d1 = w.add_derived_signal("d1", Input(0), inputs=[a], bit_width=1)
        # Force a back-edge: make d1 depend on a not-yet but then add d2->d1 and
        # tamper d1's inputs to point at d2 -> cycle on next declaration.
        d2 = w.add_derived_signal("d2", Input(0), inputs=[d1], bit_width=1)
        w._derived_inputs[d1] = [d2]              # tamper to create d1<->d2
        with pytest.raises(DependencyError):
            w.add_derived_signal("d3", Input(0), inputs=[d2], bit_width=1)
        w.close()

    def test_read_time_cycle_defended(self):
        """A hand-corrupted trace with cyclic derived defs errors at read rather
        than looping forever."""
        buf = io.BytesIO()
        with TrlWriter(buf, compress=False) as w:
            # two derived defs that reference each other's ids, written directly
            from trlog._derived import DERIVED_VAR_BASE
            v0 = DERIVED_VAR_BASE + 0
            v1 = DERIVED_VAR_BASE + 1
            from trlog._codec import encode_uvarint
            def payload(name, inputs):
                d = DerivedDef(1, inputs, compile_expr(Input(0)))
                return encode_uvarint(w.intern(name)) + d.encode()
            w.write_aux(DERIVED_AUX_OWNER, v0, payload("c0", [v1]), compress=False)
            w.write_aux(DERIVED_AUX_OWNER, v1, payload("c1", [v0]), compress=False)
        buf.seek(0)
        with TrlReader(buf) as r:
            with pytest.raises(DependencyError):
                r.read_derived(DERIVED_VAR_BASE + 0)
