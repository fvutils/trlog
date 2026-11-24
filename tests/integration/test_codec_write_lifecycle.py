"""Codec write lifecycle (issues #10-#12).

* The Python writer opens a selected value-change codec once per trace, not
  once per block, and calls its ``finalize`` and ``close`` once at close.
* A codec driven from the C writer through the ctypes bridge writes blocks a
  reader can decode: the codec returns the complete block (header and
  payload), and the file reads back with the same changes.

The C writer's NULL-safe ``finalize``/``close`` is checked in
``c/tests/test_codec.c``.
"""

from __future__ import annotations

import os
import struct
import tempfile
from typing import List

import pytest

from trlog import BLOCK_HEADER_SIZE, BlockType, Capability, Store, VcChange
from trlog._reader import TrlReader
from trlog._types import FLAG_STRUCT_CODEC, ScopeType, SignalEncoding
from trlog._writer import TrlWriter
from trlog.codec import VcCodec, lookup_vc_codec, vc_codec

COUNTING_ID = "com.example.lifecycle.counting"
BRIDGED_ID = "com.example.lifecycle.bridged"


class _Block:
    def __init__(self, start_time: int) -> None:
        self.start_time = start_time
        self.end_time = start_time
        self.changes: List[tuple] = []

    def add_change(self, var_id, time, value) -> None:
        self.changes.append((var_id, time, value))
        self.end_time = max(self.end_time, time)

    def set_initial(self, var_id, value) -> None:
        self.changes.insert(0, (var_id, self.start_time, value))


class _State:
    def __init__(self) -> None:
        self.blk = None
        self.blocks = 0


class _CountingCodec(VcCodec):
    """Complete-block codec that records its lifecycle calls."""

    def __init__(self) -> None:
        self.events: List[str] = []

    def open_writer(self, store, stream_id, params):
        self.events.append("open_writer")
        return _State()

    def open_reader(self, store, stream_id, params):
        return None

    def new_block(self, store, state, start_time):
        state.blk = _Block(start_time)
        return state.blk

    def encode_block(self, state, store):
        self.events.append("encode_block")
        state.blocks += 1
        blk = state.blk
        out = bytearray()
        out += Store.encode_uvarint(store.intern(self.codec_id))
        out += Store.encode_uvarint(len(blk.changes))
        for var_id, time, value in sorted(blk.changes, key=lambda c: c[1]):
            out += Store.encode_uvarint(var_id)
            out += Store.encode_uvarint(time)
            out += Store.encode_uvarint(value)
        payload = bytes(out)
        header = struct.pack("<BBQ", BlockType.BLK_VC_DATA, FLAG_STRUCT_CODEC,
                             BLOCK_HEADER_SIZE + len(payload))
        return header + payload, blk.start_time, blk.end_time

    def decode_block(self, state, store, payload, flags, emit):
        _cid, o = Store.decode_uvarint(payload, 0)
        n, o = Store.decode_uvarint(payload, o)
        for _ in range(n):
            var_id, o = Store.decode_uvarint(payload, o)
            time, o = Store.decode_uvarint(payload, o)
            value, o = Store.decode_uvarint(payload, o)
            emit(VcChange(var_id=var_id, time=time, value=value))

    def finalize(self, state, store) -> int:
        self.events.append(f"finalize:{state.blocks}")
        return 0

    def close(self, state) -> None:
        self.events.append("close")


@vc_codec(COUNTING_ID, version=1, caps=Capability.LOSSLESS)
class CountingCodec(_CountingCodec):
    pass


@vc_codec(BRIDGED_ID, version=1, caps=Capability.LOSSLESS)
class BridgedCodec(_CountingCodec):
    pass


def test_python_writer_opens_codec_once_and_finalizes(tmp_path):
    codec = lookup_vc_codec(COUNTING_ID)
    codec.events.clear()
    path = tmp_path / "life.trl"
    with TrlWriter(str(path)) as w:
        bit = w.add_signal_type(SignalEncoding.SE_2STATE, 8)
        with w.begin_hierarchy() as h:
            h.begin_scope(ScopeType.ST_MODULE, "top")
            a = h.add_var("a", bit)
            h.end_scope()
        for blk in range(3):
            with w.begin_vc_block(blk * 100, codec=COUNTING_ID) as vc:
                vc.add_change(a, blk * 100, blk)
                vc.add_change(a, blk * 100 + 5, blk + 1)
        assert codec.events.count("open_writer") == 1
        assert "close" not in codec.events
    # One state across the three blocks; finalize then close, once each.
    assert codec.events == ["open_writer"] + ["encode_block"] * 3 + [
        "finalize:3", "close"]
    with TrlReader(str(path)) as r:
        got = [(c.time, c.value) for c in r.read_signal(a)]
    assert got == [(0, 0), (5, 1), (100, 1), (105, 2), (200, 2), (205, 3)]


def test_python_writer_unused_codec_not_opened(tmp_path):
    codec = lookup_vc_codec(COUNTING_ID)
    codec.events.clear()
    with TrlWriter(str(tmp_path / "none.trl")) as w:
        bit = w.add_signal_type(SignalEncoding.SE_2STATE, 1)
        with w.begin_hierarchy() as h:
            h.begin_scope(ScopeType.ST_MODULE, "top")
            a = h.add_var("a", bit)
            h.end_scope()
        with w.begin_vc_block(0) as vc:
            vc.add_change(a, 0, 1)
    assert codec.events == []


def _bridge_available():
    from trlog._codec_bridge import AVAILABLE
    return AVAILABLE


@pytest.mark.skipif(not _bridge_available(), reason="libtrl.so not available")
def test_bridged_codec_file_reads_back():
    from trlog._codec_bridge import register_native
    from trlog._native import _LIB

    codec = lookup_vc_codec(BRIDGED_ID)
    codec.events.clear()
    bridge = register_native(codec)
    fd, path = tempfile.mkstemp(suffix=".trl")
    os.close(fd)
    try:
        h = _LIB.trl_writer_open(path.encode("utf-8"), -9, 0)
        assert h
        assert _LIB.trl_writer_set_vc_codec(h, BRIDGED_ID.encode("utf-8")) == 0
        changes = [(0, 0, 7), (1, 0, 100), (0, 5, 8), (1, 5, 99), (0, 10, 9)]
        for start, chunk in ((0, changes[:2]), (5, changes[2:])):
            _LIB.trl_vc_begin(h, start)
            for var_id, time, value in chunk:
                _LIB.trl_vc_change_u64(h, var_id, time, value)
            assert _LIB.trl_vc_flush(h) == 0
        assert _LIB.trl_writer_close(h) == 0
        bridge.raise_pending()
        assert codec.events == ["open_writer"] + ["encode_block"] * 2 + [
            "finalize:2", "close"]

        with TrlReader(path) as r:
            blocks = [[(c.var_id, c.time, c.value) for c in blk]
                      for blk in r.iter_vc_blocks()]
        assert sorted(sum(blocks, [])) == sorted(changes)
        assert len(blocks) == 2
    finally:
        os.unlink(path)
