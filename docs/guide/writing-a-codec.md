# Writing a Codec

This guide shows how to add a value-change codec, first in Python and then
in C. Read {doc}`../codec-registry` first for the registry, codec ids and
the built-in codecs.

The example codec, `com.example.codec.delta`, stores each variable's
changes as pairs of unsigned varints: the time since the previous change and
the value. It handles unsigned integer values only. It is short, not
efficient; the built-in `core.valuechange` codec is usually smaller.

## What the container expects

When a value-change block is written with a non-default codec:

- The block is a normal `BLK_VC_DATA` block (type `0x10`) with
  `FLAG_STRUCT_CODEC` (`0x80`) set in the block header.
- The payload starts with `uvarint codec_id_str`, the string-table id of the
  codec id. The reader uses it to find the codec, and a reader without the
  codec uses it to report the missing id before skipping the block.
- Everything after the prefix belongs to the codec. It must contain
  whatever the decoder needs, including the block start time if times are
  stored as deltas: the decoder is not given the block's index entry.
- The codec returns the complete block, header included. The writer
  appends it to the file as is and records `(start_time, end_time, offset)`
  in the index. The writer does not compress codec blocks; compress inside
  the payload if you need to (`Store.compress` uses zlib).

See {doc}`format` for the block header and flags.

## A Python codec

### Block accumulator and state

`TrlWriter.begin_vc_block(start_time, codec=...)` calls the codec's
`new_block(store, state, start_time)` and forwards the block context's
`add_change` and `set_initial` calls to the object it returns. That object
also needs `start_time` and `end_time` attributes.

```python
import struct

from trlog import (
    BlockType, BLOCK_HEADER_SIZE, Capability, Store, VcChange, VcCodec,
    vc_codec,
)
from trlog._types import FLAG_STRUCT_CODEC   # not re-exported by trlog

DELTA_ID = "com.example.codec.delta"


class DeltaBlock:
    """Collects the changes of one block."""

    def __init__(self, start_time):
        self.start_time = start_time
        self.end_time = start_time
        self.changes = {}            # var_id -> [(time, value), ...]

    def add_change(self, var_id, time, value):
        self.changes.setdefault(var_id, []).append((time, value))
        self.end_time = max(self.end_time, time)

    def set_initial(self, var_id, value):
        self.changes.setdefault(var_id, []).insert(0, (self.start_time, value))


class DeltaState:
    """Per-stream writer state returned by open_writer."""

    def __init__(self):
        self.blk = None
```

### The codec class

Subclass {class}`trlog.codec.VcCodec` and register it with the
{data}`trlog.codec.vc_codec` decorator. The decorator sets `codec_id`,
`version` and `caps`, creates one instance and registers it.

```python
@vc_codec(DELTA_ID, version=1, caps=Capability.LOSSLESS)
class DeltaCodec(VcCodec):
    """Stores each variable's changes as (time delta, value) uvarints."""

    def open_writer(self, store, stream_id, params):
        return DeltaState()

    def open_reader(self, store, stream_id, params):
        return None

    def new_block(self, store, state, start_time):
        state.blk = DeltaBlock(start_time)
        return state.blk

    def encode_block(self, state, store):
        blk = state.blk
        out = bytearray()
        out += Store.encode_uvarint(store.intern(self.codec_id))  # prefix
        out += Store.encode_uvarint(blk.start_time)
        out += Store.encode_uvarint(len(blk.changes))
        for var_id in sorted(blk.changes):
            changes = blk.changes[var_id]
            out += Store.encode_uvarint(var_id)
            out += Store.encode_uvarint(len(changes))
            prev = blk.start_time
            for t, v in changes:
                out += Store.encode_uvarint(t - prev)
                out += Store.encode_uvarint(v)
                prev = t
        payload = bytes(out)
        header = struct.pack("<BBQ", BlockType.BLK_VC_DATA, FLAG_STRUCT_CODEC,
                             BLOCK_HEADER_SIZE + len(payload))
        return header + payload, blk.start_time, blk.end_time

    def decode_block(self, state, store, payload, flags, emit):
        _cid, o = Store.decode_uvarint(payload, 0)   # skip the prefix
        start, o = Store.decode_uvarint(payload, o)
        n_vars, o = Store.decode_uvarint(payload, o)
        for _ in range(n_vars):
            var_id, o = Store.decode_uvarint(payload, o)
            count, o = Store.decode_uvarint(payload, o)
            t = start
            for _ in range(count):
                dt, o = Store.decode_uvarint(payload, o)
                v, o = Store.decode_uvarint(payload, o)
                t += dt
                emit(VcChange(var_id=var_id, time=t, value=v))
```

