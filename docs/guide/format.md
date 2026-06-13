# BLK_VC_DATA Wave Encoding  (format version 2.1)

## Block flags

Each `BLK_VC_DATA` block carries a one-byte flags field in its block header.

| Bit | Name | Meaning |
|-----|------|---------|
| 0 | `FLAG_COMPRESSED` | Whole-block payload is compressed (see `FLAG_COMPRESS_ALG`). |
| 1 | `FLAG_COMPRESS_ALG` | When `FLAG_COMPRESSED`: 0 = zlib, 1 = zstd. |
| 2 | `FLAG_TIME_ZLIB` | Time-table deltas are themselves zlib-compressed (sub-section; ignored when `FLAG_COMPRESSED` is set). |
| 3 | `FLAG_WAVE_LZ4` | Each wave entry in `wave_data` is individually LZ4-compressed (sub-section; ignored when `FLAG_COMPRESSED` is set). |
| 4 | `FLAG_WAVE_XOR_DELTA` | Wave data for `SE_2STATE` multi-bit signals uses XOR-delta + RLE encoding (see below). Always set by the current writer. |
| 5 | `FLAG_WAVE_ZLIB` | Each wave entry in `wave_data` is individually zlib-compressed (sub-section; ignored when `FLAG_COMPRESSED` is set). Mutually exclusive with `FLAG_WAVE_LZ4`. |
| 6 | `FLAG_SEEKABLE` | The last 8 bytes of the payload are a `u64` giving the byte offset of the position table within the payload, enabling O(1) seek to any signal without parsing the time table or init values. |

`FLAG_WAVE_XOR_DELTA` is an **encoding flag**, not a compression flag: it describes how values are represented, not whether bytes are compressed. It survives whole-block decompression and must be forwarded to the wave decoder.

## Payload layout

```
u64   start_time
u64   end_time
uvarint time_count
  [time_count × uvarint delta_time]       -- delta-encoded sorted unique timestamps
uvarint var_count
  [var_count × (uvarint var_id, init_value)]  -- initial value for each variable
u8    pos_entry_size                       -- 4 (u32 offsets) or 8 (u64 offsets)
  [var_count × u32|u64 wave_offset]       -- byte offset of each signal's wave entry
uvarint wave_data_size
  [wave_data_size bytes]                   -- concatenated wave entries (one per variable)
```

## Wave entry format

### SE_2STATE, bit_width == 1  (unchanged)

```
uvarint count
for each change:
    uvarint (tidx_delta << 2) | (value << 1) | 1
```

`tidx_delta` is the skip from `prev_tidx + 1` into the global time table.
`value` is 0 or 1.

### SE_2STATE, bit_width > 1  (XOR-delta + RLE, requires `FLAG_WAVE_XOR_DELTA`)

```
uvarint rle_group_count
for each group:
    uvarint (tidx_delta << 1) | 1    -- time-index delta; LSB=1 is a sentinel
    uvarint xor_delta                -- prev XOR current value, as unsigned integer
    uvarint repeat - 1               -- 0 = single change; N = N+1 identical changes
```

**Decoding a group** with `(tidx_delta = D, xor_delta = X, count = N)`:

- `prev` starts at 0 at the beginning of each signal's wave entry.
- For each of the `N` changes:  `tidx = prev_tidx + 1 + D`;  `value = prev XOR X`;  update `prev_tidx = tidx`, `prev = value`; emit `(times[tidx], value)`.

**Rationale**: XOR-delta makes toggle signals (constant `xor_delta`) and
counter-like signals (periodic `xor_delta` sequence) highly compressible.
RLE collapses runs of identical `(tidx_delta, xor_delta)` pairs — a signal
toggling at regular intervals encodes entirely as one or two groups regardless
of simulation length.

### SE_4STATE, SE_9STATE, SE_REAL, SE_STRING (unchanged)

These types continue to use the count-prefixed change-by-change encoding
described in the original format. See the source for per-type details.

## Compression interaction

When `FLAG_COMPRESSED` is set the entire payload above is compressed as a single
unit (zlib or zstd). After decompression, `FLAG_WAVE_XOR_DELTA` still applies.

When `FLAG_WAVE_LZ4` is set (without `FLAG_COMPRESSED`), each wave entry's raw
bytes are individually LZ4-compressed.  Each entry is prefixed with a flag byte:
`0x00` = raw bytes follow; `0x01` = `orig_size:uvarint compressed_len:uvarint
lz4_block`.

## Seekability footer

When `FLAG_SEEKABLE` is set, the **last 8 bytes** of the payload (after
decompression if `FLAG_COMPRESSED` is also set) are a little-endian `u64`
giving the byte offset from the start of the payload to the position table
(the `pos_entry_size` byte). This allows a reader to jump directly to the
position table without parsing the time table or initial values, then use the
position table to seek to any signal's wave data in O(1).

