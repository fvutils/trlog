"""``trlog export sqlite|parquet`` (query_cli_design.md §4.2).

Both formats get the same normalised tables:

    txn          id, stream_id, type_id, start, end, dur, parent, open
    attr_<type>  id plus one column per field of that type
    link         src, tgt, kind, label
    meta         id, key, value
    stream       id, path, type, first, last, count, metadata (JSON)
    type         id, name, fields (JSON)
    trace        key, value  (export info: source, query, time_unit, timescale_exp)
    trace_meta   key, value  (the trace's own metadata, as written)

SQLite also gets a ``txn_<type>`` view per type that joins ``txn``, the stream
path and ``attr_<type>``. Parquet writes one ``<table>.parquet`` per table into
a directory and needs ``pyarrow``, which is optional.

Column values: integers that fit in a signed 64-bit integer are stored as
integers; wider ones, and 4-state vectors with x/z bits, as hex text. Enums are
stored as their label, bytes as blobs.
"""

from __future__ import annotations

import json
import math
import os
import re
import shutil
import sqlite3
import tempfile
from typing import Dict, List, Optional, Tuple

from ._expr import QueryError
from ._values import MISSING, EnumVal, LogicVal

FORMATS = ("sqlite", "parquet")
INT64_MIN, INT64_MAX = -(1 << 63), (1 << 63) - 1

_SQL_TYPE = {
    "u8": "INTEGER", "u16": "INTEGER", "u32": "INTEGER", "u64": "INTEGER",
    "i8": "INTEGER", "i16": "INTEGER", "i32": "INTEGER", "i64": "INTEGER",
    "bool": "INTEGER", "time": "INTEGER", "bitvec": "INTEGER", "logicvec": "INTEGER",
    "f32": "REAL", "f64": "REAL", "str": "TEXT", "enum": "TEXT", "bytes": "BLOB",
}


def cell(v):
    """One field value as a SQLite/Arrow-friendly Python value."""
    if v is MISSING or v is None:
        return None
    if isinstance(v, EnumVal):
        return v.label if v.label is not None else int(v)
    if isinstance(v, LogicVal):
        return v.hex()
    if isinstance(v, bool):
        return int(v)
    if isinstance(v, int):
        return v if INT64_MIN <= v <= INT64_MAX else hex(v)
    if isinstance(v, (bytes, bytearray)):
        return bytes(v)
    if isinstance(v, (list, tuple)):
        return json.dumps([cell(x) if not isinstance(cell(x), bytes) else cell(x).hex()
                           for x in v])
    return v


def _ident(name: str) -> str:
    return re.sub(r"\W", "_", name) or "_"


def _jsonable(v):
    if isinstance(v, (bytes, bytearray)):
        return bytes(v).hex()
    if isinstance(v, float) and (math.isnan(v) or math.isinf(v)):
        return str(v)
    return v


# ---------------------------------------------------------------------------
# Table model
# ---------------------------------------------------------------------------

class Table:
    def __init__(self, name: str, columns: List[Tuple[str, str]]) -> None:
        self.name = name
        self.columns = columns          # (name, sqlite type)
        self.rows: List[tuple] = []


