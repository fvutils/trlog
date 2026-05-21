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
from ._codec import encode_svarint, decode_svarint
from ._string_table import _make_block
from ._types import (
    BlockType, FLAG_COMPRESSED, FLAG_COMPRESS_ALG,
    FLAG_TXN_DELTA, FLAG_TXN_COLUMN,
    FieldType, TxnRecordTag, LinkType,
    TxnFull, TxnBegin, TxnAttrRecord, TxnEnd, TxnLink, TxnAttr, TxnMeta,
    TxnColumnId, _FIXED_WIDTH_FTS,
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
        column_layout: str = "auto",
    ) -> None:
        self.start_time = start_time
        self.end_time   = start_time
        self._compress  = compress
        self._use_zstd  = use_zstd
        self._delta_encode = True
        self._column_layout = column_layout
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

    def _should_use_column(self) -> bool:
        """Schema-based heuristic for auto column layout selection."""
        if len(self._records) < 32:
            return False
        # Collect all txn_type_ids referenced by records that carry schemas
        for rec in self._records:
            tag = rec[0]
            if tag in (TxnRecordTag.TR_FULL, TxnRecordTag.TR_BEGIN):
                txn_type_id = rec[2]
                schema = self._schemas.get(txn_type_id)
                if schema is None:
                    continue
                for ft in schema:
                    if ft not in _FIXED_WIDTH_FTS:
                        return False
        return True

    def encode_block(self) -> bytes:
        delta = self._delta_encode
        flags = FLAG_TXN_DELTA if delta else 0

        # Decide layout
        layout = self._column_layout
        use_column = False
        if layout == "column":
            use_column = True
        elif layout == "row":
            use_column = False
        elif layout == "auto":
            use_column = self._should_use_column()
        elif layout == "adaptive":
            # Encode both, pick smaller after compression
            row_payload = self._encode_payload(delta=delta)
            col_payload = self._encode_payload_column(delta=delta)
            row_compressed = self._compress_payload(row_payload)
            col_compressed = self._compress_payload(col_payload)
            if len(col_compressed) < len(row_compressed):
                use_column = True
                payload = col_payload
            else:
                payload = row_payload

        if layout != "adaptive":
            if use_column:
                payload = self._encode_payload_column(delta=delta)
            else:
                payload = self._encode_payload(delta=delta)

        if use_column:
            flags |= FLAG_TXN_COLUMN

        if self._compress:
            payload = self._compress_payload(payload)
            if self._use_zstd:
                flags |= FLAG_COMPRESSED | FLAG_COMPRESS_ALG
            else:
                flags |= FLAG_COMPRESSED
        return _make_block(BlockType.BLK_TXN_DATA, flags, payload)

    def _compress_payload(self, payload: bytes) -> bytes:
        """Compress payload with the configured algorithm."""
        if self._use_zstd:
            try:
                import zstandard as zstd  # type: ignore
                return zstd.ZstdCompressor().compress(payload)
            except ImportError:
                raise RuntimeError("zstandard is not installed")
        else:
            return zlib.compress(payload)

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
            flags &= ~(FLAG_COMPRESSED | FLAG_COMPRESS_ALG)
        if flags & FLAG_TXN_COLUMN:
            return self._decode_payload_column(bytes(payload), flags)
        return self._decode_payload(bytes(payload), flags)

    # ------------------------------------------------------------------
    # Internal encode
    # ------------------------------------------------------------------

    def _encode_payload(self, delta: bool = False) -> bytes:
        out = bytearray()
        out += struct.pack('<QQ', self.start_time, self.end_time)
        out += encode_uvarint(len(self._records))
        # Running state for delta encoding
        prev_txn_id = 0
        prev_time = self.start_time
        for rec in self._records:
            out += self._encode_record(rec, delta, prev_txn_id, prev_time)
            if delta:
                prev_txn_id, prev_time = self._advance_state(rec, prev_txn_id, prev_time)
        return bytes(out)

    @staticmethod
    def _advance_state(rec: tuple, prev_txn_id: int, prev_time: int):
        """Return updated (prev_txn_id, prev_time) after encoding *rec*."""
        tag = rec[0]
        if tag == TxnRecordTag.TR_FULL:
            _, _, _, txn_id, start, end, _, _ = rec
            return txn_id, end
        elif tag == TxnRecordTag.TR_BEGIN:
            _, _, _, txn_id, start, _ = rec
            return txn_id, start
        elif tag == TxnRecordTag.TR_ATTR:
            _, txn_id, _ = rec
            return txn_id, prev_time
        elif tag == TxnRecordTag.TR_END:
            _, txn_id, end_time = rec
            return txn_id, end_time
        elif tag == TxnRecordTag.TR_LINK:
            _, _, src, _, _ = rec
            return src, prev_time
        elif tag == TxnRecordTag.TR_META:
            _, txn_id, _, _ = rec
            return txn_id, prev_time
        return prev_txn_id, prev_time

    def _encode_record(self, rec: tuple, delta: bool = False,
                       prev_txn_id: int = 0, prev_time: int = 0) -> bytes:
        tag = rec[0]
        out = bytearray()
        out.append(int(tag))

        if tag == TxnRecordTag.TR_FULL:
            _, stream_inst_id, txn_type_id, txn_id, start, end, parent, attrs = rec
            out += encode_uvarint(stream_inst_id)
            out += encode_uvarint(txn_type_id)
            if delta:
                out += encode_svarint(txn_id - prev_txn_id)
                out += encode_svarint(start - prev_time)
                out += encode_uvarint(end - start)       # duration (always >= 0)
                out += encode_uvarint(parent)
            else:
                out += struct.pack('<QQQ', txn_id, start, end)
                out += struct.pack('<Q', parent)
            out += self._encode_attrs(txn_type_id, attrs)

        elif tag == TxnRecordTag.TR_BEGIN:
            _, stream_inst_id, txn_type_id, txn_id, start, parent = rec
            out += encode_uvarint(stream_inst_id)
            out += encode_uvarint(txn_type_id)
            if delta:
                out += encode_svarint(txn_id - prev_txn_id)
                out += encode_svarint(start - prev_time)
                out += encode_uvarint(parent)
            else:
                out += struct.pack('<QQQ', txn_id, start, parent)

        elif tag == TxnRecordTag.TR_ATTR:
            _, txn_id, attrs = rec
            if delta:
                out += encode_svarint(txn_id - prev_txn_id)
            else:
                out += struct.pack('<Q', txn_id)
            out += encode_uvarint(len(attrs))
            for attr in attrs:
                out += encode_uvarint(attr.field_idx)
                out += self._encode_attr_value_raw(attr)

        elif tag == TxnRecordTag.TR_END:
            _, txn_id, end_time = rec
            if delta:
                out += encode_svarint(txn_id - prev_txn_id)
                out += encode_svarint(end_time - prev_time)
            else:
                out += struct.pack('<QQ', txn_id, end_time)

        elif tag == TxnRecordTag.TR_LINK:
            _, link_type, src, tgt, label_id = rec
            out.append(int(link_type))
            if delta:
                out += encode_svarint(src - prev_txn_id)
                out += encode_uvarint(tgt)              # absolute — target can be any txn
            else:
                out += struct.pack('<QQ', src, tgt)
            out += encode_uvarint(label_id)

        elif tag == TxnRecordTag.TR_META:
            _, txn_id, key_str_id, value_str_id = rec
            if delta:
                out += encode_svarint(txn_id - prev_txn_id)
            else:
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
    # Column-oriented encode / decode
    # ------------------------------------------------------------------

    def _encode_payload_column(self, delta: bool = False) -> bytes:
        """Encode records using column-oriented layout."""
        C = TxnColumnId

        # Separate records by tag for type-specific columns
        col_tag = bytearray()
        col_stream_inst_id = bytearray()
        col_txn_type_id = bytearray()
        col_txn_id = bytearray()
        col_time = bytearray()
        col_duration = bytearray()
        col_parent = bytearray()
        col_link_type = bytearray()
        col_link_tgt = bytearray()
        col_link_label = bytearray()
        col_meta_key = bytearray()
        col_meta_val = bytearray()
        # Attribute columns: field_idx -> bytearray
        attr_columns: Dict[int, bytearray] = {}

        prev_txn_id = 0
        prev_time = self.start_time

        for rec in self._records:
            tag = rec[0]
            col_tag.append(int(tag))

            if tag == TxnRecordTag.TR_FULL:
                _, stream_inst_id, txn_type_id, txn_id, start, end, parent, attrs = rec
                col_stream_inst_id += encode_uvarint(stream_inst_id)
                col_txn_type_id += encode_uvarint(txn_type_id)
                if delta:
                    col_txn_id += encode_svarint(txn_id - prev_txn_id)
                    col_time += encode_svarint(start - prev_time)
                    col_duration += encode_uvarint(end - start)
                else:
                    col_txn_id += struct.pack('<Q', txn_id)
                    col_time += struct.pack('<Q', start)
                    col_duration += struct.pack('<Q', end - start)
                col_parent += encode_uvarint(parent)
                self._encode_attrs_column(txn_type_id, attrs, attr_columns)
                if delta:
                    prev_txn_id = txn_id
                    prev_time = end

            elif tag == TxnRecordTag.TR_BEGIN:
                _, stream_inst_id, txn_type_id, txn_id, start, parent = rec
                col_stream_inst_id += encode_uvarint(stream_inst_id)
                col_txn_type_id += encode_uvarint(txn_type_id)
                if delta:
                    col_txn_id += encode_svarint(txn_id - prev_txn_id)
                    col_time += encode_svarint(start - prev_time)
                else:
                    col_txn_id += struct.pack('<Q', txn_id)
                    col_time += struct.pack('<Q', start)
                col_parent += encode_uvarint(parent)
                if delta:
                    prev_txn_id = txn_id
                    prev_time = start

            elif tag == TxnRecordTag.TR_ATTR:
                _, txn_id, attrs = rec
                if delta:
                    col_txn_id += encode_svarint(txn_id - prev_txn_id)
                    prev_txn_id = txn_id
                else:
                    col_txn_id += struct.pack('<Q', txn_id)
                # TR_ATTR carries inline field-type bytes; store as length-prefixed
                # blob in a dedicated column (COL_ATTR_DATA = 12).
                attr_blob = bytearray()
                attr_blob += encode_uvarint(len(attrs))
                for attr in attrs:
                    attr_blob += encode_uvarint(attr.field_idx)
                    attr_blob += self._encode_attr_value_raw(attr)
                if not hasattr(self, '_col_attr_data'):
                    self._col_attr_data = bytearray()
                self._col_attr_data += encode_uvarint(len(attr_blob))
                self._col_attr_data += attr_blob

            elif tag == TxnRecordTag.TR_END:
                _, txn_id, end_time = rec
                if delta:
                    col_txn_id += encode_svarint(txn_id - prev_txn_id)
                    col_time += encode_svarint(end_time - prev_time)
                    prev_txn_id = txn_id
                    prev_time = end_time
                else:
                    col_txn_id += struct.pack('<Q', txn_id)
                    col_time += struct.pack('<Q', end_time)

            elif tag == TxnRecordTag.TR_LINK:
                _, link_type, src, tgt, label_id = rec
                col_link_type.append(int(link_type))
                if delta:
                    col_txn_id += encode_svarint(src - prev_txn_id)
                    prev_txn_id = src
                else:
                    col_txn_id += struct.pack('<Q', src)
                col_link_tgt += encode_uvarint(tgt)
                col_link_label += encode_uvarint(label_id)

            elif tag == TxnRecordTag.TR_META:
                _, txn_id, key_str_id, value_str_id = rec
                if delta:
                    col_txn_id += encode_svarint(txn_id - prev_txn_id)
                    prev_txn_id = txn_id
                else:
                    col_txn_id += struct.pack('<Q', txn_id)
                col_meta_key += encode_uvarint(key_str_id)
                col_meta_val += encode_uvarint(value_str_id)

        # COL_ATTR_DATA (12) stores TR_ATTR inline blobs
        col_attr_data_bytes = bytes(getattr(self, '_col_attr_data', b''))
        if hasattr(self, '_col_attr_data'):
            del self._col_attr_data

        # Build the column table
        columns: List[Tuple[int, bytes]] = []
        columns.append((C.COL_TAG, bytes(col_tag)))
        if col_txn_id:
            columns.append((C.COL_TXN_ID_DELTA, bytes(col_txn_id)))
        if col_stream_inst_id:
            columns.append((C.COL_STREAM_INST_ID, bytes(col_stream_inst_id)))
        if col_txn_type_id:
            columns.append((C.COL_TXN_TYPE_ID, bytes(col_txn_type_id)))
        if col_time:
            columns.append((C.COL_TIME_DELTA, bytes(col_time)))
        if col_duration:
            columns.append((C.COL_DURATION, bytes(col_duration)))
        if col_parent:
            columns.append((C.COL_PARENT, bytes(col_parent)))
        if col_link_type:
            columns.append((C.COL_LINK_TYPE, bytes(col_link_type)))
        if col_link_tgt:
            columns.append((C.COL_LINK_TGT, bytes(col_link_tgt)))
        if col_link_label:
            columns.append((C.COL_LINK_LABEL, bytes(col_link_label)))
        if col_meta_key:
            columns.append((C.COL_META_KEY, bytes(col_meta_key)))
        if col_meta_val:
            columns.append((C.COL_META_VAL, bytes(col_meta_val)))
        if col_attr_data_bytes:
            columns.append((12, col_attr_data_bytes))  # COL_ATTR_DATA
        for field_idx in sorted(attr_columns):
            columns.append((C.COL_ATTR_BASE + field_idx, bytes(attr_columns[field_idx])))

        # Serialize
        out = bytearray()
        out += struct.pack('<QQ', self.start_time, self.end_time)
        out += encode_uvarint(len(self._records))
        out += encode_uvarint(len(columns))
        for col_id, col_data in columns:
            out += encode_uvarint(col_id)
            out += encode_uvarint(len(col_data))
            out += col_data
        return bytes(out)

    def _encode_attrs_column(self, txn_type_id: int, attrs: List[TxnAttr],
                             attr_columns: Dict[int, bytearray]) -> None:
        """Encode attribute values into per-field-index columns."""
        schema = self._schemas.get(txn_type_id, [])
        for attr in attrs:
            ft = schema[attr.field_idx] if attr.field_idx < len(schema) else FieldType.FT_U64
            col = attr_columns.setdefault(attr.field_idx, bytearray())
            col += encode_field(ft, attr.value)

    def _decode_payload_column(self, data: bytes, flags: int = 0) -> List[object]:
        """Decode a column-oriented BLK_TXN_DATA payload."""
        C = TxnColumnId
        delta = bool(flags & FLAG_TXN_DELTA)

        offset = 0
        start_time, end_time = struct.unpack_from('<QQ', data, offset)
        self.start_time = start_time
        self.end_time = end_time
        offset += 16

        record_count, offset = decode_uvarint(data, offset)
        column_count, offset = decode_uvarint(data, offset)

        # Read all columns into a dict
        col_data: Dict[int, bytes] = {}
        for _ in range(column_count):
            col_id, offset = decode_uvarint(data, offset)
            byte_length, offset = decode_uvarint(data, offset)
            col_data[col_id] = data[offset:offset + byte_length]
            offset += byte_length

        if record_count == 0:
            return []

        # Read tag column
        tags_raw = col_data.get(C.COL_TAG, b'')
        tags = [TxnRecordTag(b) for b in tags_raw]

        # Prepare column cursors (offset into each column's bytes)
        cur: Dict[int, int] = {col_id: 0 for col_id in col_data}

        def read_uvarint(col_id: int) -> int:
            val, new_off = decode_uvarint(col_data[col_id], cur[col_id])
            cur[col_id] = new_off
            return val

        def read_svarint(col_id: int) -> int:
            val, new_off = decode_svarint(col_data[col_id], cur[col_id])
            cur[col_id] = new_off
            return val

        def read_u64(col_id: int) -> int:
            off = cur[col_id]
            val, = struct.unpack_from('<Q', col_data[col_id], off)
            cur[col_id] = off + 8
            return val

        def read_u8(col_id: int) -> int:
            off = cur[col_id]
            val = col_data[col_id][off]
            cur[col_id] = off + 1
            return val

        records: List[object] = []
        prev_txn_id = 0
        prev_time = start_time

        for tag in tags:
            if tag == TxnRecordTag.TR_FULL:
                stream_inst_id = read_uvarint(C.COL_STREAM_INST_ID)
                txn_type_id = read_uvarint(C.COL_TXN_TYPE_ID)
                if delta:
                    txn_id = prev_txn_id + read_svarint(C.COL_TXN_ID_DELTA)
                    start = prev_time + read_svarint(C.COL_TIME_DELTA)
                    duration = read_uvarint(C.COL_DURATION)
                    end = start + duration
                else:
                    txn_id = read_u64(C.COL_TXN_ID_DELTA)
                    start = read_u64(C.COL_TIME_DELTA)
                    duration = read_u64(C.COL_DURATION)
                    end = start + duration
                parent = read_uvarint(C.COL_PARENT)
                attrs = self._decode_attrs_column(txn_type_id, col_data, cur)
                records.append(TxnFull(
                    stream_inst_id=stream_inst_id,
                    txn_type_id=txn_type_id,
                    txn_id=txn_id,
                    start_time=start,
                    end_time=end,
                    parent_txn_id=parent,
                    attrs=attrs,
                ))
                if delta:
                    prev_txn_id = txn_id
                    prev_time = end

            elif tag == TxnRecordTag.TR_BEGIN:
                stream_inst_id = read_uvarint(C.COL_STREAM_INST_ID)
                txn_type_id = read_uvarint(C.COL_TXN_TYPE_ID)
                if delta:
                    txn_id = prev_txn_id + read_svarint(C.COL_TXN_ID_DELTA)
                    start = prev_time + read_svarint(C.COL_TIME_DELTA)
                else:
                    txn_id = read_u64(C.COL_TXN_ID_DELTA)
                    start = read_u64(C.COL_TIME_DELTA)
                parent = read_uvarint(C.COL_PARENT)
                records.append(TxnBegin(
                    stream_inst_id=stream_inst_id,
                    txn_type_id=txn_type_id,
                    txn_id=txn_id,
                    start_time=start,
                    parent_txn_id=parent,
                ))
                if delta:
                    prev_txn_id = txn_id
                    prev_time = start

            elif tag == TxnRecordTag.TR_ATTR:
                if delta:
                    txn_id = prev_txn_id + read_svarint(C.COL_TXN_ID_DELTA)
                    prev_txn_id = txn_id
                else:
                    txn_id = read_u64(C.COL_TXN_ID_DELTA)
                # TR_ATTR data stored as length-prefixed blob in column 12
                _COL_ATTR_DATA = 12
                blob_len = read_uvarint(_COL_ATTR_DATA)
                blob_start = cur[_COL_ATTR_DATA]
                blob_data = col_data[_COL_ATTR_DATA][blob_start:blob_start + blob_len]
                cur[_COL_ATTR_DATA] = blob_start + blob_len
                # Parse the blob
                boff = 0
                count, boff = decode_uvarint(blob_data, boff)
                attrs = []
                for _ in range(count):
                    field_idx, boff = decode_uvarint(blob_data, boff)
                    ft = FieldType(blob_data[boff]); boff += 1
                    val, boff = decode_field(ft, blob_data, boff)
                    attrs.append(TxnAttr(field_idx=field_idx, value=val))
                records.append(TxnAttrRecord(txn_id=txn_id, attrs=attrs))

            elif tag == TxnRecordTag.TR_END:
                if delta:
                    txn_id = prev_txn_id + read_svarint(C.COL_TXN_ID_DELTA)
                    end_time = prev_time + read_svarint(C.COL_TIME_DELTA)
                    prev_txn_id = txn_id
                    prev_time = end_time
                else:
                    txn_id = read_u64(C.COL_TXN_ID_DELTA)
                    end_time = read_u64(C.COL_TIME_DELTA)
                records.append(TxnEnd(txn_id=txn_id, end_time=end_time))

            elif tag == TxnRecordTag.TR_LINK:
                link_type = LinkType(read_u8(C.COL_LINK_TYPE))
                if delta:
                    src = prev_txn_id + read_svarint(C.COL_TXN_ID_DELTA)
                    prev_txn_id = src
                else:
                    src = read_u64(C.COL_TXN_ID_DELTA)
                tgt = read_uvarint(C.COL_LINK_TGT)
                label_id = read_uvarint(C.COL_LINK_LABEL)
                records.append(TxnLink(
                    link_type=link_type,
                    source_txn_id=src,
                    target_txn_id=tgt,
                    label_str_id=label_id,
                ))

            elif tag == TxnRecordTag.TR_META:
                if delta:
                    txn_id = prev_txn_id + read_svarint(C.COL_TXN_ID_DELTA)
                    prev_txn_id = txn_id
                else:
                    txn_id = read_u64(C.COL_TXN_ID_DELTA)
                key_str_id = read_uvarint(C.COL_META_KEY)
                value_str_id = read_uvarint(C.COL_META_VAL)
                records.append(TxnMeta(
                    txn_id=txn_id,
                    key_str_id=key_str_id,
                    value_str_id=value_str_id,
                ))

        return records

    def _decode_attrs_column(self, txn_type_id: int, col_data: Dict[int, bytes],
                             cur: Dict[int, int]) -> List[TxnAttr]:
        """Decode attribute values from per-field-index columns."""
        C = TxnColumnId
        schema = self._schemas.get(txn_type_id, [])
        attrs = []
        for field_idx, ft in enumerate(schema):
            col_id = C.COL_ATTR_BASE + field_idx
            if col_id not in col_data:
                continue
            val, new_off = decode_field(ft, col_data[col_id], cur[col_id])
            cur[col_id] = new_off
            attrs.append(TxnAttr(field_idx=field_idx, value=val))
        return attrs

    # ------------------------------------------------------------------
    # Internal decode
    # ------------------------------------------------------------------

    def _decode_payload(self, data: bytes, flags: int = 0) -> List[object]:
        delta = bool(flags & FLAG_TXN_DELTA)
        offset = 0
        start_time, end_time = struct.unpack_from('<QQ', data, offset)
        self.start_time = start_time
        self.end_time   = end_time
        offset += 16

        count, offset = decode_uvarint(data, offset)
        records = []
        prev_txn_id = 0
        prev_time = start_time
        for _ in range(count):
            rec, offset = self._decode_record(data, offset, delta, prev_txn_id, prev_time)
            if rec is not None:
                records.append(rec)
                if delta:
                    prev_txn_id, prev_time = self._advance_decoded_state(rec, prev_txn_id, prev_time)
        return records

    @staticmethod
    def _advance_decoded_state(rec, prev_txn_id: int, prev_time: int):
        """Return updated (prev_txn_id, prev_time) from a decoded record object."""
        if isinstance(rec, TxnFull):
            return rec.txn_id, rec.end_time
        elif isinstance(rec, TxnBegin):
            return rec.txn_id, rec.start_time
        elif isinstance(rec, TxnAttrRecord):
            return rec.txn_id, prev_time
        elif isinstance(rec, TxnEnd):
            return rec.txn_id, rec.end_time
        elif isinstance(rec, TxnLink):
            return rec.source_txn_id, prev_time
        elif isinstance(rec, TxnMeta):
            return rec.txn_id, prev_time
        return prev_txn_id, prev_time

    def _decode_record(self, data: bytes, offset: int,
                       delta: bool = False,
                       prev_txn_id: int = 0, prev_time: int = 0):
        tag_byte = data[offset]; offset += 1
        try:
            tag = TxnRecordTag(tag_byte)
        except ValueError:
            return None, offset

        if tag == TxnRecordTag.TR_FULL:
            stream_inst_id, offset = decode_uvarint(data, offset)
            txn_type_id,    offset = decode_uvarint(data, offset)
            if delta:
                txn_id_d, offset = decode_svarint(data, offset)
                start_d,  offset = decode_svarint(data, offset)
                duration, offset = decode_uvarint(data, offset)
                parent,   offset = decode_uvarint(data, offset)
                txn_id = prev_txn_id + txn_id_d
                start  = prev_time + start_d
                end    = start + duration
            else:
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
            if delta:
                txn_id_d, offset = decode_svarint(data, offset)
                start_d,  offset = decode_svarint(data, offset)
                parent,   offset = decode_uvarint(data, offset)
                txn_id = prev_txn_id + txn_id_d
                start  = prev_time + start_d
            else:
                txn_id, start, parent = struct.unpack_from('<QQQ', data, offset); offset += 24
            return TxnBegin(
                stream_inst_id=stream_inst_id,
                txn_type_id=txn_type_id,
                txn_id=txn_id,
                start_time=start,
                parent_txn_id=parent,
            ), offset

        elif tag == TxnRecordTag.TR_ATTR:
            if delta:
                txn_id_d, offset = decode_svarint(data, offset)
                txn_id = prev_txn_id + txn_id_d
            else:
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
            if delta:
                txn_id_d,   offset = decode_svarint(data, offset)
                end_time_d, offset = decode_svarint(data, offset)
                txn_id   = prev_txn_id + txn_id_d
                end_time = prev_time + end_time_d
            else:
                txn_id, end_time = struct.unpack_from('<QQ', data, offset); offset += 16
            return TxnEnd(txn_id=txn_id, end_time=end_time), offset

        elif tag == TxnRecordTag.TR_LINK:
            link_type = LinkType(data[offset]); offset += 1
            if delta:
                src_d, offset = decode_svarint(data, offset)
                tgt,   offset = decode_uvarint(data, offset)
                src = prev_txn_id + src_d
            else:
                src, tgt = struct.unpack_from('<QQ', data, offset); offset += 16
            label_id, offset = decode_uvarint(data, offset)
            return TxnLink(
                link_type=link_type,
                source_txn_id=src,
                target_txn_id=tgt,
                label_str_id=label_id,
            ), offset

        elif tag == TxnRecordTag.TR_META:
            if delta:
                txn_id_d, offset = decode_svarint(data, offset)
                txn_id = prev_txn_id + txn_id_d
            else:
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