```
[... time table ... init values ...]
u8    pos_entry_size                ← seekability offset points here
  [var_count × u32|u64 wave_offset]
uvarint wave_data_size
  [wave_data_size bytes]
u64   seekability_offset            ← last 8 bytes of payload (FLAG_SEEKABLE)
```

## Scope-grouped blocks

Instead of writing a single `BLK_VC_DATA` block covering all signals over a
time window, a writer may emit one `BLK_VC_DATA` per **scope group** (signals
declared under the same hierarchy scope), each spanning the full simulation
time. Combined with `FLAG_SEEKABLE`, this allows a reader to decompress only
the scope of interest, rather than all signals.

Scope-grouped blocks use per-signal compression (`FLAG_WAVE_ZLIB` or
`FLAG_WAVE_LZ4`), not whole-block compression (`FLAG_COMPRESSED`), so that
the seekability footer remains useful.

## Recommended configurations

| Use case | Flags | Notes |
|----------|-------|-------|
| Maximum compression | `FLAG_COMPRESSED \| FLAG_WAVE_XOR_DELTA` | Whole-block zlib; not seekable. |
| Seekable, good compression | `FLAG_WAVE_ZLIB \| FLAG_WAVE_XOR_DELTA \| FLAG_SEEKABLE` | Per-signal zlib with seekability footer. |
| Seekable, fastest decode | `FLAG_WAVE_LZ4 \| FLAG_WAVE_XOR_DELTA \| FLAG_SEEKABLE` | Per-signal LZ4 with seekability footer. |
| Scope-grouped | Per-scope `BLK_VC_DATA` with `FLAG_WAVE_ZLIB \| FLAG_WAVE_XOR_DELTA \| FLAG_SEEKABLE` | Best for large multi-module traces. |

---

# BLK_TXN_DATA Delta Encoding  (format version 2.1)

## Block flags

| Bit | Name | Meaning |
|-----|------|---------|
| 0 | `FLAG_COMPRESSED` | Whole-block payload is compressed (see `FLAG_COMPRESS_ALG`). |
| 1 | `FLAG_COMPRESS_ALG` | When `FLAG_COMPRESSED`: 0 = zlib, 1 = zstd. |
| 2 | `FLAG_TXN_DELTA` | Transaction IDs and timestamps are delta-encoded with zigzag varint. |

## Legacy payload layout (no `FLAG_TXN_DELTA`)

```
u64   start_time
u64   end_time
uvarint record_count
  [record_count x record]
```

Each record starts with a `u8 tag` followed by tag-specific fields.
Numeric fields (`txn_id`, `start`, `end`, `parent`) use fixed-width
`u64` little-endian encoding.

## Delta payload layout (`FLAG_TXN_DELTA`)

The outer structure is identical:

```
u64   start_time
u64   end_time
uvarint record_count
  [record_count x record]
```

**Running state** is maintained across records:
- `prev_txn_id`: starts at 0; updated by every record that carries a txn_id.
- `prev_time`: starts at `start_time`; updated by time fields (see below).

### TR_FULL (tag 0x01)

```
u8       tag (0x01)
uvarint  stream_inst_id
uvarint  txn_type_id
svarint  txn_id - prev_txn_id        → update prev_txn_id = txn_id
svarint  start  - prev_time          → update prev_time = start
uvarint  end    - start              (duration, always >= 0)
                                     → update prev_time = end
uvarint  parent                      (absolute)
uvarint  attr_count
  [attr_count x (uvarint field_idx, field_type-encoded value)]
```

### TR_BEGIN (tag 0x02)

```
u8       tag (0x02)
uvarint  stream_inst_id
uvarint  txn_type_id
svarint  txn_id - prev_txn_id        → update prev_txn_id = txn_id
svarint  start  - prev_time          → update prev_time = start
uvarint  parent                      (absolute)
```

### TR_ATTR (tag 0x03)

```
u8       tag (0x03)
svarint  txn_id - prev_txn_id        → update prev_txn_id = txn_id
uvarint  attr_count
  [attr_count x (uvarint field_idx, u8 field_type, field_type-encoded value)]
```

### TR_END (tag 0x04)

```
u8       tag (0x04)
svarint  txn_id   - prev_txn_id      → update prev_txn_id = txn_id
svarint  end_time - prev_time        → update prev_time = end_time
```

### TR_LINK (tag 0x05)

```
u8       tag (0x05)
u8       link_type
svarint  src - prev_txn_id           → update prev_txn_id = src
uvarint  tgt                         (absolute — target can be any txn)
uvarint  label_str_id
```

