# Hierarchies

A hierarchy is a tree of scopes that names the signals (variables) and
transaction sources (streams) in a trace. A trace file can contain several
hierarchies, each identified by an integer `hier_id`.

## Building a hierarchy

`TrlWriter.begin_hierarchy()` returns a builder. Use it as a context manager:
the hierarchy is written to the file when the `with` block exits.

Inside the builder, `begin_scope()` opens a scope and `end_scope()` closes the
most recently opened one. Variables and streams belong to the scope that is
open when you add them.

```python
from trlog import TrlWriter, SignalEncoding, ScopeType, VarDir, HierKind

with TrlWriter("soc.trl") as w:
    bit = w.add_signal_type(SignalEncoding.SE_2STATE, 1)
    word = w.add_signal_type(SignalEncoding.SE_2STATE, 32)
    msg_t = w.add_stream_type("uart_msgs", kind="log")

    with w.begin_hierarchy(hier_id=1, kind=HierKind.HK_DESIGN, name="design") as h:
        h.begin_scope(ScopeType.ST_MODULE, "top", component="soc_top",
                      src_file="rtl/soc_top.sv", src_line=12)
        clk = h.add_var("clk", bit, VarDir.VD_IN)

        h.begin_scope(ScopeType.ST_MODULE, "cpu", component="rv32_core")
        h.add_typed_attr("xlen", 32)          # attaches to scope "cpu"
        pc = h.add_var("pc", word, VarDir.VD_OUT,
                       src_file="rtl/core.sv", src_line=88,
                       driver_file="rtl/fetch.sv", driver_line=140)
        h.add_attr("role", "program counter")  # attaches to var "pc"
        h.end_scope()                           # cpu

        uart = h.add_stream(msg_t, "uart0", src_file="rtl/uart.sv", src_line=5)
        h.end_scope()                           # top

    with w.begin_vc_block(0) as vc:
        vc.add_change(clk, 0, 0)
        vc.add_change(pc, 0, 0x8000_0000)
```

### Scopes

`begin_scope(scope_type, name, component="", src_file="", src_line=0)`:

- `scope_type` is a `ScopeType` value (see the table below).
- `name` is the instance name.
- `component` is the definition name, for example the module type.
- `src_file` and `src_line` record where the scope is declared.

| `ScopeType` | Meaning |
|---|---|
| `ST_MODULE` | HDL module instance |
| `ST_TASK` | Task |
| `ST_FUNCTION` | Function |
| `ST_BLOCK` | Named `begin`/`end` block |
| `ST_FORK` | `fork`/`join` block |
| `ST_GENERATE` | Generate block |
| `ST_STRUCT` | Struct |
| `ST_UNION` | Union |
| `ST_INTERFACE` | Interface instance |
| `ST_PACKAGE` | Package |
| `ST_PROGRAM` | Program |
| `ST_VHDL_ARCH` | VHDL architecture |
| `ST_VHDL_PROC` | VHDL process |
| `ST_ANONYMOUS` | Unnamed scope |

### Variables

`add_var(name, sig_type_id, direction=VarDir.VD_IN, src_file="", src_line=0,
driver_file="", driver_line=0)` adds a signal and returns its variable ID. You
pass this ID to `add_change()` when you write value changes.

- `sig_type_id` comes from `TrlWriter.add_signal_type()`.
- `direction` is a `VarDir` value: `VD_NONE`, `VD_IN`, `VD_OUT`, `VD_INOUT` or
  `VD_BUFFER`. The default is `VD_IN`.
- `src_file`/`src_line` record where the signal is declared.
- `driver_file`/`driver_line` record the code that drives the signal. They are
  stored as the attributes `trlog.driver.file` and `trlog.driver.line` on the
  variable.

Variable IDs start at 1 and increase by one for each variable. Numbering
continues across hierarchies, so every variable in the file has its own ID.

### Streams

`add_stream(stream_type_id, name, src_file="", src_line=0)` adds a transaction
stream and returns its stream instance ID. `stream_type_id` comes from
`TrlWriter.add_stream_type()`. Transactions are written against the stream
instance ID; see [Transactions](transactions.md). When `src_file` is given it
is stored as the attributes `trlog.src.file` and `trlog.src.line` on the
stream.

### Attributes

