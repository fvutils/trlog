"""Pluggable codec layer (pure-Python reference).

This is the Python mirror of the C storage SPI + codec vtable described in
``docs/design/pluggable-codecs.md`` §4 and the implementation plan §3 / Phase 1.
It introduces the *seam* between the container (writer/reader internals) and the
*semantics* (how a stream's payload bytes are produced and consumed), without
changing any on-disk bytes for the built-in path.

Three things live here:

* :class:`VcCodec` / :class:`TxnCodec` — the abstract codec families, with the
  full lifecycle surface (``open_writer``/``open_reader``/``close``/
  ``finalize``/``encode_block``/``decode_block``/``input_streams``). The whole
  surface — including ``finalize`` and the trace-scoped context accessor — is
  defined now (Phase 1) even though the re-homed built-ins use a no-op
  ``finalize``, so later phases add capability without reshaping the ABI.
* A process-wide **registry** keyed by reverse-DNS codec id, with the
  :func:`vc_codec` / :func:`txn_codec` decorators mirroring the C
  ``trl_register_*_codec`` calls.
* :class:`Store` — the storage-SPI facade the core hands a codec: string
  interning, varint helpers, byte-compression-as-a-service, and a per-codec,
  per-trace shared context with a ``finalize`` hook.

The built-in :class:`CoreValueChangeCodec` / :class:`CoreRecordCodec` wrap the
existing ``VcDataBlock`` / ``TxnDataBlock`` encoders unchanged, so the default
write path is byte-for-byte identical (proven by the golden fixtures).
"""

from __future__ import annotations

import enum
import re
import zlib
from abc import ABC, abstractmethod
from typing import Any, Callable, Dict, List, Optional, Tuple

from ._codec import (
    encode_uvarint, decode_uvarint, encode_svarint, decode_svarint,
)


# ---------------------------------------------------------------------------
# Reverse-DNS ids of the built-in (core) codecs. The prefix
# ``org.fvutils.trlog.`` is reserved for built-ins (design §4.1/§4.2).
# ---------------------------------------------------------------------------

CORE_VALUECHANGE = "org.fvutils.trlog.core.valuechange"
CORE_RECORD      = "org.fvutils.trlog.core.record"
CORE_BYTESPLIT   = "org.fvutils.trlog.core.bytesplit"
CORE_DERIVED     = "org.fvutils.trlog.core.derived"

_CODEC_ID_RE = re.compile(r"^[a-z0-9]+(\.[a-z0-9-]+)+$")


def canonical_codec_id(codec_id: str) -> str:
    """Validate/normalise a reverse-DNS codec id (design §4.1 canonical form:
    case-sensitive, characters ``[a-z0-9.-]``, dot-separated, no empty
    segments). Returns the id unchanged if valid; raises ``ValueError`` else."""
    if not _CODEC_ID_RE.match(codec_id):
        raise ValueError(
            f"invalid codec id {codec_id!r}: must be reverse-DNS, lowercase "
            f"[a-z0-9-], dot-separated with no empty segments"
        )
    return codec_id


class Capability(enum.IntFlag):
    """Codec capability bits (design §4.1)."""
    NONE               = 0
    RANDOM_ACCESS      = 1 << 0
    NEEDS_INPUT_STREAMS = 1 << 1
    HAS_RAW_FALLBACK   = 1 << 2
    LOSSLESS           = 1 << 3
    MATERIALIZED       = 1 << 4


# ---------------------------------------------------------------------------
# Storage SPI facade
# ---------------------------------------------------------------------------

