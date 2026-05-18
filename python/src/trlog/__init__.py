"""TRLOG — ZuSpec Trace binary format reader/writer."""

from ._types import (
    BlockType, HierKind, ScopeType, VarDir, SignalEncoding,
    FieldType, TxnRecordTag, LinkType, Radix, HierTag,
    FILE_MAGIC, BLOCK_HEADER_SIZE, FLAG_COMPRESSED, FLAG_COMPRESS_ALG,
    FieldDef, SignalTypeEntry, TxnSchemaEntry, EnumValue, EnumTypeEntry,
    StreamDeclEntry, HierarchyHeader, HScope, HVar, HStream, HAttr,
    VcChange, TxnAttr, TxnFull, TxnBegin, TxnAttrRecord, TxnEnd, TxnLink,
    TxnMeta, WellKnownAttr,
)
from ._codec import (
    encode_uvarint, decode_uvarint,
    encode_svarint, decode_svarint,
    encode_field, decode_field,
)
from ._native import TrlWriter, TrlReader
from ._ext import ExtBlock
from ._exceptions import (
    ZstError, ZstMagicError, ZstVersionError, ZstFormatError,
    ZstCompressionError, ZstNoIndexError, ZstCorruptError,
)

__all__ = [
    "BlockType", "HierKind", "ScopeType", "VarDir", "SignalEncoding",
    "FieldType", "TxnRecordTag", "LinkType", "Radix", "HierTag",
    "FILE_MAGIC", "BLOCK_HEADER_SIZE", "FLAG_COMPRESSED", "FLAG_COMPRESS_ALG",
    "FieldDef", "SignalTypeEntry", "TxnSchemaEntry", "EnumValue", "EnumTypeEntry",
    "StreamDeclEntry", "HierarchyHeader", "HScope", "HVar", "HStream", "HAttr",
    "VcChange", "TxnAttr", "TxnFull", "TxnBegin", "TxnAttrRecord", "TxnEnd", "TxnLink",
    "TxnMeta", "WellKnownAttr",
    "encode_uvarint", "decode_uvarint",
    "encode_svarint", "decode_svarint",
    "encode_field", "decode_field",
    "TrlWriter", "TrlReader", "ExtBlock",
    "ZstError", "ZstMagicError", "ZstVersionError", "ZstFormatError",
    "ZstCompressionError", "ZstNoIndexError", "ZstCorruptError",
]