What each method does:

| Method | Called by | Notes |
|--------|-----------|-------|
| `open_writer(store, stream_id, params)` | the first `begin_vc_block` that selects the codec; the state is reused for later blocks | Returns your state object. The Python writer passes `stream_id = 0` and `params = b""`. |
| `new_block(store, state, start_time)` | `begin_vc_block` | Not part of the abstract base class, but required by the writer. |
| `encode_block(state, store)` | the block context's `flush()` or `__exit__` | Returns `(block_bytes, start_time, end_time)`. `store.intern()` works here. |
| `decode_block(state, store, payload, flags, emit)` | `TrlReader.iter_vc_blocks()`, `read_signal()` | `payload` excludes the 10-byte header and still starts with the prefix. `state` is `None`. Call `emit` once per {class}`~trlog.VcChange`. |
| `finalize(state, store)` then `close(state)` | `TrlWriter.close()`, once for each codec the writer opened | `finalize` runs before the index and footer are written. The defaults do nothing. |
| `open_reader`, `input_streams` | not called for per-block codecs today | Implement `open_reader` (it is abstract); `input_streams` has a working default. |

`Store` gives the codec container services: `intern`, the varint helpers
`encode_uvarint` / `decode_uvarint` / `encode_svarint` / `decode_svarint`,
`compress` / `decompress` (zlib) and `codec_ctx` for state shared across a
trace. Use these rather than writer internals. On the read path
`store.intern` raises `RuntimeError`; the reader resolves the prefix itself.

### Using the codec

Importing the module that defines the class registers the codec. Then
select it per block:

```python
import io
from trlog import TrlReader, TrlWriter, ScopeType, SignalEncoding

buf = io.BytesIO()
with TrlWriter(buf) as w:
    u8 = w.add_signal_type(SignalEncoding.SE_2STATE, 8)
    with w.begin_hierarchy() as h:
        h.begin_scope(ScopeType.ST_MODULE, "top")
        count = h.add_var("count", u8)
        h.end_scope()
    with w.begin_vc_block(100, codec=DELTA_ID) as vc:
        for i in range(5):
            vc.add_change(count, 100 + 10 * i, i)

buf.seek(0)
with TrlReader(buf) as r:
    print(r.vc_block_report())
    # [{'codec': 'com.example.codec.delta', 'available': True,
    #   'start': 100, 'end': 140}]
    for blk in r.iter_vc_blocks():
        print([(c.time, c.value) for c in blk])
    # [(100, 0), (110, 1), (120, 2), (130, 3), (140, 4)]
```

A reader in a process that has not registered the codec skips the block.
`TrlReader.missing_codecs()` then returns `['com.example.codec.delta']`, and
blocks written by other codecs in the same file are still read.

### Checklist

- Choose an id under a domain you control; do not use `org.fvutils.trlog.`.
- Put everything the decoder needs in the payload. The decoder gets the
  payload and the flags byte, nothing else from the block.
- Make the payload self-delimiting or rely on its length; the container
  passes exactly the bytes after the header.
- Bump `version` when the payload layout changes. The version is not yet
  written to value-change blocks, so a decoder that must read several
  layouts needs its own version field in the payload.