class Store:
    """The storage-SPI surface the core offers a codec (design §4.0).

    A single ``Store`` is created per writer or per reader and shared across all
    codecs operating on that trace. It exposes only container services; it never
    interprets payload semantics. The bound ``writer`` / ``reader`` are exposed
    so the built-in codecs can reach the existing internals while the richer SPI
    is fleshed out in later phases.
    """

    def __init__(self, *, writer=None, reader=None) -> None:
        self.writer = writer
        self.reader = reader
        # Trace-scoped, per-codec shared context (design §4.7). Lazily created;
        # finalized once at trace close.
        self._codec_ctx: Dict[str, Any] = {}

    # -- string + buffer primitives ------------------------------------------

    def intern(self, s: str) -> int:
        if self.writer is not None:
            return self.writer.intern(s)
        raise RuntimeError("intern() is only available on the write path")

    # varint helpers are part of the SPI surface (design §4.0)
    encode_uvarint = staticmethod(encode_uvarint)
    decode_uvarint = staticmethod(decode_uvarint)
    encode_svarint = staticmethod(encode_svarint)
    decode_svarint = staticmethod(decode_svarint)

    # -- byte compression as a service ---------------------------------------

    @staticmethod
    def compress(data: bytes) -> bytes:
        return zlib.compress(data)

    @staticmethod
    def decompress(data: bytes) -> bytes:
        return zlib.decompress(data)

    # -- trace-scoped per-codec context (design §4.7) ------------------------

    def codec_ctx(self, codec_id: str, factory: Callable[[], Any] = dict) -> Any:
        """Return the per-codec, per-trace shared context, creating it on first
        use. Trace-wide accumulators (intern tables, shared registries) hang off
        this, distinct from per-stream state."""
        ctx = self._codec_ctx.get(codec_id)
        if ctx is None:
            ctx = factory()
            self._codec_ctx[codec_id] = ctx
        return ctx

    def has_codec_ctx(self, codec_id: str) -> bool:
        return codec_id in self._codec_ctx


# Type aliases for the emit callbacks the core hands a decoder.
VcEmit  = Callable[[Any], None]   # receives a VcChange
TxnEmit = Callable[[Any], None]   # receives a transaction record


# ---------------------------------------------------------------------------
# Codec base classes
# ---------------------------------------------------------------------------

class _CodecBase(ABC):
    """Common identity + lifecycle for both codec families."""

    #: reverse-DNS canonical id; set by subclass or the decorator
    codec_id: str = ""
    #: codec-internal format version
    version: int = 1
    #: capability bitset
    caps: Capability = Capability.NONE

    # -- lifecycle (design §4.1/§4.7) ----------------------------------------

    @abstractmethod
    def open_writer(self, store: Store, stream_id: int, params: bytes) -> Any:
        """Create codec-private per-stream writer state."""

    @abstractmethod
    def open_reader(self, store: Store, stream_id: int, params: bytes) -> Any:
        """Create codec-private per-stream reader state."""

    def close(self, state: Any) -> None:
        """Release per-stream state. Default: nothing to do."""

    def finalize(self, state: Any, store: Store) -> int:
        """Flush trace-wide accumulated metadata at trace close, after the last
        ``encode_block`` and before the index/footer. Built-ins are stateless
        per trace, so the default is a no-op. Returns 0 on success."""
        return 0

    def input_streams(self, state: Any, store: Store) -> List[int]:
        """Stream ids that must be materialized before this codec can decode.
        Empty for self-contained codecs."""
        return []


class VcCodec(_CodecBase):
    """Value-change codec family: per-variable change sequences ⇄ block bytes."""

    @abstractmethod
    def encode_block(self, state: Any, store: Store) -> Tuple[bytes, int, int]:
        """Flush accumulated state to block bytes. Returns
        ``(block_bytes, start_time, end_time)`` — the core compresses/indexes."""

    @abstractmethod
    def decode_block(self, state: Any, store: Store, payload: bytes, flags: int,
                     emit: VcEmit) -> None:
        """Reconstruct value-changes from a (already block-framed) payload and
        push each through ``emit``."""


class TxnCodec(_CodecBase):
    """Record codec family: sequences of transaction records ⇄ block bytes."""

    @abstractmethod
    def encode_block(self, state: Any, store: Store) -> Tuple[bytes, int, int]:
        """Flush accumulated records to block bytes. Returns
        ``(block_bytes, start_time, end_time)``."""

    @abstractmethod
    def decode_block(self, state: Any, store: Store, payload: bytes, flags: int,
                     emit: TxnEmit) -> None:
        """Reconstruct records from a payload and push each through ``emit``."""


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------

_VC_CODECS:  Dict[str, VcCodec]  = {}
_TXN_CODECS: Dict[str, TxnCodec] = {}


def register_vc_codec(codec: VcCodec) -> VcCodec:
    canonical_codec_id(codec.codec_id)
    _VC_CODECS[codec.codec_id] = codec
    return codec


def register_txn_codec(codec: TxnCodec) -> TxnCodec:
    canonical_codec_id(codec.codec_id)
    _TXN_CODECS[codec.codec_id] = codec
    return codec