def build_tables(trace, sel) -> Tuple[List[Table], Dict[str, Tuple[str, List[str]]]]:
    """The §4.2 tables for the selection, plus {view name: (attr table,
    field columns)} for the per-type views."""
    txns = trace._matches(sel, want_links=True)
    txns.sort(key=lambda t: (t.start, t.id))

    txn = Table("txn", [("id", "INTEGER PRIMARY KEY"), ("stream_id", "INTEGER"),
                        ("type_id", "INTEGER"), ("start", "INTEGER"),
                        ("end", "INTEGER"), ("dur", "INTEGER"), ("parent", "INTEGER"),
                        ("open", "INTEGER")])
    link = Table("link", [("src", "INTEGER"), ("tgt", "INTEGER"), ("kind", "TEXT"),
                          ("label", "TEXT")])
    meta = Table("meta", [("id", "INTEGER"), ("key", "TEXT"), ("value", "TEXT")])

    # one attribute table per type, named after the type (made unique)
    attr: Dict[int, Table] = {}
    attr_cols: Dict[int, List[str]] = {}
    views: Dict[str, Tuple[str, List[str]]] = {}
    used = {"txn", "link", "meta", "stream", "type", "trace", "trace_meta"}
    for ti in sorted(trace.types.values(), key=lambda t: t.id):
        base = "attr_" + _ident(ti.name)
        name = base if base not in used else f"{base}_{ti.id}"
        used.add(name)
        cols, seen = [], {"id"}
        for f in ti.fields:
            c = _ident(f.name)
            while c in seen:
                c += "_"
            seen.add(c)
            cols.append((c, _SQL_TYPE.get(f.kind, "")))
        attr[ti.id] = Table(name, [("id", "INTEGER PRIMARY KEY")] + cols)
        attr_cols[ti.id] = [c for c, _ in cols]
        views["txn_" + name[len("attr_"):]] = (name, attr_cols[ti.id])

    per_stream: Dict[int, list] = {}
    edges = set()
    for t in txns:
        txn.rows.append((t.id, t.stream_id, t.type_id, t.start, t.end, t.dur,
                         t.parent, int(t.end is None)))
        ti = trace.types.get(t.type_id)
        if ti is not None:
            attr[t.type_id].rows.append(
                (t.id,) + tuple(cell(t.fields.get(f.name, MISSING)) for f in ti.fields))
        for k, v in t.meta.items():
            meta.rows.append((t.id, k, v if isinstance(v, str) else json.dumps(_jsonable(v))))
        for ln in t.links:
            src, tgt = (t.id, ln.other) if ln.dir == "out" else (ln.other, t.id)
            edges.add((src, tgt, ln.kind, ln.label))
        st = per_stream.setdefault(t.stream_id, [t.start, t.start, 0])
        st[0] = min(st[0], t.start)
        st[1] = max(st[1], t.start if t.end is None else t.end)
        st[2] += 1
    link.rows = sorted(edges, key=lambda e: (e[0], e[1], e[2], e[3] or ""))

    stream = Table("stream", [("id", "INTEGER PRIMARY KEY"), ("path", "TEXT"),
                              ("type", "TEXT"), ("first", "INTEGER"),
                              ("last", "INTEGER"), ("count", "INTEGER"),
                              ("metadata", "TEXT")])
    for s in sorted(trace.streams.values(), key=lambda s: s.id):
        first, last, n = per_stream.get(s.id, (None, None, 0))
        md = {k: _jsonable(v) for k, v in s.metadata.items()}
        stream.rows.append((s.id, s.path, s.decl_name, first, last, n,
                            json.dumps(md, sort_keys=True) if md else None))

    typ = Table("type", [("id", "INTEGER PRIMARY KEY"), ("name", "TEXT"),
                         ("fields", "TEXT")])
    for ti in sorted(trace.types.values(), key=lambda t: t.id):
        fields = []
        for f in ti.fields:
            d = {"name": f.name, "kind": f.kind}
            if f.enum_labels:
                d["enum"] = {str(k): v for k, v in sorted(f.enum_labels.items())}
            fields.append(d)
        typ.rows.append((ti.id, ti.name, json.dumps(fields)))

    tr = Table("trace", [("key", "TEXT PRIMARY KEY"), ("value", "TEXT")])
    kv = {"source": os.path.abspath(trace.path), "time_unit": trace.time_unit,
          "timescale_exp": str(trace.timescale_exp),
          "query": json.dumps(sel.query_dict(), sort_keys=True)}
    tr.rows = sorted(kv.items())
    # kept apart from ``trace`` so a metadata key such as "source" cannot
    # shadow (or be shadowed by) an export key
    tm = Table("trace_meta", [("key", "TEXT PRIMARY KEY"), ("value", "TEXT")])
    tm.rows = sorted((k, v if isinstance(v, str) else json.dumps(_jsonable(v)))
                     for k, v in trace._r.trace_metadata().items())

    tables = ([txn] + [attr[i] for i in sorted(attr)]
              + [link, meta, stream, typ, tr, tm])
    return tables, views


# ---------------------------------------------------------------------------
# Writers
# ---------------------------------------------------------------------------

