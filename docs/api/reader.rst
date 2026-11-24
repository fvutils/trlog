Reader
======

.. currentmodule:: trlog

``TrlReader`` opens a trace file and reads its contents. The guide pages show
it in use: :doc:`../guide/quickstart`, :doc:`../guide/hierarchies`,
:doc:`../guide/py-value-changes` and :doc:`../guide/transactions`.

TrlReader
---------

.. autoclass:: trlog.TrlReader
   :members: timescale_exp, timezero, version_major, string_table,
             type_registry, hierarchies, scope_tree, scopes,
             read_signal, iter_vc_blocks, seek_time,
             read_stream, iter_txn_blocks, find_streams,
             trace_metadata, stream_metadata, stream_codec, profiles,
             aux_entries, aux_keys, read_aux,
             iter_ext_blocks,
             derived_signals, read_derived,
             dependency_catalog, dependencies, dependency_graph,
             dependency_order, materializer,
             close

Hierarchies, strings and types
------------------------------

.. autoclass:: trlog.ScopeNode

.. autoclass:: trlog._hierarchy.HierarchyBlock
   :members: hier_id

.. autoclass:: trlog._string_table.StringTable
   :members: lookup

.. autoclass:: trlog._type_registry.TypeRegistry
   :members: get_signal_type, get_txn_schema, get_enum_type, get_stream_decl
