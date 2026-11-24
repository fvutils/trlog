SystemVerilog API (``trl_pkg``)
===============================

Reference for the SystemVerilog package. For an introduction with examples,
see :doc:`../guide/systemverilog`. The DPI-C imports the classes are built
on are listed separately, in :doc:`sv_dpi`.

.. autosvpackage:: trl_pkg

Package functions and types
---------------------------

.. autosvfunction:: trl_pkg::trl_enabled
.. autosvfunction:: trl_pkg::trl_shutdown
.. autosvfunction:: trl_pkg::trl_now

.. autosvsummary::
   :packages: trl_pkg
   :kinds: typedef

Recording
---------

.. autosvclass:: trl_pkg::trl_db
   :members:
   :exclude-members: h, m_path, m_rdb, m_fidx

.. autosvclass:: trl_pkg::trl_stream
   :members:
   :exclude-members: h, m_path, m_rdb, m_fidx

.. autosvclass:: trl_pkg::trl_type
   :members:
   :exclude-members: h, m_path, m_rdb, m_fidx

.. autosvclass:: trl_pkg::trl_txn
   :members:
   :exclude-members: h, m_path, m_rdb, m_fidx

Reading
-------

.. autosvclass:: trl_pkg::trl_reader
   :members:
   :exclude-members: h, m_path, m_rdb, m_fidx

.. autosvclass:: trl_pkg::trl_rstream
   :members:
   :exclude-members: h, m_path, m_rdb, m_fidx

.. autosvclass:: trl_pkg::trl_schema
   :members:
   :exclude-members: h, m_path, m_rdb, m_fidx

.. autosvclass:: trl_pkg::trl_cursor
   :members:
   :exclude-members: h, m_path, m_rdb, m_fidx

Shutdown module
---------------

.. autosvmodule:: trl_final

Macros
------

Defined in ``trl_macros.svh``, which ``trl_pkg.sv`` includes.

.. autosvsummary::
   :kinds: macro
   :files: trl_macros.svh
   :exclude: TRL_MACROS_SVH
