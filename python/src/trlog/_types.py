"""Enums, constants, and dataclasses mirroring the TRLOG binary format spec."""

from __future__ import annotations

import enum
from dataclasses import dataclass, field
from typing import List, Optional


class BlockType(enum.IntEnum):
    BLK_FILE_HDR  = 0x00
    BLK_STR_TABLE = 0x01
    BLK_TYPE_REG  = 0x02
    BLK_HIERARCHY = 0x03
    BLK_VC_DATA   = 0x10
    BLK_TXN_DATA  = 0x11
    BLK_EXT       = 0x20
    BLK_INDEX     = 0xFE
    BLK_SKIP      = 0xFF


class HierKind(enum.IntEnum):
    HK_DESIGN     = 0x00
    HK_BEHAVIORAL = 0x01
    HK_SW         = 0x02
    HK_CUSTOM     = 0xFF


class ScopeType(enum.IntEnum):
    ST_MODULE      = 0x00
    ST_TASK        = 0x01
    ST_FUNCTION    = 0x02
    ST_BLOCK       = 0x03
    ST_FORK        = 0x04
    ST_GENERATE    = 0x05
    ST_STRUCT      = 0x06
    ST_UNION       = 0x07
    ST_INTERFACE   = 0x08
    ST_PACKAGE     = 0x09
    ST_PROGRAM     = 0x0A
    ST_VHDL_ARCH   = 0x0B
    ST_VHDL_PROC   = 0x0C
    ST_ANONYMOUS   = 0xFF


class VarDir(enum.IntEnum):
    VD_NONE   = 0x00
    VD_IN     = 0x01
    VD_OUT    = 0x02
    VD_INOUT  = 0x03
    VD_BUFFER = 0x04


class SignalEncoding(enum.IntEnum):
    SE_2STATE = 0x00
    SE_4STATE = 0x01
    SE_9STATE = 0x02
    SE_REAL   = 0x03
    SE_STRING = 0x04
    SE_BITVEC = 0x05
    SE_ANALOG = 0x06


class FieldType(enum.IntEnum):
    FT_U8     = 0x00
    FT_U16    = 0x01
    FT_U32    = 0x02
    FT_U64    = 0x03
    FT_I8     = 0x04
    FT_I16    = 0x05
    FT_I32    = 0x06
    FT_I64    = 0x07
    FT_F32    = 0x08
    FT_F64    = 0x09
    FT_BOOL   = 0x0A
    FT_STRING = 0x0B
    FT_BITVEC = 0x0C
    FT_BYTES  = 0x0D
    FT_TIME   = 0x0E
    FT_ENUM   = 0x0F


class TxnRecordTag(enum.IntEnum):
    TR_FULL  = 0x01
    TR_BEGIN = 0x02
    TR_ATTR  = 0x03
    TR_END   = 0x04
    TR_LINK  = 0x05
    TR_META  = 0x06


class LinkType(enum.IntEnum):
    LT_CAUSE_EFFECT = 0x00
    LT_PARENT_CHILD = 0x01
    LT_RELATED      = 0x02
    LT_CUSTOM       = 0xFF


class Radix(enum.IntEnum):
    RX_HEX    = 0x00
    RX_DEC    = 0x01
    RX_OCT    = 0x02
    RX_BIN    = 0x03
    RX_UDEC   = 0x04
    RX_SIGNED = 0x05
    RX_REAL   = 0x06
    RX_STRING = 0x07
    RX_TIME   = 0x08


class HierTag(enum.IntEnum):
    H_SCOPE   = 0x01
    H_UPSCOPE = 0x02
    H_VAR     = 0x03
    H_STREAM  = 0x04
    H_ATTR    = 0x05


# File magic bytes: "TRL\x00" + version 1.0
FILE_MAGIC = b'TRL\x00\x00\x00\x01\x00'

# Common block header size (block_type:1 + block_flags:1 + block_length:8)
BLOCK_HEADER_SIZE = 10

# Block compression flags
FLAG_COMPRESSED   = 0x01
FLAG_COMPRESS_ALG = 0x02  # 0=ZLib, 1=Zstd (when FLAG_COMPRESSED set)
# BLK_VC_DATA sub-section flags (only meaningful when FLAG_COMPRESSED is NOT set)
FLAG_TIME_ZLIB    = 0x04  # time table delta bytes are zlib-compressed within payload
FLAG_WAVE_LZ4     = 0x08  # each wave in wave_data has a per-wave LZ4 compression header
# BLK_VC_DATA encoding flags (survive whole-block decompression)
FLAG_WAVE_XOR_DELTA = 0x10  # 2-state multi-bit wave values are XOR-delta + RLE encoded
FLAG_WAVE_ZLIB    = 0x20  # each wave in wave_data has per-wave zlib compression
FLAG_SEEKABLE     = 0x40  # last 8 bytes of payload = u64 offset to position table

# BLK_TXN_DATA encoding flags
FLAG_TXN_DELTA    = 0x04  # txn_id / timestamps are delta-encoded with svarint
FLAG_TXN_COLUMN   = 0x08  # BLK_TXN_DATA payload uses column-oriented layout


