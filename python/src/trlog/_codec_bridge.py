"""ctypes codec bridge — a Python codec driven by the C container (pairing 3).

This is Phase 3.5 of the pluggable-codecs plan (§0 pairing 3, §4 Phase 3.5): the
all-C storage container drives a *Python* :class:`~trlog.codec.VcCodec` through
the plain-C codec ABI (``c/include/trl/trl_codec.h``). It works precisely because
that ABI is ctypes-friendly by construction — every vtable slot is a function
pointer and every SPI call is an exported symbol (impl-plan §3, decision 5):

* ctypes synthesizes C-callable ``CFUNCTYPE`` thunks for the Python codec's
  vtable and registers them with the C registry (``trl_register_vc_codec``), so
  the C writer calls ``encode_block``/``finalize``/``close`` as ordinary function
  pointers that trampoline into Python;
* the Python codec calls *back* into the SPI — ``trl_store_vc_change_*`` to read
  the block's accumulated value-changes, ``trl_blkout_append`` to write its
  payload, ``trl_store_intern`` for the string table.

**Minimal slice (this file).** It bridges a single value-change stream of U64
changes, which is enough to prove the mechanism end-to-end and to lock the SPI
additions (writer codec-selection + the change-accessor) that Phase 1 deferred.
The same :class:`VcCodec` runs unchanged in the pure-Python container (pairing 1)
and here under the C container (pairing 3); only the *source* of the changes
differs (an in-process block vs. the C accessor), so the codec's payload bytes
are byte-identical between the two. Container framing (block header, index,
compression) is whatever the respective container writes — the two containers
have not yet converged (the Phase-0 xfails), so parity here is at the
codec-*payload* seam, as the design intends (§0: "wrapped by the C container's
framing").

The bridge is import-safe without ``libtrl.so``: :data:`AVAILABLE` is then False
and :func:`register_native` raises a clear error.
"""

from __future__ import annotations

import ctypes
from typing import Any, List, Optional

from ._native import _LIB
from .codec import Store, VcCodec


AVAILABLE = _LIB is not None


# ---------------------------------------------------------------------------
# ctypes mirror of the codec ABI (c/include/trl/trl_codec.h)
# ---------------------------------------------------------------------------

# Vtable slot signatures. `trl_store`/`trl_blkout`/`self` are opaque pointers.
_OPEN_FN     = ctypes.CFUNCTYPE(ctypes.c_void_p, ctypes.c_void_p,
                                ctypes.c_uint32, ctypes.c_void_p, ctypes.c_size_t)
_CLOSE_FN    = ctypes.CFUNCTYPE(None, ctypes.c_void_p)
_FINALIZE_FN = ctypes.CFUNCTYPE(ctypes.c_int, ctypes.c_void_p, ctypes.c_void_p)
_ENCODE_FN   = ctypes.CFUNCTYPE(ctypes.c_int, ctypes.c_void_p, ctypes.c_void_p,
                                ctypes.c_void_p)
_DECODE_FN   = ctypes.CFUNCTYPE(ctypes.c_int, ctypes.c_void_p, ctypes.c_void_p,
                                ctypes.c_void_p, ctypes.c_size_t, ctypes.c_uint8,
                                ctypes.c_void_p, ctypes.c_void_p)
_INPUTS_FN   = ctypes.CFUNCTYPE(ctypes.c_int, ctypes.c_void_p, ctypes.c_void_p,
                                ctypes.c_void_p, ctypes.c_void_p)


class _VcCodecVTable(ctypes.Structure):
    """Layout of ``trl_vc_codec_t``. Field order + types must match the header
    exactly; ctypes uses native alignment, matching the C struct ABI."""

    _fields_ = [
        ("codec_id", ctypes.c_char_p),
        ("version", ctypes.c_uint16),
        ("caps", ctypes.c_uint32),
        ("open_writer", _OPEN_FN),
        ("open_reader", _OPEN_FN),
        ("close", _CLOSE_FN),
        ("finalize", _FINALIZE_FN),
        ("encode_block", _ENCODE_FN),
        ("decode_block", _DECODE_FN),
        ("input_streams", _INPUTS_FN),
    ]


