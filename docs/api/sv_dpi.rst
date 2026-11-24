SystemVerilog DPI imports
=========================

The raw DPI-C imports that the :doc:`sv` classes are built on. They are
declared in ``trl_sv_dpi.svh``, inside ``trl_pkg``, and implemented in
``c/src/trl_sv.c``. Handles are ``chandle``; an ``int`` status of ``0`` means
success.

Use the classes instead where possible: they add the ``null`` checks that
make disabled recording free, and keep handles of different kinds apart.

.. autosvsummary::
   :kinds: function
   :files: trl_sv_dpi.svh
   :undoc-members:
