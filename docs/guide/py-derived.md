# Derived signals and stream dependencies

## Derived signals

A derived signal is defined by an expression over other signals. The trace
stores the expression, not the values; the reader evaluates it when you ask
for the signal. Use derived signals for handshakes (`valid & ready`), decoded
fields (a bit slice of a bus), comparisons, and signal aliases (`Input(0)`).
The on-disk encoding is described under {ref}`codec-derived`.

### Defining a derived signal

Build an expression from `Input(i)` and `Const(value)` and pass it to
`TrlWriter.add_derived_signal(name, expr, inputs, bit_width)`. `Input(i)`
refers to `inputs[i]`, which is a variable ID (from `add_var()`) or the ID of
another derived signal. The method returns the derived signal's ID.

```python
from trlog import TrlWriter, TrlReader, SignalEncoding, ScopeType, Input, Const

valid_chg = [(0, 0), (10, 1), (30, 0), (50, 1)]
ready_chg = [(0, 0), (20, 1), (40, 0), (60, 1)]

with TrlWriter("derived.trl") as w:
    bit = w.add_signal_type(SignalEncoding.SE_2STATE, 1)
    byte = w.add_signal_type(SignalEncoding.SE_2STATE, 8)
    with w.begin_hierarchy() as h:
        h.begin_scope(ScopeType.ST_MODULE, "top")
        valid = h.add_var("valid", bit)
        ready = h.add_var("ready", bit)
        status = h.add_var("status", byte)
        h.end_scope()
    with w.begin_vc_block(0) as vc:
        for t, v in valid_chg:
            vc.add_change(valid, t, v)
        for t, v in ready_chg:
            vc.add_change(ready, t, v)
        vc.add_change(status, 0, 0x3C)

    fire = w.add_derived_signal("fire", Input(0) & Input(1),
                                inputs=[valid, ready], bit_width=1)
    # Derived signals can be inputs to other derived signals.
    idle = w.add_derived_signal("idle", Input(0).lnot(),
                                inputs=[fire], bit_width=1)
    hi = w.add_derived_signal("status_hi", Input(0).slice_(4, 4),
                              inputs=[status], bit_width=4)
    is3 = w.add_derived_signal("hi_is_3", Input(0).eq(Const(3)),
                               inputs=[hi], bit_width=1)
```

Derived signal IDs start at `0x40000000` so that they do not collide with
variable IDs. Derived signals are not part of the hierarchy.

If an expression would make a derived signal depend on itself, directly or
through other derived signals, `add_derived_signal()` raises
`DependencyError`.

### Expression operators

Values are unsigned integers. The result is masked to `bit_width` bits.

| Expression | Result |
|---|---|
| `Input(i)` | Current value of `inputs[i]` (0 before its first change) |
| `Const(n)` | The integer `n` |
| `a & b`, `a \| b`, `a ^ b` | Bitwise AND, OR, XOR |
| `~a` | Bitwise NOT |
| `a.lnot()` | Logical NOT: 1 if `a` is 0, else 0 |
| `a.eq(b)`, `a.ne(b)` | 1 if equal / not equal, else 0 |
| `a.shl(k)`, `a.shr(k)` | Shift left / right by the constant `k` |
| `a.reduce_and(w)`, `a.reduce_or(w)` | AND / OR of the low `w` bits of `a` (0 or 1) |
| `a.slice_(lo, w)` | Bits `lo` to `lo + w - 1` of `a` |
| `a.concat(b, wb)` | `(a << wb) \| (b & (2**wb - 1))` |

Plain integers are accepted where an expression is expected on the right-hand
side of `&`, `|`, `^`, `eq`, `ne` and `concat`; they become `Const`.

`compile_expr(expr)` returns the bytecode that is stored in the file. You do
not need it to write a derived signal, but `add_derived_signal()` also accepts
bytecode in place of an expression.

### Reading derived signals

`TrlReader.read_derived(derived_id)` evaluates the signal and returns a list
of `VcChange`. The value is computed at every time at which any input
changes, and a change is reported only when the computed value differs from
the previous one. Inputs that are themselves derived are evaluated first, and
each input is evaluated once per call.

`TrlReader.derived_signals()` lists the derived signals in the trace:
`{derived_id: {"name", "bit_width", "inputs", "materialized", "def"}}`.