def _bind_spi() -> None:
    """Declare arg/return types for the SPI symbols the bridge uses."""
    _LIB.trl_register_vc_codec.argtypes = [ctypes.POINTER(_VcCodecVTable)]
    _LIB.trl_register_vc_codec.restype = ctypes.c_int
    _LIB.trl_writer_set_vc_codec.argtypes = [ctypes.c_void_p, ctypes.c_char_p]
    _LIB.trl_writer_set_vc_codec.restype = ctypes.c_int
    _LIB.trl_store_intern.argtypes = [ctypes.c_void_p, ctypes.c_char_p]
    _LIB.trl_store_intern.restype = ctypes.c_uint32
    _LIB.trl_blkout_append.argtypes = [ctypes.c_void_p, ctypes.c_void_p,
                                       ctypes.c_size_t]
    _LIB.trl_blkout_append.restype = ctypes.c_int
    _LIB.trl_store_vc_change_count.argtypes = [ctypes.c_void_p]
    _LIB.trl_store_vc_change_count.restype = ctypes.c_size_t
    _LIB.trl_store_vc_start_time.argtypes = [ctypes.c_void_p]
    _LIB.trl_store_vc_start_time.restype = ctypes.c_uint64
    _LIB.trl_store_vc_end_time.argtypes = [ctypes.c_void_p]
    _LIB.trl_store_vc_end_time.restype = ctypes.c_uint64
    _LIB.trl_store_vc_change_at.argtypes = [
        ctypes.c_void_p, ctypes.c_size_t,
        ctypes.POINTER(ctypes.c_uint32), ctypes.POINTER(ctypes.c_uint64),
        ctypes.POINTER(ctypes.c_uint32), ctypes.POINTER(ctypes.c_uint64)]
    _LIB.trl_store_vc_change_at.restype = ctypes.c_int


if AVAILABLE:
    _bind_spi()


# ---------------------------------------------------------------------------
# Store facade over a live C trl_store pointer (valid only inside a callback)
# ---------------------------------------------------------------------------

class _BridgeStore(Store):
    """A :class:`Store` whose services are fulfilled by the C container through
    the SPI. Created per-callback around the live ``trl_store*`` the core hands
    in; do not retain it past the callback (the pointer is only valid then)."""

    def __init__(self, cstore: int) -> None:
        super().__init__(writer=None, reader=None)
        self._cstore = cstore

    def intern(self, s: str) -> int:
        return int(_LIB.trl_store_intern(self._cstore, s.encode("utf-8")))

    # -- change-accessor: the block's accumulated changes, var-major ----------

    def vc_start_time(self) -> int:
        return int(_LIB.trl_store_vc_start_time(self._cstore))

    def vc_end_time(self) -> int:
        return int(_LIB.trl_store_vc_end_time(self._cstore))

    def vc_changes(self) -> List[tuple]:
        """Return ``[(var_id, time, value), ...]`` for the current block
        (U64-kind values, this slice)."""
        n = int(_LIB.trl_store_vc_change_count(self._cstore))
        out: List[tuple] = []
        var_id = ctypes.c_uint32()
        t = ctypes.c_uint64()
        kind = ctypes.c_uint32()
        u64 = ctypes.c_uint64()
        for i in range(n):
            rc = _LIB.trl_store_vc_change_at(
                self._cstore, i, ctypes.byref(var_id), ctypes.byref(t),
                ctypes.byref(kind), ctypes.byref(u64))
            if rc != 0:
                break
            out.append((var_id.value, t.value, u64.value))
        return out


# ---------------------------------------------------------------------------
# The bridge: wrap a VcCodec instance as a C vtable and register it
# ---------------------------------------------------------------------------

# Module-level keep-alive: ctypes does NOT keep CFUNCTYPE thunks or the vtable
# Structure alive once register_native returns, but the C registry stores the
# pointer for the trace's lifetime. Dropping them would crash the writer.
_KEEPALIVE: List[Any] = []


