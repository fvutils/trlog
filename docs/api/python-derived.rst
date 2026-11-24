Derived signals and dependencies
================================

.. currentmodule:: trlog

See :doc:`../guide/py-derived` for usage. Derived signals are defined with
:meth:`TrlWriter.add_derived_signal` and evaluated with
:meth:`TrlReader.read_derived`.

Expressions
-----------

.. autoclass:: trlog.Input

.. autoclass:: trlog.Const

.. autoclass:: trlog._derived.Expr
   :members: lnot, eq, ne, shl, shr, reduce_and, reduce_or, slice_, concat

.. autofunction:: trlog.compile_expr

Dependency graph
----------------

.. autoclass:: trlog.DependencyGraph
   :members: add, from_catalog, inputs, nodes, find_cycle, validate_acyclic,
             topo_order, finalize_order

.. autoclass:: trlog.Materializer
   :members: materialize, is_materialized

.. autoexception:: trlog.DependencyError
   :no-index:
