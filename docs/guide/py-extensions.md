# Extension blocks

## Application-defined blocks

An extension block (`BLK_EXT`) carries data that the TRLOG format does not
define: coverage results, tool state, a custom binary table. Each block has a
4-byte type tag, a 16-bit version and a payload of bytes. Readers that do not
know a tag skip the block.

Write one with `TrlWriter.write_ext(ext_type, ext_version=0, payload=b"")`.
The block is written to the file immediately, at the current position among
the other blocks.

```python
import json
from trlog import TrlWriter, TrlReader

with TrlWriter("ext.trl") as w:
    cov = json.dumps({"covered": 42, "total": 50}).encode()
    w.write_ext(b"COVG", ext_version=1, payload=cov)
    w.write_ext(b"NOTE", payload=b"free-form text")

with TrlReader("ext.trl") as r:
    for blk in r.iter_ext_blocks(ext_type=b"COVG"):
        print(blk.ext_type, blk.ext_version, json.loads(blk.payload))
    print([b.ext_type for b in r.iter_ext_blocks()])
```

Output:

```text
b'COVG' 1 {'covered': 42, 'total': 50}
[b'COVG', b'NOTE']
```

`TrlReader.iter_ext_blocks(ext_type=None)` yields `ExtBlock` objects in file
order, optionally only those with a given tag. `ExtBlock` has the attributes
`ext_type` (`bytes`), `ext_version` (`int`) and `payload` (`bytes`).
`ExtBlock(ext_type, ...)` raises `ValueError` unless `ext_type` is exactly 4
bytes.

Extension blocks are not indexed: reading them scans the whole file. For data
you need to look up by key, use aux blocks (`write_aux()` / `read_aux()`, see
[Metadata](py-metadata.md)), which are indexed.

Choose a tag that is unlikely to clash with other tools, and bump
`ext_version` when you change the payload layout so readers can tell
versions apart.

## Call/return recording (`trlog.ext.callreturn`)

`trlog.ext.callreturn` records function call and return events, for example
firmware execution, and rebuilds the call tree when reading. It is built on
public writer and reader calls only: function signatures are stored in an aux
block under a signature stream type, the call stream declares that stream as
a dependency, and the call events go in `BLK_EXT` blocks tagged `TRCR`.

```python
from trlog import TrlWriter, TrlReader
from trlog.ext.callreturn import (
    CallReturnWriter, CallReturnReader, Signature, ArgType,
)

with TrlWriter("calls.trl") as w:
    calls = w.add_stream_type("cpu0_calls")
    sigs = w.add_stream_type("fw_signatures")
    cr = CallReturnWriter(w, calls, sigs, [
        Signature(1, "main", [], ArgType.INT),
        Signature(2, "memcpy", [ArgType.UINT, ArgType.UINT, ArgType.UINT]),
    ])
    cr.enter(1, time=0)
    cr.enter(2, [0x1000, 0x2000, 64], time=10)
    cr.exit(time=50)
    cr.exit(ret=0, time=60)
    cr.close()                      # must run before the writer closes

with TrlReader("calls.trl") as r:
    rd = CallReturnReader(r, calls)

    def show(call, depth=0):
        print("  " * depth + f"{call.name}{tuple(call.args)} "
              f"[{call.start}..{call.end}] -> {call.ret}")
        for child in call.children:
            show(child, depth + 1)

    for root in rd.call_tree():
        show(root)
```

Output:

```text
main() [0..60] -> 0
  memcpy(4096, 8192, 64) [10..50] -> None
```

Writer side:

- `Signature(func_id, name, arg_types, ret_type=ArgType.NONE)` describes a
  function. `ArgType` is `NONE`, `UINT`, `INT`, `REAL` or `STR`.
- `CallReturnWriter(writer, call_stream_id, sig_stream_id, signatures,
  window=64)` takes two stream type IDs from `add_stream_type()`. Several call
  streams can share one signature stream.
- `enter(func_id, args=(), time=0)` records a call. The number of arguments
  must match the signature, otherwise it raises `ValueError`.
- `exit(ret=None, time=0)` records the return of the innermost open call.
- `enter_raw(func_id, args, start, end)` records a call that has no matching
  signature. It is written as an ordinary transaction, so readers without
  this module can still see it.
- `close()` writes everything. Call it before closing the `TrlWriter`;
  nothing is written until then.

Reader side:

- `CallReturnReader(reader, call_stream_id)` loads the events and signatures.
- `call_tree()` returns the root `Call` objects. A `Call` has `func_id`,
  `name`, `args`, `start`, `end`, `ret` and `children`.
- `events()` returns the flat list of enter/exit events as dictionaries with
  `kind`, `time`, `depth` and, for enter events, `func_id`, `name` and `args`,
  and for exit events, `ret`.
- `signatures()` returns `{func_id: Signature}`.
