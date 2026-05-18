# Transactions

TRLOG supports recording PSS/UVM-style transaction streams alongside VC data.

## Transaction Schemas

Register a schema describing the transaction's attribute fields:

```python
from trlog._types import FieldType, FieldDef

fields = [
    FieldDef(name_str_id=w.intern("address"), field_type=FieldType.FT_U64),
    FieldDef(name_str_id=w.intern("data"),    field_type=FieldType.FT_U64),
    FieldDef(name_str_id=w.intern("write"),   field_type=FieldType.FT_BOOL),
]
schema_id = w.add_txn_schema("AXI_Transaction", fields)
```

## Writing Transactions

### Atomic (TR_FULL)

Use `write_full` when the entire transaction is captured at flush time:

```python
from trlog._types import TxnAttr

with w.begin_txn_block(start_time=0) as txn:
    txn.write_full(
        stream_inst_id=1,
        txn_type_id=schema_id,
        txn_id=42,
        start=100,
        end=200,
        parent=0,
        attrs=[
            TxnAttr(field_idx=0, value=0xDEAD),
            TxnAttr(field_idx=1, value=0xBEEF),
            TxnAttr(field_idx=2, value=True),
        ],
    )
```

### Streaming (TR_BEGIN / TR_ATTR / TR_END)

Use begin/end pairs when the transaction spans multiple blocks:

```python
with w.begin_txn_block(0) as txn:
    txn.write_begin(stream_inst_id=1, txn_type_id=schema_id, txn_id=1,
                    start=0, parent=0)

# ... later, possibly in a different block ...
with w.begin_txn_block(500) as txn:
    txn.write_end(txn_id=1, end_time=600)
```

## Reading Transactions

```python
with ZstReader("trace.trl") as r:
    for block_records in r.iter_txn_blocks():
        for rec in block_records:
            print(rec)
```
