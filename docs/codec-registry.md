# Codecs

A *codec* converts a stream's data into the payload of a data block and
back. The container (the writer and reader) owns everything around the
payload: block framing, compression of whole blocks, the index, the string
table and the type registry. Codecs are looked up by id in a registry, so
new encodings can be added without changing the container. Files that use
only the default codecs are byte-for-byte the same as files written before
codecs existed.

There are two codec families:

| Family | Python base class | C vtable | Block type |
|--------|-------------------|----------|------------|
| Value change | {class}`trlog.codec.VcCodec` | `trl_vc_codec_t` | `BLK_VC_DATA` |
| Transaction record | {class}`trlog.codec.TxnCodec` | `trl_txn_codec_t` | `BLK_TXN_DATA` |

The Python API is in {mod}`trlog.codec` (see {doc}`api/codec`). The C API is
in `c/include/trl/trl_codec.h`. To write your own codec, see
{doc}`guide/writing-a-codec`.

## Codec ids

A codec is identified by a reverse-DNS string such as
`com.acme.codec.fancy`. The canonical form, checked by
{func}`trlog.codec.canonical_codec_id`, is:

- at least two segments separated by `.`, with no empty segments;
- lowercase letters `a-z`, digits and `-` only (no `-` in the first segment);
- compared case-sensitively.

The prefix `org.fvutils.trlog.` is reserved for codecs that ship with
trlog. Use a domain you control for your own codecs.

In a file, a codec id is stored as a string-table id: in the payload prefix
of a codec-produced block, and in stream type v2 entries of the type
registry (see {doc}`guide/format`).

## Built-in codecs

| Id | Python constant | C macro | Family | Caps | Default |
|----|-----------------|---------|--------|------|---------|
| `org.fvutils.trlog.core.valuechange` | `CORE_VALUECHANGE` | `TRL_CODEC_CORE_VALUECHANGE` | value change | `RANDOM_ACCESS`, `LOSSLESS` | yes |
| `org.fvutils.trlog.core.record` | `CORE_RECORD` | `TRL_CODEC_CORE_RECORD` | record | `RANDOM_ACCESS`, `LOSSLESS` | yes |
| `org.fvutils.trlog.core.bytesplit` | `CORE_BYTESPLIT` | none | value change | `RANDOM_ACCESS`, `LOSSLESS` | no |
| `org.fvutils.trlog.core.derived` | `CORE_DERIVED` | none | value change | `NEEDS_INPUT_STREAMS`, `LOSSLESS` | no |
| `org.fvutils.trlog.ext.callreturn` | `trlog.ext.callreturn.CALLRETURN_CODEC_ID` | none | record | `NEEDS_INPUT_STREAMS`, `HAS_RAW_FALLBACK`, `LOSSLESS` | no |

All have version 1. The Python constants are importable from both `trlog`
and `trlog.codec`.

- **`core.valuechange`** writes the default value-change block: a time
  table, initial values and per-variable wave entries with XOR-delta + RLE
  for multi-bit 2-state signals. Registered in Python and C.
- **`core.record`** writes the default transaction block: row or column
  layout with delta-encoded ids and times. Registered in Python and C.
