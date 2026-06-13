"""Stream dependency resolution (design §4.4, impl-plan §2.5/§2.c).

A small DAG over stream-type ids: ``stream S depends on input streams I`` means
S's codec must have I materialized before it can decode (derived/consumer
codecs). The same graph drives three things:

* **cycle detection** — mandatory at write time (reject), defensive at read;
* **read order** — :meth:`topo_order` yields dependencies *before* dependents,
  so a derived stream materializes after its inputs;
* **finalize order** — :meth:`finalize_order` is the *reverse*: a depended-on
  (shared) stream is finalized only after every stream that feeds it has closed
  (design §4.7), so a shared metadata stream is provably complete at close.

The reader pairs the graph with a lazy, memoized :class:`Materializer` so a
derived stream is computed only when iterated and reused for the query's life.
"""

from __future__ import annotations

from typing import Callable, Dict, List, Optional

from ._exceptions import ZstError


class DependencyError(ZstError):
    """A dependency cycle or a reference to an undeclared stream."""


class DependencyGraph:
    def __init__(self) -> None:
        self._deps: Dict[int, List[int]] = {}

    def add(self, stream_id: int, inputs) -> None:
        self._deps[stream_id] = list(inputs)

    @classmethod
    def from_catalog(cls, catalog: Dict[int, List[int]]) -> "DependencyGraph":
        g = cls()
        for sid, inputs in catalog.items():
            g.add(sid, inputs)
        return g

    def inputs(self, stream_id: int) -> List[int]:
        return list(self._deps.get(stream_id, []))

    def nodes(self) -> List[int]:
        nodes = set(self._deps)
        for ins in self._deps.values():
            nodes.update(ins)
        return sorted(nodes)

    def find_cycle(self) -> Optional[List[int]]:
        """Return a node sequence forming a cycle, or ``None`` if acyclic."""
        WHITE, GRAY, BLACK = 0, 1, 2
        color: Dict[int, int] = {}
        path: List[int] = []

        def visit(n: int) -> Optional[List[int]]:
            color[n] = GRAY
            path.append(n)
            for m in self._deps.get(n, []):
                if color.get(m, WHITE) == GRAY:
                    return path[path.index(m):] + [m]
                if color.get(m, WHITE) == WHITE:
                    c = visit(m)
                    if c:
                        return c
            path.pop()
            color[n] = BLACK
            return None

        for n in self.nodes():
            if color.get(n, WHITE) == WHITE:
                c = visit(n)
                if c:
                    return c
        return None

    def validate_acyclic(self) -> None:
        cycle = self.find_cycle()
        if cycle:
            arrow = " -> ".join(str(x) for x in cycle)
            raise DependencyError(f"stream dependency cycle: {arrow}")

    def topo_order(self) -> List[int]:
        """Dependencies before dependents. Raises :class:`DependencyError` on a
        cycle."""
        self.validate_acyclic()
        order: List[int] = []
        seen = set()

        def visit(n: int) -> None:
            if n in seen:
                return
            seen.add(n)
            for m in self._deps.get(n, []):
                visit(m)
            order.append(n)

        for n in self.nodes():
            visit(n)
        return order

    def finalize_order(self) -> List[int]:
        """Reverse dependency order: dependents first, depended-on streams last
        (design §4.7)."""
        return list(reversed(self.topo_order()))


class Materializer:
    """Lazy, memoized materialization of derived streams (design §4.4).

    ``compute(stream_id)`` is invoked at most once per stream and only when the
    stream (or a dependent) is first requested; the graph guarantees inputs are
    materialized first. Results are cached for the materializer's lifetime.
    """

    def __init__(self, graph: DependencyGraph,
                 compute: Callable[[int, Dict[int, object]], object]) -> None:
        self._graph = graph
        self._compute = compute
        self._cache: Dict[int, object] = {}
        self._in_progress: set = set()

    def materialize(self, stream_id: int) -> object:
        if stream_id in self._cache:
            return self._cache[stream_id]
        if stream_id in self._in_progress:
            raise DependencyError(
                f"dependency cycle hit at read time through stream {stream_id}")
        self._in_progress.add(stream_id)
        try:
            inputs = {i: self.materialize(i) for i in self._graph.inputs(stream_id)}
            result = self._compute(stream_id, inputs)
            self._cache[stream_id] = result
            return result
        finally:
            self._in_progress.discard(stream_id)

    def is_materialized(self, stream_id: int) -> bool:
        return stream_id in self._cache
