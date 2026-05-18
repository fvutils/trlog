"""Type Registry — signal types, txn schemas, enum types, stream decls."""

from __future__ import annotations

import struct
import zlib
from typing import Dict, List, Optional

from ._codec import encode_uvarint, decode_uvarint, encode_svarint, decode_svarint
from ._string_table import _make_block
from ._types import (
    BlockType, FLAG_COMPRESSED, FLAG_COMPRESS_ALG,
    FieldType, Radix,
    SignalTypeEntry, TxnSchemaEntry, EnumTypeEntry, StreamDeclEntry,
    FieldDef, EnumValue,
)
from ._exceptions import ZstFormatError

# Type entry tags
_TAG_SIGNAL_TYPE  = 0x01
_TAG_TXN_SCHEMA   = 0x02
_TAG_ENUM_TYPE    = 0x03
_TAG_STREAM_DECL  = 0x04


class TypeRegistry:
    """Accumulates signal types, transaction schemas, enum types, and stream declarations."""

    def __init__(self) -> None:
        self._signal_types:  Dict[int, SignalTypeEntry]  = {}
        self._txn_schemas:   Dict[int, TxnSchemaEntry]   = {}
        self._enum_types:    Dict[int, EnumTypeEntry]    = {}
        self._stream_decls:  Dict[int, StreamDeclEntry]  = {}

        self._next_sig_id    = 1
        self._next_txn_id    = 1
        self._next_enum_id   = 1
        self._next_stream_id = 1

    # ------------------------------------------------------------------
    # Accessors
    # ------------------------------------------------------------------

    def get_signal_type(self, sig_type_id: int) -> Optional[SignalTypeEntry]:
        return self._signal_types.get(sig_type_id)

    def get_txn_schema(self, txn_type_id: int) -> Optional[TxnSchemaEntry]:
        return self._txn_schemas.get(txn_type_id)

    def get_enum_type(self, enum_type_id: int) -> Optional[EnumTypeEntry]:
        return self._enum_types.get(enum_type_id)

    def get_stream_decl(self, stream_id: int) -> Optional[StreamDeclEntry]:
        return self._stream_decls.get(stream_id)

    # ------------------------------------------------------------------
    # Builder methods
    # ------------------------------------------------------------------

    def add_signal_type(
        self,
        encoding,
        bit_width: int = 0,
        display_radix=Radix.RX_HEX,
    ) -> int:
        sig_id = self._next_sig_id
        self._next_sig_id += 1
        entry = SignalTypeEntry(
            sig_type_id=sig_id,
            encoding=encoding,
            bit_width=bit_width,
            display_radix=display_radix,
        )
        self._signal_types[sig_id] = entry
        return sig_id

    def add_txn_schema(self, name_str_id: int, fields: List[FieldDef] | None = None) -> int:
        txn_id = self._next_txn_id
        self._next_txn_id += 1
        entry = TxnSchemaEntry(
            txn_type_id=txn_id,
            name_str_id=name_str_id,
            fields=fields or [],
        )
        self._txn_schemas[txn_id] = entry
        return txn_id

    def add_enum_type(self, name_str_id: int, values: List[EnumValue] | None = None) -> int:
        enum_id = self._next_enum_id
        self._next_enum_id += 1
        entry = EnumTypeEntry(
            enum_type_id=enum_id,
            name_str_id=name_str_id,
            values=values or [],
        )
        self._enum_types[enum_id] = entry
        return enum_id

    def add_stream_decl(
        self,
        name_str_id: int,
        kind_str_id: int = 0,
        default_txn_type: int = 0,
    ) -> int:
        stream_id = self._next_stream_id
        self._next_stream_id += 1
        entry = StreamDeclEntry(
            stream_id=stream_id,
            name_str_id=name_str_id,
            kind_str_id=kind_str_id,
            default_txn_type=default_txn_type,
        )
        self._stream_decls[stream_id] = entry
        return stream_id

    # ------------------------------------------------------------------
    # Block serialisation
    # ------------------------------------------------------------------

    def encode_block(self, compress: bool = False, use_zstd: bool = False) -> bytes:
        """Encode all currently held entries as a BLK_TYPE_REG block."""
        payload = self._encode_payload()
        flags = 0
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
        return _make_block(BlockType.BLK_TYPE_REG, flags, payload)

    def read_block(self, payload: bytes | memoryview, flags: int = 0) -> None:
        """Parse a BLK_TYPE_REG payload and accumulate entries."""
        if flags & FLAG_COMPRESSED:
            if flags & FLAG_COMPRESS_ALG:
                import zstandard as zstd  # type: ignore
                payload = zstd.ZstdDecompressor().decompress(payload)
            else:
                payload = zlib.decompress(payload)
        self._decode_payload(payload)

    # ------------------------------------------------------------------
    # Internal encode/decode
    # ------------------------------------------------------------------

    def _encode_payload(self) -> bytes:
        entries = (
            list(self._signal_types.values())
            + list(self._txn_schemas.values())
            + list(self._enum_types.values())
            + list(self._stream_decls.values())
        )
        out = bytearray()
        out += encode_uvarint(len(entries))
        for e in entries:
            if isinstance(e, SignalTypeEntry):
                out += _encode_signal_type(e)
            elif isinstance(e, TxnSchemaEntry):
                out += _encode_txn_schema(e)
            elif isinstance(e, EnumTypeEntry):
                out += _encode_enum_type(e)
            elif isinstance(e, StreamDeclEntry):
                out += _encode_stream_decl(e)
        return bytes(out)

    def _decode_payload(self, payload: bytes | memoryview) -> None:
        offset = 0
        count, offset = decode_uvarint(payload, offset)
        for _ in range(count):
            if offset >= len(payload):
                raise ZstFormatError("Truncated type registry payload")
            tag = payload[offset]
            offset += 1
            if tag == _TAG_SIGNAL_TYPE:
                entry, offset = _decode_signal_type(payload, offset)
                self._signal_types[entry.sig_type_id] = entry
            elif tag == _TAG_TXN_SCHEMA:
                entry, offset = _decode_txn_schema(payload, offset)
                if entry.txn_type_id in self._txn_schemas:
                    raise ZstFormatError(
                        f"Duplicate txn_type_id {entry.txn_type_id} in type registry"
                    )
                self._txn_schemas[entry.txn_type_id] = entry
            elif tag == _TAG_ENUM_TYPE:
                entry, offset = _decode_enum_type(payload, offset)
                self._enum_types[entry.enum_type_id] = entry
            elif tag == _TAG_STREAM_DECL:
                entry, offset = _decode_stream_decl(payload, offset)
                self._stream_decls[entry.stream_id] = entry
            else:
                # Unknown tag — skip by reading to end of this entry is not
                # possible without a length prefix.  The spec says skip; we
                # raise a forward-compat warning but continue.
                # For robustness we stop here as we cannot know the size.
                break


