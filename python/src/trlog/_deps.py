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
    """A directed graph of "X depends on Y" edges between integer IDs.

    Used for stream-type dependencies and derived-signal inputs. Nodes are
    created implicitly by :meth:`add`.
    """

    def __init__(self) -> None:
        self._deps: Dict[int, List[int]] = {}

    def add(self, stream_id: int, inputs) -> None:
        """Set the inputs of ``stream_id``, replacing any earlier ones."""
        self._deps[stream_id] = list(inputs)

    @classmethod
    def from_catalog(cls, catalog: Dict[int, List[int]]) -> "DependencyGraph":
        """Build a graph from a ``{id: [input_ids]}`` dictionary."""
        g = cls()
        for sid, inputs in catalog.items():
            g.add(sid, inputs)
        return g

    def inputs(self, stream_id: int) -> List[int]:
        """Return the direct inputs of ``stream_id`` (empty if none)."""
        return list(self._deps.get(stream_id, []))

    def nodes(self) -> List[int]:
        """Return all IDs that appear in the graph, sorted."""
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
        """Raise :class:`DependencyError` if the graph has a cycle."""
        cycle = self.find_cycle()
        if cycle:
            arrow = " -> ".join(str(x) for x in cycle)
            raise DependencyError(f"stream dependency cycle: {arrow}")

    def topo_order(self) -> List[int]:
        """Return all nodes with each node's inputs before the node.

        Raises:
            DependencyError: The graph has a cycle.
        """
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
        """Return the reverse of :meth:`topo_order`: dependents first.

        Raises:
            DependencyError: The graph has a cycle.
        """
        return list(reversed(self.topo_order()))


class Materializer:
    """Computes nodes of a :class:`DependencyGraph` on demand, inputs first.

    Usually obtained from :meth:`~trlog.TrlReader.materializer`.

    Args:
        graph: The dependency graph.
        compute: Called as ``compute(stream_id, inputs)``, where ``inputs``
            maps each input ID to its computed result. Called at most once
            per ID; results are cached for the materializer's lifetime.
    """

    def __init__(self, graph: DependencyGraph,
                 compute: Callable[[int, Dict[int, object]], object]) -> None:
        self._graph = graph
        self._compute = compute
        self._cache: Dict[int, object] = {}
        self._in_progress: set = set()

    def materialize(self, stream_id: int) -> object:
        """Return the result for ``stream_id``, computing it and its inputs if
        they are not cached yet.

        Raises:
            DependencyError: A cycle was reached.
        """
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
        """Return whether a result for ``stream_id`` is cached."""
        return stream_id in self._cache
