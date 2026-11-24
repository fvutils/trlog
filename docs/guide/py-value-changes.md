# Value changes

Value changes record the values of hierarchy variables over time. They are
written in value-change blocks; each block covers a span of time and holds
changes for any number of variables.

## Writing value changes

`TrlWriter.begin_vc_block(start_time)` returns a block builder. Add changes
with `add_change(var_id, time, value)`; the block is encoded and written when
the `with` block exits (or when you call `flush()`).

```python
from trlog import TrlWriter, SignalEncoding, ScopeType

with TrlWriter("waves.trl") as w:
    bit = w.add_signal_type(SignalEncoding.SE_2STATE, 1)
    byte = w.add_signal_type(SignalEncoding.SE_2STATE, 8)
    with w.begin_hierarchy() as h:
        h.begin_scope(ScopeType.ST_MODULE, "top")
        clk = h.add_var("clk", bit)
        data = h.add_var("data", byte)
        mode = h.add_var("mode", byte)
        h.end_scope()

    with w.begin_vc_block(start_time=0) as vc:
        vc.set_initial(mode, 3)
        for t in range(0, 100, 5):
            vc.add_change(clk, t, (t // 5) % 2)
        vc.add_change(data, 10, 0x12)
        vc.add_change(data, 30, 0x34)

    with w.begin_vc_block(start_time=100) as vc:
        vc.add_change(data, 110, 0x56)
```

Rules:

- `var_id` is the ID returned by `add_var()`. The variable's signal type
  determines how the value is encoded.
- `time` is an absolute time in trace units. Changes within a block do not
  need to be added in time order; the writer sorts them.
- Write blocks in increasing time order, and give each block a `start_time`
  no later than its first change. The index uses block start and end times
  for time-range reads.
- `set_initial(var_id, value)` sets a variable's value at the start of the
  block. The reader reports it (as a change at `start_time`) only when the
  variable has no other changes in that block. Without `set_initial()`, the
  first change in the block is used as the initial value.

Many small blocks cost space; a block per simulation time slice or per a few
thousand changes is typical.

## Values by signal encoding

| `SignalEncoding` | Value written | Value read back |
|---|---|---|
| `SE_2STATE`, `SE_BITVEC` | non-negative `int` | `int` |
| `SE_4STATE`, 1 bit | `0`, `1`, or `"x"`/`"z"` (any case) | `0`, `1`, `'X'` or `'Z'` |
| `SE_4STATE`, wider | `int`, or a string or list of per-bit states (MSB first): `0`/`1` or `'0'`, `'1'`, `'X'`, `'Z'` | `int`, or a list of per-bit states (MSB first) |
| `SE_9STATE` | `int`, or a string or list of per-bit states (MSB first): `0`/`1` or `'0'`, `'1'`, `'X'`, `'Z'`, `'H'`, `'U'`, `'W'`, `'L'`, `'-'` | `int` or list |
| `SE_REAL`, `SE_ANALOG` | `float` | `float` |
| `SE_STRING` | `str` | `str` |

```{note}
State characters may be in any case: `"01xz"` and `["0", 1, "X", "z"]` are
the same 4-bit value. A single state fills every bit (`"X"` on an 8-bit
signal is all X). A shorter value is zero-extended on the left, and a
longer one keeps its low bits. A value whose bits are all 0 or 1 is stored
and read back as an `int`. Any other character raises `ValueError`.
```

## Reading value changes

`TrlReader.read_signal(var_id, start=None, end=None)` returns the changes for
one variable as a list of `VcChange(var_id, time, value)`, sorted by time.
`start` and `end` are inclusive bounds. When `start` is given, the reader uses
the file index to read only the blocks that overlap the range.

```python
from trlog import TrlReader

with TrlReader("waves.trl") as r:
    print([(c.time, c.value) for c in r.read_signal(data)])
    print([(c.time, c.value) for c in r.read_signal(data, start=20, end=200)])
    print([(c.time, c.value) for c in r.read_signal(mode)])
```

Output:

```text
[(10, 18), (30, 52), (110, 86)]
[(30, 52), (110, 86)]
[(0, 3)]
```

`TrlReader.iter_vc_blocks()` yields, for each value-change block in file
order, a list of `VcChange` for all variables, sorted by time. Use it to
process every signal in one pass:

```python
from collections import defaultdict

with TrlReader("waves.trl") as r:
    per_var = defaultdict(list)
    for block in r.iter_vc_blocks():
        for c in block:
            per_var[c.var_id].append((c.time, c.value))
```

`TrlReader.seek_time(time)` returns the file offset of the first
value-change block whose start time is at or after `time`. It raises
`ZstNoIndexError` if the file has no index or no such block.

## Block codecs and compression

By default blocks are compressed with zlib (`TrlWriter(..., compress=True)`).
`begin_vc_block(start_time, codec=...)` selects a different value-change codec
for one block by its codec ID, for example `trlog.CORE_BYTESPLIT`. Codecs and
the encoding options are described in the codec documentation.
