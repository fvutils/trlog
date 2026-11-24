# C library

`libtrl` is the C implementation of the trlog format. It writes and reads
`.trl` files that are interchangeable with the Python package, and it is
the native layer under the SystemVerilog API ({doc}`systemverilog`), the
VPI dumper ({doc}`vpi`) and the optional C scan engine of `trlog.query`.
The full reference is in {doc}`../api/c`.

The public headers are in `c/include/trl/`:

| Header | Purpose |
|---|---|
| `trl/trl.h` | core writer and reader: strings, types, hierarchy, value-change and transaction blocks |
| `trl/trl_rec.h` | recording session: named types and streams, transaction ids and block management handled for you |
| `trl/trl_query.h` | reading session: streams, schemas, and a cursor over joined transactions |
| `trl/trl_codec.h` | codec registry and the storage SPI that codecs use |
| `trl/trl_qscan.h` | scan engine used by `trlog.query`; not meant for direct use |

Most programs that record transactions only need `trl_rec.h` to write and
`trl_query.h` to read. Use `trl.h` directly for value-change (waveform)
data, or when you need control over ids and block boundaries.

## Building

The top-level `CMakeLists.txt` builds the C library (`c/`) and the VPI
dumper (`vpi/`). It needs CMake 3.14 or later, a C11 compiler, zlib and
liblz4. The VPI part also needs `vpi_user.h`; it uses the copy in
`packages/verilator` when present, otherwise a system copy.

```bash
cmake -S . -B build
cmake --build build
ctest --test-dir build/c        # C unit tests
```

The build produces:

| File | Contents |
|---|---|
| `build/c/libtrl.so` | the core library: every function in the headers above |
| `build/c/libtrl_sv.so` | DPI-C glue for the SystemVerilog package; links `libtrl` |
| `build/vpi/libtrlog_vpi.so` | VPI value-change dumper; links `libtrl` |

`cmake --install build --prefix <dir>` installs the libraries to
`<dir>/lib` and the headers to `<dir>/include/trl` (plus
`<dir>/include/trlog_vpi.h`).

The Python package loads `libtrl.so` with `ctypes` when it can find it.
Set `TRL_LIBRARY_PATH` to the full path of the library file to choose one;
otherwise the query engine also looks in `build/c/` of the checkout.

## Compiling and linking

Add `c/include` (or the installed `include`) to the include path, and link
`-ltrl`. Set an rpath, or `LD_LIBRARY_PATH`, so the loader finds the
library at run time:

```bash
cc -std=c11 -Ic/include app.c -Lbuild/c -ltrl -Wl,-rpath,$PWD/build/c -o app
```

All the headers have `extern "C"` guards, so C++ code includes them
directly.

Functions that can fail return a `trl_status_t` (`TRL_OK` is 0), or a
handle or id that is `NULL` or 0 on failure. The `trl_rec.h` functions also
store a message that `trl_last_error()` returns.

## Recording transactions with `trl_rec.h`

A recording session is a *db*: one output file, with the types, streams and
open transactions that go into it. You declare transaction types and
streams by name, then record transactions on a stream. The session assigns
transaction ids, writes the stream declarations, and buffers each stream's
records into its own block, which it writes when the buffer reaches
`block_bytes` or when the db closes.

