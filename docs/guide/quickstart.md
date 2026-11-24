# Quickstart

trlog reads and writes TRLOG trace files (`.trl`). A trace file holds
signal value changes (waveforms) and transaction streams in one file, together
with the design hierarchy that names them. This page shows how to install the
Python package and write and read a first trace.

## Installation

The Python package is not published on PyPI. Install it from the root of a
checkout of the trlog repository:

```bash
pip install -e .
```

The package needs Python 3.10 or newer and has no required dependencies. The
optional extras in `pyproject.toml` add features that need third-party
packages:

| Extra | Installs | Used for |
|---|---|---|
| `zstd` | `zstandard` | Zstandard-compressed blocks |
| `lz4` | `lz4` | LZ4-compressed value-change data |
| `parquet` | `pyarrow` | Parquet export from the query layer |

Install an extra with, for example, `pip install -e ".[zstd]"`.

Installing the package also installs the `trlog` command-line tool.

### The C library is not required

`trlog.TrlWriter` and `trlog.TrlReader` are implemented in pure Python, and
this is what you get by default. The repository also contains a C library
(`libtrl.so`, built under `build/c`), which the SystemVerilog and VPI
integrations use. The Python package only loads it when you set the
environment variable `TRL_USE_CTYPES_NATIVE=1`, and then only for
`TrlWriter`; `TrlReader` is always the pure-Python reader. The ctypes writer
supports a small subset of the writer API (no streams, metadata, derived
signals or extension blocks), so leave it off unless you are testing the C
library. The library is looked up at `TRL_LIBRARY_PATH`, then
`build/c/libtrl.so` in the repository.

## Concepts

A trace file contains:

- **Types.** Signal types (encoding and bit width), transaction schemas
  (ordered, typed fields), enum types and stream types. You register them on
  the writer and get back integer IDs.
- **Hierarchies.** Trees of scopes (modules, tasks, packages, ...) that contain
  variables (signals) and streams (transaction sources). See
  [Hierarchies](hierarchies.md).
- **Value-change blocks.** Time-stamped value changes for variables. See
  [Value changes](py-value-changes.md).
- **Transaction blocks.** Transactions on streams, with typed attributes,
  parent/child relationships, links and metadata. See
  [Transactions](transactions.md).
- **Metadata, derived signals and extension blocks.** See
  [Metadata](py-metadata.md), [Derived signals](py-derived.md) and
  [Extension blocks](py-extensions.md).

Names (scope, variable, field and stream names, string attribute values) are
stored once in a string table and referred to by integer string IDs. The writer
interns strings for you; on the reader side you turn an ID back into text with
`reader.string_table.lookup(str_id)`.

Times are unsigned integers in units of the trace timescale. The timescale is
`10**timescale_exp` seconds; the default `timescale_exp=-9` means nanoseconds.

## Writing a trace

```python
from trlog import TrlWriter, SignalEncoding, ScopeType, VarDir

with TrlWriter("counter.trl", timescale_exp=-9) as w:
    # 1. Register signal types.
    bit = w.add_signal_type(SignalEncoding.SE_2STATE, 1)
    byte = w.add_signal_type(SignalEncoding.SE_2STATE, 8)

    # 2. Describe the hierarchy. add_var() returns the variable's ID.
    with w.begin_hierarchy() as h:
        h.begin_scope(ScopeType.ST_MODULE, "top")
        clk = h.add_var("clk", bit, VarDir.VD_IN)
        count = h.add_var("count", byte, VarDir.VD_OUT)
        h.end_scope()

    # 3. Write value changes.
    with w.begin_vc_block(start_time=0) as vc:
        for i in range(8):
            vc.add_change(clk, time=i * 5, value=i % 2)
            if i % 2:
                vc.add_change(count, time=i * 5, value=i // 2 + 1)
```

Each `with` block writes its data to the file when it exits. Closing the
writer (here, leaving the outer `with`) writes the string table, type
registry and index, and is required for a readable file. If you do not use a
`with` statement, call `w.close()` yourself.

## Reading a trace

```python
from trlog import TrlReader

with TrlReader("counter.trl") as r:
    print("timescale: 1e%d s" % r.timescale_exp)

    hier = r.hierarchies[1]                    # keyed by hier_id
    for var_id, var in hier.vars.items():
        print(var_id, r.string_table.lookup(var.name_str_id))

    for c in r.read_signal(count):             # count = var ID from above
        print(f"t={c.time} count={c.value}")
```

Output:

```text
timescale: 1e-9 s
1 clk
2 count
t=5 count=1
t=15 count=2
t=25 count=3
t=35 count=4
```

`read_signal()` returns a list of `VcChange(var_id, time, value)` records.

## Writing to a file object

`TrlWriter` and `TrlReader` accept either a path or an open binary file
object. When you pass a file object, the writer does not close it, so you can
write to memory and read it back:

```python
import io
from trlog import TrlWriter, TrlReader

buf = io.BytesIO()
with TrlWriter(buf) as w:
    ...
buf.seek(0)
with TrlReader(buf) as r:
    ...
```

The file object must be seekable for the writer to record the index location
in the file header.

## Next steps

- To find signals by hierarchical path, filter transactions or export data,
  use the `trlog.query` package and the `trlog` command-line tool, which are
  documented separately.
- The full Python API is listed in the API reference ({doc}`../api/writer`,
  {doc}`../api/reader`).
