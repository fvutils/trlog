Types and records
=================

.. currentmodule:: trlog

All names on this page are importable from the ``trlog`` package.

Enumerations
------------

.. autoclass:: trlog.SignalEncoding
   :members:
   :undoc-members:
   :member-order: bysource

.. autoclass:: trlog.Radix
   :members:
   :undoc-members:
   :member-order: bysource

.. autoclass:: trlog.ScopeType
   :members:
   :undoc-members:
   :member-order: bysource

.. autoclass:: trlog.VarDir
   :members:
   :undoc-members:
   :member-order: bysource

.. autoclass:: trlog.HierKind
   :members:
   :undoc-members:
   :member-order: bysource

.. autoclass:: trlog.FieldType
   :members:
   :undoc-members:
   :member-order: bysource

.. autoclass:: trlog.LinkType
   :members:
   :undoc-members:
   :member-order: bysource

.. autoclass:: trlog.AttrType
   :members:
   :undoc-members:
   :member-order: bysource

.. autoclass:: trlog.WellKnownAttr
   :members:
   :undoc-members:
   :member-order: bysource

Type registry entries
---------------------

.. autoclass:: trlog.FieldDef

.. autoclass:: trlog.SignalTypeEntry

.. autoclass:: trlog.TxnSchemaEntry

.. autoclass:: trlog.EnumTypeEntry

.. autoclass:: trlog.EnumValue

.. autoclass:: trlog.StreamDeclEntry

Hierarchy nodes
---------------

.. autoclass:: trlog.HierarchyHeader

.. autoclass:: trlog.HScope

.. autoclass:: trlog.HVar

.. autoclass:: trlog.HStream

.. autoclass:: trlog.HAttr

.. autoclass:: trlog.TypedAttr

Value changes
-------------

.. autoclass:: trlog.VcChange

Transaction records
-------------------

.. autoclass:: trlog.TxnAttr

.. autoclass:: trlog.TxnFull

.. autoclass:: trlog.TxnBegin

.. autoclass:: trlog.TxnAttrRecord

.. autoclass:: trlog.TxnEnd

.. autoclass:: trlog.TxnLink

.. autoclass:: trlog.TxnMeta

Exceptions
----------

.. autoexception:: trlog.ZstError

.. autoexception:: trlog.ZstMagicError

.. autoexception:: trlog.ZstVersionError

.. autoexception:: trlog.ZstFormatError

.. autoexception:: trlog.ZstCompressionError

.. autoexception:: trlog.ZstNoIndexError

.. autoexception:: trlog.ZstCorruptError

.. autoexception:: trlog.DependencyError
