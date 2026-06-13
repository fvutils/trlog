"""Opaque keyed (non-temporal) metadata blocks — BLK_AUX (impl-plan §2.4).

Opaque bulk metadata is *just a non-temporal stream*: the same self-delimiting
block framing and byte compression as value-change / transaction data, but
addressed by **key** instead of time and recorded in a parallel **key index**
("one block pool, two indexes", design §4.0). The payload bytes are produced by
a codec and are opaque to the core — a reader lacking the codec enumerates aux
blocks by ``(owner_stream_id, key)`` and skips them by length, exactly like any
unknown block (§2.8), with no new skip logic.
"""

from __future__ import annotations

import zlib

from ._string_table import _make_block
from ._types import (
    BlockType, FLAG_COMPRESSED, FLAG_COMPRESS_ALG,
)


class AuxBlock:
    """Encode/decode one opaque keyed metadata block (``BLK_AUX``).

    The block carries raw codec-produced bytes; the core only frames and
    (optionally) byte-compresses them, identically to data blocks.
    """

    @staticmethod
    def encode(payload: bytes, compress: bool = False, use_zstd: bool = False) -> bytes:
        flags = 0
        data = bytes(payload)
        if compress and data:
            if use_zstd:
                import zstandard as zstd  # type: ignore
                data = zstd.ZstdCompressor().compress(data)
                flags |= FLAG_COMPRESSED | FLAG_COMPRESS_ALG
            else:
                data = zlib.compress(data)
                flags |= FLAG_COMPRESSED
        return _make_block(BlockType.BLK_AUX, flags, data)

    @staticmethod
    def decode(payload: bytes | memoryview, flags: int = 0) -> bytes:
        if flags & FLAG_COMPRESSED:
            if flags & FLAG_COMPRESS_ALG:
                import zstandard as zstd  # type: ignore
                return zstd.ZstdDecompressor().decompress(bytes(payload))
            return zlib.decompress(bytes(payload))
        return bytes(payload)
