Writer
======

.. currentmodule:: trlog

``TrlWriter`` creates a trace file. The guide pages show it in use:
:doc:`../guide/quickstart`, :doc:`../guide/hierarchies`,
:doc:`../guide/py-value-changes` and :doc:`../guide/transactions`.

TrlWriter
---------

.. autoclass:: trlog.TrlWriter
   :members: intern, add_signal_type, add_txn_schema, add_enum_type,
             add_stream_type, set_stream_codec, begin_hierarchy, begin_vc_block,
             begin_txn_block, add_trace_metadata, add_stream_metadata,
             add_stream_profile, add_derived_signal, declare_dependencies,
             write_aux, write_ext, flush_string_table, flush_type_registry,
             close

Block builders
--------------

These objects are returned by the ``begin_*`` methods. Do not create them
directly.

.. autoclass:: trlog._writer._HierarchyContext
   :members: begin_scope, end_scope, add_var, add_stream, add_attr,
             add_typed_attr, flush

.. autoclass:: trlog._writer._VcBlockContext
   :members: add_change, set_initial, flush

.. autoclass:: trlog._writer._TxnBlockContext
   :members: write_full, write_begin, write_attr, write_end, write_link,
             write_meta, flush
