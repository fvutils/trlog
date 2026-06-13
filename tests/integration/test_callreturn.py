"""ext.callreturn — the reference external codec — Phase 5 (design §6.2).

Proves the storage SPI is complete: the whole call/return domain codec is built
on public API only, with no core changes. Covers enter/exit round-trip incl.
nesting/recursion, back-reference dedup, signature sharing across streams via the
dependency catalog, and the raw-fallback path readable *without* the codec.
"""

from __future__ import annotations

import io

import pytest

from trlog._writer import TrlWriter
from trlog._reader import TrlReader
from trlog._types import TxnFull
from trlog.ext.callreturn import (
    CallReturnWriter, CallReturnReader, Signature, ArgType,
    CALLRETURN_CODEC_ID,
)
from trlog.codec import lookup_txn_codec, Capability


def _open():
    buf = io.BytesIO()
    return TrlWriter(buf, compress=False), buf


# ---------------------------------------------------------------------------
# Registration / governance
# ---------------------------------------------------------------------------

def test_codec_registered_with_fallback_cap():
    codec = lookup_txn_codec(CALLRETURN_CODEC_ID)
    assert codec is not None
    assert codec.codec_id.startswith("org.fvutils.trlog.ext.")
    assert codec.caps & Capability.HAS_RAW_FALLBACK
    assert codec.caps & Capability.NEEDS_INPUT_STREAMS


# ---------------------------------------------------------------------------
# Round-trip incl. nesting / recursion
# ---------------------------------------------------------------------------

class TestRoundtrip:
    def test_simple_enter_exit(self):
        w, buf = _open()
        calls = w.add_stream_type("calls")
        sig_s = w.add_stream_type("sig")
        cr = CallReturnWriter(w, calls, sig_s,
                              [Signature(1, "f", [ArgType.INT], ArgType.INT)])
        cr.enter(1, [5], time=0)
        cr.exit(ret=10, time=8)
        cr.close()
        w.close()
        buf.seek(0)
        with TrlReader(buf) as r:
            rd = CallReturnReader(r, calls)
            tree = rd.call_tree()
            assert len(tree) == 1
            c = tree[0]
            assert c.name == "f" and c.args == [5]
            assert c.start == 0 and c.end == 8 and c.ret == 10

    def test_nesting_and_args(self):
        w, buf = _open()
        calls = w.add_stream_type("calls")
        sig_s = w.add_stream_type("sig")
        cr = CallReturnWriter(w, calls, sig_s, [
            Signature(1, "outer", [ArgType.INT], ArgType.INT),
            Signature(2, "inner", [ArgType.STR], ArgType.NONE),
        ])
        cr.enter(1, [3], time=10)
        cr.enter(2, ["hi"], time=12)
        cr.exit(time=13)
        cr.exit(ret=99, time=20)
        cr.close()
        w.close()
        buf.seek(0)
        with TrlReader(buf) as r:
            rd = CallReturnReader(r, calls)
            roots = rd.call_tree()
            assert len(roots) == 1
            outer = roots[0]
            assert outer.name == "outer" and outer.start == 10 and outer.end == 20
            assert outer.ret == 99
            assert len(outer.children) == 1
            inner = outer.children[0]
            assert inner.name == "inner" and inner.args == ["hi"]
            assert inner.start == 12 and inner.end == 13
            # depths annotated on the flat event list
            depths = [(e["kind"], e["depth"]) for e in rd.events()]
            assert depths == [("enter", 0), ("enter", 1), ("exit", 1), ("exit", 0)]

    def test_recursion(self):
        w, buf = _open()
        calls = w.add_stream_type("calls")
        sig_s = w.add_stream_type("sig")
        cr = CallReturnWriter(w, calls, sig_s,
                              [Signature(1, "fib", [ArgType.UINT], ArgType.UINT)])
        # fib(3) -> fib(2) -> fib(1); then unwind
        cr.enter(1, [3], time=0)
        cr.enter(1, [2], time=1)
        cr.enter(1, [1], time=2)
        cr.exit(ret=1, time=3)
        cr.exit(ret=1, time=4)
        cr.exit(ret=2, time=5)
        cr.close()
        w.close()
        buf.seek(0)
        with TrlReader(buf) as r:
            rd = CallReturnReader(r, calls)
            roots = rd.call_tree()
            assert roots[0].args == [3]
            assert roots[0].children[0].args == [2]
            assert roots[0].children[0].children[0].args == [1]
            assert roots[0].ret == 2

    def test_real_and_str_args(self):
        w, buf = _open()
        calls = w.add_stream_type("calls")
        sig_s = w.add_stream_type("sig")
        cr = CallReturnWriter(w, calls, sig_s, [
            Signature(1, "g", [ArgType.REAL, ArgType.STR], ArgType.NONE)])
        cr.enter(1, [3.5, "abc"], time=0)
        cr.exit(time=1)
        cr.close()
        w.close()
        buf.seek(0)
        with TrlReader(buf) as r:
            rd = CallReturnReader(r, calls)
            c = rd.call_tree()[0]
            assert c.args[0] == 3.5 and c.args[1] == "abc"


