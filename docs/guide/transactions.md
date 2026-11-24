# Transactions and streams

A transaction is a time span (start and end time) with typed attribute values,
recorded on a stream. Transactions can have a parent transaction, can be
linked to other transactions, and can carry string metadata. Use them for bus
transfers, sequence items, function calls, log messages and similar events.

Recording transactions takes four steps:

1. Register a **transaction schema** (the ordered, typed attribute fields).
2. Register a **stream type** and add a **stream instance** to the hierarchy.
3. Write transactions in **transaction blocks**.
4. Close the writer.

## Schemas

`TrlWriter.add_txn_schema(name, fields)` registers a schema and returns its
transaction type ID. `fields` is a list of `FieldDef`; the position of a field
in the list is its field index.

```python
from trlog import FieldDef, FieldType

axi = w.add_txn_schema("axi_xfer", [
    FieldDef(name_str_id=w.intern("addr"),  field_type=FieldType.FT_U64),
    FieldDef(name_str_id=w.intern("data"),  field_type=FieldType.FT_U32),
    FieldDef(name_str_id=w.intern("write"), field_type=FieldType.FT_BOOL),
    FieldDef(name_str_id=w.intern("resp"),  field_type=FieldType.FT_STRING),
])
```

`FieldDef` takes the field name as a string ID (from `w.intern()`), a
`FieldType`, and optionally `display_hint` (a `Radix`) and `enum_type_id`.

| `FieldType` | Python value |
|---|---|
| `FT_U8` ... `FT_U64`, `FT_I8` ... `FT_I64` | `int` |
| `FT_F32`, `FT_F64` | `float` |
| `FT_BOOL` | `bool` |
| `FT_STRING` | `str` |
| `FT_BYTES` | `bytes` |
| `FT_TIME` | `int` (trace time units) |
| `FT_ENUM` | `int` (an enum value; set `enum_type_id`) |
| `FT_BITVEC` | `(bit_width, value)` |
| `FT_LOGICVEC` | `(bit_width, aval, bval)`, 4-state value in aval/bval form |

### Enum fields

Register an enum type with `TrlWriter.add_enum_type(name, values)`, where
`values` is a list of `(label, integer)` pairs, and pass its ID as the field's
`enum_type_id`:

```python
op = w.add_enum_type("op_e", [("READ", 0), ("WRITE", 1), ("FENCE", 7)])
pkt = w.add_txn_schema("pkt", [
    FieldDef(w.intern("op"),      FieldType.FT_ENUM, enum_type_id=op),
    FieldDef(w.intern("payload"), FieldType.FT_BYTES),
    FieldDef(w.intern("mask"),    FieldType.FT_BITVEC),
    FieldDef(w.intern("bus"),     FieldType.FT_LOGICVEC),
])
```

The attribute value of an enum field is the integer. The reader maps it to a
label through `reader.type_registry.get_enum_type(enum_id)` (see below).

## Streams

A stream type groups streams that carry the same kind of transactions.
`TrlWriter.add_stream_type(name, kind="")` registers one and returns its
stream type ID. `kind` is a free-form category string (for example `"bus"`
or `"log"`) that readers can filter on.

A stream instance is a node in the hierarchy, added with
`add_stream(stream_type_id, name)`. It returns the stream instance ID that
transactions are written against:

```python
bus_t = w.add_stream_type("axi_bus", kind="bus")
with w.begin_hierarchy() as h:
    h.begin_scope(ScopeType.ST_MODULE, "top")
    h.begin_scope(ScopeType.ST_MODULE, "m0")
    m0 = h.add_stream(bus_t, "transfers")
    h.end_scope()
    h.end_scope()
```

## Writing transactions

`TrlWriter.begin_txn_block(start_time, stream_inst_id=None)` returns a block
builder, used as a context manager. Pass `stream_inst_id` when every record in
the block belongs to one stream: the index then records the block under that
stream and the reader can read one stream without decoding the others' blocks.
A block without `stream_inst_id` may mix streams.

Transaction IDs (`txn_id`) are integers that you choose. Keep them unique
within the trace; parents, links and metadata refer to transactions by ID.

### Complete transactions

When the whole transaction is known, write it with one `write_full()` call:

```python
from trlog import TxnAttr

with w.begin_txn_block(start_time=0, stream_inst_id=m0) as tb:
    tb.write_full(m0, axi, txn_id=1, start=10, end=30, parent=0,
                  attrs=[TxnAttr(0, 0x1000), TxnAttr(1, 0xCAFE),
                         TxnAttr(2, True), TxnAttr(3, "OKAY")])
```

`write_full(stream_inst_id, txn_type_id, txn_id, start, end, parent,
attrs=None, origin_file="", origin_line=0, origin_component="")`:

- `parent` is the parent transaction's ID, or `0` for none. It is required.
- `attrs` is a list of `TxnAttr(field_idx, value)`. Fields can be omitted.
- `origin_file`, `origin_line` and `origin_component` record the code that
  produced the transaction. They are stored as metadata with the keys
  `trlog.origin.file`, `trlog.origin.line` and `trlog.origin.component`.

### Transactions that span blocks

