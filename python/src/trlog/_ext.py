"""ExtBlock — application-specific BLK_EXT block for TRLOG."""

from __future__ import annotations

import struct
from typing import Optional

from ._types import BlockType, BLOCK_HEADER_SIZE


# BLK_EXT payload layout:
#   ext_type:    u32 (4 bytes, big-endian per convention, but we use little-endian)
#   ext_version: u16 LE
#   payload:     u8[...]


class ExtBlock:
    """Wraps an application-defined extension block (BLK_EXT).

    Parameters
    ----------
    ext_type:
        4-byte identifier, e.g. ``b"COVG"``.
    ext_version:
        Version number for the extension (u16).
    payload:
        Raw bytes of the extension data.
    """

    def __init__(
        self,
        ext_type: bytes,
        ext_version: int = 0,
        payload: bytes = b"",
    ) -> None:
        if len(ext_type) != 4:
            raise ValueError(f"ext_type must be exactly 4 bytes, got {len(ext_type)!r}")
        self.ext_type: bytes = bytes(ext_type)
        self.ext_version: int = ext_version
        self.payload: bytes = bytes(payload)

    # ------------------------------------------------------------------
    # Encode
    # ------------------------------------------------------------------

    def encode_block(self) -> bytes:
        """Return the full BLK_EXT byte sequence (header + body)."""
        body = self.ext_type + struct.pack('<H', self.ext_version) + self.payload
        block_length = BLOCK_HEADER_SIZE + len(body)
        header = struct.pack(
            '<BBIQ',
            BlockType.BLK_EXT.value,   # block_type
            0,                          # flags (no compression)
            0,                          # reserved / padding (unused byte)
            block_length,
        )
        # Header is: u8 type, u8 flags, u64 length  (10 bytes total)
        header = bytes([BlockType.BLK_EXT.value, 0]) + struct.pack('<Q', block_length)
        return header + body

    # ------------------------------------------------------------------
    # Decode
    # ------------------------------------------------------------------

    @classmethod
    def read_block(cls, payload: bytes) -> "ExtBlock":
        """Parse a BLK_EXT payload (without the 10-byte block header).

        Parameters
        ----------
        payload:
            Bytes starting at the first byte after the block header.
        """
        if len(payload) < 6:
            raise ValueError(f"ExtBlock payload too short: {len(payload)} bytes")
        ext_type = payload[:4]
        ext_version = struct.unpack_from('<H', payload, 4)[0]
        data = payload[6:]
        return cls(ext_type=ext_type, ext_version=ext_version, payload=data)

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def __repr__(self) -> str:
        return (
            f"ExtBlock(ext_type={self.ext_type!r}, "
            f"ext_version={self.ext_version}, "
            f"payload_len={len(self.payload)})"
        )

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, ExtBlock):
            return NotImplemented
        return (
            self.ext_type == other.ext_type
            and self.ext_version == other.ext_version
            and self.payload == other.payload
        )