class TxnColumnId(enum.IntEnum):
    """Column identifiers for column-oriented BLK_TXN_DATA layout."""
    COL_TAG            = 0
    COL_STREAM_INST_ID = 1
    COL_TXN_TYPE_ID    = 2
    COL_TXN_ID_DELTA   = 3
    COL_TIME_DELTA     = 4
    COL_DURATION       = 5
    COL_PARENT         = 6
    COL_LINK_TYPE      = 7
    COL_LINK_TGT       = 8
    COL_LINK_LABEL     = 9
    COL_META_KEY       = 10
    COL_META_VAL       = 11
    COL_ATTR_BASE      = 0x80


# Fixed-width field types eligible for column encoding
_FIXED_WIDTH_FTS = frozenset({
    FieldType.FT_U8, FieldType.FT_U16, FieldType.FT_U32, FieldType.FT_U64,
    FieldType.FT_I8, FieldType.FT_I16, FieldType.FT_I32, FieldType.FT_I64,
    FieldType.FT_F32, FieldType.FT_F64, FieldType.FT_BOOL, FieldType.FT_TIME,
    FieldType.FT_ENUM,
})



# --- Dataclasses ---

@dataclass
class FieldDef:
    name_str_id:  int
    field_type:   FieldType
    display_hint: Radix = Radix.RX_HEX
    enum_type_id: int   = 0


@dataclass
class SignalTypeEntry:
    sig_type_id:   int
    encoding:      SignalEncoding
    bit_width:     int = 0
    display_radix: Radix = Radix.RX_HEX


@dataclass
class TxnSchemaEntry:
    txn_type_id: int
    name_str_id: int
    fields:      List[FieldDef] = field(default_factory=list)


@dataclass
class EnumValue:
    integer_value: int
    label_str_id:  int


@dataclass
class EnumTypeEntry:
    enum_type_id: int
    name_str_id:  int
    values:       List[EnumValue] = field(default_factory=list)


@dataclass
class StreamDeclEntry:
    stream_id:        int
    name_str_id:      int
    kind_str_id:      int = 0
    default_txn_type: int = 0


@dataclass
class HierarchyHeader:
    hier_id:     int
    kind:        HierKind
    name_str_id: int


@dataclass
class HScope:
    scope_type:       ScopeType
    name_str_id:      int
    component_str_id: int = 0
    src_file_str_id:  int = 0
    src_line:         int = 0
    children:         list = field(default_factory=list)
    attrs:            list = field(default_factory=list)


@dataclass
class HVar:
    var_id:          int
    name_str_id:     int
    sig_type_id:     int
    var_dir:         VarDir = VarDir.VD_NONE
    alias_of_var_id: int = 0
    src_file_str_id: int = 0
    src_line:        int = 0
    attrs:           list = field(default_factory=list)


@dataclass
class HStream:
    stream_inst_id: int
    stream_type_id: int
    name_str_id:    int
    attrs:          list = field(default_factory=list)


@dataclass
class HAttr:
    key_str_id:   int
    value_str_id: int


@dataclass
class VcChange:
    var_id: int
    time:   int
    value:  object  # int, float, str, or bytes depending on encoding


@dataclass
class TxnAttr:
    field_idx: int
    value:     object


@dataclass
class TxnFull:
    stream_inst_id: int
    txn_type_id:    int
    txn_id:         int
    start_time:     int
    end_time:       int
    parent_txn_id:  int
    attrs:          List[TxnAttr] = field(default_factory=list)


@dataclass
class TxnBegin:
    stream_inst_id: int
    txn_type_id:    int
    txn_id:         int
    start_time:     int
    parent_txn_id:  int


@dataclass
class TxnAttrRecord:
    txn_id:     int
    attrs:      List[TxnAttr] = field(default_factory=list)


@dataclass
class TxnEnd:
    txn_id:   int
    end_time: int


@dataclass
class TxnLink:
    link_type:     LinkType
    source_txn_id: int
    target_txn_id: int
    label_str_id:  int = 0


@dataclass
class TxnMeta:
    """Key/value string metadata attached to a transaction (TR_META record).

    Semantics mirror H_ATTR for hierarchy nodes.  Multiple records with the
    same key are allowed; the raw sequence is preserved.  For map-style access
    the last value for a given key takes precedence.

    Line numbers are encoded as decimal ASCII strings (e.g. ``"42"``).
    """
    txn_id:       int
    key_str_id:   int
    value_str_id: int


# Union-like type alias for transaction records
TxnRecord = object  # TxnFull | TxnBegin | TxnAttrRecord | TxnEnd | TxnLink | TxnMeta


class WellKnownAttr:
    """Standard attribute key strings for H_ATTR and TR_META metadata.

    These keys are reserved for use by the TRLOG format itself.  User- or
    tool-defined keys should use a different namespace to avoid collisions.

    Line-number values (SRC_LINE, DRIVER_LINE, ORIGIN_LINE) are stored as
    decimal ASCII strings (e.g. ``"42"``).  A value of ``"0"`` means unknown.
    """

    # Source location of a hierarchy node (scope, var, stream)
    SRC_FILE  = "trlog.src.file"
    SRC_LINE  = "trlog.src.line"

    # Driver location for a signal variable (who drives the signal)
    DRIVER_FILE = "trlog.driver.file"
    DRIVER_LINE = "trlog.driver.line"

    # Origin location for a transaction (where it was created, e.g. in a sequence)
    ORIGIN_FILE      = "trlog.origin.file"
    ORIGIN_LINE      = "trlog.origin.line"
    ORIGIN_COMPONENT = "trlog.origin.component"