### TR_META (tag 0x06)

```
u8       tag (0x06)
svarint  txn_id - prev_txn_id        → update prev_txn_id = txn_id
uvarint  key_str_id
uvarint  value_str_id
```

## Rationale

Sequential transaction IDs produce constant deltas (typically 1), which
encode as a single byte.  Timestamps within a block are roughly monotonic,
so their deltas are small positive numbers.  Duration encoding for TR_FULL
`end` exploits the fact that transaction lifetimes are typically much
smaller than absolute timestamps.

Combined with whole-block zlib, these constant/small-valued varint streams
compress almost to nothing — sequential workloads see **30-50x** improvement
over legacy encoding, and realistic mixed workloads see **~1.4x**.


## BLK_TXN_DATA Column Layout (`FLAG_TXN_COLUMN`)

### Overview

`FLAG_TXN_COLUMN` (0x08) switches the `BLK_TXN_DATA` payload from
row-oriented to column-oriented layout.  Instead of interleaving all
fields per record sequentially, column layout groups all values of each
field together into separate columns.  This dramatically improves zlib
compression for fixed-width numeric fields because the compressor sees
homogeneous byte streams with regular patterns.

### Block flags

| Bit | Flag | Meaning |
|-----|------|---------|
| 2 | `FLAG_TXN_DELTA` (0x04) | Delta + zigzag encoding for txn_id and time fields |
| 3 | `FLAG_TXN_COLUMN` (0x08) | Column-oriented payload layout |

Both flags can be set simultaneously (recommended).  The reader detects
column mode from `FLAG_TXN_COLUMN` and auto-dispatches to the column
decoder.

### Column payload format

When `FLAG_TXN_COLUMN` is set, the decompressed payload has this structure:

```
u64     start_time
u64     end_time
uvarint record_count
uvarint column_count

for each column:
    uvarint column_id
    uvarint byte_length
    [byte_length bytes of column data]
```

### Column IDs

| ID | Name | Encoding | Record types |
|----|------|----------|--------------|
| 0 | `COL_TAG` | 1 byte per record | All |
| 1 | `COL_STREAM_INST_ID` | uvarint | TR_FULL, TR_BEGIN |
| 2 | `COL_TXN_TYPE_ID` | uvarint | TR_FULL, TR_BEGIN |
| 3 | `COL_TXN_ID_DELTA` | svarint (delta) or u64 (absolute) | All |
| 4 | `COL_TIME_DELTA` | svarint (delta) or u64 (absolute) | TR_FULL, TR_BEGIN, TR_END |
| 5 | `COL_DURATION` | uvarint (end - start) | TR_FULL |
| 6 | `COL_PARENT` | uvarint | TR_FULL, TR_BEGIN |
| 7 | `COL_LINK_TYPE` | 1 byte | TR_LINK |
| 8 | `COL_LINK_TGT` | uvarint | TR_LINK |
| 9 | `COL_LINK_LABEL` | uvarint | TR_LINK |
| 10 | `COL_META_KEY` | uvarint | TR_META |
| 11 | `COL_META_VAL` | uvarint | TR_META |
| 12 | (TR_ATTR data) | length-prefixed blob | TR_ATTR |
| 0x80+N | `COL_ATTR_N` | field-type-specific | TR_FULL (schema-based) |

### Delta encoding interaction

When both `FLAG_TXN_COLUMN` and `FLAG_TXN_DELTA` are set:

- `COL_TXN_ID_DELTA` contains signed zigzag deltas from the previous
  record's txn_id.
- `COL_TIME_DELTA` contains signed zigzag deltas from the previous time
  reference (same state machine as row-oriented delta encoding).
- `COL_DURATION` contains unsigned varints (always non-negative).
- Attribute columns remain absolute (not delta-encoded).

Without `FLAG_TXN_DELTA`, txn_id and time columns contain absolute `u64` values.

### Attribute column encoding

| Field type | Column encoding |
|------------|----------------|
| `FT_U8`, `FT_I8`, `FT_BOOL` | 1 byte per entry |
| `FT_U16`, `FT_I16` | 2 bytes LE per entry |
| `FT_U32`, `FT_I32`, `FT_F32` | 4 bytes LE per entry |
| `FT_U64`, `FT_I64`, `FT_F64`, `FT_TIME` | 8 bytes LE per entry |
| `FT_ENUM` | uvarint per entry |

Variable-length field types (`FT_STRING`, `FT_BYTES`, `FT_BITVEC`) are not
supported in column mode.  The auto heuristic falls back to row layout for
schemas containing variable-length fields.

### Layout selection policy

The `column_layout` parameter accepts:

| Value | Behavior |
|-------|----------|
| `"auto"` (default) | Schema-based heuristic per block |
| `"column"` | Always use column layout |
| `"row"` | Always use row layout |
| `"adaptive"` | Encode both, compress both, emit the smaller one |

**Auto mode heuristic:**

1. If the block has fewer than 32 records, use row layout.
2. If all attribute field types for all txn_type_ids used in the block are
   fixed-width, use column layout.
3. If any attribute field type is variable-length, use row layout.

### Recommended configurations

| Workload | Configuration | Expected benefit |
|----------|---------------|-----------------|
| Fixed-width attrs, regular patterns | `column_layout="auto"` | 2-3x smaller than row+zlib |
| Variable-length attrs (strings) | `column_layout="auto"` | Falls back to row automatically |
| Mixed/uncertain | `column_layout="adaptive"` | Always picks the better layout |
| Maximum compatibility | `column_layout="row"` | Legacy behavior |

---

# Pluggable codecs & transforms  (format version 2.1+)

These additive changes let a stream's payload be produced by a *codec* behind a
stable SPI without breaking older readers (see `docs/design/pluggable-codecs.md`
and `docs/codec-registry.md`). Files that use no codec/metadata features are
**byte-for-byte identical** to pre-codec files.

## Type-registry codec identity (`TRL_TYPE_TAG_STREAM_V2`, 0x05)

A new, **length-prefixed** type-registry entry carries per-stream-type codec
identity and metadata. It is emitted *only* when a stream type selects a
non-default codec or carries transparent metadata / dependencies; otherwise the
legacy fixed-layout stream-decl entry (tag `0x04`) is written unchanged.

```
tag(0x05) uvarint(body_len) body:
  uvarint stream_id
  uvarint name_str_id
  uvarint kind_str_id
  uvarint default_txn_type
  uvarint codec_id_str        // string-table id of reverse-DNS codec id (0 = legacy)
  uvarint codec_version
  uvarint param_len  params[param_len]      // codec-private, opaque to core
  typed-attr-list metadata                  // transparent metadata (see below)
  uvarint dep_count  dep_count × uvarint     // input-stream dependency catalog
```

**Convention:** every type-registry tag `>= 0x05` is length-prefixed, so a reader
that does not recognise a tag skips the entry by its declared length and keeps
parsing — forward-compatible evolution.

## Block flag `FLAG_STRUCT_CODEC` (0x80)

Set on a data block whose payload was produced by the stream-type's structural
codec rather than the legacy built-in path. Value-change blocks that set it are
**self-identifying**: the payload begins with `uvarint codec_id_str` (interned),
so the reader resolves and dispatches the codec, and a reader lacking the codec
skips the block by length (the 10-byte header `u64` length stays authoritative).

## Transparent metadata (typed)

A small **closed** value-type set readable *without* the codec:
`bool, i64, u64, f64, string` + homogeneous arrays (`AttrType`, `AT_ARRAY` flag
OR'd with the element type; scalar tags `0x05..0x3F` and `0x80+` reserved). A
typed attr is `uvarint key_str_id, u8 type, value-by-type` (string values are
string-table ids on the wire). It attaches at three scopes:

- **trace-global** — a skippable `BLK_META` (0x04) block; its offset is recorded
  in the index under sentinel `0x03`.
- **stream-type** — in the `TRL_TYPE_TAG_STREAM_V2` entry above.
- **stream-instance** — the `H_ATTR2` (0x06) hierarchy tag (legacy `H_ATTR`
  decodes as a `string`).

Profiles (`trlog.profile`) are a transparent array-of-string of reverse-DNS ids.

## Opaque keyed metadata (`BLK_AUX`, 0x12)

Opaque bulk metadata is a non-temporal stream: the same self-delimiting framing
and byte compression as data blocks, addressed by **key** instead of time and
recorded in a parallel **key index** in the index block (sentinel `0x04`):
`(owner_stream_id, key, ordinal, offset, length)`. A reader enumerates aux
blocks by key and skips their opaque bytes without the producing codec.

## `core.bytesplit` transform

Byte-plane split (Parquet BYTE_STREAM_SPLIT) for fixed-width REAL / wide-vector
signals, composed under byte compression. See `docs/codec-registry.md` for the
block layout. Opt-in via `writer.begin_vc_block(t, codec=CORE_BYTESPLIT)`.

## `core.derived` virtual signals

Derived (virtual) signals store an **expression over other signals**, not
per-event data. Their definitions live in opaque keyed (`BLK_AUX`) blocks under
a reserved owner, keyed by the derived var id; with `materialized=True` the
computed changes are additionally written as an ordinary value-change block. See
`docs/codec-registry.md` for the definition layout and the expression bytecode.
