# Hierarchies

TRLOG files contain one or more *hierarchy blocks* that describe the design structure.

## Scopes and Variables

Hierarchies are built as nested scopes using `begin_scope`/`end_scope`:

```python
with w.begin_hierarchy() as h:
    h.begin_scope(ScopeType.ST_MODULE, "top")

    h.begin_scope(ScopeType.ST_MODULE, "cpu")
    clk = h.add_var("clk", clk_type)
    data = h.add_var("data_bus", bus_type)
    h.end_scope()  # cpu

    h.end_scope()  # top
```

## Variable IDs

`add_var()` returns a monotonically-increasing `var_id`. This ID is used in
`VcDataBlock` to associate value changes with signals. The ID is stable across
the lifetime of the writer.

## Multiple Hierarchies

A single TRLOG file may contain multiple hierarchy blocks (e.g., for multiple
elaboration instances). Each is identified by a `hier_id`:

```python
with w.begin_hierarchy(hier_id=1, name="cpu_core") as h:
    ...
with w.begin_hierarchy(hier_id=2, name="memory") as h:
    ...
```

## Scope Types

| Constant | Description |
|---|---|
| `ST_MODULE` | HDL module / SystemVerilog module |
| `ST_TASK` | Task or function |
| `ST_FUNCTION` | Function |
| `ST_BEGIN` | Named begin/end block |
| `ST_FORK` | Fork/join block |