def _q(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def write_sqlite(tables: List[Table], views, path: str) -> None:
    con = sqlite3.connect(path)
    try:
        for t in tables:
            cols = ", ".join(f"{_q(c)} {ty}".rstrip() for c, ty in t.columns)
            con.execute(f"CREATE TABLE {_q(t.name)} ({cols})")
            marks = ", ".join("?" * len(t.columns))
            con.executemany(f"INSERT INTO {_q(t.name)} VALUES ({marks})", t.rows)
        con.execute("CREATE INDEX txn_stream_start ON txn(stream_id, start)")
        con.execute("CREATE INDEX txn_start ON txn(start)")
        con.execute("CREATE INDEX link_tgt ON link(tgt)")
        con.execute("CREATE INDEX link_src ON link(src)")
        con.execute("CREATE INDEX meta_id ON meta(id)")
        for view, (attr, cols) in views.items():
            fcols = "".join(f", a.{_q(c)}" for c in cols)
            con.execute(
                f"CREATE VIEW {_q(view)} AS SELECT t.id, s.path AS stream, t.start, "
                f"t.\"end\", t.dur, t.parent, t.open{fcols} FROM txn t "
                f"JOIN {_q(attr)} a ON a.id = t.id "
                f"JOIN stream s ON s.id = t.stream_id")
        con.commit()
    finally:
        con.close()


def _arrow_column(values: list, sql_type: str):
    import pyarrow as pa
    if sql_type.startswith("INTEGER"):
        if all(v is None or isinstance(v, int) for v in values):
            return pa.array(values, pa.int64())
        return pa.array([None if v is None else (hex(v) if isinstance(v, int) else str(v))
                         for v in values], pa.string())
    if sql_type == "REAL":
        return pa.array(values, pa.float64())
    if sql_type == "BLOB":
        return pa.array(values, pa.binary())
    if sql_type == "TEXT":
        return pa.array([None if v is None else str(v) for v in values], pa.string())
    return pa.array([None if v is None else str(v) for v in values], pa.string())


def write_parquet(tables: List[Table], outdir: str) -> None:
    import pyarrow as pa
    import pyarrow.parquet as pq
    os.makedirs(outdir, exist_ok=True)
    for t in tables:
        cols = list(zip(*t.rows)) if t.rows else [()] * len(t.columns)
        arrays = [_arrow_column(list(c), ty) for c, (_, ty) in zip(cols, t.columns)]
        pq.write_table(pa.Table.from_arrays(arrays, names=[c for c, _ in t.columns]),
                       os.path.join(outdir, t.name + ".parquet"))


def _need_pyarrow() -> None:
    try:
        import pyarrow  # noqa: F401
        import pyarrow.parquet  # noqa: F401
    except ImportError:
        raise QueryError("missing_dependency",
                         "export parquet needs pyarrow, which is not installed",
                         package="pyarrow",
                         hint="pip install pyarrow  (or `pip install trlog[parquet]`); "
                              "`trlog export sqlite` needs nothing extra")


def export(trace, fmt: str, out: str, sel, force: bool = False) -> dict:
    """Write the selection and return {"format", "out", "tables": {name: rows}}."""
    if fmt not in FORMATS:
        raise QueryError("bad_value", f"unknown export format '{fmt}'", value=fmt,
                         valid_values=list(FORMATS))
    if fmt == "parquet":
        _need_pyarrow()
    out = os.path.abspath(out)
    if os.path.exists(out) and not force:
        raise QueryError("exists", f"{out} already exists", path=out,
                         hint="pass --force to replace it")
    if fmt == "parquet" and os.path.isdir(out) and any(
            not n.endswith(".parquet") for n in os.listdir(out)):
        raise QueryError("exists", f"{out} holds files other than *.parquet; "
                         "not replacing it", path=out)
    if fmt == "sqlite" and os.path.isdir(out):
        raise QueryError("exists", f"{out} is a directory", path=out)
    tables, views = build_tables(trace, sel)
    parent = os.path.dirname(out) or "."
    if not os.path.isdir(parent):
        raise QueryError("file_error", f"directory {parent} does not exist", path=out)
    # write next to the target, then move it into place
    if fmt == "sqlite":
        fd, tmp = tempfile.mkstemp(prefix=".trlog-export-", suffix=".tmp", dir=parent)
        os.close(fd)
        os.unlink(tmp)
        try:
            write_sqlite(tables, views, tmp)
            os.replace(tmp, out)
        finally:
            if os.path.exists(tmp):
                os.unlink(tmp)
    else:
        tmp = tempfile.mkdtemp(prefix=".trlog-export-", dir=parent)
        try:
            write_parquet(tables, tmp)
            if os.path.isdir(out):
                shutil.rmtree(out)
            elif os.path.exists(out):
                os.unlink(out)
            os.replace(tmp, out)
        finally:
            if os.path.isdir(tmp):
                shutil.rmtree(tmp)
    return {"format": fmt, "out": out,
            "tables": {t.name: len(t.rows) for t in tables},
            "views": sorted(views) if fmt == "sqlite" else []}