```c
#include <stdio.h>
#include "trl/trl_rec.h"

int main(void) {
    trl_db_cfg_t cfg = {0};
    cfg.path          = "bus.trl";
    cfg.timescale_exp = -9;               /* 1 ns */
    cfg.compress      = TRL_COMPRESS_ZLIB;

    trl_db_t *db = trl_db_open("main", &cfg);
    if (!db) {
        fprintf(stderr, "open failed: %s\n", trl_last_error());
        return 1;
    }

    /* A transaction type with three fields. */
    trl_rtype_t *rd = trl_type_new(db, "axi_rd", NULL);
    int32_t f_addr = trl_type_add_field(rd, "addr", TRL_FT_U64, 0, NULL);
    int32_t f_len  = trl_type_add_field(rd, "len",  TRL_FT_U8,  0, NULL);
    int32_t f_resp = trl_type_add_field(rd, "resp", TRL_FT_STRING, 0, NULL);
    trl_type_seal(rd);

    /* Streams: "env" with "env.axi" under it. */
    int enabled = 0;
    trl_stream_t *env = trl_stream_new(db, NULL, "env", NULL, NULL, NULL);
    trl_stream_t *axi = trl_stream_new(db, env, "axi", NULL, rd, &enabled);

    if (enabled) {
        /* One call per complete transaction. */
        uint64_t vals[3];
        vals[f_addr] = 0x1000;
        vals[f_len]  = 4;
        vals[f_resp] = trl_db_intern(db, "OKAY");   /* STRING: a string id */
        uint64_t id1 = trl_rec_full(axi, rd, 10, 25, 0, vals, 3);

        /* An open transaction, filled in while it runs. */
        trl_txn_t *t = trl_rec_begin(axi, rd, 30, id1);  /* parent = id1 */
        trl_rec_set_u64(t, (uint32_t)f_addr, 0x2000);
        trl_rec_set_u64(t, (uint32_t)f_len, 8);
        trl_rec_set_str(t, (uint32_t)f_resp, "SLVERR");
        trl_rec_meta(t, trl_db_intern(db, TRL_WATTR_ORIGIN_COMPONENT),
                        trl_db_intern(db, "env.seq0"));
        trl_rec_link(t, id1, 2 /* LT_RELATED */, trl_db_intern(db, "retry_of"));
        trl_rec_end(t, 60);
    }

    if (trl_db_close(db) != TRL_OK) {
        fprintf(stderr, "close failed\n");
        return 1;
    }
    return 0;
}
```

### The db

`trl_db_open(name, cfg)` creates the file at `cfg->path` and registers the
db under `name` in a process-wide table, so other code in the same process
(for example the DPI layer and a VPI library) can get the same db with
`trl_db_find(name)`. The name must not already be open. `trl_db_close`
writes all buffered blocks, the stream declarations, the string table, the
type registry and the index, then frees every type, stream and transaction
handle of that db.

`cfg->stream_include` and `cfg->stream_exclude` are comma-separated glob
lists (`fnmatch` syntax) matched against a stream's dotted path. A stream is
enabled when it matches the include list (or the list is `NULL`), does not
match the exclude list, and its parent is enabled. `trl_stream_new` reports
the result through its `enabled` argument. Records sent to a disabled
stream are dropped: `trl_rec_full` returns 0, and `trl_rec_begin` returns a
handle with id 0 on which the set, link, meta and end calls do nothing.

### Types

`trl_type_new` creates an empty type. Add fields, then call
`trl_type_seal`, which registers the schema in the file. A sealed type
accepts no more fields.

- `trl_type_add_field` adds one field and returns its index.
- `trl_type_add_struct` copies the fields of another sealed type, named
  `name.sub`, and returns the index of the first one.
- `trl_type_add_array` adds `n` fields named `name[0]` .. `name[n-1]` and
  returns the index of the first one.

Types are flat: a struct or array member is an ordinary field with a dotted
or indexed name. Passing a stream as the `scope` argument of `trl_type_new`
names the type `<stream path>::<name>`.

A stream created without a default type can use an *ad-hoc* type, from
`trl_stream_adhoc_type`. Fields are added to it on first use with
`trl_type_field_get_or_add`, which returns the existing index when the name
is already there with the same type and width. Each new field registers a
new schema version in the file; readers resolve the name to the newest one.

### Recording

`trl_rec_full` records a complete transaction in one call. `vals[i]` is the
value of field `i`, as raw 64 bits:

- integer, `BOOL`, `TIME` and `ENUM` fields: the value;
- `F64`: the bits of a `double`; `F32`: the bits of a `float` in the low
  32 bits;
