"""TRLOG — ZuSpec Trace binary format reader/writer."""

from ._types import (
    BlockType, HierKind, ScopeType, VarDir, SignalEncoding,
    FieldType, TxnRecordTag, LinkType, Radix, HierTag, AttrType,
    FILE_MAGIC, BLOCK_HEADER_SIZE, FLAG_COMPRESSED, FLAG_COMPRESS_ALG,
    FieldDef, SignalTypeEntry, TxnSchemaEntry, EnumValue, EnumTypeEntry,
    StreamDeclEntry, HierarchyHeader, HScope, HVar, HStream, HAttr, TypedAttr,
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
from ._deps import DependencyGraph, Materializer, DependencyError
from ._derived import Input, Const, compile_expr
from . import codec
from .codec import (
    VcCodec, TxnCodec, Capability, Store,
    vc_codec, txn_codec,
    register_vc_codec, register_txn_codec,
    lookup_vc_codec, lookup_txn_codec,
    CORE_VALUECHANGE, CORE_RECORD, CORE_BYTESPLIT, CORE_DERIVED,
)
from ._exceptions import (
    ZstError, ZstMagicError, ZstVersionError, ZstFormatError,
    ZstCompressionError, ZstNoIndexError, ZstCorruptError,
)

__all__ = [
    "BlockType", "HierKind", "ScopeType", "VarDir", "SignalEncoding",
    "FieldType", "TxnRecordTag", "LinkType", "Radix", "HierTag", "AttrType",
    "FILE_MAGIC", "BLOCK_HEADER_SIZE", "FLAG_COMPRESSED", "FLAG_COMPRESS_ALG",
    "FieldDef", "SignalTypeEntry", "TxnSchemaEntry", "EnumValue", "EnumTypeEntry",
    "StreamDeclEntry", "HierarchyHeader", "HScope", "HVar", "HStream", "HAttr", "TypedAttr",
    "VcChange", "TxnAttr", "TxnFull", "TxnBegin", "TxnAttrRecord", "TxnEnd", "TxnLink",
    "TxnMeta", "WellKnownAttr",
    "encode_uvarint", "decode_uvarint",
    "encode_svarint", "decode_svarint",
    "encode_field", "decode_field",
    "TrlWriter", "TrlReader", "ExtBlock",
    "DependencyGraph", "Materializer", "DependencyError",
    "Input", "Const", "compile_expr",
    "codec", "VcCodec", "TxnCodec", "Capability", "Store",
    "vc_codec", "txn_codec",
    "register_vc_codec", "register_txn_codec",
    "lookup_vc_codec", "lookup_txn_codec",
    "CORE_VALUECHANGE", "CORE_RECORD", "CORE_BYTESPLIT", "CORE_DERIVED",
    "ZstError", "ZstMagicError", "ZstVersionError", "ZstFormatError",
    "ZstCompressionError", "ZstNoIndexError", "ZstCorruptError",
]