- **`core.bytesplit`** is an opt-in value-change codec for fixed-width
  values. Python only. See [core.bytesplit](#codec-bytesplit).
- **`core.derived`** describes derived (virtual) signals. Python only. It is
  registered so the id and its capabilities can be looked up; the data is
  written and read through `TrlWriter.add_derived_signal` and
  `TrlReader.read_derived`, not through the codec's block methods. See
  [core.derived](#codec-derived).
- **`ext.callreturn`** records call/return (call-stack) events. It lives in
  `trlog.ext.callreturn` and is registered only when that module is
  imported. Like `core.derived`, its registration is for lookup only; the
  module's own writer and reader classes do the encoding, using `BLK_EXT`,
  `BLK_AUX` and stream dependencies.

## Capabilities

Each codec declares a {class}`trlog.codec.Capability` bitset (`caps`). The
C equivalent is `trl_codec_caps_t`, with the same bit values.

| Bit | Python | C | Meaning |
|-----|--------|---|---------|
| `1 << 0` | `RANDOM_ACCESS` | `TRL_CAP_RANDOM_ACCESS` | Each block decodes on its own. |
| `1 << 1` | `NEEDS_INPUT_STREAMS` | `TRL_CAP_NEEDS_INPUT_STREAMS` | Decoding needs other streams first. |
| `1 << 2` | `HAS_RAW_FALLBACK` | `TRL_CAP_HAS_RAW_FALLBACK` | Data the codec cannot represent is also written in a generic form. |
| `1 << 3` | `LOSSLESS` | `TRL_CAP_LOSSLESS` | Decoding reproduces the written values exactly. |
| `1 << 4` | `MATERIALIZED` | `TRL_CAP_MATERIALIZED` | Values are stored, not computed at read time. |

The container does not act on these bits today. They describe the codec to
tools and to code that inspects the registry.

## The Python registry

The registry is a process-wide dictionary per family, keyed by codec id.
The built-in codecs register themselves when `trlog` is imported.

```python
from trlog import codec, lookup_vc_codec, CORE_BYTESPLIT

codec.registered_vc_codecs()
# ['org.fvutils.trlog.core.bytesplit', 'org.fvutils.trlog.core.derived',
#  'org.fvutils.trlog.core.valuechange']
codec.registered_txn_codecs()
# ['org.fvutils.trlog.core.record']

bs = lookup_vc_codec(CORE_BYTESPLIT)
bs.codec_id, bs.version   # ('org.fvutils.trlog.core.bytesplit', 1)
```

| Function | Purpose |
|----------|---------|
| {func}`~trlog.codec.register_vc_codec`, {func}`~trlog.codec.register_txn_codec` | Register a codec instance. Validates its id. A later registration with the same id replaces the earlier one. |
| {data}`~trlog.codec.vc_codec`, {data}`~trlog.codec.txn_codec` | Class decorators: set `codec_id`, `version` and `caps`, then register one instance of the class. |
| {func}`~trlog.codec.lookup_vc_codec`, {func}`~trlog.codec.lookup_txn_codec` | Return the codec for an id, or `None`. |
| {func}`~trlog.codec.registered_vc_codecs`, {func}`~trlog.codec.registered_txn_codecs` | List the registered ids. |

The registry holds codec *instances*. A codec keeps its per-stream state in
the object returned by `open_writer`, not on the instance.

## The C registry

```c
#include "trl/trl_codec.h"

int trl_register_vc_codec(const trl_vc_codec_t *codec);
int trl_register_txn_codec(const trl_txn_codec_t *codec);
const trl_vc_codec_t  *trl_lookup_vc_codec(const char *codec_id);
const trl_txn_codec_t *trl_lookup_txn_codec(const char *codec_id);
```

- The core codecs register themselves from a library constructor when
  `libtrl` is loaded.
- `trl_register_*_codec` returns 0 on success and non-zero if `codec` or
  its `codec_id` is `NULL`, or if the table is full (64 codecs per family).
  It does not check that the id is in canonical form.
- Registering an id that is already registered replaces the earlier entry.
- The registry stores the pointer. The vtable and its `codec_id` string
  must stay valid for as long as any writer or reader may use them; a
  `static const` vtable is the usual choice.
- The registry is not thread-safe. Register codecs during setup, before
  opening writers or readers.
- `trl_lookup_*_codec` returns `NULL` for an unknown or `NULL` id.

## Selecting a codec when writing

The default codecs are used unless you select another one.

**Python.** Pass the codec id to `TrlWriter.begin_vc_block`. The choice
applies to that block only.

```python
with w.begin_vc_block(0, codec=CORE_BYTESPLIT) as vc:
    vc.add_change(var_id, 0, 1.0)
```

An id that is not registered raises `ValueError`. Transaction blocks always
use `core.record`; there is no per-block selection for record codecs.

**C.** Call `trl_writer_set_vc_codec` on an open writer. The choice applies
to all following value-change blocks until it is changed. Passing `NULL`
returns to the default codec.

```c
int trl_writer_set_vc_codec(trl_writer_t *w, const char *codec_id);
```

The call returns non-zero if the id is not registered. It calls the codec's
`open_writer` immediately; `trl_writer_close` calls its `finalize` and
`close`.

## How readers find a codec

A block written by a non-default codec has `FLAG_STRUCT_CODEC` (`0x80`) set
in its block header, and its payload starts with the string-table id of the
codec id. The Python reader:

1. reads the prefix and looks the id up with `lookup_vc_codec`;
2. if found, calls the codec's `decode_block` with the payload;
3. if not found, skips the block. It does not raise an error.

Two `TrlReader` methods show which codecs a file uses without decoding it:

```python
r.vc_block_report()
# [{'codec': 'org.fvutils.trlog.core.bytesplit', 'available': True,
#   'start': 0, 'end': 990}]
r.missing_codecs()      # ids used in the file but not registered
# []
```

`codec` is `None` for blocks written by the default codec.

The C reader (`trl_iter_vc`) does the same: it decodes a codec-produced block
with the C codec registered under its id, and skips the block if there is none.

(codec-bytesplit)=
## core.bytesplit

`org.fvutils.trlog.core.bytesplit` stores each variable's values as byte
planes: byte 0 of every value, then byte 1 of every value, and so on (the
same transform as Parquet's BYTE_STREAM_SPLIT). It then zlib-compresses the
block. The high-order bytes of slowly varying real values and wide counters
change rarely, so they compress to long runs.

It supports `SE_REAL` (8-byte doubles) and `SE_2STATE` signals of any
width. Other encodings raise `ValueError` when the block is encoded. Select
it per block:

```python
import io
from trlog import (TrlReader, TrlWriter, ScopeType, SignalEncoding,
                   CORE_BYTESPLIT)

buf = io.BytesIO()
with TrlWriter(buf) as w:
    real_t = w.add_signal_type(SignalEncoding.SE_REAL, 0)
    with w.begin_hierarchy() as h:
        h.begin_scope(ScopeType.ST_MODULE, "top")
        vdd = h.add_var("vdd", real_t)
        h.end_scope()
    with w.begin_vc_block(0, codec=CORE_BYTESPLIT) as vc:
        for i in range(100):
            vc.add_change(vdd, i * 10, 1.0 + i * 0.001)

buf.seek(0)
with TrlReader(buf) as r:
    changes = [c for blk in r.iter_vc_blocks() for c in blk]
```

Block layout (block type `BLK_VC_DATA`, flags `FLAG_STRUCT_CODEC`):

```
uvarint codec_id_str        string-table id of the codec id
uvarint internal_flags      bit 0: body is zlib-compressed
body:
  uvarint start_time
  uvarint var_count
  var_count x:
    uvarint var_id
    u8      kind            0 = unsigned integer, 1 = real
    uvarint width           bytes per value = number of planes
    uvarint count
    count x uvarint         time deltas; the first from 0, then from the
                            previous change
    count * width bytes     byte planes of the little-endian values
```

The writer's `compress` argument controls `internal_flags` bit 0; the block
header never has `FLAG_COMPRESSED` set.

(codec-derived)=
## core.derived

A derived signal stores an expression over other signals instead of
per-change values. The reader evaluates the expression at every time any
input changes and reports a change when the result differs from the
previous one. Inputs are 0 before their first change. For the full Python
API (expression operators, storing computed values), see
{doc}`guide/py-derived`.

```python
import io
from trlog import TrlReader, TrlWriter, ScopeType, SignalEncoding, Input

buf = io.BytesIO()
w = TrlWriter(buf)
bit = w.add_signal_type(SignalEncoding.SE_2STATE, 1)
with w.begin_hierarchy() as h:
    h.begin_scope(ScopeType.ST_MODULE, "top")
    valid = h.add_var("valid", bit)
    ready = h.add_var("ready", bit)
    h.end_scope()
with w.begin_vc_block(0) as vc:
    vc.add_change(valid, 0, 0); vc.add_change(valid, 10, 1); vc.add_change(valid, 30, 0)
    vc.add_change(ready, 0, 0); vc.add_change(ready, 20, 1)
fire = w.add_derived_signal("fire", Input(0) & Input(1),
                            inputs=[valid, ready], bit_width=1)
w.close()

buf.seek(0)
with TrlReader(buf) as r:
    [(c.time, c.value) for c in r.read_derived(fire)]
    # [(0, 0), (20, 1), (30, 0)]
```

`Input(i)` refers to `inputs[i]`. Expressions support `&`, `|`, `^`, `~`
and the methods `lnot()`, `eq()`, `ne()`, `shl()`, `shr()`,
`reduce_and()`, `reduce_or()`, `slice_()` and `concat()`. A derived signal
can use other derived signals as inputs; the writer rejects a definition
that creates a cycle. With `materialized=True` (which also requires
`input_changes`), the writer also stores the computed values in an ordinary
value-change block, so readers that do not know about derived signals still
see them.

Derived var ids start at `0x40000000` so they do not collide with hierarchy
var ids. Each definition is stored, uncompressed, in a `BLK_AUX` block with
owner `0xFFFFFFFE` and key equal to the derived var id:

```
str_id  name
uvarint flags               bit 0: materialized
uvarint bit_width           result is masked to this width
uvarint input_count
input_count x uvarint var_id
uvarint bytecode_length
u8[bytecode_length] bytecode
```

The bytecode is a post-order stack program. Binary operators pop `b`, then
`a`, and push the result.

| Opcode | Name | Operands | Result |
|--------|------|----------|--------|
| `0x01` | `PUSH_INPUT` | `uvarint idx` | current value of input `idx` |
| `0x02` | `PUSH_CONST` | `uvarint value` | `value` |
| `0x03` | `NOT` | | `~a` |
| `0x04` | `AND` | | `a & b` |
| `0x05` | `OR` | | `a \| b` |
| `0x06` | `XOR` | | `a ^ b` |
| `0x07` | `LNOT` | | 1 if `a == 0`, else 0 |
| `0x08` | `EQ` | | 1 if `a == b`, else 0 |
| `0x09` | `NE` | | 1 if `a != b`, else 0 |
| `0x0A` | `SHL` | `uvarint k` | `a << k` |
| `0x0B` | `SHR` | `uvarint k` | `a >> k` |
| `0x0C` | `RED_AND` | `uvarint w` | 1 if the low `w` bits of `a` are all 1 |
| `0x0D` | `RED_OR` | `uvarint w` | 1 if any of the low `w` bits of `a` is 1 |
| `0x0E` | `SLICE` | `uvarint lo, uvarint w` | `(a >> lo) & mask(w)` |
| `0x0F` | `CONCAT` | `uvarint wb` | `(a << wb) \| (b & mask(wb))` |

The program must leave exactly one value on the stack. `Input(0)` alone
compiles to one `PUSH_INPUT` instruction, which makes the derived signal an
alias of its input.

## Python codecs under the C writer

`trlog._codec_bridge.register_native(codec)` registers a Python
{class}`~trlog.codec.VcCodec` with the C registry through ctypes, so the C
writer can call it after `trl_writer_set_vc_codec`. It requires `libtrl.so`
and is limited: write side only, one stream, and only unsigned 64-bit
values. See {doc}`guide/writing-a-codec`.

## Current limitations

- The Python writer passes `stream_id = 0` and `params = b""` to
  `open_writer`, and never calls `open_reader`; `decode_block` receives
  `state = None`.
- A stream type's codec (`TrlWriter.set_stream_codec`, read back with
  `TrlReader.stream_codec`) is declarative: readers decode each block by the
  codec ID the block carries, not by its stream type's codec.
- `core.record`'s C `decode_block` does nothing.