- `STRING`: a string id from `trl_db_intern`.

- `BITVEC` and `LOGICVEC` fields up to 64 bits wide: the value in the low
  bits (a `LOGICVEC` gets no X or Z bits).

Wider vectors and `BYTES` fields cannot be passed this way. They are left
out of the record and `trl_last_error()` names the field; use the
begin/set/end form for them.

`trl_rec_begin` opens a transaction and returns a handle. The `trl_rec_set_*`
calls each record one field value, `trl_rec_meta` adds a key/value string
pair, `trl_rec_link` adds a link to another transaction, and `trl_rec_end`
closes the transaction and releases the handle. Every call is written to
the stream's buffer as it is made, so a transaction can stay open across
block flushes; readers join the pieces back together by id.

Transaction ids start at 1 and are unique within the db. A `parent` of 0
means no parent. Link types are the values of the Python `LinkType`
enum: 0 cause/effect, 1 parent/child, 2 related, 0xFF custom.

## Reading transactions with `trl_query.h`

`trl_rdb_open(name, path)` opens a file and loads its stream list and
transaction schemas. A cursor then returns the selected transactions, with
the `TR_BEGIN`, `TR_ATTR` and `TR_END` records of each one joined into a
single `trl_txn_hdr_t`, in order of start time and then id.

```c
#include <inttypes.h>
#include <stdio.h>
#include "trl/trl_query.h"

int main(void) {
    trl_rdb_t *db = trl_rdb_open("in", "bus.trl");
    if (!db) {
        fprintf(stderr, "cannot open bus.trl\n");
        return 1;
    }

    for (uint32_t i = 0; i < trl_rdb_num_streams(db); ++i)
        printf("stream %u: %s\n", trl_rstream_inst_id(trl_rdb_stream_at(db, i)),
               trl_rstream_path(trl_rdb_stream_at(db, i)));

    trl_rstream_t *axi = trl_rdb_stream_find(db, "env.axi");
    trl_cursor_t *c = trl_cursor_new(db, axi, 0, UINT64_MAX);
    trl_txn_hdr_t h;
    while (trl_cursor_next(c, &h)) {
        const trl_rschema_t *s = trl_rdb_schema_by_id(db, h.txn_type_id);
        printf("txn %" PRIu64 " %s [%" PRIu64 ", %" PRIu64 "] parent=%" PRIu64 "\n",
               h.txn_id, trl_rschema_name(s), h.start, h.end, h.parent);
        for (uint32_t a = 0; a < h.attr_count; ++a) {
            const trl_attr_t *at = &h.attrs[a];
            const char *name = trl_rschema_field_name(s, at->field_idx);
            if (at->field_type == TRL_FT_STRING)
                printf("  %s = %s\n", name, at->value.str);
            else
                printf("  %s = 0x%" PRIx64 "\n", name, at->value.u64);
        }
        for (uint32_t m = 0; m < h.meta_count; ++m)
            printf("  meta %s = %s\n",
                   trl_rdb_string_lookup(db, h.metas[m].key_sid),
                   trl_rdb_string_lookup(db, h.metas[m].val_sid));
        for (uint32_t l = 0; l < h.link_count; ++l)
            printf("  link type %u -> %" PRIu64 " (%s)\n", h.links[l].link_type,
                   h.links[l].target, trl_rdb_string_lookup(db, h.links[l].label_sid));
    }
    trl_cursor_free(c);
    trl_rdb_close(db);
    return 0;
}
```

Run on the file from the recording example, it prints:

```text
stream 2: env.axi
stream 1: env
txn 1 axi_rd [10, 25] parent=0
  addr = 0x1000
  len = 0x4
  resp = OKAY
txn 2 axi_rd [30, 60] parent=1
  addr = 0x2000
  len = 0x8
  resp = SLVERR
  meta trlog.origin.component = env.seq0
  link type 2 -> 1 (retry_of)
```

Points to note:

- `trl_cursor_new(db, NULL, ...)` selects every stream. Call
  `trl_cursor_add_stream` before the first `trl_cursor_next` to select
  more than one specific stream.
- The time window keeps a transaction when `start < t_to` and it has not
  ended before `t_from` (`end >= t_from`, or still open). Pass
  `0, UINT64_MAX` for everything.
- The cursor builds its whole result on the first `trl_cursor_next` call.
  The pointers in a `trl_txn_hdr_t` stay valid until `trl_cursor_free`.
- A transaction with no `TR_END` in the file has `end == UINT64_MAX`.
- Attribute values use the `trl_attr_t` layout from `trl.h`. `STRING`
  values come back as `value.str`; meta keys and values and link labels
  are string ids, resolved with `trl_rdb_string_lookup`.
- A link is attached to its source transaction only.
- Look up a transaction's type with `trl_rdb_schema_by_id(db,
  h.txn_type_id)`. The stream's declared default type is not stored in the
  file.

## Low-level writing and reading with `trl.h`

`trl.h` is the block-level API that `trl_rec.h` is built on. You manage
string ids, type ids, variable and stream ids, and block boundaries
yourself. The order of calls on a writer is:

1. `trl_writer_open`.
2. Register strings (`trl_intern`), signal types (`trl_add_signal_type`)
   and transaction schemas (`trl_add_txn_schema`) as needed. They are kept
   in memory and written at close.
3. Declare a hierarchy: `trl_begin_hierarchy`, then nested
   `trl_hier_begin_scope` / `trl_hier_end_scope` pairs, with
   `trl_hier_add_var` for signals and `trl_hier_add_stream` for transaction
   streams, then `trl_flush_hierarchy`.
4. Write data blocks: `trl_vc_begin`, `trl_vc_change_*` calls,
   `trl_vc_flush` for value changes; `trl_txn_begin_block` (or
   `trl_txn_begin_block_stream`), `trl_txn_full` / `trl_txn_meta` calls,
   `trl_txn_end_block` for transactions. Only one block of each kind is open
   at a time.
5. `trl_writer_close`, which ends any open block and writes the string
   table, type registry and index.

### Value changes

```c
#include <inttypes.h>
#include <stdio.h>
#include "trl/trl.h"

static void on_change(uint32_t var_id, uint64_t time, const uint8_t *val,
                      uint32_t len, void *user) {
    (void)user;
    uint64_t v = 0;
    for (uint32_t i = 0; i < len; ++i) v = (v << 8) | val[i];   /* big-endian */
    printf("var %u @%" PRIu64 " = %" PRIu64 "\n", var_id, time, v);
}

int main(void) {
    trl_writer_t *w = trl_writer_open("wave.trl", -9, TRL_COMPRESS_NONE);
    if (!w) return 1;

    uint32_t bit  = trl_add_signal_type(w, TRL_SE_2STATE, 1, 0);
    uint32_t byte = trl_add_signal_type(w, TRL_SE_2STATE, 8, 0 /* hex */);

    trl_begin_hierarchy(w, 1, 0 /* HK_DESIGN */, trl_intern(w, "design"));
    trl_hier_begin_scope(w, 0 /* ST_MODULE */, trl_intern(w, "top"), 0, 0, 0);
    uint32_t clk   = trl_hier_add_var(w, trl_intern(w, "clk"),   bit,  0, 0, 0);
    uint32_t count = trl_hier_add_var(w, trl_intern(w, "count"), byte, 0, 0, 0);
    trl_hier_end_scope(w);
    trl_flush_hierarchy(w);

    trl_vc_begin(w, 0);
    for (uint64_t t = 0; t < 4; ++t) {
        trl_vc_change_u64(w, clk,   t * 5, t & 1);
        trl_vc_change_u64(w, count, t * 5, t * 3);
    }
    trl_vc_flush(w);
    if (trl_writer_close(w) != TRL_OK) return 1;

    trl_reader_t *r = trl_reader_open("wave.trl");
    if (!r) return 1;
    trl_iter_vc(r, on_change, NULL);
    trl_reader_close(r);
    return 0;
}
```

`trl_iter_vc` calls the callback once per value change, block by block in
file order. Within a block the changes are grouped by variable, not sorted
by time, so the output above lists every change of `clk` before those of
`count`. The value bytes depend on the signal encoding: a 2-state value is
`ceil(width/8)` bytes, big-endian; a real is the 8 bytes of a `double`; a
string is its bytes without a terminator. See {doc}`format` for the 4-state
and 9-state forms.

The hierarchy kind, scope type and variable direction arguments are plain
integers; the values are those of the Python `HierKind`, `ScopeType` and
`VarDir` enums (listed in {doc}`../api/c`).

### Transactions

```c
#include "trl/trl.h"

