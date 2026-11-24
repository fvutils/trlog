Extension blocks
================

See :doc:`../guide/py-extensions` for usage.

ExtBlock
--------

.. autoclass:: trlog.ExtBlock
   :members: read_block, encode_block

Call/return recording
---------------------

``trlog.ext.callreturn`` records function call and return events and
rebuilds the call tree when reading.

.. autoclass:: trlog.ext.callreturn.CallReturnWriter
   :members: enter, exit, enter_raw, close

.. autoclass:: trlog.ext.callreturn.CallReturnReader
   :members: call_tree, events, signatures

.. autoclass:: trlog.ext.callreturn.Signature

.. autoclass:: trlog.ext.callreturn.ArgType
   :members:
   :undoc-members:
   :member-order: bysource

.. autoclass:: trlog.ext.callreturn.Call