class NativeBridge:
    """Holds the C vtable + CFUNCTYPE thunks for one bridged :class:`VcCodec`,
    the per-stream state threaded across the lifecycle, and any exception raised
    inside a callback (so it can be re-raised on the Python side rather than
    silently swallowed when it crosses the C boundary)."""

    def __init__(self, codec: VcCodec) -> None:
        self.codec = codec
        self._state: Any = None
        self.pending_exc: Optional[BaseException] = None
        self.last_payload: Optional[bytes] = None
        self._vtable = self._build_vtable(codec)

    # -- exception trampoline -------------------------------------------------

    def _record(self, exc: BaseException) -> None:
        # Keep the first exception; later callbacks won't run meaningful work.
        if self.pending_exc is None:
            self.pending_exc = exc

    def raise_pending(self) -> None:
        """Re-raise (and clear) any exception captured inside a callback. Call
        after a C entry point returns an error so failures surface in Python."""
        exc = self.pending_exc
        if exc is not None:
            self.pending_exc = None
            raise exc

    # -- vtable construction --------------------------------------------------

    def _build_vtable(self, codec: VcCodec) -> _VcCodecVTable:
        codec_id_bytes = codec.codec_id.encode("utf-8")

        def open_writer(cstore, stream_id, params, param_len):
            try:
                self._state = codec.open_writer(_BridgeStore(cstore),
                                                int(stream_id), b"")
            except BaseException as e:  # noqa: BLE001 - trampoline to C
                self._record(e)
            return 0   # per-stream state lives on the Python side (single stream)

        def open_reader(cstore, stream_id, params, param_len):
            return 0

        def close(self_ptr):
            try:
                codec.close(self._state)
            except BaseException as e:  # noqa: BLE001
                self._record(e)

        def finalize(self_ptr, cstore):
            try:
                return int(codec.finalize(self._state, _BridgeStore(cstore)))
            except BaseException as e:  # noqa: BLE001
                self._record(e)
                return -1

        def encode_block(self_ptr, cstore, out):
            try:
                store = _BridgeStore(cstore)
                # Rebuild the codec's per-block state from the C-accumulated
                # changes, then run the *same* encode_block as the pure-Python
                # container — that shared path is what guarantees payload parity.
                blk = codec.new_block(store, self._state, store.vc_start_time())
                for var_id, time, value in store.vc_changes():
                    blk.add_change(var_id, time, value)
                payload, _start, _end = codec.encode_block(self._state, store)
                payload = bytes(payload)
                self.last_payload = payload
                if payload:
                    rc = _LIB.trl_blkout_append(out, payload, len(payload))
                    if rc != 0:
                        return -1
                return 0
            except BaseException as e:  # noqa: BLE001
                self._record(e)
                return -1

        def decode_block(self_ptr, cstore, payload, length, flags, emit, user):
            return 0   # decode-side bridge is future work (this slice is write)

        def input_streams(self_ptr, cstore, out_ids, n):
            return 0

        vt = _VcCodecVTable()
        vt.codec_id = codec_id_bytes
        vt.version = int(getattr(codec, "version", 1))
        vt.caps = int(getattr(codec, "caps", 0))
        vt.open_writer = _OPEN_FN(open_writer)
        vt.open_reader = _OPEN_FN(open_reader)
        vt.close = _CLOSE_FN(close)
        vt.finalize = _FINALIZE_FN(finalize)
        vt.encode_block = _ENCODE_FN(encode_block)
        vt.decode_block = _DECODE_FN(decode_block)
        vt.input_streams = _INPUTS_FN(input_streams)

        # Keep the thunks, the codec-id bytes, and the struct itself alive.
        _KEEPALIVE.extend([
            vt, codec_id_bytes,
            vt.open_writer, vt.open_reader, vt.close, vt.finalize,
            vt.encode_block, vt.decode_block, vt.input_streams,
        ])
        return vt

    def register(self) -> None:
        rc = _LIB.trl_register_vc_codec(ctypes.byref(self._vtable))
        if rc != 0:
            raise RuntimeError(
                f"trl_register_vc_codec failed for {self.codec.codec_id!r} (rc={rc})")


def register_native(codec: VcCodec) -> NativeBridge:
    """Register a Python :class:`VcCodec` with the C container's codec registry
    so the C writer can drive it (pairing 3). Returns the :class:`NativeBridge`,
    which the caller keeps for the trace's lifetime (it owns the live callbacks)
    and uses to surface callback exceptions via :meth:`NativeBridge.raise_pending`."""
    if not AVAILABLE:
        raise RuntimeError(
            "the ctypes codec bridge requires libtrl.so, which is not loaded")
    bridge = NativeBridge(codec)
    bridge.register()
    return bridge


__all__ = ["AVAILABLE", "NativeBridge", "register_native"]