int main(void) {
    trl_writer_t *w = trl_writer_open("txn.trl", -9, TRL_COMPRESS_NONE);
    if (!w) return 1;

    trl_field_def_t fields[2] = {
        { trl_intern(w, "addr"), TRL_FT_U32 },
        { trl_intern(w, "data"), TRL_FT_BITVEC },
    };
    uint32_t wr = trl_add_txn_schema(w, trl_intern(w, "write"), 2, fields);

    trl_begin_hierarchy(w, 1, 1 /* HK_BEHAVIORAL */, trl_intern(w, "streams"));
    uint32_t s = trl_hier_add_stream(w, 0, trl_intern(w, "bus"));
    trl_flush_hierarchy(w);

    uint8_t data[2] = { 0x0A, 0xBC };          /* 12 bits, big-endian */
    trl_attr_t attrs[2] = {
        { .field_idx = 0, .field_type = TRL_FT_U32, .value.u64 = 0x40 },
        { .field_idx = 1, .field_type = TRL_FT_BITVEC,
          .value.vec = { .data = data, .len = 2, .width = 12 } },
    };
    trl_txn_begin_block_stream(w, 100, s);
    trl_txn_full(w, s, wr, 1, 100, 120, 0, 2, attrs);
    trl_txn_meta(w, 1, trl_intern(w, "note"), trl_intern(w, "first"));
    trl_txn_end_block(w);
    return trl_writer_close(w) == TRL_OK ? 0 : 1;
}
```

With `trl.h` you choose the transaction ids. `trl.h` has no transaction
reader; read transactions with `trl_query.h`, or with the Python package.

## Codecs

`trl_codec.h` declares the codec registry (`trl_register_vc_codec`,
`trl_register_txn_codec`, the lookups), the codec vtables, the storage SPI
that a codec's `encode_block` uses, and `trl_writer_set_vc_codec`, which
selects a registered value-change codec for a writer. The built-in codecs
register themselves when the library loads. See {doc}`writing-a-codec` for
how codecs work; the declarations are listed in {doc}`../api/c`.

## The query scan engine

`trl_qscan.h` is the C back end of the `trlog.query` Python package. The
Python side computes the block plan, the field catalog and the compiled
filter, and the engine decodes and filters the selected blocks. When the
file holds something the engine does not decode exactly as Python does, it
returns `TRL_QS_FALLBACK` and Python runs the query itself. Set
`TRLOG_ENGINE=python` to skip the C engine. The API is described in
{doc}`../api/c` for completeness; it is not meant to be called from other
programs.

## Limitations

- `trl_rec.h` declares every stream flat, under one behavioral hierarchy
  named `streams`, with the full dotted path as the stream name. The
  `kind` argument of `trl_stream_new` is not written to the file.
- Enum label tables from `trl_enum_new` / `trl_enum_add` are kept in
  memory only. `ENUM` field values are written as plain integers.
- `trl_query.h` builds the cursor result in memory, so memory use grows
  with the number of selected transactions.
