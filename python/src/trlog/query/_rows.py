"""Columns of matching rows: what ``count``, ``info``, ``stats`` and ``hist``
aggregate over (query_impl_status.md, Q4 aggregates).

Both engines produce :class:`Rows`. The Python engine fills it from
``Txn`` objects; the C engine fills it straight from its projection
columns (no ``Txn`` per row), plus ``Txn`` objects for the rows it could
not evaluate exactly. The aggregations here never depend on row order
(ties are broken by (start, id)), so the two give the same answers.
"""

from __future__ import annotations

from typing import Callable, List, Optional, Sequence

from ._values import MISSING, EnumVal


class TxnRef:
    """A light reference to a transaction, without fields.

    Aggregate results point at these (``Summary.min_txn``/``max_txn``,
    ``Gap.prev``/``next``).

    Attributes:
        id: Transaction id.
        start: Start time in ticks.
        end: End time in ticks, or None while open.
        stream: Stream path.
        type: Type name.
    """

    __slots__ = ("id", "start", "end", "stream", "type")

    def __init__(self, id, start, end, stream, type_name) -> None:
        self.id = id
        self.start = start
        self.end = end
        self.stream = stream
        self.type = type_name

    @property
    def ref(self) -> str:
        """The ``t:ID`` reference."""
        return f"t:{self.id}"

    def __repr__(self) -> str:
        return f"TxnRef(t:{self.id})"


class FieldKey:
    """A bare field read for ``--by FIELD``: ``t.fields.get(name)``, never a
    builtin (a field called ``start`` stays the field)."""

    uses_parent = uses_links = uses_children = False

    def __init__(self, name: str) -> None:
        self.name = name
        self.text = name

    def set_parent_lookup(self, lookup) -> None:
        pass

    def __call__(self, t):
        return t.fields.get(self.name, MISSING)


class Rows:
    """Per-row columns, in no particular order. ``ends`` is None for an open
    row; ``parents`` None for no parent; ``vals[k]`` holds the k-th value
    expression's query value (MISSING, int, bool, EnumVal, float, str ...)."""

    def __init__(self, trace, nvals: int) -> None:
        self.trace = trace
        self.ids: List[int] = []
        self.starts: List[int] = []
        self.ends: List[Optional[int]] = []
        self.stream_ids: List[int] = []
        self.type_ids: List[int] = []
        self.parents: List[Optional[int]] = []
        self.vals: List[list] = [[] for _ in range(nvals)]

    def __len__(self) -> int:
        return len(self.ids)

    @classmethod
    def from_txns(cls, trace, txns, values: Sequence[Callable]) -> "Rows":
        r = cls(trace, len(values))
        r.add_txns(txns, values)
        return r

    def add_txns(self, txns, values: Sequence[Callable]) -> None:
        txns = list(txns)
        self.ids += [t.id for t in txns]
        self.starts += [t.start for t in txns]
        self.ends += [t.end for t in txns]
        self.stream_ids += [t.stream_id for t in txns]
        self.type_ids += [t.type_id for t in txns]
        self.parents += [t.parent for t in txns]
        for col, f in zip(self.vals, values):
            col += [f(t) for t in txns]

    # -- derived columns ------------------------------------------------------

    def stream_names(self) -> list:
        path = {i: s.path for i, s in self.trace.streams.items()}
        return [path.get(i) or f"<stream#{i}>" for i in self.stream_ids]

    def type_names(self) -> list:
        name = {i: t.name for i, t in self.trace.types.items()}
        return [name.get(i) or f"<type#{i}>" for i in self.type_ids]

    def ref(self, i: int) -> TxnRef:
        s = self.trace.streams.get(self.stream_ids[i])
        t = self.trace.types.get(self.type_ids[i])
        return TxnRef(self.ids[i], self.starts[i], self.ends[i],
                      s.path if s else f"<stream#{self.stream_ids[i]}>",
                      t.name if t else f"<type#{self.type_ids[i]}>")

    def first(self, idx: Sequence[int]) -> int:
        """The row of ``idx`` that comes first by (start, id)."""
        st, ids = self.starts, self.ids
        return min(idx, key=lambda i: (st[i], ids[i]))

    def by_start(self, idx: Sequence[int]) -> List[int]:
        st, ids = self.starts, self.ids
        return sorted(idx, key=lambda i: (st[i], ids[i]))


def group_key_value(v):
    """``--by FIELD`` key from a field value (Trace._group_key)."""
    if v is MISSING:
        return None
    if isinstance(v, EnumVal):
        return v.label if v.label is not None else int(v)
    return v


def key_columns(rows: Rows, by: Sequence[str], field_cols: dict) -> List[list]:
    """One column per ``--by`` key. ``field_cols`` maps a field key to its
    value column index in ``rows.vals``."""
    out = []
    for k in by:
        if k == "stream":
            out.append(rows.stream_names())
        elif k == "type":
            out.append(rows.type_names())
        elif k == "open":
            out.append([e is None for e in rows.ends])
        elif k == "parent":
            out.append(list(rows.parents))
        else:
            out.append([group_key_value(v) for v in rows.vals[field_cols[k]]])
    return out


def group_order(rows: Rows, keycols: List[list]) -> Sequence[int]:
    """Row order for building group dicts. Equal keys of different types
    (0, 0.0, False) share a group named by the first row inserted, which the
    Python engine does in (start, id) order; match that when it can matter."""
    if any(type(v) in (float, bool) for col in keycols for v in col):
        return rows.by_start(range(len(rows)))
    return range(len(rows))
