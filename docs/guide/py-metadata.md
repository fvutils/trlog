# Metadata

A trace can carry metadata at several levels. All of it can be read without
any codec.

| Level | Write with | Read with |
|---|---|---|
| Whole trace | `TrlWriter.add_trace_metadata()` | `TrlReader.trace_metadata()` |
| Stream type | `TrlWriter.add_stream_metadata()`, `add_stream_profile()` | `TrlReader.stream_metadata()`, `profiles()` |
| Scope, variable or stream instance | `add_attr()`, `add_typed_attr()` on the hierarchy builder | `attrs`, `typed_attrs` on the hierarchy node |
| Transaction | `write_meta()` on a transaction block | `TxnMeta` records |
| Opaque, keyed binary data | `TrlWriter.write_aux()` | `TrlReader.read_aux()` |

Transaction metadata is described in [Transactions](transactions.md).

## Typed values

Trace, stream-type and typed hierarchy metadata store a typed value. The type
is inferred from the Python value unless you pass an `AttrType`:

| Python value | Inferred `AttrType` |
|---|---|
| `bool` | `AT_BOOL` |
| `int` >= 0 | `AT_U64` |
| `int` < 0 | `AT_I64` |
| `float` | `AT_F64` |
| `str` | `AT_STR` |
| list of one of the above | `AT_ARRAY` combined with the element type, e.g. `AT_ARRAY \| AT_STR` |

Pass `attr_type` to override inference, for example to store a non-negative
value as signed: `attr_type=AttrType.AT_I64`.

## Trace and stream-type metadata

```python
from trlog import TrlWriter, TrlReader, AttrType

with TrlWriter("meta.trl") as w:
    w.add_trace_metadata("tool", "my_sim")
    w.add_trace_metadata("seed", 12345)
    w.add_trace_metadata("vdd", 0.9)
    w.add_trace_metadata("tags", ["smoke", "nightly"])
    w.add_trace_metadata("offset", 7, attr_type=AttrType.AT_I64)

    bus_t = w.add_stream_type("apb", kind="bus")
    w.add_stream_metadata(bus_t, "protocol_version", "APB4")
    w.add_stream_profile(bus_t, "org.example.apb")
    w.add_stream_profile(bus_t, "org.example.bus")

with TrlReader("meta.trl") as r:
    print(r.trace_metadata())
    print(r.stream_metadata(bus_t))
    print(r.profiles(bus_t))
```

Output:

```text
{'tool': 'my_sim', 'seed': 12345, 'vdd': 0.9, 'tags': ['smoke', 'nightly'], 'offset': 7}
{'protocol_version': 'APB4', 'trlog.profile': ['org.example.apb', 'org.example.bus']}
['org.example.apb', 'org.example.bus']
```

The reader returns plain dictionaries with string values already resolved. If
a key is added more than once, the last value wins.

A **profile** is a reverse-DNS identifier that says which conventions a stream
type follows, so a tool can recognize streams it knows how to interpret.
`add_stream_profile()` appends to the stream type's `trlog.profile` list.

## Hierarchy attributes

On the hierarchy builder, `add_typed_attr(key, value, attr_type=None)`
attaches a typed value to the scope, variable or stream declared most
recently. (`add_attr(key, value)` stores a plain string; see
[Hierarchies](hierarchies.md).)

```python
from trlog import TrlWriter, TrlReader, AttrType, ScopeType, SignalEncoding

with TrlWriter("attrs.trl") as w:
    word = w.add_signal_type(SignalEncoding.SE_2STATE, 32)
    with w.begin_hierarchy() as h:
        h.begin_scope(ScopeType.ST_MODULE, "top")
        h.add_typed_attr("clock_mhz", 250.0)          # on scope "top"
        pc = h.add_var("pc", word)
        h.add_typed_attr("reset_value", 0x8000_0000)  # on var "pc"
        h.add_typed_attr("aliases", ["ip", "iptr"])
        h.end_scope()
```

On the reader side the node's `typed_attrs` list holds `TypedAttr(key_str_id,
attr_type, value)` in its stored form: string values are string IDs. Resolve
them with the string table:

```python
def resolve(r, attr):
    st = r.string_table
    is_array = attr.attr_type & AttrType.AT_ARRAY
    base = attr.attr_type & ~AttrType.AT_ARRAY
    if base == AttrType.AT_STR:
        return [st.lookup(v) for v in attr.value] if is_array else st.lookup(attr.value)
    return attr.value

with TrlReader("attrs.trl") as r:
    var = r.hierarchies[1].vars[pc]
    for a in var.typed_attrs:
        print(r.string_table.lookup(a.key_str_id), resolve(r, a))
```

Output:

```text
reset_value 2147483648
aliases ['ip', 'iptr']
```

## Well-known keys

The writer uses these keys for information it records itself. They are
available as constants on `trlog.WellKnownAttr`.

| Key | Constant | Set by |
|---|---|---|
| `trlog.src.file`, `trlog.src.line` | `SRC_FILE`, `SRC_LINE` | `add_stream(..., src_file=, src_line=)` |
| `trlog.driver.file`, `trlog.driver.line` | `DRIVER_FILE`, `DRIVER_LINE` | `add_var(..., driver_file=, driver_line=)` |
| `trlog.origin.file`, `trlog.origin.line`, `trlog.origin.component` | `ORIGIN_FILE`, `ORIGIN_LINE`, `ORIGIN_COMPONENT` | `write_full()`/`write_begin()` origin arguments |
| `trlog.profile` | `PROFILE` | `add_stream_profile()` |

## Opaque keyed data (aux blocks)

`TrlWriter.write_aux(owner_stream_id, key, payload, compress=None)` stores a
blob of bytes that is not tied to time, under an `(owner, key)` pair. The
owner is normally a stream type ID; the key is an integer you choose. Writing
the same pair again adds another blob with the next ordinal (0, 1, ...);
`write_aux()` returns the ordinal. The bytes are not interpreted by trlog.

```python
from trlog import TrlWriter, TrlReader

with TrlWriter("aux.trl") as w:
    cfg_t = w.add_stream_type("config")
    w.write_aux(cfg_t, key=0, payload=b"first")
    w.write_aux(cfg_t, key=0, payload=b"second")   # ordinal 1
    w.write_aux(cfg_t, key=7, payload=b"other")

with TrlReader("aux.trl") as r:
    print(r.aux_keys(cfg_t))       # [0, 7]
    print(r.read_aux(cfg_t, 0))    # [b'first', b'second']
    for owner, key, ordinal, offset, length in r.aux_entries(cfg_t):
        print(owner, key, ordinal)
```

`aux_entries()` lists entries from the file index without reading the blobs;
call it without an argument to list every owner. Aux blocks are how codecs
store tables and definitions; [derived signals](py-derived.md) use them too.