# ---------------------------------------------------------------------------
# Back-reference dedup (Perfetto interning)
# ---------------------------------------------------------------------------

def _encoded_size(identical, n=100):
    buf = io.BytesIO()
    with TrlWriter(buf, compress=False) as w:
        calls = w.add_stream_type("calls")
        sig_s = w.add_stream_type("sig")
        cr = CallReturnWriter(w, calls, sig_s, [
            Signature(1, "f", [ArgType.INT] * 4, ArgType.NONE)], window=64)
        for i in range(n):
            args = [1, 2, 3, 4] if identical else [i, i + 1, i + 2, i + 3]
            cr.enter(1, args, time=i * 10)
            cr.exit(time=i * 10 + 5)
        cr.close()
    return buf.getvalue()


class TestBackReferences:
    def test_identical_calls_are_smaller(self):
        assert len(_encoded_size(True)) < len(_encoded_size(False))

    def test_backref_roundtrips(self):
        buf = io.BytesIO()
        with TrlWriter(buf, compress=False) as w:
            calls = w.add_stream_type("calls")
            sig_s = w.add_stream_type("sig")
            cr = CallReturnWriter(w, calls, sig_s,
                                  [Signature(1, "f", [ArgType.INT, ArgType.INT])])
            cr.enter(1, [7, 8], time=0); cr.exit(time=1)
            cr.enter(1, [7, 8], time=2); cr.exit(time=3)   # identical -> back-ref
            cr.enter(1, [9, 9], time=4); cr.exit(time=5)
            cr.close()
        buf.seek(0)
        with TrlReader(buf) as r:
            rd = CallReturnReader(r, calls)
            args = [e["args"] for e in rd.events() if e["kind"] == "enter"]
            assert args == [[7, 8], [7, 8], [9, 9]]


# ---------------------------------------------------------------------------
# Signature sharing across streams (via the dependency catalog)
# ---------------------------------------------------------------------------

def test_signature_sharing_across_streams():
    buf = io.BytesIO()
    with TrlWriter(buf, compress=False) as w:
        sig_s = w.add_stream_type("shared_sigs")
        calls_a = w.add_stream_type("calls_a")
        calls_b = w.add_stream_type("calls_b")
        sigs = [Signature(1, "f", [ArgType.INT], ArgType.INT)]
        cra = CallReturnWriter(w, calls_a, sig_s, sigs)
        crb = CallReturnWriter(w, calls_b, sig_s, sigs)
        cra.enter(1, [1], time=0); cra.exit(ret=2, time=1); cra.close()
        crb.enter(1, [3], time=0); crb.exit(ret=4, time=1); crb.close()
    buf.seek(0)
    with TrlReader(buf) as r:
        # both call streams depend on the same signature stream
        assert r.dependencies(calls_a) == [sig_s]
        assert r.dependencies(calls_b) == [sig_s]
        rda = CallReturnReader(r, calls_a)
        rdb = CallReturnReader(r, calls_b)
        assert rda.signatures()[1].name == "f"
        assert rdb.signatures()[1].name == "f"
        assert rda.call_tree()[0].args == [1]
        assert rdb.call_tree()[0].args == [3]


# ---------------------------------------------------------------------------
# Raw fallback — readable WITHOUT the codec
# ---------------------------------------------------------------------------

def test_raw_fallback_readable_without_codec():
    buf = io.BytesIO()
    with TrlWriter(buf, compress=False) as w:
        calls = w.add_stream_type("calls")
        sig_s = w.add_stream_type("sig")
        cr = CallReturnWriter(w, calls, sig_s,
                              [Signature(1, "f", [ArgType.INT], ArgType.INT)])
        cr.enter(1, [5], time=0); cr.exit(ret=10, time=5)
        cr.enter_raw(99, [42, 43], start=20, end=30)   # doesn't fit -> raw txn
        cr.close()
    buf.seek(0)
    # A plain reader with no knowledge of callreturn still sees the spilled call
    # as an ordinary transaction.
    with TrlReader(buf) as r:
        fulls = [rec for blk in r.iter_txn_blocks() for rec in blk
                 if isinstance(rec, TxnFull)]
        assert len(fulls) == 1
        rec = fulls[0]
        assert rec.txn_type_id == 99
        assert rec.start_time == 20 and rec.end_time == 30
        assert [a.value for a in rec.attrs] == [42, 43]


def test_enter_mismatched_args_rejected():
    w, buf = _open()
    calls = w.add_stream_type("calls")
    sig_s = w.add_stream_type("sig")
    cr = CallReturnWriter(w, calls, sig_s,
                          [Signature(1, "f", [ArgType.INT, ArgType.INT])])
    with pytest.raises(ValueError):
        cr.enter(1, [5], time=0)        # arity mismatch
    w.close()