```python
with TrlReader("derived.trl") as r:
    for sid, info in r.derived_signals().items():
        print(hex(sid), info["name"], info["bit_width"], info["inputs"])
    print([(c.time, c.value) for c in r.read_derived(fire)])
    print([(c.time, c.value) for c in r.read_derived(idle)])
    print([(c.time, c.value) for c in r.read_derived(is3)])
```

Output:

```text
0x40000000 fire 1 [1, 2]
0x40000001 idle 1 [1073741824]
0x40000002 status_hi 4 [3]
0x40000003 hi_is_3 1 [1073741826]
[(0, 0), (20, 1), (30, 0), (60, 1)]
[(0, 1), (20, 0), (30, 1), (60, 0)]
[(0, 1)]
```

`read_signal()` does not evaluate derived signals; use `read_derived()`.

### Storing the computed values

A reader that does not understand derived signals sees no values for them.
To also store the computed values as ordinary value changes, pass
`materialized=True` together with `input_changes`, a dictionary from each
input ID to its `[(time, value), ...]` list:

```python
fire_m = w.add_derived_signal(
    "fire_m", Input(0) & Input(1), inputs=[valid, ready], bit_width=1,
    materialized=True,
    input_changes={valid: valid_chg, ready: ready_chg})
```

The writer evaluates the expression and writes the result in a new
value-change block, so `read_signal(fire_m)` returns the values too.
`input_changes` is required with `materialized=True`, because the writer does
not keep the value changes it has already written.

## Stream dependencies

Some streams are computed from other streams: a packet stream decoded from a
raw bus stream, or a call stream whose function signatures live in a separate
stream. `TrlWriter.declare_dependencies(stream_type_id, input_stream_ids)`
records that a stream type depends on other stream types. The writer rejects
a declaration that would create a cycle by raising `DependencyError`, and the
declaration is not stored.

```python
from trlog import TrlWriter, TrlReader, DependencyError

with TrlWriter("deps.trl") as w:
    raw = w.add_stream_type("raw_bus")
    pkts = w.add_stream_type("packets")
    flows = w.add_stream_type("flows")
    w.declare_dependencies(pkts, [raw])
    w.declare_dependencies(flows, [pkts])
    try:
        w.declare_dependencies(raw, [flows])
    except DependencyError as e:
        print("rejected:", e)      # stream dependency cycle: 1 -> 3 -> 2 -> 1
```

On the reader:

- `dependency_catalog()` returns `{stream_type_id: [input_ids]}`.
- `dependencies(stream_type_id)` returns one stream type's inputs.
- `dependency_order()` returns the stream types in an order where inputs
  come before the streams that use them.
- `dependency_graph()` returns a `DependencyGraph` built from the catalog.
- `materializer(compute)` returns a `Materializer` that computes streams on
  demand.

```python
with TrlReader("deps.trl") as r:
    print(r.dependency_catalog())    # {2: [1], 3: [2]}
    print(r.dependency_order())      # [1, 2, 3]

    def compute(stream_id, inputs):
        # inputs maps each input stream ID to its computed result
        print("computing", stream_id, "from", sorted(inputs))
        return f"stream{stream_id}"

    m = r.materializer(compute)
    m.materialize(flows)
```

Output of the `materialize` call:

```text
computing 1 from []
computing 2 from [1]
computing 3 from [2]
```

`Materializer.materialize(stream_id)` computes the stream's inputs first,
then calls `compute(stream_id, inputs)`. Each stream is computed at most
once; later calls return the cached result. `is_materialized(stream_id)`
says whether a result is cached. A cycle found while materializing raises
`DependencyError`.

### Using DependencyGraph directly

`DependencyGraph` works on any integer IDs:

```python
from trlog import DependencyGraph

g = DependencyGraph()
g.add(3, [1, 2])          # 3 depends on 1 and 2
g.add(2, [1])
print(g.nodes())           # [1, 2, 3]
print(g.topo_order())      # [1, 2, 3]  inputs first
print(g.finalize_order())  # [3, 2, 1]  dependents first
print(g.find_cycle())      # None
g.add(1, [3])
print(g.find_cycle())      # [1, 3, 1]
```

`validate_acyclic()` raises `DependencyError` if there is a cycle;
`topo_order()` and `finalize_order()` call it first.
`DependencyGraph.from_catalog(catalog)` builds a graph from a
`{id: [input_ids]}` dictionary.
