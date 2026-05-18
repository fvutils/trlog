"""String Table — bidirectional str↔id mapping with block encode/decode."""

from __future__ import annotations

import zlib
from typing import Dict, List, Optional

from ._codec import encode_uvarint, decode_uvarint
from ._types import BlockType, FLAG_COMPRESSED, FLAG_COMPRESS_ALG, BLOCK_HEADER_SIZE


class StringTable:
    """Bidirectional mapping between strings and monotonically-assigned IDs.

    ID 0 is reserved to mean "absent / null".  The first interned string gets ID 1.
    """

    def __init__(self) -> None:
        self._str_to_id: Dict[str, int] = {}
        self._id_to_str: List[str] = [""]   # index 0 = reserved null

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def intern(self, s: str) -> int:
        """Return the ID for *s*, inserting it if not already present."""
        if s in self._str_to_id:
            return self._str_to_id[s]
        new_id = len(self._id_to_str)
        self._str_to_id[s] = new_id
        self._id_to_str.append(s)
        return new_id

    def lookup(self, str_id: int) -> str:
        """Return the string for *str_id*.  Raises ``KeyError`` for unknown IDs."""
        if str_id < 0 or str_id >= len(self._id_to_str):
            raise KeyError(f"Unknown string ID {str_id}")
        return self._id_to_str[str_id]

    def __len__(self) -> int:
        # Does not count the null slot
        return len(self._id_to_str) - 1

    # ------------------------------------------------------------------
    # Block serialisation
    # ------------------------------------------------------------------

    def encode_block(self, compress: bool = False, use_zstd: bool = False) -> bytes:
        """Encode all currently interned strings as a BLK_STR_TABLE block."""
        entries = self._id_to_str[1:]   # skip null slot
        payload = _encode_entries(entries)

        flags: int = 0
        if compress:
            if use_zstd:
                try:
                    import zstandard as zstd  # type: ignore
                    payload = zstd.ZstdCompressor().compress(payload)
                    flags |= FLAG_COMPRESSED | FLAG_COMPRESS_ALG
                except ImportError:
                    raise RuntimeError("zstandard is not installed")
            else:
                payload = zlib.compress(payload)
                flags |= FLAG_COMPRESSED

        return _make_block(BlockType.BLK_STR_TABLE, flags, payload)

    def read_block(self, payload: bytes | memoryview, flags: int = 0) -> None:
        """Append entries from a BLK_STR_TABLE payload into this table."""
        if flags & FLAG_COMPRESSED:
            if flags & FLAG_COMPRESS_ALG:
                import zstandard as zstd  # type: ignore
                payload = zstd.ZstdDecompressor().decompress(payload)
            else:
                payload = zlib.decompress(payload)
        _decode_entries(payload, self._id_to_str, self._str_to_id)


# ---------------------------------------------------------------------------
# Module-level helpers
# ---------------------------------------------------------------------------

def _encode_entries(entries: List[str]) -> bytes:
    out = bytearray()
    out += encode_uvarint(len(entries))
    for s in entries:
        encoded = s.encode('utf-8')
        out += encode_uvarint(len(encoded))
        out += encoded
    return bytes(out)


def _decode_entries(
    payload: bytes | memoryview,
    id_to_str: List[str],
    str_to_id: Dict[str, int],
) -> None:
    offset = 0
    count, offset = decode_uvarint(payload, offset)
    for _ in range(count):
        length, offset = decode_uvarint(payload, offset)
        s = bytes(payload[offset:offset + length]).decode('utf-8')
        offset += length
        new_id = len(id_to_str)
        id_to_str.append(s)
        str_to_id[s] = new_id


def _make_block(block_type: BlockType, flags: int, payload: bytes) -> bytes:
    """Wrap *payload* in a standard 10-byte block header."""
    block_length = BLOCK_HEADER_SIZE + len(payload)
    import struct
    header = struct.pack('<BBQ', int(block_type), flags, block_length)
    return header + payload