- Test the round trip and the skip path: write with the codec, read with it,
  then remove it from the registry and read again.

## A C codec

A C codec is a `trl_vc_codec_t` vtable registered with
`trl_register_vc_codec`. The C writer calls it after
`trl_writer_set_vc_codec`.

```c
typedef struct trl_vc_codec {
    const char *codec_id;
    uint16_t    version;
    uint32_t    caps;
    void *(*open_writer)(trl_store *st, uint32_t stream_id,
                         const void *params, size_t param_len);
    void *(*open_reader)(trl_store *st, uint32_t stream_id,
                         const void *params, size_t param_len);
    void  (*close)(void *self);
    int   (*finalize)(void *self, trl_store *st);
    int   (*encode_block)(void *self, trl_store *st, trl_blkout *out);
    int   (*decode_block)(void *self, trl_store *st,
                          const uint8_t *payload, size_t len, uint8_t flags,
                          trl_vc_cb emit, void *user);
    int   (*input_streams)(void *self, trl_store *st,
                           uint32_t **out_ids, size_t *n);
} trl_vc_codec_t;
```

`self` is the pointer `open_writer` returned. The C writer calls:

- `open_writer` from `trl_writer_set_vc_codec` (and `close` on the codec it
  replaces);
- `encode_block` from every `trl_vc_flush`;
- `finalize` and then `close` from `trl_writer_close`.

`open_writer`, `finalize` and `close` may be `NULL`; the writer then skips
them (`self` is `NULL` without `open_writer`). `encode_block` and
`decode_block` are required.

Inside `encode_block`, read the block's changes with the accessor functions
and append the complete block (header and payload) to `out`:

| Function | Purpose |
|----------|---------|
| `trl_store_vc_change_count(st)` | Number of changes in the block. |
| `trl_store_vc_start_time(st)`, `trl_store_vc_end_time(st)` | Block time range. |
| `trl_store_vc_change_at(st, i, &var_id, &time, &kind, &u64val)` | The `i`-th change, grouped by variable. `kind` is a `trl_vc_change_kind_t`; only `TRL_VC_KIND_U64` values are returned in `u64val`. |
| `trl_store_intern(st, s)` | Intern a string; returns its id. |
| `trl_blkout_append(out, data, len)` | Append bytes to the output block. Returns 0 on success. |

The C version of the example codec writes a flat list of
`(var_id, time delta, value)` triples after the prefix:

```c
#include <stdint.h>
#include "trl/trl.h"
#include "trl/trl_codec.h"

#define DELTA_ID "com.example.codec.delta"

static size_t put_uvarint(uint8_t *p, uint64_t v) {
    size_t n = 0;
    while (v >= 0x80) { p[n++] = (uint8_t)(v | 0x80); v >>= 7; }
    p[n++] = (uint8_t)v;
    return n;
}

static void *delta_open(trl_store *st, uint32_t stream_id,
                        const void *params, size_t param_len) {
    (void)st; (void)stream_id; (void)params; (void)param_len;
    return NULL;                      /* no per-stream state */
}
static void delta_close(void *self) { (void)self; }
static int  delta_finalize(void *self, trl_store *st) {
    (void)self; (void)st; return 0;
}

/* Append one complete BLK_VC_DATA block to `out`. */
static int delta_encode(void *self, trl_store *st, trl_blkout *out) {
    (void)self;
    uint8_t payload[4096];
    size_t n = 0;
    size_t count = trl_store_vc_change_count(st);
    uint64_t start = trl_store_vc_start_time(st), prev = start;
    uint32_t last_var = UINT32_MAX;

    n += put_uvarint(payload + n, trl_store_intern(st, DELTA_ID));
    n += put_uvarint(payload + n, start);
    n += put_uvarint(payload + n, count);
    for (size_t i = 0; i < count; ++i) {
        uint32_t var_id, kind; uint64_t t, v;
        if (trl_store_vc_change_at(st, i, &var_id, &t, &kind, &v) != 0) return -1;
        if (kind != TRL_VC_KIND_U64) return -1;
        if (var_id != last_var) { prev = start; last_var = var_id; }
        n += put_uvarint(payload + n, var_id);
        n += put_uvarint(payload + n, t - prev);
        n += put_uvarint(payload + n, v);
        prev = t;
        if (n > sizeof(payload) - 32) return -1;
    }

    uint8_t hdr[10];
    uint64_t len = sizeof(hdr) + n;
    hdr[0] = 0x10;                    /* BLK_VC_DATA */
    hdr[1] = TRL_FLAG_STRUCT_CODEC;
    for (int i = 0; i < 8; ++i) hdr[2 + i] = (uint8_t)(len >> (8 * i));
    if (trl_blkout_append(out, hdr, sizeof(hdr)) != 0) return -1;
    return trl_blkout_append(out, payload, n);
}

static int delta_decode(void *self, trl_store *st, const uint8_t *payload,
                        size_t len, uint8_t flags, trl_vc_cb emit, void *user) {
    (void)self; (void)st; (void)payload; (void)len; (void)flags;
    (void)emit; (void)user;
    return 0;                         /* the C reader does not call this yet */
}

static int delta_inputs(void *self, trl_store *st, uint32_t **ids, size_t *n) {
    (void)self; (void)st;
    *ids = NULL; *n = 0;
    return 0;
}

static const trl_vc_codec_t DELTA_CODEC = {
    .codec_id      = DELTA_ID,
    .version       = 1,
    .caps          = TRL_CAP_LOSSLESS,
    .open_writer   = delta_open,
    .open_reader   = delta_open,
    .close         = delta_close,
    .finalize      = delta_finalize,
    .encode_block  = delta_encode,
    .decode_block  = delta_decode,
    .input_streams = delta_inputs,
};
```

Changes come grouped by variable, in time order within each variable, so
the encoder restarts the time delta at the block start for each variable.

Register the codec, select it, and write as usual:

```c
trl_register_vc_codec(&DELTA_CODEC);

trl_writer_t *w = trl_writer_open("out.trl", -9, TRL_COMPRESS_NONE);
/* ... signal types and hierarchy ... */
trl_writer_set_vc_codec(w, DELTA_ID);
trl_vc_begin(w, 100);
trl_vc_change_u64(w, var_id, 100, 0);
trl_vc_flush(w);                      /* calls delta_encode */
trl_writer_close(w);                  /* calls delta_finalize, delta_close */
```

The Python reader can open the resulting file. Without a Python codec
registered under the same id it reports the id in `missing_codecs()` and
skips the block. The C reader (`trl_iter_vc`) decodes the block with the C
codec registered under its id, and skips it when none is registered.

The C library does not include the built-in `core.bytesplit` or
`core.derived` codecs.

## Running a Python codec under the C writer

`trlog._codec_bridge.register_native(codec)` builds a `trl_vc_codec_t` from
a Python {class}`~trlog.codec.VcCodec` with ctypes and registers it with the
C registry. After `trl_writer_set_vc_codec(w, codec_id)`, each
`trl_vc_flush` calls the codec's `new_block`, fills it with the C block's
changes, and appends the result of `encode_block` to the C block buffer.

The bridge needs `libtrl.so` (`trlog._codec_bridge.AVAILABLE` is False
without it) and has these limits:

- write side only; the bridged `decode_block` does nothing;
- one stream at a time;
- only `U64` values are passed through.

The C writer writes the buffer as the block, so a bridged codec returns the
complete block (header and payload), as in the Python example above; the
file then reads back with {class}`~trlog.TrlReader` while the codec is
registered. Keep the returned
`NativeBridge` object alive while the writer can call the codec, and call
`NativeBridge.raise_pending()` after a C call fails to re-raise any
exception the codec raised.