`add_attr(key, value)` and `add_typed_attr(key, value, attr_type=None)` attach
an attribute to the scope, variable or stream that was declared most recently
(after `end_scope()`, that is the enclosing scope again).

- `add_attr()` stores the value as a string (`str(value)`).
- `add_typed_attr()` keeps the value's type: `bool`, signed or unsigned 64-bit
  integer, `float`, `str`, or a list of one of these. The type is inferred
  from the value unless you pass an `AttrType`. See [Metadata](py-metadata.md).

Calling either method before any scope, variable or stream has been declared
raises `ZstFormatError`.

## Signal types

Register a signal type once and use its ID for every variable of that type:

```python
bit  = w.add_signal_type(SignalEncoding.SE_2STATE, 1)
bus  = w.add_signal_type(SignalEncoding.SE_4STATE, 32, radix=Radix.RX_HEX)
volt = w.add_signal_type(SignalEncoding.SE_REAL, 64)
name = w.add_signal_type(SignalEncoding.SE_STRING, 0)
```

The `radix` argument (a `Radix` value, default `RX_HEX`) is a display hint for
viewers. The encodings and the values each one accepts are described in
[Value changes](py-value-changes.md).

## Multiple hierarchies

Call `begin_hierarchy()` once per hierarchy, with a distinct `hier_id`. The
`kind` argument (`HierKind.HK_DESIGN`, `HK_BEHAVIORAL`, `HK_SW` or
`HK_CUSTOM`) says what the hierarchy describes; `name` is a label.

```python
with w.begin_hierarchy(hier_id=1, kind=HierKind.HK_DESIGN, name="design") as h:
    ...
with w.begin_hierarchy(hier_id=2, kind=HierKind.HK_SW, name="firmware") as h:
    ...
```

Variable IDs and stream instance IDs are numbered across all hierarchies of
the writer, so value-change and transaction data, which refer to them by ID
only, are never ambiguous.

## Reading a hierarchy

`TrlReader.hierarchies` is a dictionary from `hier_id` to a hierarchy object.
Each hierarchy has:

- `header` with `hier_id`, `kind` and `name_str_id`.
- `vars`, a dictionary from variable ID to `HVar`.
- `streams`, a dictionary from stream instance ID to `HStream`.

`HVar` has `name_str_id`, `sig_type_id`, `var_dir`, `src_file_str_id`,
`src_line`, `attrs` (string attributes, `HAttr`) and `typed_attrs`
(`TypedAttr`). `HStream` has `stream_type_id`, `name_str_id`, `attrs` and
`typed_attrs`. Names are string IDs; resolve them with the string table.

```python
from trlog import TrlReader

with TrlReader("soc.trl") as r:
    st = r.string_table
    hier = r.hierarchies[1]
    print(hier.header.kind.name, st.lookup(hier.header.name_str_id))

    for var_id, var in hier.vars.items():
        stype = r.type_registry.get_signal_type(var.sig_type_id)
        attrs = {st.lookup(a.key_str_id): st.lookup(a.value_str_id)
                 for a in var.attrs}
        print(var_id, st.lookup(var.name_str_id), stype.encoding.name,
              stype.bit_width, var.var_dir.name, attrs)

    for inst_id, s in hier.streams.items():
        print(inst_id, st.lookup(s.name_str_id), s.stream_type_id)
```

Output:

```text
HK_DESIGN design
1 clk SE_2STATE 1 VD_IN {}
2 pc SE_2STATE 32 VD_OUT {'trlog.driver.file': 'rtl/fetch.sv', 'trlog.driver.line': '140', 'role': 'program counter'}
1 uart0 1
```

`TrlReader.find_streams(kind=None)` returns the streams of every hierarchy,
optionally only those whose stream type was registered with the given `kind`.

`hierarchies` exposes variables and streams as flat dictionaries. To walk the
scope tree, use `TrlReader.scope_tree(hier_id)`, which returns the hierarchy
root as a `ScopeNode` (`name`, `scope_type`, `path`, `children`, `vars`,
`streams`), or `TrlReader.scopes()`, which yields every scope depth-first:

```python
with TrlReader("out.trl") as r:
    for scope in r.scopes():
        names = [r.string_table.lookup(v.name_str_id) for v in scope.vars]
        print(scope.path, scope.scope_type.name, names)
```

The `trlog.query` package selects streams by hierarchical path (for example
`top.env.*`).