def lookup_vc_codec(codec_id: str) -> Optional[VcCodec]:
    return _VC_CODECS.get(codec_id)


def lookup_txn_codec(codec_id: str) -> Optional[TxnCodec]:
    return _TXN_CODECS.get(codec_id)


def registered_vc_codecs() -> List[str]:
    return sorted(_VC_CODECS)


def registered_txn_codecs() -> List[str]:
    return sorted(_TXN_CODECS)


def _make_decorator(register, base):
    def deco(codec_id: str, *, version: int = 1, caps: Capability = Capability.NONE):
        canonical_codec_id(codec_id)

        def wrap(cls):
            if not issubclass(cls, base):
                raise TypeError(f"{cls.__name__} must subclass {base.__name__}")
            cls.codec_id = codec_id
            cls.version = version
            cls.caps = caps
            register(cls())   # register a singleton instance (the "vtable")
            return cls
        return wrap
    return deco


#: ``@vc_codec("com.acme.codec.fancy", version=1)`` — mirrors trl_register_vc_codec
vc_codec  = _make_decorator(register_vc_codec, VcCodec)
#: ``@txn_codec("com.acme.codec.fancy", version=1)``
txn_codec = _make_decorator(register_txn_codec, TxnCodec)


# ---------------------------------------------------------------------------
# Built-in codecs — thin wrappers over the existing encoders (zero byte change)
# ---------------------------------------------------------------------------

class _VcWriterState:
    """Per-stream writer state for the core value-change codec: owns the
    ``VcDataBlock`` currently being filled."""

    __slots__ = ("blk",)

    def __init__(self, blk) -> None:
        self.blk = blk


@vc_codec(CORE_VALUECHANGE, version=1, caps=Capability.RANDOM_ACCESS | Capability.LOSSLESS)
class CoreValueChangeCodec(VcCodec):
    """The current time-table + XOR-delta + RLE value-change encoder, re-homed
    behind the codec SPI (design §5.1). No behaviour or byte change."""

    def open_writer(self, store: Store, stream_id: int, params: bytes) -> _VcWriterState:
        # Per-block VcDataBlock is built lazily in new_block() exactly as
        # _VcBlockContext used to, so output stays byte-identical.
        return _VcWriterState(None)

    def new_block(self, store: Store, state: _VcWriterState, start_time: int):
        from ._vc_data import VcDataBlock
        w = store.writer
        sig_types = dict(w._sig_types)
        sig_types.update(w._var_types)
        state.blk = VcDataBlock(
            start_time=start_time,
            sig_types=sig_types,
            compress=w._compress,
            compress_time_table=w._compress_time_table,
            compress_waves=w._compress_waves,
            compress_waves_alg=w._compress_waves_alg,
            seekable=w._seekable,
        )
        return state.blk

    def open_reader(self, store: Store, stream_id: int, params: bytes) -> None:
        return None

    def encode_block(self, state: _VcWriterState, store: Store) -> Tuple[bytes, int, int]:
        blk = state.blk
        return blk.encode_block(), blk.start_time, blk.end_time

    def decode_block(self, state, store: Store, payload: bytes, flags: int,
                     emit: VcEmit) -> None:
        from ._vc_data import VcDataBlock
        sig_types = store.reader._build_sig_types() if store.reader else {}
        blk = VcDataBlock(start_time=0, sig_types=sig_types)
        for change in blk.read_block(payload, flags=flags):
            emit(change)


@vc_codec(CORE_BYTESPLIT, version=1,
          caps=Capability.RANDOM_ACCESS | Capability.LOSSLESS)
