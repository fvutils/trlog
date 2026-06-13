"""Opaque keyed (aux) metadata blocks — Phase 2b (impl-plan §2.4).

Covers round-trip by ``(owner, key)``, multiple ordinals per key, multiple
owners, optional compression, the codec-free enumerate-and-skip discovery
surface, coexistence with value-change data, and byte-compat (no aux → no key
index section, legacy files unchanged).
"""

from __future__ import annotations

import io

import pytest

from trlog._writer import TrlWriter
from trlog._reader import TrlReader
from trlog._types import SignalEncoding, ScopeType, BlockType
from trlog._aux import AuxBlock


def _writer(compress=False):
    buf = io.BytesIO()
    return TrlWriter(buf, compress=compress), buf


def _reader(buf):
    buf.seek(0)
    return TrlReader(buf)


class TestAuxCodec:
    @pytest.mark.parametrize("compress", [False, True])
    def test_block_roundtrip(self, compress):
        payload = b"the quick brown fox" * 10
        block = AuxBlock.encode(payload, compress=compress)
        assert block[0] == int(BlockType.BLK_AUX)
        decoded = AuxBlock.decode(block[10:], flags=block[1])
        assert decoded == payload

    def test_empty_payload(self):
        block = AuxBlock.encode(b"", compress=True)
        assert AuxBlock.decode(block[10:], flags=block[1]) == b""


class TestKeyedRoundtrip:
    def test_basic_keyed_roundtrip(self):
        w, buf = _writer()
        w.write_aux(1, 7, b"signature-table")
        w.write_aux(1, 9, b"\x00\x01\x02")
        w.close()
        r = _reader(buf)
        assert r.read_aux(1, 7) == [b"signature-table"]
        assert r.read_aux(1, 9) == [b"\x00\x01\x02"]
        assert r.read_aux(1, 1234) == []   # absent key

    def test_multiple_ordinals_in_order(self):
        w, buf = _writer()
        for i in range(5):
            ordinal = w.write_aux(3, 42, f"chunk{i}".encode())
            assert ordinal == i
        w.close()
        r = _reader(buf)
        assert r.read_aux(3, 42) == [f"chunk{i}".encode() for i in range(5)]

    def test_multiple_owners_isolated(self):
        w, buf = _writer()
        w.write_aux(1, 7, b"owner-1")
        w.write_aux(2, 7, b"owner-2")
        w.close()
        r = _reader(buf)
        assert r.read_aux(1, 7) == [b"owner-1"]
        assert r.read_aux(2, 7) == [b"owner-2"]

    @pytest.mark.parametrize("compress", [False, True])
    def test_compressed_keyed_roundtrip(self, compress):
        w, buf = _writer(compress=compress)
        big = bytes(range(256)) * 20
        w.write_aux(1, 5, big)
        w.close()
        r = _reader(buf)
        assert r.read_aux(1, 5) == [big]


class TestEnumerateAndSkip:
    """A reader lacking the producing codec still enumerates aux blocks by key
    and skips their opaque bytes — the §2.4 / §2.8 skip-unknown property."""

    def test_enumerate_without_decode(self):
        w, buf = _writer()
        w.write_aux(1, 7, b"aaa")
        w.write_aux(1, 7, b"bbbb")
        w.write_aux(1, 9, b"ccccc")
        w.close()
        r = _reader(buf)
        entries = r.aux_entries(1)
        # owner, key, ordinal, offset, length — discoverable with no codec
        assert [(o, k, ordn) for (o, k, ordn, _off, _ln) in entries] == [
            (1, 7, 0), (1, 7, 1), (1, 9, 0)]
        assert r.aux_keys(1) == [7, 9]
        # every entry has a real file offset/length (skippable by length)
        for _o, _k, _ord, off, length in entries:
            assert off > 0 and length > 0

    def test_aux_does_not_disturb_vc(self):
        w, buf = _writer()
        st = w.add_signal_type(SignalEncoding.SE_2STATE, 1)
        w.write_aux(1, 7, b"meta")
        with w.begin_hierarchy() as h:
            h.begin_scope(ScopeType.ST_MODULE, "top")
            clk = h.add_var("clk", st)
            h.end_scope()
        with w.begin_vc_block(0) as vc:
            for i in range(8):
                vc.add_change(clk, i * 10, i % 2)
        w.close()
        r = _reader(buf)
        blocks = list(r.iter_vc_blocks())
        assert len(blocks) == 1
        assert [(c.time, c.value) for c in blocks[0]] == [(i * 10, i % 2) for i in range(8)]
        assert r.read_aux(1, 7) == [b"meta"]


def test_aux_is_opt_in():
    """No aux blocks → no key-index section; reader reports nothing."""
    w, buf = _writer()
    st = w.add_signal_type(SignalEncoding.SE_2STATE, 1)
    with w.begin_hierarchy() as h:
        h.begin_scope(ScopeType.ST_MODULE, "top")
        h.add_var("clk", st)
        h.end_scope()
    w.close()
    r = _reader(buf)
    assert r.aux_entries() == []
    assert r.read_aux(1, 0) == []
