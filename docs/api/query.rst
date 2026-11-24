Query API (trlog.query)
=======================

The ``trlog.query`` package searches, navigates and summarises the
transactions in a trace. The :doc:`../guide/query` guide shows it in use,
and the :doc:`../guide/cli` guide covers the ``trlog`` command built on it
and the filter expression syntax.

.. automodule:: trlog.query
   :no-members:

All times, in arguments and in results, are integer ticks in the trace's
time unit (``Trace.timescale_exp``).

Opening a trace
---------------

.. autofunction:: trlog.query.open

.. autoclass:: trlog.query.Trace
   :members: select, find, count, show, graph, tree, links, around, active,
             stats, hist, gaps, match, export, info, stream_summary, schema,
             span, type_by_name, stream_by_path, schema_view, has_index, close

Transactions
------------

.. autoclass:: trlog.query.Txn
   :members: ref, open, dur

.. autoclass:: trlog.query.Link

.. autodata:: trlog.query.LINK_KINDS
   :annotation:

Catalog
-------

.. autoclass:: trlog.query.StreamInfo

.. autoclass:: trlog.query.TypeInfo
   :members: field

.. autoclass:: trlog.query.FieldInfo

Selections and results
----------------------

.. autoclass:: trlog.query.Selection
   :members: query_dict

.. autoclass:: trlog.query.FindResult
   :members: matched, span, by_stream

.. autoclass:: trlog.query.CountResult

.. autoclass:: trlog.query.TreeResult

.. autoclass:: trlog.query.LinkResult

.. autoclass:: trlog.query.AroundResult

.. autoclass:: trlog.query.ScanStats
   :members: to_dict

Analysis results
----------------

:meth:`~trlog.query.Trace.stats`, :meth:`~trlog.query.Trace.hist`,
:meth:`~trlog.query.Trace.gaps` and :meth:`~trlog.query.Trace.match` return
these classes. They live in ``trlog.query._analysis`` and are not exported
from ``trlog.query``.

.. autoclass:: trlog.query._analysis.StatsResult

.. autoclass:: trlog.query._analysis.Summary

.. autoclass:: trlog.query._analysis.HistResult
   :members: edges

.. autoclass:: trlog.query._analysis.GapsResult

.. autoclass:: trlog.query._analysis.Gap
   :members: dur

.. autoclass:: trlog.query._analysis.MatchResult

.. autoclass:: trlog.query._rows.TxnRef
   :members: ref

Values and times
----------------

.. autodata:: trlog.query.MISSING
   :annotation:

.. autoclass:: trlog.query.EnumVal

.. autoclass:: trlog.query.LogicVal
   :members: hex

.. autofunction:: trlog.query.parse_time

.. autofunction:: trlog.query.parse_window

.. autofunction:: trlog.query.format_time

Expressions and errors
----------------------

.. autoexception:: trlog.query.QueryError

.. autofunction:: trlog.query.compile_filter

.. autoclass:: trlog.query.Filter()
   :members: match, set_parent_lookup

.. autoclass:: trlog.query.SchemaView
   :members: visible_types, field_specs

.. autoclass:: trlog.query._expr.FieldSpec

Command-line entry point
------------------------

.. autofunction:: trlog.cli.main