When a transaction starts before its end is known, write it in parts:

```python
with w.begin_txn_block(start_time=60, stream_inst_id=m0) as tb:
    tb.write_begin(m0, axi, txn_id=3, start=60)
    tb.write_attr(3, [TxnAttr(0, 0x2000), TxnAttr(2, True)])

with w.begin_txn_block(start_time=90, stream_inst_id=m0) as tb:
    tb.write_attr(3, [TxnAttr(1, 0x55)])
    tb.write_end(3, end_time=95)
```

- `write_begin(stream_inst_id, txn_type_id, txn_id, start, parent=0,
  origin_file="", origin_line=0, origin_component="")` opens the transaction.
- `write_attr(txn_id, attrs)` adds attribute values. It can be called any
  number of times, in any block. Each value is encoded with the field type
  of the schema given to `write_begin()`, as in `write_full()`.
- `write_end(txn_id, end_time)` closes the transaction.

### Links and metadata

`write_link(link_type, src, tgt, label_id=0)` records a relationship between
two transactions by ID. `link_type` is a `LinkType`: `LT_CAUSE_EFFECT`,
`LT_PARENT_CHILD`, `LT_RELATED` or `LT_CUSTOM`. `label_id` is an optional
string ID (from `w.intern()`).

`write_meta(txn_id, key, value)` attaches a string key/value pair to a
transaction.

```python
from trlog import LinkType

with w.begin_txn_block(start_time=40, stream_inst_id=m0) as tb:
    tb.write_full(m0, axi, txn_id=2, start=40, end=55, parent=0,
                  attrs=[TxnAttr(0, 0x1004), TxnAttr(2, False)],
                  origin_file="seq.py", origin_line=42)
    tb.write_link(LinkType.LT_CAUSE_EFFECT, 1, 2)
    tb.write_meta(2, "note", "read-after-write")
```

## Reading transactions

Each record is returned as one of these dataclasses:

| Record | Written by | Fields |
|---|---|---|
| `TxnFull` | `write_full` | `stream_inst_id`, `txn_type_id`, `txn_id`, `start_time`, `end_time`, `parent_txn_id`, `attrs` |
| `TxnBegin` | `write_begin` | `stream_inst_id`, `txn_type_id`, `txn_id`, `start_time`, `parent_txn_id` |
| `TxnAttrRecord` | `write_attr` | `txn_id`, `attrs` |
| `TxnEnd` | `write_end` | `txn_id`, `end_time` |
| `TxnLink` | `write_link` | `link_type`, `source_txn_id`, `target_txn_id`, `label_str_id` |
| `TxnMeta` | `write_meta` and origin arguments | `txn_id`, `key_str_id`, `value_str_id` |

`attrs` is a list of `TxnAttr(field_idx, value)`.

The reader offers three ways in:

- `read_stream(stream_inst_id, start=None, end=None)` returns the `TxnFull`
  and `TxnBegin` records of one stream whose start time is within
  `[start, end]`.
- `iter_txn_blocks(stream_inst_id=None)` yields the records of each
  transaction block as a list, in file order. With `stream_inst_id`, records
  that belong to other streams are dropped; `TxnAttrRecord`, `TxnEnd`,
  `TxnLink` and `TxnMeta` records carry no stream ID and are always kept.
- `find_streams(kind=None)` returns `{stream_inst_id: HStream}` for all
  streams, or for those whose stream type has the given `kind`.

To turn field indices into names, look up the schema in the type registry:

```python
from trlog import TrlReader, TxnFull, TxnLink, TxnMeta

with TrlReader("bus.trl") as r:
    st = r.string_table
    for inst_id, hs in r.find_streams(kind="bus").items():
        print(inst_id, st.lookup(hs.name_str_id))

    schema = r.type_registry.get_txn_schema(axi)
    names = [st.lookup(f.name_str_id) for f in schema.fields]

    for rec in r.read_stream(m0):
        if isinstance(rec, TxnFull):
            vals = {names[a.field_idx]: a.value for a in rec.attrs}
            print(rec.txn_id, rec.start_time, rec.end_time, vals)

    for block in r.iter_txn_blocks():
        for rec in block:
            if isinstance(rec, TxnLink):
                print("link", rec.link_type.name, rec.source_txn_id, rec.target_txn_id)
            elif isinstance(rec, TxnMeta):
                print("meta", rec.txn_id, st.lookup(rec.key_str_id),
                      st.lookup(rec.value_str_id))
```

Output for the transactions written above:

```text
1 transfers
1 10 30 {'addr': 4096, 'data': 51966, 'write': True, 'resp': 'OKAY'}
2 40 55 {'addr': 4100, 'write': False}
meta 2 trlog.origin.file seq.py
meta 2 trlog.origin.line 42
link LT_CAUSE_EFFECT 1 2
meta 2 note read-after-write
```

An enum value is mapped to its label with the enum type:

```python
enum = r.type_registry.get_enum_type(op)
labels = {v.integer_value: st.lookup(v.label_str_id) for v in enum.values}
```

For filtering transactions by attribute value, following parent/child trees
and links, and exporting results, use the `trlog.query` package or the
`trlog` command-line tool.
