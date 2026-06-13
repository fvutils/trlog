"""Phase 3.5 — ctypes codec bridge (Python codec + C container, pairing 3).

Proves the minimal slice: a single Python :class:`VcCodec` driven by the *C*
container through the codec ABI produces the **same payload bytes** as the same
codec run in the pure-Python container, that the lifecycle
(open_writer → encode_block → finalize → close) fires across the bridge, and that
an exception raised inside a Python callback propagates back rather than being
swallowed at the C boundary.

Skipped unless ``libtrl.so`` is loaded (the bridge's whole point is the native
container).
"""

from __future__ import annotations

import ctypes
import os
import tempfile
from typing import Any, List, Tuple

import pytest

from trlog._codec_bridge import AVAILABLE, register_native
from trlog._native import _LIB
from trlog.codec import (
    Capability, Store, VcCodec, lookup_vc_codec, vc_codec,
)

pytestmark = pytest.mark.skipif(not AVAILABLE, reason="libtrl.so not available")

TRIVIAL_ID = "com.example.bridge.trivial"
FAILING_ID = "com.example.bridge.failing"


# ---------------------------------------------------------------------------
# A trivial, self-contained value-change codec used on both sides of the bridge.
# Its block collects (var, time, value) and its encode is a canonical (sorted)
# uvarint stream, so the byte output is independent of the order the container
# happens to present the changes in (the C container groups them var-major).
# ---------------------------------------------------------------------------

class _TrivialBlock:
    def __init__(self, start_time: int) -> None:
        self.start_time = start_time
        self.end_time = start_time
        self.changes: List[Tuple[int, int, int]] = []

    def add_change(self, var_id: int, time: int, value: int) -> None:
        self.changes.append((var_id, time, value))
        self.end_time = max(self.end_time, time)


class _TrivialState:
    __slots__ = ("blk",)

    def __init__(self) -> None:
        self.blk = None


@vc_codec(TRIVIAL_ID, version=3, caps=Capability.LOSSLESS)
class TrivialVcCodec(VcCodec):
    def __init__(self) -> None:
        self.events: List[str] = []

    def open_writer(self, store: Store, stream_id: int, params: bytes) -> _TrivialState:
        self.events.append("open_writer")
        return _TrivialState()

    def new_block(self, store: Store, state: _TrivialState, start_time: int):
        state.blk = _TrivialBlock(start_time)
        return state.blk

    def open_reader(self, store: Store, stream_id: int, params: bytes) -> None:
        return None

    def encode_block(self, state: _TrivialState, store: Store) -> Tuple[bytes, int, int]:
        self.events.append("encode_block")
        blk = state.blk
        body = bytearray()
        body += Store.encode_uvarint(len(blk.changes))
        for var_id, time, value in sorted(blk.changes):
            body += Store.encode_uvarint(var_id)
            body += Store.encode_uvarint(time)
            body += Store.encode_uvarint(value)
        return bytes(body), blk.start_time, blk.end_time

    def decode_block(self, state, store: Store, payload: bytes, flags: int, emit) -> None:
        off = 0
        n, off = Store.decode_uvarint(payload, off)
        for _ in range(n):
            var_id, off = Store.decode_uvarint(payload, off)
            time, off = Store.decode_uvarint(payload, off)
            value, off = Store.decode_uvarint(payload, off)
            emit((var_id, time, value))

    def finalize(self, state, store: Store) -> int:
        self.events.append("finalize")
        return 0

    def close(self, state) -> None:
        self.events.append("close")


@vc_codec(FAILING_ID, version=1, caps=Capability.LOSSLESS)
class FailingVcCodec(VcCodec):
    """encode_block raises — used to prove exception propagation across the C
    boundary."""

    def open_writer(self, store, stream_id, params):
        return _TrivialState()

    def new_block(self, store, state, start_time):
        state.blk = _TrivialBlock(start_time)
        return state.blk

    def open_reader(self, store, stream_id, params):
        return None

    def encode_block(self, state, store):
        raise ValueError("boom from a Python codec callback")

    def decode_block(self, state, store, payload, flags, emit):
        return None


CHANGES = [(0, 0, 7), (1, 0, 100), (0, 5, 8), (1, 5, 99), (0, 10, 9)]


def _python_payload(codec: TrivialVcCodec) -> bytes:
    """Run the codec in the pure-Python container path and capture its payload."""
    store = Store()
    state = codec.open_writer(store, 0, b"")
    blk = codec.new_block(store, state, 0)
    for var_id, time, value in CHANGES:
        blk.add_change(var_id, time, value)
    payload, _start, _end = codec.encode_block(state, store)
    return payload


def _write_native(codec_id: str, changes) -> Tuple[Any, str]:
    """Drive the C writer with the bridged codec; returns (handle-already-closed
    NativeBridge, path). Leaves the C writer closed."""
    bridge = register_native(lookup_vc_codec(codec_id))
    fd, path = tempfile.mkstemp(suffix=".trl")
    os.close(fd)
    h = _LIB.trl_writer_open(path.encode("utf-8"), -9, 0)
    assert h, "trl_writer_open returned NULL"
    rc = _LIB.trl_writer_set_vc_codec(h, codec_id.encode("utf-8"))
    assert rc == 0, f"set_vc_codec failed rc={rc}"
    _LIB.trl_vc_begin(h, 0)
    for var_id, time, value in changes:
        _LIB.trl_vc_change_u64(h, var_id, time, value)
    flush_rc = _LIB.trl_vc_flush(h)
    _LIB.trl_writer_close(h)
    return bridge, path, flush_rc


def test_payload_parity_python_vs_c_container():
    """The same VcCodec emits byte-identical payloads in pairing 1 (pure Python)
    and pairing 3 (C container via the bridge)."""
    codec = lookup_vc_codec(TRIVIAL_ID)
    payload_py = _python_payload(codec)

    bridge, path, flush_rc = _write_native(TRIVIAL_ID, CHANGES)
    try:
        assert flush_rc == 0
        bridge.raise_pending()  # no callback exception expected
        assert bridge.last_payload is not None
        # The codec-payload seam is byte-identical across containers.
        assert bridge.last_payload == payload_py
        # And the C container actually produced a non-empty file around it.
        assert os.path.getsize(path) > 0
    finally:
        os.unlink(path)


def test_lifecycle_order_across_bridge():
    """open_writer → encode_block → finalize → close fire in order over the SPI."""
    codec = lookup_vc_codec(TRIVIAL_ID)
    codec.events.clear()
    bridge, path, _flush_rc = _write_native(TRIVIAL_ID, CHANGES)
    try:
        assert codec.events == ["open_writer", "encode_block", "finalize", "close"]
    finally:
        os.unlink(path)


def test_exception_propagates_across_boundary():
    """An exception in a Python callback is surfaced (the C entry point reports
    an error and raise_pending re-raises it), not silently swallowed."""
    bridge, path, flush_rc = _write_native(FAILING_ID, CHANGES)
    try:
        assert flush_rc != 0, "C flush should report the callback failure"
        with pytest.raises(ValueError, match="boom from a Python codec callback"):
            bridge.raise_pending()
    finally:
        os.unlink(path)
