"""Query API over trlog traces.

The ``trlog`` command is a thin layer over this package; scripts can use it
directly::

    import trlog.query
    with trlog.query.open("run.trl") as q:
        r = q.find("resp != 'OKAY'", streams="top.env.*", types="axi_wr")
        print(r.matched, r.by_stream())
        for t in r.txns[:5]:
            print(t.ref, t.start, t.dur, t.fields)
        print(q.count(by=["stream", "resp"]).groups)
        print([(d, t.ref) for d, t in q.tree(10, down=None).nodes])
        print([(d, t.ref) for d, t in q.links(102, up=None, down=0).nodes])

Scans run on the C engine in libtrl when it loads, else in pure Python,
with the same results; see :func:`open`.
"""

from ._engine import (
    LINK_KINDS, AroundResult, CountResult, FieldInfo, FindResult, Link, LinkResult,
    ScanStats, Selection, StreamInfo, Trace, TreeResult, Txn, TypeInfo, open,
)
from ._expr import Filter, QueryError, SchemaView, compile_filter
from ._values import MISSING, EnumVal, LogicVal, format_time, parse_time, parse_window

__all__ = [
    "open", "Trace", "Txn", "Link", "Selection", "FindResult", "CountResult",
    "TreeResult", "LinkResult", "AroundResult", "LINK_KINDS",
    "ScanStats", "StreamInfo", "TypeInfo", "FieldInfo",
    "Filter", "QueryError", "SchemaView", "compile_filter",
    "MISSING", "EnumVal", "LogicVal", "format_time", "parse_time", "parse_window",
]
