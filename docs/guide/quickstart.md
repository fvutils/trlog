# Quickstart

## Installation

```bash
pip install zuspec-trace
```

## Writing a trace

```python
from trlog import ZstWriter
from trlog._types import SignalEncoding, ScopeType

with ZstWriter("my_trace.trl") as w:
    # Register signal type: 1-bit 2-state
    clk_type = w.add_signal_type(SignalEncoding.SE_2STATE, 1)

    # Build hierarchy
    with w.begin_hierarchy() as h:
        h.begin_scope(ScopeType.ST_MODULE, "top")
        clk = h.add_var("clk", clk_type)
        h.end_scope()

    # Write value changes
    with w.begin_vc_block(start_time=0) as vc:
        for i in range(100):
            vc.add_change(clk, time=i * 10, value=i % 2)
```

## Reading a trace

```python
from trlog import ZstReader

with ZstReader("my_trace.trl") as r:
    print(f"Timescale: 10^{r.timescale_exp} s")
    changes = r.read_signal(clk)
    for c in changes:
        print(f"  t={c.time}: {c.value}")
```
