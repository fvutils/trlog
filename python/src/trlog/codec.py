"""Pluggable codec layer (pure-Python implementation).

A *codec* turns a stream's data (value changes or transaction records) into
block bytes and back. The container (the writer and reader) owns framing,
indexing, the string table and the type registry; a codec owns only the
payload. This module is the Python side of that boundary. The C side is
``c/include/trl/trl_codec.h``.

The module provides:

* :class:`VcCodec` and :class:`TxnCodec`, the abstract base classes for the
  value-change and transaction-record codec families. Both share the lifecycle
  ``open_writer`` / ``open_reader`` / ``encode_block`` / ``decode_block`` /
  ``finalize`` / ``close`` / ``input_streams``.
* A process-wide registry keyed by reverse-DNS codec id, filled by
  :func:`register_vc_codec` / :func:`register_txn_codec` or the
  :func:`vc_codec` / :func:`txn_codec` class decorators, and queried with
  :func:`lookup_vc_codec` / :func:`lookup_txn_codec`.
* :class:`Store`, the service object the container passes to every codec call:
  string interning, varint helpers, zlib compression and a per-codec,
  per-trace context.
* The built-in codecs, registered at import time:
  :class:`CoreValueChangeCodec` (:data:`CORE_VALUECHANGE`),
  :class:`CoreRecordCodec` (:data:`CORE_RECORD`),
  :class:`CoreBytesplitCodec` (:data:`CORE_BYTESPLIT`) and
  :class:`CoreDerivedCodec` (:data:`CORE_DERIVED`).

The default value-change and record codecs wrap the ``VcDataBlock`` and
``TxnDataBlock`` encoders, so a file written without selecting a codec is
byte-for-byte the same as one written before the codec layer existed. The
golden fixtures in ``tests/golden/`` check this.
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
# ``org.fvutils.trlog.`` is reserved for codecs shipped with trlog.
# ---------------------------------------------------------------------------

#: Default value-change codec (time table + XOR-delta + RLE).
CORE_VALUECHANGE = "org.fvutils.trlog.core.valuechange"
#: Default transaction-record codec (row or column layout, delta encoding).
CORE_RECORD      = "org.fvutils.trlog.core.record"
#: Opt-in value-change codec: byte-plane split + zlib for fixed-width values.
CORE_BYTESPLIT   = "org.fvutils.trlog.core.bytesplit"
#: Derived (virtual) signals stored as an expression over other signals.
CORE_DERIVED     = "org.fvutils.trlog.core.derived"

_CODEC_ID_RE = re.compile(r"^[a-z0-9]+(\.[a-z0-9-]+)+$")


def canonical_codec_id(codec_id: str) -> str:
    """Validate a reverse-DNS codec id.

    A valid id has at least two dot-separated segments, no empty segments,
    and uses only lowercase ``a-z``, digits and ``-``. The first segment may
    not contain ``-``.

    Args:
        codec_id: The id to check, for example ``"com.acme.codec.fancy"``.

    Returns:
        ``codec_id`` unchanged.

    Raises:
        ValueError: If ``codec_id`` is not in canonical form.
    """
    if not _CODEC_ID_RE.match(codec_id):
        raise ValueError(
            f"invalid codec id {codec_id!r}: must be reverse-DNS, lowercase "
            f"[a-z0-9-], dot-separated with no empty segments"
        )
    return codec_id


class Capability(enum.IntFlag):
    """Codec capability bits, declared by a codec through its ``caps``.

    The container does not change its behavior based on these bits today;
    they describe the codec for discovery and tooling.

    Attributes:
        NONE: No capabilities.
        RANDOM_ACCESS: Each block decodes on its own, without earlier blocks.
        NEEDS_INPUT_STREAMS: Decoding needs other streams first (see
            :meth:`VcCodec.input_streams`).
        HAS_RAW_FALLBACK: Data the codec cannot represent is also written in
            a generic form that readers without the codec can read.
        LOSSLESS: Decoding reproduces the written values exactly.
        MATERIALIZED: Decoded values are stored in the file rather than
            computed at read time.
    """
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
    """Container services passed to every codec call.

    Each :class:`~trlog._writer.TrlWriter` and :class:`~trlog._reader.TrlReader`
    creates one ``Store`` and passes it to all codecs that work on its trace.
    It offers container services only and never interprets payload bytes.

    Attributes:
        writer: The bound writer, or ``None`` on the read path. The built-in
            codecs use it to reach writer settings; third-party codecs should
            not depend on writer internals.
        reader: The bound reader, or ``None`` on the write path.
        encode_uvarint: :func:`trlog.encode_uvarint` (static).
        decode_uvarint: :func:`trlog.decode_uvarint` (static).
        encode_svarint: :func:`trlog.encode_svarint` (static).
        decode_svarint: :func:`trlog.decode_svarint` (static).
    """

    def __init__(self, *, writer=None, reader=None) -> None:
        self.writer = writer
        self.reader = reader
        # Trace-scoped, per-codec shared context. Created on first use.
        self._codec_ctx: Dict[str, Any] = {}

    # -- string + buffer primitives ------------------------------------------

    def intern(self, s: str) -> int:
        """Intern ``s`` in the trace's string table.

        Args:
            s: The string to intern.

        Returns:
            The string-table id. The same string always returns the same id.

        Raises:
            RuntimeError: On the read path (no writer is bound).
        """
        if self.writer is not None:
            return self.writer.intern(s)
        raise RuntimeError("intern() is only available on the write path")

    # varint helpers, available to codecs without importing private modules
    encode_uvarint = staticmethod(encode_uvarint)
    decode_uvarint = staticmethod(decode_uvarint)
    encode_svarint = staticmethod(encode_svarint)
    decode_svarint = staticmethod(decode_svarint)

    # -- byte compression as a service ---------------------------------------

    @staticmethod
    def compress(data: bytes) -> bytes:
        """Compress ``data`` with zlib (default level)."""
        return zlib.compress(data)

    @staticmethod
    def decompress(data: bytes) -> bytes:
        """Decompress zlib ``data`` produced by :meth:`compress`."""
        return zlib.decompress(data)

    # -- trace-scoped per-codec context ---------------------------------------

    def codec_ctx(self, codec_id: str, factory: Callable[[], Any] = dict) -> Any:
        """Return the shared context for one codec on this trace.

        Use it for state that spans all streams of a trace, such as a
        codec-private intern table, as opposed to the per-stream state that
        ``open_writer`` returns.

        Args:
            codec_id: Key for the context, normally the codec's own id.
            factory: Called with no arguments to create the context the first
                time it is requested.

        Returns:
            The context object. Later calls with the same ``codec_id`` return
            the same object.
        """
        ctx = self._codec_ctx.get(codec_id)
        if ctx is None:
            ctx = factory()
            self._codec_ctx[codec_id] = ctx
        return ctx

    def has_codec_ctx(self, codec_id: str) -> bool:
        """Return True if :meth:`codec_ctx` has created a context for ``codec_id``."""
        return codec_id in self._codec_ctx


# Type aliases for the emit callbacks the core hands a decoder.
VcEmit  = Callable[[Any], None]   # receives a VcChange
TxnEmit = Callable[[Any], None]   # receives a transaction record


# ---------------------------------------------------------------------------
# Codec base classes
# ---------------------------------------------------------------------------

class _CodecBase(ABC):
    """Identity and lifecycle shared by both codec families."""

    #: Reverse-DNS canonical id. Set by the subclass or by the decorator.
    codec_id: str = ""
    #: Codec-internal format version.
    version: int = 1
    #: :class:`Capability` bitset.
    caps: Capability = Capability.NONE

    # -- lifecycle -----------------------------------------------------------

    @abstractmethod
    def open_writer(self, store: Store, stream_id: int, params: bytes) -> Any:
        """Create per-stream writer state.

        Args:
            store: The writer's :class:`Store`.
            stream_id: Stream the state is for. The Python writer passes 0.
            params: Codec parameters. The Python writer passes ``b""``.

        Returns:
            Any object. It is passed back as ``state`` to the other methods.
        """

    @abstractmethod
    def open_reader(self, store: Store, stream_id: int, params: bytes) -> Any:
        """Create per-stream reader state.

        Args:
            store: The reader's :class:`Store`.
            stream_id: Stream the state is for.
            params: Codec parameters.

        Returns:
            Any object. The Python reader does not call this method today and
            passes ``state=None`` to :meth:`decode_block`.
        """

    def close(self, state: Any) -> None:
        """Release per-stream state. The default does nothing."""

    def finalize(self, state: Any, store: Store) -> int:
        """Write trace-wide data at trace close.

        Runs after the last ``encode_block`` and before the index and footer
        are written. The default does nothing.

        Args:
            state: The writer state from :meth:`open_writer`.
            store: The writer's :class:`Store`.

        Returns:
            0 on success.
        """
        return 0

    def input_streams(self, state: Any, store: Store) -> List[int]:
        """Return the stream ids that must be decoded before this codec can decode.

        The default returns an empty list (the codec is self-contained).
        """
        return []


class VcCodec(_CodecBase):
    """Base class for value-change codecs.

    A value-change codec converts per-variable ``(time, value)`` changes to
    the bytes of one ``BLK_VC_DATA`` block and back.

    Besides the abstract methods, a codec used with
    :meth:`TrlWriter.begin_vc_block() <trlog._writer.TrlWriter.begin_vc_block>`
    must implement ``new_block(store, state, start_time)``. It returns the
    block accumulator that receives ``add_change(var_id, time, value)`` and
    ``set_initial(var_id, value)`` calls, and it must expose ``start_time``
    and ``end_time``. ``encode_block`` then flushes that accumulator. See
    :doc:`/guide/writing-a-codec`.
    """

    @abstractmethod
    def encode_block(self, state: Any, store: Store) -> Tuple[bytes, int, int]:
        """Encode the block accumulated in ``state``.

        Args:
            state: The writer state from :meth:`open_writer`.
            store: The writer's :class:`Store`.

        Returns:
            ``(block_bytes, start_time, end_time)``. In the Python writer,
            ``block_bytes`` is a complete block: the 10-byte block header
            followed by the payload. The writer appends it to the file as is
            and indexes it under ``[start_time, end_time]``. A non-default
            codec sets ``FLAG_STRUCT_CODEC`` in the header and starts the
            payload with the uvarint string-table id of its codec id.
        """

    @abstractmethod
    def decode_block(self, state: Any, store: Store, payload: bytes, flags: int,
                     emit: VcEmit) -> None:
        """Decode one block and emit its changes.

        Args:
            state: Reader state. The Python reader passes ``None``.
            store: The reader's :class:`Store`.
            payload: The block payload, without the 10-byte block header. For
                a ``FLAG_STRUCT_CODEC`` block it still starts with the codec-id
                prefix.
            flags: The block's flags byte.
            emit: Called once per change with a :class:`~trlog.VcChange`.
        """


class TxnCodec(_CodecBase):
    """Base class for transaction-record codecs.

    A record codec converts transaction records to the bytes of one
    ``BLK_TXN_DATA`` block and back. The Python writer always uses
    :class:`CoreRecordCodec` for transaction blocks; registering another
    record codec makes it discoverable but does not route blocks to it.
    """

    @abstractmethod
    def encode_block(self, state: Any, store: Store) -> Tuple[bytes, int, int]:
        """Encode the records accumulated in ``state``.

        Returns:
            ``(block_bytes, start_time, end_time)``, with ``block_bytes`` a
            complete block (header and payload), as for
            :meth:`VcCodec.encode_block`.
        """

    @abstractmethod
    def decode_block(self, state: Any, store: Store, payload: bytes, flags: int,
                     emit: TxnEmit) -> None:
        """Decode one block payload (no header) and emit each record."""


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------

_VC_CODECS:  Dict[str, VcCodec]  = {}
_TXN_CODECS: Dict[str, TxnCodec] = {}


def register_vc_codec(codec: VcCodec) -> VcCodec:
    """Register a value-change codec instance under ``codec.codec_id``.

    Registering a second codec with the same id replaces the first.

    Args:
        codec: The codec instance. Its ``codec_id`` must be set.

    Returns:
        ``codec``.

    Raises:
        ValueError: If ``codec.codec_id`` is not a canonical id.
    """
    canonical_codec_id(codec.codec_id)
    _VC_CODECS[codec.codec_id] = codec
    return codec


def register_txn_codec(codec: TxnCodec) -> TxnCodec:
    """Register a transaction-record codec instance under ``codec.codec_id``.

    Same rules as :func:`register_vc_codec`.
    """
    canonical_codec_id(codec.codec_id)
    _TXN_CODECS[codec.codec_id] = codec
    return codec


def lookup_vc_codec(codec_id: str) -> Optional[VcCodec]:
    """Return the registered value-change codec for ``codec_id``, or ``None``."""
    return _VC_CODECS.get(codec_id)


def lookup_txn_codec(codec_id: str) -> Optional[TxnCodec]:
    """Return the registered record codec for ``codec_id``, or ``None``."""
    return _TXN_CODECS.get(codec_id)


def registered_vc_codecs() -> List[str]:
    """Return the ids of all registered value-change codecs, sorted."""
    return sorted(_VC_CODECS)


def registered_txn_codecs() -> List[str]:
    """Return the ids of all registered record codecs, sorted."""
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


#: Class decorator that registers a :class:`VcCodec` subclass.
#:
#: ``@vc_codec(codec_id, *, version=1, caps=Capability.NONE)`` validates
#: ``codec_id``, sets the ``codec_id``, ``version`` and ``caps`` class
#: attributes, creates one instance with no arguments and registers it with
#: :func:`register_vc_codec`. It returns the class unchanged. Raises
#: ``ValueError`` for an invalid id and ``TypeError`` if the class does not
#: subclass :class:`VcCodec`. The Python counterpart of
#: ``trl_register_vc_codec``.
vc_codec  = _make_decorator(register_vc_codec, VcCodec)
#: Class decorator that registers a :class:`TxnCodec` subclass. Same
#: signature and behavior as :data:`vc_codec`.
txn_codec = _make_decorator(register_txn_codec, TxnCodec)


# ---------------------------------------------------------------------------
# Built-in codecs. The default ones wrap the VcDataBlock / TxnDataBlock
# encoders, so their output is unchanged by the codec layer.
# ---------------------------------------------------------------------------

class _VcWriterState:
    """Per-stream writer state for the built-in value-change codecs: holds the
    block currently being filled."""

    __slots__ = ("blk",)

    def __init__(self, blk) -> None:
        self.blk = blk


@vc_codec(CORE_VALUECHANGE, version=1, caps=Capability.RANDOM_ACCESS | Capability.LOSSLESS)
class CoreValueChangeCodec(VcCodec):
    """Default value-change codec (:data:`CORE_VALUECHANGE`).

    Wraps ``VcDataBlock``: a per-block time table, per-variable initial values
    and per-variable wave entries (XOR-delta + RLE for multi-bit 2-state
    signals). Blocks do not set ``FLAG_STRUCT_CODEC``. The wire layout is in
    :doc:`/guide/format`.
    """

    def open_writer(self, store: Store, stream_id: int, params: bytes) -> _VcWriterState:
        # The VcDataBlock is created per block in new_block().
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
            use_zstd=w._use_zstd,
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
    """Byte-plane split value-change codec (:data:`CORE_BYTESPLIT`).

    Opt-in codec for fixed-width values: ``SE_REAL`` and ``SE_2STATE`` signals
    of any width. It stores the values of each variable as byte planes (all
    byte 0s, then all byte 1s, ...) and zlib-compresses the block, which
    suits slowly varying reals and wide counters. Blocks set
    ``FLAG_STRUCT_CODEC``, so a reader without this codec skips them. Select
    it per block with ``writer.begin_vc_block(t, codec=CORE_BYTESPLIT)``.
    See :class:`trlog._bytesplit.BytesplitBlock` for the payload layout.
    """

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
    """Derived (virtual) signal codec (:data:`CORE_DERIVED`).

    A derived signal stores an expression over other signals instead of
    per-change data. ``TrlWriter.add_derived_signal`` writes the definition
    to a ``BLK_AUX`` block and ``TrlReader.read_derived`` evaluates it from
    the input signals. This codec is registered only so the id can be looked
    up and its capabilities read; :meth:`encode_block` and
    :meth:`decode_block` raise ``NotImplementedError``.
    """

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
    """Default transaction-record codec (:data:`CORE_RECORD`).

    Wraps ``TxnDataBlock``: row or column layout with delta-encoded
    transaction ids and times. The wire layout is in :doc:`/guide/format`.
    """

    def open_writer(self, store: Store, stream_id: int, params: bytes) -> _TxnWriterState:
        return _TxnWriterState(None)

    def new_block(self, store: Store, state: _TxnWriterState, start_time: int):
        from ._txn_data import TxnDataBlock
        w = store.writer
        blk = TxnDataBlock(start_time=start_time, compress=w._compress,
                           use_zstd=w._use_zstd,
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
