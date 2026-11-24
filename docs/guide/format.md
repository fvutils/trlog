# File Format Reference

This page describes the on-disk layout of a trlog file (`.trl`), format
version 2.1. The Python implementation in `python/src/trlog/` is the
reference. Where the C library (`c/src/`) writes or reads something
differently, the section says so; [C implementation notes](#fmt-c-notes)
lists those differences in one place.

## Conventions

| Notation | Meaning |
|----------|---------|
| `u8`, `u16`, `u32`, `u64` | Unsigned integer, little-endian. |
| `i8`, `i64` | Signed two's-complement integer, little-endian. |
| `f64` | IEEE 754 double, little-endian. |
| `uvarint` | Unsigned LEB128: 7 bits per byte, low group first, bit 7 set on every byte except the last. |
| `svarint` | Zigzag-mapped signed value, `(n << 1) ^ (n >> 63)`, then encoded as a `uvarint`. |
| `str_id` | A `uvarint` index into the string table. Id 0 means "no string". |
| `BE bytes` | A bit vector stored as `max(1, ceil(width / 8))` bytes, most significant byte first. |

Times are unsigned integers in units of `10^timescale_exp` seconds (see
[BLK_FILE_HDR](#fmt-file-hdr)).

## File layout

A file is a sequence of blocks. There is no separate magic number, footer
or trailer: the first block is the file header and the last block is the
index.

```
BLK_FILE_HDR      always first; holds the magic and the index offset
BLK_HIERARCHY     zero or more, written when each hierarchy is flushed
BLK_VC_DATA   \
BLK_TXN_DATA   |  data blocks, in the order the application writes them
BLK_EXT        |
BLK_AUX       /
BLK_META          optional; trace-global metadata, written at close
BLK_STR_TABLE     written at close; holds every interned string
BLK_TYPE_REG      written at close; holds every registered type
BLK_INDEX         always last
```

The Python writer emits `BLK_FILE_HDR` when the file is opened. Hierarchy,
data, extension and aux blocks are written as soon as each one is flushed.
At close, the writer writes `BLK_META` (only if trace metadata was added),
one `BLK_STR_TABLE`, one `BLK_TYPE_REG` and the `BLK_INDEX`, then seeks back
and stores the index offset in the file header.

`TrlWriter.flush_string_table()` and `flush_type_registry()` write an extra,
complete copy of the table before close. A reader that uses the index reads
only the copies the index points to.

### Reading a file

1. Read the first block. It must be `BLK_FILE_HDR` with a matching magic and
   `version_major` 1 or 2.
2. If `index_offset` in the header is non-zero, read the `BLK_INDEX` there.
   It gives the offsets of the string table, the type registry, every
   hierarchy block and the `BLK_META` block, plus time ranges for every data
   block.
3. If `index_offset` is 0 (the writer could not seek, or did not close), scan
   the file block by block from offset 0 using the length in each block
   header.

A reader skips any block whose type it does not recognize, using the block
length.

## Block framing

Every block starts with a 10-byte header:

```
u8   block_type
u8   block_flags
u64  block_length     total length in bytes, including this header
```

The payload follows the header. `block_length - 10` is the payload length.

### Block types

| Value | Name | Contents |
|-------|------|----------|
| `0x00` | `BLK_FILE_HDR` | Magic, version, timescale, index offset. |
| `0x01` | `BLK_STR_TABLE` | Interned strings. |
| `0x02` | `BLK_TYPE_REG` | Signal types, transaction schemas, enum types, stream types. |
| `0x03` | `BLK_HIERARCHY` | One scope tree with its variables and stream instances. |
| `0x04` | `BLK_META` | Trace-global typed metadata. |
| `0x10` | `BLK_VC_DATA` | Value changes for a time window. |
| `0x11` | `BLK_TXN_DATA` | Transaction records for a time window. |
| `0x12` | `BLK_AUX` | Opaque keyed (non-temporal) data written by a codec. |
| `0x20` | `BLK_EXT` | Application-defined extension data. |
| `0xFE` | `BLK_INDEX` | Offsets and time ranges of the other blocks. |
| `0xFF` | `BLK_SKIP` | Reserved. Not written by the current writers. |

### Block flags

Bits 0, 1 and 7 have the same meaning in every block type. Bits 2 to 6 are
defined per block type (see [BLK_VC_DATA](#fmt-vc-data) and
[BLK_TXN_DATA](#fmt-txn-data)).

| Bit | Mask | Name | Meaning |
|-----|------|------|---------|
| 0 | `0x01` | `FLAG_COMPRESSED` | The payload is compressed as one unit. |
| 1 | `0x02` | `FLAG_COMPRESS_ALG` | With `FLAG_COMPRESSED`: 0 = zlib, 1 = zstd. |
| 7 | `0x80` | `FLAG_STRUCT_CODEC` | The payload was produced by a non-default codec and starts with the codec id. See [Codec-produced blocks](#fmt-codec-blocks). |

### Compression

When `FLAG_COMPRESSED` is set, the payload is a compressed stream. How the
compressed payload is laid out depends on the block type:

| Block | Compressed payload |
|-------|--------------------|
| `BLK_STR_TABLE`, `BLK_TYPE_REG`, `BLK_VC_DATA`, `BLK_TXN_DATA`, `BLK_AUX` | The zlib or zstd stream only. |
| `BLK_META` | The zlib or zstd stream only. |
| `BLK_HIERARCHY` | `u64 uncompressed_length`, then the zlib or zstd stream. |
| `BLK_FILE_HDR`, `BLK_INDEX`, `BLK_EXT` | Never compressed. |

The Python writer compresses blocks when `compress=True` (the default), with
zlib unless `compressor="zstd"` is given (this needs the `zstandard` package).
The C writer and reader support zlib only, so a zstd-compressed file can only
be read from Python.

(fmt-file-hdr)=
## BLK_FILE_HDR (0x00)

The payload is 49 bytes; the whole block is 59 bytes.

| Payload offset | File offset | Field | Type | Notes |
|----------------|-------------|-------|------|-------|
| 0 | 10 | `magic` | `u8[8]` | `54 52 4C 00 00 00 01 00` (`"TRL\0\0\0\x01\0"`) |
| 8 | 18 | `version_major` | `u8` | 2 |
| 9 | 19 | `version_minor` | `u8` | 1 |
| 10 | 20 | `timescale_exp` | `i8` | Time unit is `10^timescale_exp` s; default -9 (ns). |
| 11 | 21 | `timezero` | `i64` | Time offset; default 0. |
| 19 | 29 | `flags` | `u32` | 0. |
| 23 | 33 | `creator_str_id` | `uvarint` | 0 (one byte). |
| 24 | 34 | `date_str_id` | `uvarint` | 0 (one byte). |
| 25 | 35 | `index_offset` | `u64` | File offset of `BLK_INDEX`, or 0 if unknown. |
| 33 | 43 | reserved | `u8[16]` | Zero. |

The writer writes `index_offset` as 0 and patches file offset 35 at close.
The fixed position relies on `creator_str_id` and `date_str_id` each being
one byte, which they are because both are always 0. A reader must decode the
two uvarints to find `index_offset` rather than assume offset 35.

The reader rejects a file whose magic differs or whose `version_major` is not
1 or 2. It ignores `version_minor`, `flags` and the reserved bytes.

## BLK_STR_TABLE (0x01)

```
uvarint count
count x:
  uvarint byte_length
  u8[byte_length]  UTF-8 text
```

String id 0 is reserved for "no string" and is not stored. The first entry
in the table is id 1, the next id 2, and so on. Interning the same string
twice returns the same id.

(fmt-type-reg)=
## BLK_TYPE_REG (0x02)

```
uvarint entry_count
entry_count x entry
```

Each entry starts with a `u8` tag. The writer emits all signal types, then
all transaction schemas, then enum types, then stream types. Each kind has
its own id space, starting at 1.

| Tag | Entry | Layout after the tag |
|-----|-------|----------------------|
| `0x01` | Signal type | `uvarint sig_type_id, u8 encoding, uvarint bit_width, u8 display_radix` |
| `0x02` | Transaction schema | `uvarint txn_type_id, str_id name, uvarint field_count`, then per field: `str_id name, u8 field_type, u8 display_hint, uvarint enum_type_id` |
| `0x03` | Enum type | `uvarint enum_type_id, str_id name, uvarint value_count`, then per value: `svarint value, str_id label` |
| `0x04` | Stream type | `uvarint stream_id, str_id name, str_id kind, uvarint default_txn_type` |
| `0x05` | Stream type, v2 | `uvarint body_length`, then the body below |

`encoding` is a `SignalEncoding`: `SE_2STATE` 0, `SE_4STATE` 1, `SE_9STATE` 2,
`SE_REAL` 3, `SE_STRING` 4, `SE_BITVEC` 5, `SE_ANALOG` 6. `display_radix` and
`display_hint` are `Radix` values. `field_type` is a `FieldType` (see
[Field value encoding](#fmt-field-values)).

### Stream type v2 (`TRL_TYPE_TAG_STREAM_V2`, 0x05)

The writer uses tag `0x05` instead of `0x04` only for a stream type that has
a codec, transparent metadata, or declared dependencies. Other stream types
keep the `0x04` layout, so files that use none of these features are
unchanged.

```
uvarint stream_id
str_id  name
str_id  kind
uvarint default_txn_type
str_id  codec_id            reverse-DNS codec id; 0 = default codec
uvarint codec_version
uvarint param_length
u8[param_length] params     codec-private; opaque to the container
uvarint attr_count          transparent metadata (typed attributes)
attr_count x typed_attr
uvarint dep_count           input-stream dependencies
dep_count x uvarint stream_id
```

Every tag `>= 0x05` is followed by a `uvarint` body length. A reader that
does not recognize such a tag skips the body and continues. An unknown tag
below `0x05` ends parsing of the block.

## BLK_HIERARCHY (0x03)

The (decompressed) payload is a short header followed by a sequence of tagged
records that runs to the end of the payload:

```
uvarint hier_id
u8      kind            HierKind: 0 design, 1 behavioral, 2 software, 0xFF custom
str_id  name
tagged records ...
```

| Tag | Record | Fields after the tag |
|-----|--------|----------------------|
| `0x01` | `H_SCOPE` | `u8 scope_type, str_id name, str_id component, str_id src_file, uvarint src_line` |
| `0x02` | `H_UPSCOPE` | none; closes the most recent open scope |
| `0x03` | `H_VAR` | `uvarint var_id, str_id name, uvarint sig_type_id, u8 direction, uvarint alias_of_var_id, str_id src_file, uvarint src_line` |
| `0x04` | `H_STREAM` | `uvarint stream_inst_id, uvarint stream_type_id, str_id name` |
| `0x05` | `H_ATTR` | `str_id key, str_id value`; string attribute on the preceding node |
| `0x06` | `H_ATTR2` | `typed_attr`; typed attribute on the preceding node |

A scope's own attribute records follow its `H_SCOPE` record, then its
children, then `H_UPSCOPE`. `scope_type` is a `ScopeType` and `direction` a
`VarDir`. `alias_of_var_id` is 0 for a variable that is not an alias.
Readers treat a legacy `H_ATTR` as a typed string attribute. An unknown tag
ends parsing.

`var_id` and `stream_inst_id` must be unique across all hierarchy blocks of a
file: value-change and transaction blocks refer to them by id only. The
writers number them from 1 and continue across hierarchies. (Files from
writers before this rule may reuse ids across hierarchies.)

(fmt-vc-data)=
## BLK_VC_DATA (0x10)

A value-change block holds every change of a set of variables in the time
window `[start_time, end_time]`. This section describes the block written by
the default codec, `org.fvutils.trlog.core.valuechange`. Blocks written by
another codec have `FLAG_STRUCT_CODEC` set and a codec-defined payload.

### Flags

| Bit | Mask | Name | Meaning |
|-----|------|------|---------|
| 0 | `0x01` | `FLAG_COMPRESSED` | Whole payload compressed. |
| 1 | `0x02` | `FLAG_COMPRESS_ALG` | 0 = zlib, 1 = zstd. |
| 2 | `0x04` | `FLAG_TIME_ZLIB` | The time-delta table is zlib-compressed inside the payload. |
| 3 | `0x08` | `FLAG_WAVE_LZ4` | Each wave entry has its own LZ4 framing. |
| 4 | `0x10` | `FLAG_WAVE_XOR_DELTA` | Multi-bit `SE_2STATE` waves use XOR-delta + RLE. |
| 5 | `0x20` | `FLAG_WAVE_ZLIB` | Each wave entry has its own zlib framing. |
| 6 | `0x40` | `FLAG_SEEKABLE` | The payload ends with the offset of the position table. |
| 7 | `0x80` | `FLAG_STRUCT_CODEC` | Payload produced by a non-default codec. |

The writer always sets `FLAG_WAVE_XOR_DELTA`. It is an encoding flag, not a
compression flag, and still applies after whole-payload decompression. Bits
2, 3, 5 and 6 are set only when `FLAG_COMPRESSED` is not set; a reader clears
them after decompressing a whole payload. With the writer defaults
(`compress=True`) a block's flags are `0x11`; with `compress=False` they are
`0x30` (per-wave zlib).

### Payload

```
u64     start_time
u64     end_time
uvarint time_count
time table:
  if FLAG_TIME_ZLIB and time_count > 0:
    uvarint compressed_length
    u8[compressed_length]   zlib stream of the time_count uvarint deltas
  else:
    time_count x uvarint delta
uvarint var_count
var_count x:
  uvarint var_id            ascending
  init_value                encoded by signal type, see below
u8      pos_entry_size      4 or 8
var_count x u32|u64 wave_offset    offset of the variable's wave in wave_data
uvarint wave_data_length
u8[wave_data_length] wave_data
[u64    position_table_offset]     only with FLAG_SEEKABLE
```

The time table lists the distinct change times of the block in ascending
order. The first delta is relative to `start_time`; each later delta is
relative to the previous time. Waves refer to times by their index in this
table (`tidx`).

`pos_entry_size` is 4 when the total wave data is shorter than 2^32 bytes,
else 8. The writer stores identical wave entries once, so several variables
may have the same `wave_offset`.

### Initial values

Each variable has an initial value, the value it has at `start_time`. It is
the value passed to `set_initial`, or the first change in the block, or the
type's zero value (0, 0.0 or the empty string).

| Encoding | Layout |
|----------|--------|
| `SE_2STATE`, `SE_BITVEC` | `uvarint bit_width`, BE bytes |
| `SE_4STATE` | `uvarint bit_width`, `max(1, ceil(2 * bit_width / 8))` bytes: 2 bits per bit, most significant bit first in bits 7:6 of the first byte; 0 = `0`, 1 = `1`, 2 = `X`, 3 = `Z` |
| `SE_9STATE` | `uvarint bit_width`, `max(1, ceil(bit_width / 2))` bytes: one nibble per bit, first bit in the high nibble; codes 0 `0`, 1 `1`, 2 `X`, 3 `Z`, 4 `H`, 5 `U`, 6 `W`, 7 `L`, 8 `-` |
| `SE_REAL`, `SE_ANALOG` | `f64` |
| `SE_STRING` | `uvarint length`, UTF-8 bytes |

### Wave entries

A wave entry lists one variable's changes. In every format below,
`tidx_delta` is the number of time-table entries skipped since the previous
change: `tidx = prev_tidx + 1 + tidx_delta`, with `prev_tidx` starting at -1.
A variable with no changes has the one-byte wave `00` (count 0).

**`SE_2STATE`, bit_width 1**

```
uvarint count
count x uvarint (tidx_delta << 2) | (value << 1) | 1
```

**`SE_2STATE`, bit_width > 1, `FLAG_WAVE_XOR_DELTA` set** (XOR-delta + RLE)

```
uvarint group_count
group_count x:
  uvarint (tidx_delta << 1) | 1
  uvarint xor_delta         previous value XOR this value
  uvarint repeat - 1        0 = one change; N = N + 1 changes
```

A group with `(tidx_delta = D, xor_delta = X, repeat = N + 1)` stands for
`N + 1` changes, each `D + 1` time-table entries after the previous one and
each XORing the running value with `X`. The running value starts at 0 for
each wave. A signal that toggles at a fixed interval, or a counter with a
periodic XOR pattern, encodes as a few groups regardless of trace length.

**`SE_2STATE`, bit_width > 1, `FLAG_WAVE_XOR_DELTA` clear** (legacy; read
only)

```
uvarint count
count x: uvarint (tidx_delta << 1) | 1, BE bytes
```

**`SE_4STATE`, bit_width 1**

```
uvarint count
count x uvarint (tidx_delta << 3) | (state << 1) | 1     state: 0, 1, X=2, Z=3
```

**`SE_4STATE` (bit_width > 1) and `SE_9STATE`**

```
uvarint count
count x:
  uvarint (tidx_delta << 1) | is_binary
  is_binary = 1: BE bytes (the value has no X/Z/... bits)
  is_binary = 0: packed states, as for the initial value
```

**`SE_REAL`, `SE_ANALOG`**: `uvarint count`, then per change
`uvarint tidx_delta, f64 value`.

**`SE_STRING`**: `uvarint count`, then per change
`uvarint tidx_delta, uvarint length, UTF-8 bytes`.

**`SE_BITVEC`**: `uvarint count`, then per change
`uvarint (tidx_delta << 1) | 1, BE bytes`.

### Per-wave compression

When `FLAG_WAVE_ZLIB` or `FLAG_WAVE_LZ4` is set, every wave entry starts
with a framing byte:

```
00                                  raw wave bytes follow
01  uvarint raw_length  uvarint compressed_length  u8[compressed_length]
```

The compressed bytes are a zlib stream or an LZ4 block without a size
header. The writer uses the compressed form only when it is smaller than
the raw form. If the `lz4` package is not installed, the writer uses the raw
form for every wave.

### Seekable payloads

With `FLAG_SEEKABLE`, the last 8 bytes of the (decompressed) payload are a
`u64` holding the payload offset of the `pos_entry_size` byte. A reader can
jump to the position table, and from there to any variable's wave, without
parsing the time table or the initial values. The time table is still needed
to turn `tidx` values into times.

### Scope-grouped blocks

With `TrlWriter(..., scope_grouped=True)`, the writer splits each
value-change block into one `BLK_VC_DATA` per hierarchy scope, so a reader
can decode one scope without decoding the others. Each scope block is
whole-payload zlib-compressed (flags `0x11`); the writer's `compress`,
`compress_waves` and `seekable` settings do not apply to them. Scope grouping
applies only to the default value-change codec.

### Writer options and resulting flags

| `TrlWriter` arguments | Flags | Notes |
|-----------------------|-------|-------|
| defaults (`compress=True`) | `0x11` | Whole-payload zlib; smallest files; not seekable. |
| `compress=False` | `0x30` | Per-wave zlib. |
| `compress=False, seekable=True` | `0x70` | Per-wave zlib with the seek offset. |
| `compress=False, compress_waves_alg="lz4", seekable=True` | `0x58` | Per-wave LZ4; fastest to decode. |
| `compress=False, compress_time_table=True` | adds `0x04` | Compressed time table. |

(fmt-txn-data)=
## BLK_TXN_DATA (0x11)

A transaction block holds records for a time window, written by the default
codec `org.fvutils.trlog.core.record`.

### Flags

| Bit | Mask | Name | Meaning |
|-----|------|------|---------|
| 0 | `0x01` | `FLAG_COMPRESSED` | Whole payload compressed. |
| 1 | `0x02` | `FLAG_COMPRESS_ALG` | 0 = zlib, 1 = zstd. |
| 2 | `0x04` | `FLAG_TXN_DELTA` | Transaction ids and times are delta-encoded. |
| 3 | `0x08` | `FLAG_TXN_COLUMN` | Column layout instead of row layout. |

The writer always sets `FLAG_TXN_DELTA`. It sets `FLAG_TXN_COLUMN` according
to the `column_layout` argument (default `"row"`).

### Row layout

```
u64     start_time
u64     end_time
uvarint record_count
record_count x record
```

Each record starts with a `u8` tag. With `FLAG_TXN_DELTA`, the reader keeps
two running values across the records of a block: `prev_txn_id`, starting at
0, and `prev_time`, starting at `start_time`.

| Tag | Record | Fields with `FLAG_TXN_DELTA` | Updates |
|-----|--------|------------------------------|---------|
| `0x01` | `TR_FULL` | `uvarint stream_inst_id, uvarint txn_type_id, svarint txn_id - prev_txn_id, svarint start - prev_time, uvarint end - start, uvarint parent, attr_list` | `prev_txn_id = txn_id`, `prev_time = end` |
| `0x02` | `TR_BEGIN` | `uvarint stream_inst_id, uvarint txn_type_id, svarint txn_id - prev_txn_id, svarint start - prev_time, uvarint parent` | `prev_txn_id`, `prev_time = start` |
| `0x03` | `TR_ATTR` | `svarint txn_id - prev_txn_id, typed_attr_list` | `prev_txn_id` |
| `0x04` | `TR_END` | `svarint txn_id - prev_txn_id, svarint end - prev_time` | `prev_txn_id`, `prev_time = end` |
| `0x05` | `TR_LINK` | `u8 link_type, svarint src - prev_txn_id, uvarint tgt, str_id label` | `prev_txn_id = src` |
| `0x06` | `TR_META` | `svarint txn_id - prev_txn_id, str_id key, str_id value` | `prev_txn_id` |

Without `FLAG_TXN_DELTA` (legacy), the same records store `txn_id`, `start`,
`end`, `parent` and the link `src` and `tgt` as absolute `u64` values; the
link `label` stays a `uvarint`. `parent` and the link `tgt` are absolute in
both forms.

The attribute lists differ between `TR_FULL` and `TR_ATTR`:

```
attr_list:        uvarint n, n x (uvarint field_idx, value)
typed_attr_list:  uvarint n, n x (uvarint field_idx, u8 field_type, value)
```

In `attr_list`, the value's type comes from field `field_idx` of the
record's transaction schema (`FT_U64` if the index is past the end of the
schema). `TR_ATTR` carries the type inline because the reader does not track
which schema an open transaction uses. `link_type` is a `LinkType`:
0 cause-effect, 1 parent-child, 2 related, 0xFF custom.

Sequential transaction ids give a delta of 1, which is one byte, and times
within a block are close together, so their deltas are small. Whole-payload
zlib compresses these repetitive varints well.

(fmt-field-values)=
### Field value encoding

| `FieldType` | Code | Encoding |
|-------------|------|----------|
| `FT_U8`, `FT_I8`, `FT_BOOL` | `0x00`, `0x04`, `0x0A` | 1 byte |
| `FT_U16`, `FT_I16` | `0x01`, `0x05` | 2 bytes |
| `FT_U32`, `FT_I32`, `FT_F32` | `0x02`, `0x06`, `0x08` | 4 bytes |
| `FT_U64`, `FT_I64`, `FT_F64`, `FT_TIME` | `0x03`, `0x07`, `0x09`, `0x0E` | 8 bytes |
| `FT_ENUM` | `0x0F` | `uvarint` |
| `FT_STRING`, `FT_BYTES` | `0x0B`, `0x0D` | `uvarint length`, bytes (UTF-8 for strings) |
| `FT_BITVEC` | `0x0C` | `uvarint width`, BE bytes |
| `FT_LOGICVEC` | `0x10` | `uvarint width`, `aval` BE bytes, `bval` BE bytes |

Multi-byte fixed-width values are little-endian.

### Column layout (`FLAG_TXN_COLUMN`)

In column layout, each field of the records is stored in its own column, so
the compressor sees runs of similar bytes.

```
u64     start_time
u64     end_time
uvarint record_count
uvarint column_count
column_count x:
  uvarint column_id
  uvarint byte_length
  u8[byte_length]
```

Only non-empty columns are written. Each column holds one entry per record
that has that field, in record order.

| Id | Column | Entry encoding | Records |
|----|--------|----------------|---------|
| 0 | `COL_TAG` | `u8` | all |
| 1 | `COL_STREAM_INST_ID` | `uvarint` | `TR_FULL`, `TR_BEGIN` |
| 2 | `COL_TXN_TYPE_ID` | `uvarint` | `TR_FULL`, `TR_BEGIN` |
| 3 | `COL_TXN_ID_DELTA` | `svarint` delta, or `u64` without `FLAG_TXN_DELTA` | all (`src` for `TR_LINK`) |
| 4 | `COL_TIME_DELTA` | `svarint` delta, or `u64` | `TR_FULL`, `TR_BEGIN`, `TR_END` |
| 5 | `COL_DURATION` | `uvarint` `end - start`, or `u64` | `TR_FULL` |
| 6 | `COL_PARENT` | `uvarint` | `TR_FULL`, `TR_BEGIN` |
| 7 | `COL_LINK_TYPE` | `u8` | `TR_LINK` |
| 8 | `COL_LINK_TGT` | `uvarint` | `TR_LINK` |
| 9 | `COL_LINK_LABEL` | `uvarint` | `TR_LINK` |
| 10 | `COL_META_KEY` | `uvarint` | `TR_META` |
| 11 | `COL_META_VAL` | `uvarint` | `TR_META` |
| 12 | `COL_ATTR_DATA` | `uvarint length`, then a `typed_attr_list` | `TR_ATTR` |
| 13 | `COL_ATTR_PRESENT` | `uvarint n`, then `n` x `uvarint field_idx` | `TR_FULL` |
| `0x80 + i` | `COL_ATTR_i` | field value of schema field `i` | `TR_FULL` that sets field `i` |

The running delta state is the same as in row layout. Attribute columns hold
absolute values in the fixed-width encodings from
[Field value encoding](#fmt-field-values).

`COL_ATTR_PRESENT` lists, for each `TR_FULL` record, the fields it sets, in
the order they were written; a field index outside the schema is read as
`FT_U64`. The writer emits this column only when some record in the block
does not set exactly the fields of its schema. Without it, every `TR_FULL`
record sets every field of its schema that has a column in the block.

### Choosing the layout

The `column_layout` argument of `TrlWriter` selects the layout per block:

| Value | Behavior |
|-------|----------|
| `"row"` (default) | Always row layout. |
| `"column"` | Always column layout. |
| `"auto"` | Column layout if the block has at least 32 records and every schema used by its `TR_FULL` and `TR_BEGIN` records has only fixed-width field types (`FT_U*`, `FT_I*`, `FT_F*`, `FT_BOOL`, `FT_TIME`, `FT_ENUM`); otherwise row layout. |
| `"adaptive"` | Encodes and compresses both layouts and keeps column layout only if it is smaller. |

(fmt-typed-attr)=
## BLK_META (0x04) and typed attributes

`BLK_META` holds trace-global metadata (`TrlWriter.add_trace_metadata`):

```
uvarint count
count x typed_attr
```

A typed attribute, also used in stream type v2 entries and `H_ATTR2`
records, is:

```
str_id  key
u8      attr_type
value
```

| `AttrType` | Code | Value |
|------------|------|-------|
| `AT_BOOL` | `0x00` | `u8` 0 or 1 |
| `AT_I64` | `0x01` | `svarint` |
| `AT_U64` | `0x02` | `uvarint` |
| `AT_F64` | `0x03` | `f64` |
| `AT_STR` | `0x04` | `str_id` |
| `AT_ARRAY` | `0x40` | Flag, OR'd with a scalar code: `uvarint count`, then `count` scalar values |

Codes `0x05` to `0x3F` and `0x80` and above are reserved. These values can
be read without any codec. Stream profiles (`TrlWriter.add_stream_profile`)
are stored as a string-array attribute on the stream type.

(fmt-aux)=
## BLK_AUX (0x12)

An aux block holds opaque bytes written by a codec, addressed by key rather
than by time. The payload is the codec's bytes, optionally compressed. The
index lists every aux block as `(owner_stream_id, key, ordinal, offset,
length)`, where `ordinal` counts blocks with the same owner and key from 0
and `length` is the full block length. A reader can list aux blocks and skip
them without the codec that wrote them. See `TrlWriter.write_aux` and
`TrlReader.read_aux`.

Derived signals store their definitions in aux blocks; see
[Codecs](#codec-derived).

## BLK_EXT (0x20)

```
u8[4]   ext_type        application-chosen tag, for example b"ACME"
u16     ext_version
u8[]    payload         rest of the block
```

Extension blocks are never compressed and always have flags 0. They are
not indexed; `TrlReader.iter_ext_blocks()` finds them by scanning.

## BLK_INDEX (0xFE)

The index is never compressed. Its sections appear in this order; optional
sections start with a one-byte sentinel and are omitted when empty.

```
uvarint vc_count
vc_count x  (u64 start_time, u64 end_time, u64 block_offset)
uvarint txn_count
txn_count x (u64 start_time, u64 end_time, u64 block_offset)
uvarint var_range_count                    always 0 today
var_range_count x (uvarint var_id, u64 first_time, u64 last_time)

u8 0x01                                    block offsets (always present)
u64 str_table_offset
u64 type_reg_offset
uvarint hier_count
hier_count x u64 hierarchy_offset

[u8 0x02                                   per-stream transaction index
 uvarint n
 n x (uvarint stream_inst_id, u64 start_time, u64 end_time, u64 block_offset)]

[u8 0x03                                   BLK_META
 u64 meta_offset]

[u8 0x04                                   aux key index
 uvarint n
 n x (uvarint owner_stream_id, uvarint key, uvarint ordinal,
      u64 block_offset, u64 block_length)]
```

Section `0x02` is written when any transaction block was begun for a single
stream (`begin_txn_block(..., stream_inst_id=...)`). It lists every
transaction block; a block that may hold several streams has
`stream_inst_id = 0xFFFFFFFF`. When present, readers use it instead of the
plain transaction list.

(fmt-codec-blocks)=
## Codec-produced blocks

The pluggable codec scheme ([Codecs](../codec-registry.md)) adds the
following to the format. A file that uses only the default codecs and no
metadata is byte-for-byte the same as a file written before the codec scheme
existed.

- **`FLAG_STRUCT_CODEC` (0x80)** on a `BLK_VC_DATA` block means a codec other
  than `core.valuechange` produced the payload. The payload starts with
  `uvarint codec_id_str`, the string-table id of the codec's reverse-DNS id.
  The rest of the payload belongs to the codec. The Python reader looks the
  id up in the codec registry and calls the codec's decoder; if no codec is
  registered under that id, it skips the block, and
  `TrlReader.missing_codecs()` reports the id.
- **Stream type v2 entries** (type registry tag `0x05`) record a stream
  type's codec id, codec version and codec parameters, plus its metadata and
  dependencies.
- **`BLK_AUX`** carries codec-private data that is not tied to time.
- **`BLK_META`** and **`H_ATTR2`** carry typed metadata that any reader can
  decode without a codec.

(fmt-c-notes)=
## C implementation notes

The C library (`c/src/`) reads and writes a subset of the format:

- The C writer writes `timezero = 0` unless `trl_writer_set_timezero` (or
  `trl_db_cfg_t.timezero`) sets it. It never writes `BLK_META` or `BLK_AUX`,
  and writes type-registry tags `0x01` and `0x02` only. The C reader skips
  enum (`0x03`) and stream (`0x04`) declarations.
- The C reader does not use `index_offset`; it scans every block.
- The C value-change writer does not deduplicate waves and does not support
  `FLAG_SEEKABLE`, `FLAG_TIME_ZLIB` or LZ4. It does not handle `SE_BITVEC`
  or `SE_ANALOG`.
- The C value-change reader decodes a `FLAG_STRUCT_CODEC` block only with a
  C codec registered under its id; it skips the block otherwise. The C library
  has no `core.bytesplit` or `core.derived` codec.
- The C transaction writer always uses row layout with `FLAG_TXN_DELTA`.
- The C hierarchy readers skip `H_ATTR2` records.