class CoreBytesplitCodec(VcCodec):
    """Byte-plane-split value-change codec (design §5.4): a transform-then-
    compress codec for fixed-width REAL / wide-vector signals. Self-identifying
    blocks (FLAG_STRUCT_CODEC + interned codec-id prefix) so unknown readers
    skip them; params record the per-variable plane count (byte width)."""

    def open_writer(self, store: Store, stream_id: int, params: bytes) -> _VcWriterState:
        return _VcWriterState(None)

    def new_block(self, store: Store, state: _VcWriterState, start_time: int):
        from ._bytesplit import BytesplitBlock
        w = store.writer
        sig_types = dict(w._sig_types)
        sig_types.update(w._var_types)
        state.blk = BytesplitBlock(start_time=start_time, sig_types=sig_types,
                                   compress=w._compress)
        return state.blk

    def open_reader(self, store: Store, stream_id: int, params: bytes) -> None:
        return None

    def encode_block(self, state: _VcWriterState, store: Store) -> Tuple[bytes, int, int]:
        blk = state.blk
        codec_id_str = store.intern(CORE_BYTESPLIT)
        return blk.encode_block(codec_id_str), blk.start_time, blk.end_time

    def decode_block(self, state, store: Store, payload: bytes, flags: int,
                     emit: VcEmit) -> None:
        from ._bytesplit import BytesplitBlock
        from ._types import VcChange
        BytesplitBlock.decode_changes(payload, emit, VcChange)


@vc_codec(CORE_DERIVED, version=1,
          caps=Capability.NEEDS_INPUT_STREAMS | Capability.LOSSLESS)
class CoreDerivedCodec(VcCodec):
    """Derived (virtual) signal codec (design §5.2): stores an *expression over
    other signals*, not per-event data. Unlike block codecs, derived values are
    not encoded into a self-contained block — they are evaluated lazily by the
    reader from the stored definition (aux catalog) and the input signals (see
    ``TrlReader.read_derived`` / ``TrlWriter.add_derived_signal``). The
    registration exists for governance and capability discovery; the
    block-encode/decode slots are unused."""

    def open_writer(self, store: Store, stream_id: int, params: bytes):
        return None

    def open_reader(self, store: Store, stream_id: int, params: bytes):
        return None

    def encode_block(self, state, store: Store) -> Tuple[bytes, int, int]:
        raise NotImplementedError(
            "core.derived stores an expression, not blocks; "
            "use TrlWriter.add_derived_signal")

    def decode_block(self, state, store: Store, payload: bytes, flags: int,
                     emit: VcEmit) -> None:
        raise NotImplementedError(
            "core.derived is evaluated via TrlReader.read_derived")


class _TxnWriterState:
    __slots__ = ("blk",)

    def __init__(self, blk) -> None:
        self.blk = blk


@txn_codec(CORE_RECORD, version=1, caps=Capability.RANDOM_ACCESS | Capability.LOSSLESS)
class CoreRecordCodec(TxnCodec):
    """The current delta + column transaction encoder, re-homed behind the
    codec SPI (design §5.3). No behaviour or byte change."""

    def open_writer(self, store: Store, stream_id: int, params: bytes) -> _TxnWriterState:
        return _TxnWriterState(None)

    def new_block(self, store: Store, state: _TxnWriterState, start_time: int):
        from ._txn_data import TxnDataBlock
        w = store.writer
        blk = TxnDataBlock(start_time=start_time, compress=w._compress,
                           column_layout=w._column_layout)
        blk._delta_encode = w._delta_encode_txn
        for txn_type_id, fts in w._txn_schemas.items():
            blk.set_schema(txn_type_id, fts)
        state.blk = blk
        return blk

    def open_reader(self, store: Store, stream_id: int, params: bytes) -> None:
        return None

    def encode_block(self, state: _TxnWriterState, store: Store) -> Tuple[bytes, int, int]:
        blk = state.blk
        return blk.encode_block(), blk.start_time, blk.end_time

    def decode_block(self, state, store: Store, payload: bytes, flags: int,
                     emit: TxnEmit) -> None:
        from ._txn_data import TxnDataBlock
        schemas = store.reader._build_txn_schemas() if store.reader else None
        blk = TxnDataBlock(compress=False)
        for rec in blk.read_block(payload, flags=flags, schemas=schemas):
            emit(rec)


__all__ = [
    "CORE_VALUECHANGE", "CORE_RECORD", "CORE_BYTESPLIT", "CORE_DERIVED",
    "canonical_codec_id", "Capability", "Store",
    "VcCodec", "TxnCodec",
    "register_vc_codec", "register_txn_codec",
    "lookup_vc_codec", "lookup_txn_codec",
    "registered_vc_codecs", "registered_txn_codecs",
    "vc_codec", "txn_codec",
    "CoreValueChangeCodec", "CoreRecordCodec", "CoreBytesplitCodec", "CoreDerivedCodec",
]
