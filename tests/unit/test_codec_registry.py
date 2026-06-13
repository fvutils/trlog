"""Codec registry + lifecycle tests (Phase 1).

Covers the codec ABI surface introduced in ``trlog.codec``: reverse-DNS id
validation, register/lookup (including unknown → None), the decorator registry,
and the documented lifecycle ordering
(``open_writer → encode_block×N → finalize → close``) — both as a direct
contract and as actually driven by ``TrlWriter``.
"""

from __future__ import annotations

import io

import pytest

import trlog.codec as C
from trlog.codec import (
    VcCodec, TxnCodec, Store, Capability,
    register_vc_codec, lookup_vc_codec, lookup_txn_codec,
    canonical_codec_id, CORE_VALUECHANGE, CORE_RECORD,
)
from trlog._writer import TrlWriter
from trlog._types import SignalEncoding, ScopeType


# ---------------------------------------------------------------------------
# Identity / canonical form
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("cid", [
    "org.fvutils.trlog.core.valuechange",
    "com.acme.codec.fancy",
    "a.b",
    "x9.y-z.w",
])
def test_canonical_id_accepts_valid(cid):
    assert canonical_codec_id(cid) == cid


@pytest.mark.parametrize("bad", [
    "NoDots", "has..empty", "Upper.Case", "", "trailing.", ".leading",
    "under_score.seg", "white space.x",
])
def test_canonical_id_rejects_invalid(bad):
    with pytest.raises(ValueError):
        canonical_codec_id(bad)


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------

def test_core_codecs_registered():
    assert CORE_VALUECHANGE in C.registered_vc_codecs()
    assert CORE_RECORD in C.registered_txn_codecs()
    vc = lookup_vc_codec(CORE_VALUECHANGE)
    assert vc is not None and vc.version == 1
    assert vc.caps & Capability.LOSSLESS


def test_lookup_unknown_returns_none():
    assert lookup_vc_codec("com.acme.does.not-exist") is None
    assert lookup_txn_codec("com.acme.does.not-exist") is None


def test_register_and_lookup_roundtrip():
    @C.vc_codec("com.example.test-reg", version=7)
    class _Reg(VcCodec):
        def open_writer(self, store, stream_id, params): return None
        def open_reader(self, store, stream_id, params): return None
        def encode_block(self, state, store): return b"", 0, 0
        def decode_block(self, state, store, payload, flags, emit): pass

    got = lookup_vc_codec("com.example.test-reg")
    assert got is not None
    assert got.codec_id == "com.example.test-reg"
    assert got.version == 7


def test_decorator_rejects_wrong_base():
    with pytest.raises(TypeError):
        @C.vc_codec("com.example.wrong-base")
        class _Bad:  # not a VcCodec subclass
            pass


def test_decorator_rejects_invalid_id():
    with pytest.raises(ValueError):
        @C.vc_codec("Not-Reverse-DNS")
        class _Bad(VcCodec):
            def open_writer(self, store, stream_id, params): return None
            def open_reader(self, store, stream_id, params): return None
            def encode_block(self, state, store): return b"", 0, 0
            def decode_block(self, state, store, payload, flags, emit): pass


# ---------------------------------------------------------------------------
# Store / trace-scoped context
# ---------------------------------------------------------------------------

def test_codec_ctx_is_lazily_created_and_shared():
    st = Store()
    assert not st.has_codec_ctx("x")
    ctx = st.codec_ctx("x")
    assert st.has_codec_ctx("x")
    ctx["k"] = 1
    assert st.codec_ctx("x")["k"] == 1            # same object returned
    assert st.codec_ctx("y") is not ctx           # distinct per codec id


# ---------------------------------------------------------------------------
# Lifecycle ordering — direct contract
# ---------------------------------------------------------------------------

class _RecordingVc(VcCodec):
    codec_id = "com.example.lifecycle"
    version = 1

    def __init__(self):
        self.log = []

    def open_writer(self, store, stream_id, params):
        self.log.append("open_writer")
        return {"blocks": 0}

    def open_reader(self, store, stream_id, params):
        self.log.append("open_reader")
        return {}

    def encode_block(self, state, store):
        self.log.append("encode_block")
        return b"", 0, 0

    def decode_block(self, state, store, payload, flags, emit):
        self.log.append("decode_block")

    def finalize(self, state, store):
        self.log.append("finalize")
        return 0

    def close(self, state):
        self.log.append("close")


def test_lifecycle_order_contract():
    codec = _RecordingVc()
    store = Store()
    state = codec.open_writer(store, 0, b"")
    for _ in range(3):
        codec.encode_block(state, store)
    codec.finalize(state, store)
    codec.close(state)
    assert codec.log == [
        "open_writer", "encode_block", "encode_block", "encode_block",
        "finalize", "close",
    ]


# ---------------------------------------------------------------------------
# Lifecycle ordering — as driven by TrlWriter through the core codec
# ---------------------------------------------------------------------------

def test_writer_drives_codec_lifecycle(monkeypatch):
    """The real writer must call open_writer once, encode_block per flushed
    block, then finalize, then close — in that order — on the core codec."""
    vc = lookup_vc_codec(CORE_VALUECHANGE)
    log = []

    def wrap(name):
        orig = getattr(vc, name)
        def spy(*a, **k):
            log.append(name)
            return orig(*a, **k)
        return spy

    for m in ("open_writer", "encode_block", "finalize", "close"):
        monkeypatch.setattr(vc, m, wrap(m))

    buf = io.BytesIO()
    with TrlWriter(buf) as w:
        st = w.add_signal_type(SignalEncoding.SE_2STATE, 1)
        with w.begin_hierarchy() as h:
            h.begin_scope(ScopeType.ST_MODULE, "top")
            clk = h.add_var("clk", st)
            h.end_scope()
        with w.begin_vc_block(0) as vc_blk:
            vc_blk.add_change(clk, 0, 0)
            vc_blk.add_change(clk, 5, 1)
        with w.begin_vc_block(10) as vc_blk:
            vc_blk.add_change(clk, 10, 0)

    assert log[0] == "open_writer"
    assert log.count("encode_block") == 2          # two flushed blocks
    assert log[-2:] == ["finalize", "close"]
    # encode_block must precede finalize
    assert log.index("finalize") > max(
        i for i, n in enumerate(log) if n == "encode_block")
