trlog
=====

trlog is a binary trace format for simulation: signal value changes and
transaction streams in one indexed, block-compressed file. This
documentation covers the Python reader/writer and query API, the ``trlog``
command-line tool, the C library and VPI dumper, the SystemVerilog
recording package, and the file format itself.

.. toctree::
   :maxdepth: 2
   :caption: Python

   guide/quickstart
   guide/hierarchies
   guide/py-value-changes
   guide/transactions
   guide/py-metadata
   guide/py-derived
   guide/py-extensions

.. toctree::
   :maxdepth: 2
   :caption: Querying

   guide/cli
   guide/query

.. toctree::
   :maxdepth: 2
   :caption: Simulator Integration

   guide/systemverilog
   guide/vpi
   guide/c-api

.. toctree::
   :maxdepth: 2
   :caption: Format & Codecs

   guide/format
   codec-registry
   guide/writing-a-codec

.. toctree::
   :maxdepth: 1
   :caption: API Reference

   api/writer
   api/reader
   api/python-types
   api/python-derived
   api/python-ext
   api/codec
   api/query
   api/c
   api/sv
   api/sv_dpi

Indices and tables
==================

* :ref:`genindex`
* :ref:`modindex`
* :ref:`search`