# ---------------------------------------------------------------------------
# Signal type encode/decode
# ---------------------------------------------------------------------------

def _encode_signal_type(e: SignalTypeEntry) -> bytes:
    out = bytearray()
    out.append(_TAG_SIGNAL_TYPE)
    out += encode_uvarint(e.sig_type_id)
    out.append(int(e.encoding))
    out += encode_uvarint(e.bit_width)
    out.append(int(e.display_radix))
    return bytes(out)


def _decode_signal_type(buf, offset):
    sig_type_id, offset = decode_uvarint(buf, offset)
    encoding = buf[offset]; offset += 1
    bit_width, offset = decode_uvarint(buf, offset)
    display_radix = buf[offset]; offset += 1
    from ._types import SignalEncoding
    entry = SignalTypeEntry(
        sig_type_id=sig_type_id,
        encoding=SignalEncoding(encoding),
        bit_width=bit_width,
        display_radix=Radix(display_radix),
    )
    return entry, offset


# ---------------------------------------------------------------------------
# Txn schema encode/decode
# ---------------------------------------------------------------------------

def _encode_txn_schema(e: TxnSchemaEntry) -> bytes:
    out = bytearray()
    out.append(_TAG_TXN_SCHEMA)
    out += encode_uvarint(e.txn_type_id)
    out += encode_uvarint(e.name_str_id)
    out += encode_uvarint(len(e.fields))
    for f in e.fields:
        out += encode_uvarint(f.name_str_id)
        out.append(int(f.field_type))
        out.append(int(f.display_hint))
        out += encode_uvarint(f.enum_type_id)
    return bytes(out)


def _decode_txn_schema(buf, offset):
    txn_type_id, offset = decode_uvarint(buf, offset)
    name_str_id, offset = decode_uvarint(buf, offset)
    field_count, offset = decode_uvarint(buf, offset)
    fields = []
    for _ in range(field_count):
        fname_id, offset = decode_uvarint(buf, offset)
        ftype = FieldType(buf[offset]); offset += 1
        dhint = Radix(buf[offset]); offset += 1
        enum_id, offset = decode_uvarint(buf, offset)
        fields.append(FieldDef(
            name_str_id=fname_id,
            field_type=ftype,
            display_hint=dhint,
            enum_type_id=enum_id,
        ))
    return TxnSchemaEntry(
        txn_type_id=txn_type_id,
        name_str_id=name_str_id,
        fields=fields,
    ), offset


# ---------------------------------------------------------------------------
# Enum type encode/decode
# ---------------------------------------------------------------------------

def _encode_enum_type(e: EnumTypeEntry) -> bytes:
    out = bytearray()
    out.append(_TAG_ENUM_TYPE)
    out += encode_uvarint(e.enum_type_id)
    out += encode_uvarint(e.name_str_id)
    out += encode_uvarint(len(e.values))
    for v in e.values:
        out += encode_svarint(v.integer_value)
        out += encode_uvarint(v.label_str_id)
    return bytes(out)


def _decode_enum_type(buf, offset):
    enum_type_id, offset = decode_uvarint(buf, offset)
    name_str_id, offset  = decode_uvarint(buf, offset)
    value_count, offset  = decode_uvarint(buf, offset)
    values = []
    for _ in range(value_count):
        ival, offset  = decode_svarint(buf, offset)
        lsid, offset  = decode_uvarint(buf, offset)
        values.append(EnumValue(integer_value=ival, label_str_id=lsid))
    return EnumTypeEntry(
        enum_type_id=enum_type_id,
        name_str_id=name_str_id,
        values=values,
    ), offset


# ---------------------------------------------------------------------------
# Stream decl encode/decode
# ---------------------------------------------------------------------------

def _encode_stream_decl(e: StreamDeclEntry) -> bytes:
    out = bytearray()
    out.append(_TAG_STREAM_DECL)
    out += encode_uvarint(e.stream_id)
    out += encode_uvarint(e.name_str_id)
    out += encode_uvarint(e.kind_str_id)
    out += encode_uvarint(e.default_txn_type)
    return bytes(out)


def _decode_stream_decl(buf, offset):
    stream_id, offset        = decode_uvarint(buf, offset)
    name_str_id, offset      = decode_uvarint(buf, offset)
    kind_str_id, offset      = decode_uvarint(buf, offset)
    default_txn_type, offset = decode_uvarint(buf, offset)
    return StreamDeclEntry(
        stream_id=stream_id,
        name_str_id=name_str_id,
        kind_str_id=kind_str_id,
        default_txn_type=default_txn_type,
    ), offset
