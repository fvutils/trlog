"""Transaction Data Block — encode/decode BLK_TXN_DATA.

Stores transaction records of various kinds (TR_FULL, TR_BEGIN, TR_ATTR,
TR_END, TR_LINK) over a time window.  The streaming model is supported:
a transaction opened in one block may be closed in a later block.
"""

from __future__ import annotations

import struct
import zlib
from typing import Any, Dict, List, Optional, Tuple

from ._codec import encode_uvarint, decode_uvarint, encode_field, decode_field
from ._string_table import _make_block
from ._types import (
    BlockType, FLAG_COMPRESSED, FLAG_COMPRESS_ALG,
    FieldType, TxnRecordTag, LinkType,
    TxnFull, TxnBegin, TxnAttrRecord, TxnEnd, TxnLink, TxnAttr, TxnMeta,
)

# Field-type mapping for txn schemas.
# The block stores a list of {field_idx, value} per record; the schema tells
# us how to encode each field.  The caller must supply a lookup function or
# the schema field list directly.
FieldList = List[FieldType]   # indexed by field_idx


class TxnDataBlock:
    """Collects and encodes transaction records as a ``BLK_TXN_DATA`` block."""

    def __init__(
        self,
        start_time: int = 0,
        compress: bool = True,
        use_zstd: bool = False,
    ) -> None:
        self.start_time = start_time
        self.end_time   = start_time
        self._compress  = compress
        self._use_zstd  = use_zstd
        self._records: List[tuple] = []   # (tag, data...)
        # Schema registry: txn_type_id → [FieldType, ...]
        self._schemas: Dict[int, FieldList] = {}

    def set_schema(self, txn_type_id: int, field_types: FieldList) -> None:
        """Register the field types for *txn_type_id*."""
        self._schemas[txn_type_id] = field_types

    # ------------------------------------------------------------------
    # Builder API
    # ------------------------------------------------------------------

    def write_full(
        self,
        stream_inst_id: int,
        txn_type_id: int,
        txn_id: int,
        start: int,
        end: int,
        parent: int,
        attrs: List[TxnAttr],
    ) -> None:
        """Write a complete TR_FULL record."""
        if end > self.end_time:
            self.end_time = end
        self._records.append((TxnRecordTag.TR_FULL,
                               stream_inst_id, txn_type_id, txn_id,
                               start, end, parent, attrs))

    def write_begin(
        self,
        stream_inst_id: int,
        txn_type_id: int,
        txn_id: int,
        start: int,
        parent: int,
    ) -> None:
        """Write a TR_BEGIN record."""
        self._records.append((TxnRecordTag.TR_BEGIN,
                               stream_inst_id, txn_type_id, txn_id, start, parent))

    def write_attr(self, txn_id: int, attrs: List[TxnAttr]) -> None:
        """Write a TR_ATTR record."""
        self._records.append((TxnRecordTag.TR_ATTR, txn_id, attrs))

    def write_end(self, txn_id: int, end_time: int) -> None:
        """Write a TR_END record."""
        if end_time > self.end_time:
            self.end_time = end_time
        self._records.append((TxnRecordTag.TR_END, txn_id, end_time))

    def write_link(
        self,
        link_type: LinkType,
        src: int,
        tgt: int,
        label_id: int = 0,
    ) -> None:
        """Write a TR_LINK record."""
        self._records.append((TxnRecordTag.TR_LINK, link_type, src, tgt, label_id))

    def write_meta(self, txn_id: int, key_str_id: int, value_str_id: int) -> None:
        """Write a TR_META record attaching a string key/value pair to a transaction.

        May be called any time after the transaction is created, even after
        TR_END.  Multiple records with the same key are allowed; for map-style
        access the last value wins.
        """
        self._records.append((TxnRecordTag.TR_META, txn_id, key_str_id, value_str_id))

    # ------------------------------------------------------------------
    # Block serialisation
    # ------------------------------------------------------------------

    def encode_block(self) -> bytes:
        payload = self._encode_payload()
        flags = 0
        if self._compress:
            if self._use_zstd:
                try:
                    import zstandard as zstd  # type: ignore
                    payload = zstd.ZstdCompressor().compress(payload)
                    flags |= FLAG_COMPRESSED | FLAG_COMPRESS_ALG
                except ImportError:
                    raise RuntimeError("zstandard is not installed")
            else:
                payload = zlib.compress(payload)
                flags |= FLAG_COMPRESSED
        return _make_block(BlockType.BLK_TXN_DATA, flags, payload)

    def read_block(
        self,
        payload: bytes | memoryview,
        flags: int = 0,
        schemas: Optional[Dict[int, FieldList]] = None,
    ) -> List[object]:
        """Decode a ``BLK_TXN_DATA`` payload.

        Returns a list of typed record objects (TxnFull, TxnBegin, etc.).
        """
        if schemas is not None:
            self._schemas = schemas
        if flags & FLAG_COMPRESSED:
            if flags & FLAG_COMPRESS_ALG:
                import zstandard as zstd  # type: ignore
                payload = zstd.ZstdDecompressor().decompress(payload)
            else:
                payload = zlib.decompress(payload)
        return self._decode_payload(bytes(payload))

    # ------------------------------------------------------------------
    # Internal encode
    # ------------------------------------------------------------------

    def _encode_payload(self) -> bytes:
        out = bytearray()
        out += struct.pack('<QQ', self.start_time, self.end_time)
        out += encode_uvarint(len(self._records))
        for rec in self._records:
            out += self._encode_record(rec)
        return bytes(out)

    def _encode_record(self, rec: tuple) -> bytes:
        tag = rec[0]
        out = bytearray()
        out.append(int(tag))

        if tag == TxnRecordTag.TR_FULL:
            _, stream_inst_id, txn_type_id, txn_id, start, end, parent, attrs = rec
            out += encode_uvarint(stream_inst_id)
            out += encode_uvarint(txn_type_id)
            out += struct.pack('<QQQ', txn_id, start, end)
            out += struct.pack('<Q', parent)
            out += self._encode_attrs(txn_type_id, attrs)

        elif tag == TxnRecordTag.TR_BEGIN:
            _, stream_inst_id, txn_type_id, txn_id, start, parent = rec
            out += encode_uvarint(stream_inst_id)
            out += encode_uvarint(txn_type_id)
            out += struct.pack('<QQQ', txn_id, start, parent)

        elif tag == TxnRecordTag.TR_ATTR:
            _, txn_id, attrs = rec
            out += struct.pack('<Q', txn_id)
            # For TR_ATTR we don't know txn_type_id here directly.
            # The schema is looked up via the txn_id in the reader.
            # Encode attrs as (field_idx, raw_bytes) with a placeholder
            # approach: encode each attr with a type tag.
            out += encode_uvarint(len(attrs))
            for attr in attrs:
                out += encode_uvarint(attr.field_idx)
                out += self._encode_attr_value_raw(attr)

        elif tag == TxnRecordTag.TR_END:
            _, txn_id, end_time = rec
            out += struct.pack('<QQ', txn_id, end_time)

        elif tag == TxnRecordTag.TR_LINK:
            _, link_type, src, tgt, label_id = rec
            out.append(int(link_type))
            out += struct.pack('<QQ', src, tgt)
            out += encode_uvarint(label_id)

        elif tag == TxnRecordTag.TR_META:
            _, txn_id, key_str_id, value_str_id = rec
            out += struct.pack('<Q', txn_id)
            out += encode_uvarint(key_str_id)
            out += encode_uvarint(value_str_id)

        return bytes(out)

    def _encode_attrs(self, txn_type_id: int, attrs: List[TxnAttr]) -> bytes:
        out = bytearray()
        out += encode_uvarint(len(attrs))
        schema = self._schemas.get(txn_type_id, [])
        for attr in attrs:
            out += encode_uvarint(attr.field_idx)
            ft = schema[attr.field_idx] if attr.field_idx < len(schema) else FieldType.FT_U64
            out += encode_field(ft, attr.value)
        return bytes(out)

    def _encode_attr_value_raw(self, attr: TxnAttr) -> bytes:
        """Encode an attr value with an inline field_type byte (for TR_ATTR)."""
        # We embed the field type so the decoder can reconstruct without schema lookup
        # by txn_id (which would require tracking open txns).
        # Format: field_type_byte + encoded_value
        ft = attr._field_type if hasattr(attr, '_field_type') else FieldType.FT_U64
        return bytes([int(ft)]) + encode_field(ft, attr.value)

    # ------------------------------------------------------------------
    # Internal decode
    # ------------------------------------------------------------------

    def _decode_payload(self, data: bytes) -> List[object]:
        offset = 0
        start_time, end_time = struct.unpack_from('<QQ', data, offset)
        self.start_time = start_time
        self.end_time   = end_time
        offset += 16

        count, offset = decode_uvarint(data, offset)
        records = []
        for _ in range(count):
            rec, offset = self._decode_record(data, offset)
            if rec is not None:
                records.append(rec)
        return records

    def _decode_record(self, data: bytes, offset: int):
        tag_byte = data[offset]; offset += 1
        try:
            tag = TxnRecordTag(tag_byte)
        except ValueError:
            return None, offset

        if tag == TxnRecordTag.TR_FULL:
            stream_inst_id, offset = decode_uvarint(data, offset)
            txn_type_id,    offset = decode_uvarint(data, offset)
            txn_id, start, end = struct.unpack_from('<QQQ', data, offset); offset += 24
            parent, = struct.unpack_from('<Q', data, offset); offset += 8
            attrs, offset = self._decode_attrs(txn_type_id, data, offset)
            return TxnFull(
                stream_inst_id=stream_inst_id,
                txn_type_id=txn_type_id,
                txn_id=txn_id,
                start_time=start,
                end_time=end,
                parent_txn_id=parent,
                attrs=attrs,
            ), offset

        elif tag == TxnRecordTag.TR_BEGIN:
            stream_inst_id, offset = decode_uvarint(data, offset)
            txn_type_id,    offset = decode_uvarint(data, offset)
            txn_id, start, parent = struct.unpack_from('<QQQ', data, offset); offset += 24
            return TxnBegin(
                stream_inst_id=stream_inst_id,
                txn_type_id=txn_type_id,
                txn_id=txn_id,
                start_time=start,
                parent_txn_id=parent,
            ), offset

        elif tag == TxnRecordTag.TR_ATTR:
            txn_id, = struct.unpack_from('<Q', data, offset); offset += 8
            count, offset = decode_uvarint(data, offset)
            attrs = []
            for _ in range(count):
                field_idx, offset = decode_uvarint(data, offset)
                ft = FieldType(data[offset]); offset += 1
                val, offset = decode_field(ft, data, offset)
                attrs.append(TxnAttr(field_idx=field_idx, value=val))
            return TxnAttrRecord(txn_id=txn_id, attrs=attrs), offset

        elif tag == TxnRecordTag.TR_END:
            txn_id, end_time = struct.unpack_from('<QQ', data, offset); offset += 16
            return TxnEnd(txn_id=txn_id, end_time=end_time), offset

        elif tag == TxnRecordTag.TR_LINK:
            link_type = LinkType(data[offset]); offset += 1
            src, tgt = struct.unpack_from('<QQ', data, offset); offset += 16
            label_id, offset = decode_uvarint(data, offset)
            return TxnLink(
                link_type=link_type,
                source_txn_id=src,
                target_txn_id=tgt,
                label_str_id=label_id,
            ), offset

        elif tag == TxnRecordTag.TR_META:
            txn_id, = struct.unpack_from('<Q', data, offset); offset += 8
            key_str_id,   offset = decode_uvarint(data, offset)
            value_str_id, offset = decode_uvarint(data, offset)
            return TxnMeta(
                txn_id=txn_id,
                key_str_id=key_str_id,
                value_str_id=value_str_id,
            ), offset

        return None, offset

    def _decode_attrs(self, txn_type_id: int, data: bytes, offset: int):
        schema = self._schemas.get(txn_type_id, [])
        count, offset = decode_uvarint(data, offset)
        attrs = []
        for _ in range(count):
            field_idx, offset = decode_uvarint(data, offset)
            ft = schema[field_idx] if field_idx < len(schema) else FieldType.FT_U64
            val, offset = decode_field(ft, data, offset)
            attrs.append(TxnAttr(field_idx=field_idx, value=val))
        return attrs, offset
