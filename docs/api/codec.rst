trlog.codec
===========

.. automodule:: trlog.codec

Codec ids
---------

.. autodata:: trlog.codec.CORE_VALUECHANGE
.. autodata:: trlog.codec.CORE_RECORD
.. autodata:: trlog.codec.CORE_BYTESPLIT
.. autodata:: trlog.codec.CORE_DERIVED
.. autofunction:: trlog.codec.canonical_codec_id

Codec base classes
------------------

.. autoclass:: trlog.codec.VcCodec
   :members:
   :inherited-members: ABC

.. autoclass:: trlog.codec.TxnCodec
   :members:
   :inherited-members: ABC

.. autoclass:: trlog.codec.Capability

.. autoclass:: trlog.codec.Store
   :members: intern, compress, decompress, codec_ctx, has_codec_ctx

Registry
--------

.. autofunction:: trlog.codec.register_vc_codec
.. autofunction:: trlog.codec.register_txn_codec
.. autofunction:: trlog.codec.lookup_vc_codec
.. autofunction:: trlog.codec.lookup_txn_codec
.. autofunction:: trlog.codec.registered_vc_codecs
.. autofunction:: trlog.codec.registered_txn_codecs

.. data:: trlog.codec.vc_codec(codec_id, *, version=1, caps=Capability.NONE)

   Class decorator that registers a :class:`VcCodec` subclass. It validates
   ``codec_id``, sets the ``codec_id``, ``version`` and ``caps`` class
   attributes, creates one instance with no arguments and registers it with
   :func:`register_vc_codec`. It returns the class unchanged.

   Raises ``ValueError`` for an invalid id and ``TypeError`` if the class does
   not subclass :class:`VcCodec`.

.. data:: trlog.codec.txn_codec(codec_id, *, version=1, caps=Capability.NONE)

   Class decorator that registers a :class:`TxnCodec` subclass. Same behavior
   as :data:`vc_codec`.

Built-in codecs
---------------

.. autoclass:: trlog.codec.CoreValueChangeCodec
.. autoclass:: trlog.codec.CoreRecordCodec
.. autoclass:: trlog.codec.CoreBytesplitCodec
.. autoclass:: trlog.codec.CoreDerivedCodec

Byte-plane split
----------------

.. automodule:: trlog._bytesplit
   :no-index:

.. autofunction:: trlog._bytesplit.byte_split
.. autofunction:: trlog._bytesplit.byte_unsplit
.. autoclass:: trlog._bytesplit.BytesplitBlock
   :members: add_change, set_initial, encode_block, decode_changes

ctypes bridge
-------------

.. automodule:: trlog._codec_bridge
   :no-index:

.. autodata:: trlog._codec_bridge.AVAILABLE
   :annotation:

.. autofunction:: trlog._codec_bridge.register_native
.. autoclass:: trlog._codec_bridge.NativeBridge
   :members: register, raise_pending
