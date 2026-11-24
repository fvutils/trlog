C API
=====

The public C API of ``libtrl``, grouped by header. The headers are in
``c/include/trl/``; include them as ``#include "trl/trl.h"`` and so on, and
link ``-ltrl``. See :doc:`../guide/c-api` for building, linking and worked
examples.

Unless stated otherwise, a function that returns :c:type:`trl_status_t`
returns :c:enumerator:`TRL_OK` on success, a function that returns a handle
returns ``NULL`` on failure, and a function that returns a ``uint32_t`` id
returns 0 on failure. String ids are 1-based; 0 means "no string".

.. contents::
   :local:
   :depth: 1


trl/trl.h: core writer and reader
---------------------------------

The block-level writer and reader. The caller manages string, type,
variable and stream ids and block boundaries.

Types and constants
~~~~~~~~~~~~~~~~~~~

.. c:type:: trl_writer_t

   Opaque writer handle, from :c:func:`trl_writer_open`.

.. c:type:: trl_reader_t

   Opaque reader handle, from :c:func:`trl_reader_open`.

.. c:enum:: trl_status_t

   Result codes.

   .. c:enumerator:: TRL_OK

      Success (0).

   .. c:enumerator:: TRL_ERR_IO

      A file could not be opened, read or written.

   .. c:enumerator:: TRL_ERR_MAGIC

      The file does not start with the trlog magic bytes.

   .. c:enumerator:: TRL_ERR_VERSION

      The file's major format version is not supported.

   .. c:enumerator:: TRL_ERR_COMPRESS

      Compression or decompression failed.

   .. c:enumerator:: TRL_ERR_CORRUPT

      The file content is malformed, or a call is out of sequence (for
      example :c:func:`trl_hier_end_scope` with no open scope).

   .. c:enumerator:: TRL_ERR_OOM

      Out of memory.

   .. c:enumerator:: TRL_ERR_INVALID

      Invalid argument, for example a record for another stream in a
      single-stream block.

.. c:enum:: trl_compress_t

   Block compression.

   .. c:enumerator:: TRL_COMPRESS_NONE
   .. c:enumerator:: TRL_COMPRESS_ZLIB

.. c:enum:: trl_signal_enc_t

   Signal value encodings, for :c:func:`trl_add_signal_type`.

   .. c:enumerator:: TRL_SE_2STATE

      0/1 values, of any width.

   .. c:enumerator:: TRL_SE_4STATE

      0/1/X/Z values.

   .. c:enumerator:: TRL_SE_9STATE

      IEEE 1164 values.

   .. c:enumerator:: TRL_SE_REAL

      Double-precision real.

   .. c:enumerator:: TRL_SE_STRING

      String.

.. c:enum:: trl_field_type_t

   Transaction field types. These are wire values and match the Python
   ``trlog._types.FieldType`` enum.

   .. c:enumerator:: TRL_FT_U8
   .. c:enumerator:: TRL_FT_U16
   .. c:enumerator:: TRL_FT_U32
   .. c:enumerator:: TRL_FT_U64
   .. c:enumerator:: TRL_FT_I8
   .. c:enumerator:: TRL_FT_I16
   .. c:enumerator:: TRL_FT_I32
   .. c:enumerator:: TRL_FT_I64
   .. c:enumerator:: TRL_FT_F32
   .. c:enumerator:: TRL_FT_F64
   .. c:enumerator:: TRL_FT_BOOL
   .. c:enumerator:: TRL_FT_STRING
   .. c:enumerator:: TRL_FT_BITVEC

      Fixed-width bit vector.

   .. c:enumerator:: TRL_FT_BYTES

      Byte string.

   .. c:enumerator:: TRL_FT_TIME

      Unsigned time value.

   .. c:enumerator:: TRL_FT_ENUM

      Enumerated value, stored as an integer.

   .. c:enumerator:: TRL_FT_LOGICVEC

      4-state vector (value bits and X/Z bits).

.. c:enum:: trl_txn_record_tag_t

   Tags of the records inside a transaction block.

   .. c:enumerator:: TRL_TR_FULL

      A complete transaction.

   .. c:enumerator:: TRL_TR_BEGIN

      Start of an open transaction.

   .. c:enumerator:: TRL_TR_ATTR

      One field value of an open transaction.

   .. c:enumerator:: TRL_TR_END

      End of an open transaction.

   .. c:enumerator:: TRL_TR_LINK

      A link between two transactions.

   .. c:enumerator:: TRL_TR_META

      A key/value string pair attached to a transaction.

.. c:struct:: trl_field_def_t

   One field of a transaction schema, for :c:func:`trl_add_txn_schema`.

   .. c:member:: uint32_t name_str_id

      Field name, as a string id.

   .. c:member:: trl_field_type_t field_type

.. c:struct:: trl_attr_t

   One attribute (field) value of a transaction.

   When passed to :c:func:`trl_txn_full`, every pointer is read during the
   call and the value is copied, so the memory need not outlive the call.

   .. c:member:: uint32_t field_idx

      Index of the field in the transaction's schema.

   .. c:member:: trl_field_type_t field_type

   .. c:member:: union @value value

      A union; the member used depends on ``field_type``:

      ===========================  ==============================================
      ``value.u64``                ``U8``..``U64``, ``BOOL``, ``TIME``, ``ENUM``
      ``value.i64``                ``I8``..``I64``
      ``value.f64``                ``F32``, ``F64``
      ``value.str``                ``STRING``: NUL-terminated UTF-8; ``NULL`` is
                                   the empty string
      ``value.vec.data``,          ``BYTES``: ``data[len]``.
      ``.len``, ``.width``         ``BITVEC``: ``width`` bits in
                                   ``ceil(width/8)`` bytes, big-endian.
                                   ``LOGICVEC``: ``width`` bits; ``data`` holds
                                   the value bytes then the X/Z bytes,
                                   ``ceil(width/8)`` each, big-endian.
      ===========================  ==============================================

.. c:macro:: TRL_STREAM_MIXED

   ``0xFFFFFFFF``. The stream id of a transaction block that may hold any
   stream's records.

.. c:macro:: TRL_WATTR_SRC_FILE
             TRL_WATTR_SRC_LINE

   Attribute keys (``"trlog.src.file"``, ``"trlog.src.line"``) for the
   source location of a hierarchy node.

.. c:macro:: TRL_WATTR_DRIVER_FILE
             TRL_WATTR_DRIVER_LINE

   Attribute keys (``"trlog.driver.file"``, ``"trlog.driver.line"``) for the
   driver location of a signal.

.. c:macro:: TRL_WATTR_ORIGIN_FILE
             TRL_WATTR_ORIGIN_LINE
             TRL_WATTR_ORIGIN_COMPONENT

   Metadata keys (``"trlog.origin.file"``, ``"trlog.origin.line"``,
   ``"trlog.origin.component"``) for where a transaction came from, for
   example the sequence that created it. Line numbers are decimal strings.

Integer codes without C names
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

Several ``uint8_t`` arguments take values that have names only in the
Python package (``trlog._types``):

=====================  ======================================================
Argument               Values
=====================  ======================================================
hierarchy ``kind``     0 ``HK_DESIGN``, 1 ``HK_BEHAVIORAL``, 2 ``HK_SW``,
                       0xFF ``HK_CUSTOM``
``scope_type``         0 module, 1 task, 2 function, 3 block, 4 fork,
                       5 generate, 6 struct, 7 union, 8 interface,
                       9 package, 10 program, 11 VHDL architecture,
                       12 VHDL process, 0xFF anonymous
variable ``dir``       0 none, 1 in, 2 out, 3 inout, 4 buffer
signal ``radix``       0 hex, 1 dec, 2 oct, 3 bin, 4 unsigned dec, 5 signed,
                       6 real, 7 string, 8 time
link ``ltype``         0 cause/effect, 1 parent/child, 2 related,
                       0xFF custom
=====================  ======================================================

Writer
~~~~~~

.. c:function:: trl_writer_t *trl_writer_open(const char *path, int timescale_exp, trl_compress_t compress)

   Create the file at ``path`` and write its header. ``timescale_exp`` is
   the power of ten of one time unit (-9 for 1 ns). Returns ``NULL`` if the
   file cannot be created.

.. c:function:: trl_status_t trl_writer_set_timezero(trl_writer_t *w, int64_t timezero)

   Set the header's time offset (default 0). The header is rewritten in
   place, so this can be called at any time before :c:func:`trl_writer_close`.

.. c:function:: trl_status_t trl_writer_close(trl_writer_t *w)

   End any open hierarchy, value-change or transaction block, finalize and
   close the codecs, write the string table, type registry and index, close
   the file and free the writer. Returns the first error. ``NULL`` is
   accepted.

.. c:function:: uint32_t trl_intern(trl_writer_t *w, const char *s)

   Return the id of string ``s``, adding it to the string table if needed.
   Returns 0 for ``NULL``.

.. c:function:: uint32_t trl_add_signal_type(trl_writer_t *w, uint8_t encoding, uint32_t bit_width, uint8_t radix)

   Register a signal type and return its id. ``encoding`` is a
   :c:enum:`trl_signal_enc_t` value. Each call adds a new type, even if an
   identical one exists.

.. c:function:: uint32_t trl_add_txn_schema(trl_writer_t *w, uint32_t name_id, uint32_t field_count, const trl_field_def_t *fields)

   Register a transaction schema named by string id ``name_id`` and return
   its id. ``fields`` is copied.

Hierarchy
~~~~~~~~~

.. c:function:: trl_status_t trl_begin_hierarchy(trl_writer_t *w, uint32_t hier_id, uint8_t kind, uint32_t name_id)

   Start a hierarchy block. ``kind`` is a hierarchy kind code (see the
   table above).

.. c:function:: trl_status_t trl_hier_begin_scope(trl_writer_t *w, uint8_t scope_type, uint32_t name_id, uint32_t component_id, uint32_t src_file_str_id, uint32_t src_line)

   Open a scope inside the current scope. ``src_file_str_id`` and
   ``src_line`` give its source location, or 0.

.. c:function:: trl_status_t trl_hier_end_scope(trl_writer_t *w)

   Close the current scope.

.. c:function:: uint32_t trl_hier_add_var(trl_writer_t *w, uint32_t name_id, uint32_t sig_type_id, uint8_t dir, uint32_t src_file_str_id, uint32_t src_line)

   Declare a signal in the current scope and return its variable id, which
   the ``trl_vc_change_*`` calls take. ``sig_type_id`` is from
   :c:func:`trl_add_signal_type`.

.. c:function:: uint32_t trl_hier_add_stream(trl_writer_t *w, uint32_t stream_type_id, uint32_t name_id)

   Declare a transaction stream in the current hierarchy and return its
   stream instance id.

.. c:function:: trl_status_t trl_hier_add_attr(trl_writer_t *w, uint32_t key_str_id, uint32_t value_str_id)

   Attach a key/value string attribute to the most recently declared
   scope, var or stream of the open hierarchy. Returns ``TRL_ERR_INVALID``
   if none has been declared yet.

.. c:function:: trl_status_t trl_flush_hierarchy(trl_writer_t *w)

   Encode and write the current hierarchy block.

Value changes
~~~~~~~~~~~~~

.. c:function:: trl_status_t trl_vc_begin(trl_writer_t *w, uint64_t start_time)

   Start a value-change block.

.. c:function:: trl_status_t trl_vc_change_u64(trl_writer_t *w, uint32_t var_id, uint64_t time, uint64_t value)

   Record a value of up to 64 bits.

.. c:function:: trl_status_t trl_vc_change_bytes(trl_writer_t *w, uint32_t var_id, uint64_t time, const uint8_t *value, uint32_t len)

   Record a value given as bytes, big-endian, for signals of any width. If
   ``len`` is more than the value needs, the last bytes are kept. For
   4-state and 9-state signals, ``len`` selects the plain binary form
   (``ceil(width/8)`` bytes) or, when it is longer, the packed form with
   X/Z; see :doc:`../guide/format`. Where the packed form is no longer than
   the binary form (4-state signals of 2 to 4 bits), add a leading zero
   byte.

.. c:function:: trl_status_t trl_vc_change_str(trl_writer_t *w, uint32_t var_id, uint64_t time, const char *value)

   Record a string value.

.. c:function:: trl_status_t trl_vc_change_real(trl_writer_t *w, uint32_t var_id, uint64_t time, double value)

   Record a real value.

.. c:function:: trl_status_t trl_vc_flush(trl_writer_t *w)

   Encode the current value-change block with the writer's value-change
   codec (see :c:func:`trl_writer_set_vc_codec`), index it and write it.

Transactions
~~~~~~~~~~~~

.. c:function:: trl_status_t trl_txn_begin_block(trl_writer_t *w, uint64_t start_time)

   Start a transaction block that may hold records of any stream. Same as
   :c:func:`trl_txn_begin_block_stream` with :c:macro:`TRL_STREAM_MIXED`.

.. c:function:: trl_status_t trl_txn_begin_block_stream(trl_writer_t *w, uint64_t start_time, uint32_t stream_inst_id)

   Start a transaction block that holds records of one stream only. The
   stream id goes into the per-stream index, so readers can skip the block
   when they do not want that stream. :c:func:`trl_txn_full` with another
   stream id returns :c:enumerator:`TRL_ERR_INVALID`.

.. c:function:: trl_status_t trl_txn_full(trl_writer_t *w, uint32_t stream_inst_id, uint32_t txn_type_id, uint64_t txn_id, uint64_t start, uint64_t end, uint64_t parent, uint32_t attr_count, const trl_attr_t *attrs)

   Add a complete transaction to the open block. ``txn_type_id`` is a
   schema id from :c:func:`trl_add_txn_schema`; ``parent`` is a transaction
   id or 0. ``attrs`` is copied.

.. c:function:: trl_status_t trl_txn_meta(trl_writer_t *w, uint64_t txn_id, uint32_t key_str_id, uint32_t value_str_id)

   Attach a key/value string pair to transaction ``txn_id``.

.. c:function:: trl_status_t trl_txn_end_block(trl_writer_t *w)

   Encode, index and write the open transaction block.

Reader
~~~~~~

.. c:function:: trl_reader_t *trl_reader_open(const char *path)

   Open a file and read its header, string table, type registry,
   hierarchy and index. Returns ``NULL`` if the file cannot be opened or
   is not a valid trace.

.. c:function:: trl_status_t trl_reader_close(trl_reader_t *r)

   Close the file and free the reader. ``NULL`` is accepted.

.. c:type:: void (*trl_vc_cb)(uint32_t var_id, uint64_t time, const uint8_t *val, uint32_t len, void *user)

   Value-change callback. ``val`` and ``len`` are the stored value bytes:
   a 2-state value is ``ceil(width/8)`` bytes, big-endian, at most 8; a
   real is the 8 bytes of a ``double``; a string is its bytes without a
   terminator. 4-state and 9-state values are in the forms described in
   :doc:`../guide/format`.

.. c:function:: trl_status_t trl_iter_vc(trl_reader_t *r, trl_vc_cb cb, void *user)

   Call ``cb`` for every value change in the file, block by block in file
   order. Within a block the changes are grouped by variable, not sorted by
   time.

.. c:type:: void (*trl_txn_cb_t)(uint32_t txn_type, uint32_t stream_inst, uint64_t txn_id, uint64_t start, uint64_t end, void *user)

   Transaction callback, used by a transaction codec's ``decode_block``
   (:c:struct:`trl_txn_codec`). ``trl.h`` has no transaction iterator; read
   transactions with ``trl_query.h``.


trl/trl_rec.h: recording session
--------------------------------

A session layer over the writer. It manages named types and streams,
string interning, stream declarations, transaction ids and per-stream
block buffering. Functions that fail set a message for
:c:func:`trl_last_error`.

Handles
~~~~~~~

.. c:type:: trl_db_t

   A recording session: one output file.

.. c:type:: trl_rtype_t

   A transaction type being recorded.

.. c:type:: trl_renum_t

   An enum label table.

.. c:type:: trl_stream_t

   A transaction stream.

.. c:type:: trl_txn_t

   An open transaction, from :c:func:`trl_rec_begin`. It is released by
   :c:func:`trl_rec_end` and must not be used after that.

All handles belong to their db and are freed by :c:func:`trl_db_close`.

.. c:struct:: trl_db_cfg_t

   Options for :c:func:`trl_db_open`. Zero-initialize it and set the
   fields you need.

   .. c:member:: const char *path

      Output file. Required.

   .. c:member:: int timescale_exp

      Power of ten of one time unit, for example -9 for 1 ns.

   .. c:member:: trl_compress_t compress

   .. c:member:: uint32_t block_bytes

      Size at which a stream's buffered records are written as a block.
      0 selects the default, 64 KiB.

   .. c:member:: const char *stream_include

      Comma-separated glob list matched against stream paths. ``NULL``
      includes every stream.

   .. c:member:: const char *stream_exclude

      Comma-separated glob list of streams to disable. ``NULL`` excludes
      none.

   .. c:member:: uint32_t flags

      Reserved; set to 0.

   .. c:member:: int64_t timezero

      Time offset written to the file header. 0 (the default) writes none.

Session
~~~~~~~

.. c:function:: trl_db_t *trl_db_open(const char *name, const trl_db_cfg_t *cfg)

   Create ``cfg->path`` and register the db under ``name`` in a
   process-wide table. Returns ``NULL`` if ``name`` or ``cfg->path`` is
   missing, a db with that name is already open, or the file cannot be
   created.

.. c:function:: trl_db_t *trl_db_find(const char *name)

   Return the open db named ``name``, or ``NULL``.

.. c:function:: trl_status_t trl_db_close(trl_db_t *db)

   Write every buffered block and the stream declarations, close the
   writer, remove the db from the table and free it with all its handles.
   ``NULL`` is accepted.

.. c:function:: trl_writer_t *trl_db_writer(trl_db_t *db)

   The underlying :c:type:`trl_writer_t`.

.. c:function:: uint32_t trl_db_intern(trl_db_t *db, const char *s)

   Intern a string in the db's file and return its id.

.. c:function:: const char *trl_last_error(void)

   The message of the last failure in a ``trl_rec.h`` function, or an empty
   string. It is process-wide and not cleared on success.

Types
~~~~~

.. c:function:: trl_rtype_t *trl_type_new(trl_db_t *db, const char *name, trl_stream_t *scope)

   Create an empty type. With a ``scope`` stream the type is named
   ``<stream path>::<name>``.

.. c:function:: trl_rtype_t *trl_type_find(trl_db_t *db, const char *name, trl_stream_t *scope)

   Find a type by the same name and scope given to :c:func:`trl_type_new`.

.. c:function:: int32_t trl_type_add_field(trl_rtype_t *t, const char *name, trl_field_type_t ft, uint32_t width, trl_renum_t *renum)

   Add a field and return its index, or -1 if the type is sealed.
   ``width`` is the bit width for vector fields (0 otherwise); ``renum`` is
   an optional enum label table.

.. c:function:: int32_t trl_type_add_struct(trl_rtype_t *t, const char *name, trl_rtype_t *sub)

   Add a copy of every field of the sealed type ``sub``, named
   ``name.<field>``, and return the index of the first one. Returns -1 if
   ``sub`` is not sealed or ``t`` is.

.. c:function:: int32_t trl_type_add_array(trl_rtype_t *t, const char *name, trl_field_type_t ft, uint32_t width, uint32_t n)

   Add ``n`` fields named ``name[0]`` .. ``name[n-1]`` and return the index
   of the first one, or -1 if the type is sealed.

.. c:function:: int32_t trl_type_field_get_or_add(trl_rtype_t *t, const char *name, trl_field_type_t ft, uint32_t width)

   For ad-hoc types. Return the index of field ``name``, adding it if it is
   new. A new field registers a new schema version at once. Returns -1 if
   the name exists with a different type or width. Calling it marks the
   type as ad-hoc.

.. c:function:: trl_status_t trl_type_seal(trl_rtype_t *t)

   Register the type's schema in the file. A sealed type accepts no more
   fields and can be recorded.

.. c:function:: const char *trl_type_name(const trl_rtype_t *t)

   The qualified type name.

.. c:function:: uint32_t trl_type_num_fields(const trl_rtype_t *t)

.. c:function:: int32_t trl_type_field_index(const trl_rtype_t *t, const char *name)

   Index of the field named ``name`` (a flattened name such as
   ``hdr.len`` or ``tag[1]``), or -1.

.. c:function:: const char *trl_type_field_name(const trl_rtype_t *t, uint32_t f)

   Name of field ``f``, or ``NULL`` if out of range.

.. c:function:: trl_field_type_t trl_type_field_type(const trl_rtype_t *t, uint32_t f)

   Type of field ``f``; :c:enumerator:`TRL_FT_U8` if out of range.

.. c:function:: uint32_t trl_type_field_width(const trl_rtype_t *t, uint32_t f)

   Width given for field ``f``; 0 if out of range.

.. c:function:: trl_renum_t *trl_enum_new(trl_db_t *db, const char *name)

   Create an enum label table. Tables are kept in memory only; ``ENUM``
   values are written as plain integers.

.. c:function:: trl_status_t trl_enum_add(trl_renum_t *e, const char *label, int64_t value)

   Add a label/value pair.

Streams
~~~~~~~

.. c:function:: trl_stream_t *trl_stream_new(trl_db_t *db, trl_stream_t *parent, const char *name, const char *kind, trl_rtype_t *dflt, int *enabled)

   Create a stream. Its path is ``name``, or ``<parent path>.<name>``. The
   stream is declared in the file before its first record is written.
   ``kind`` is stored in memory only. ``dflt`` is the default type, or
   ``NULL``. If ``enabled`` is not ``NULL`` it receives 1 when the stream is
   enabled by the db's include/exclude lists and its parent is enabled,
   else 0. Records on a disabled stream are dropped: :c:func:`trl_rec_full`
   returns 0 and :c:func:`trl_rec_begin` returns a handle with id 0 whose
   calls do nothing.

.. c:function:: trl_stream_t *trl_stream_find(trl_db_t *db, const char *path)

   Find a stream by its dotted path.

.. c:function:: trl_rtype_t *trl_stream_adhoc_type(trl_stream_t *s)

   The stream's default type. If it has none, create an empty ad-hoc type
   named ``<path>#adhoc`` and make it the default. Add fields with
   :c:func:`trl_type_field_get_or_add`.

.. c:function:: uint32_t trl_stream_inst_id(const trl_stream_t *s)

   The stream instance id written to the file.

Recording
~~~~~~~~~

.. c:function:: uint64_t trl_rec_full(trl_stream_t *s, trl_rtype_t *t, uint64_t start, uint64_t end, uint64_t parent, const uint64_t *vals, uint32_t nvals)

   Record a complete transaction and return its id, or 0 if ``t`` is not
   sealed. ``vals[i]`` is the raw 64-bit value of field ``i``: the integer
   for integer, ``BOOL``, ``TIME`` and ``ENUM`` fields; the bits of a
   ``double`` for ``F64``; the bits of a ``float`` (low 32 bits) for
   ``F32``; a string id for ``STRING``; the value in the low bits for
   ``BITVEC`` and ``LOGICVEC`` fields up to 64 bits wide. Wider vectors and
   ``BYTES`` fields are left out, and :c:func:`trl_last_error` names the
   field. Returns 0 on a disabled stream. Only the first ``min(nvals, fields)``
   values are used. ``parent`` is a transaction id or 0.

.. c:function:: trl_txn_t *trl_rec_begin(trl_stream_t *s, trl_rtype_t *t, uint64_t start, uint64_t parent)

   Open a transaction and return its handle. ``t`` may be ``NULL``, in
   which case no field values can be set.

.. c:function:: uint64_t trl_rec_id(const trl_txn_t *txn)

   The transaction's id.

.. c:function:: void trl_rec_set_u64(trl_txn_t *txn, uint32_t f, uint64_t v)
                void trl_rec_set_i64(trl_txn_t *txn, uint32_t f, int64_t v)
                void trl_rec_set_f64(trl_txn_t *txn, uint32_t f, double v)

   Record the value of field ``f``. The value is interpreted by the field's
   declared type.

.. c:function:: void trl_rec_set_str(trl_txn_t *txn, uint32_t f, const char *s)

   Record a string value. ``s`` is copied.

.. c:function:: void trl_rec_set_str_id(trl_txn_t *txn, uint32_t f, uint32_t sid)

   Record a string value given as a string id.

.. c:function:: void trl_rec_set_bits(trl_txn_t *txn, uint32_t f, uint32_t width, const uint8_t *be_bytes)

   Record a ``BITVEC`` value of ``width`` bits, ``ceil(width/8)`` bytes,
   big-endian.

.. c:function:: void trl_rec_set_logic(trl_txn_t *txn, uint32_t f, uint32_t width, const uint8_t *aval_be, const uint8_t *bval_be)

   Record a ``LOGICVEC`` value: value bits ``aval_be`` and X/Z bits
   ``bval_be``, ``ceil(width/8)`` bytes each, big-endian.

.. c:function:: void trl_rec_set_bytes(trl_txn_t *txn, uint32_t f, const uint8_t *p, uint32_t n)

   Record a ``BYTES`` value.

.. c:function:: void trl_rec_link(trl_txn_t *txn, uint64_t tgt, uint8_t ltype, uint32_t label_sid)

   Record a link from this transaction to transaction ``tgt``. ``ltype`` is
   a link type code; ``label_sid`` is a string id or 0.

.. c:function:: void trl_rec_link_ids(trl_db_t *db, uint64_t src, uint64_t tgt, uint8_t ltype, uint32_t label_sid)

   Record a link between two transactions by id, for example after both
   have ended. Such links go into a shared block that is not tied to a
   stream.

.. c:function:: void trl_rec_meta(trl_txn_t *txn, uint32_t key_sid, uint32_t val_sid)

   Attach a key/value string pair, both given as string ids.

.. c:function:: void trl_rec_end(trl_txn_t *txn, uint64_t end)

   Close the transaction and release ``txn``.

The ``trl_rec_set_*`` functions set the :c:func:`trl_last_error` message
and do nothing if ``f`` is out of range.


trl/trl_query.h: reading session
--------------------------------

Name lookup for streams and schemas, and a cursor that joins each
transaction's records into one :c:struct:`trl_txn_hdr_t`.

Handles
~~~~~~~

.. c:type:: trl_rdb_t

   A reading session: one open file.

.. c:type:: trl_rstream_t

   A stream declared in the file.

.. c:type:: trl_rschema_t

   A transaction schema from the file's type registry. It is a different
   type from the write-side :c:type:`trl_rtype_t`.

.. c:type:: trl_cursor_t

   A cursor over transactions.

Session
~~~~~~~

.. c:function:: trl_rdb_t *trl_rdb_open(const char *name, const char *path)

   Open ``path``, load its streams and schemas, and register the session
   under ``name`` in a process-wide table. Returns ``NULL`` if the name is
   already open or the file cannot be read.

.. c:function:: trl_rdb_t *trl_rdb_find(const char *name)

   Return the open session named ``name``, or ``NULL``.

.. c:function:: trl_status_t trl_rdb_close(trl_rdb_t *db)

   Close the file and free the session, its streams and schemas. Close the
   session's cursors first. ``NULL`` is accepted.

.. c:function:: const char *trl_rdb_string_lookup(trl_rdb_t *db, uint32_t sid)

   Resolve a string id from a cursor's metas or links (``key_sid``,
   ``val_sid``, ``label_sid``). ``STRING`` attribute values are not ids:
   the cursor returns them decoded in ``value.str``. Returns ``NULL`` if the
   id is out of range.

Streams
~~~~~~~

.. c:function:: uint32_t trl_rdb_num_streams(trl_rdb_t *db)

.. c:function:: trl_rstream_t *trl_rdb_stream_at(trl_rdb_t *db, uint32_t i)

   Stream ``i``, or ``NULL`` if out of range.

.. c:function:: trl_rstream_t *trl_rdb_stream_find(trl_rdb_t *db, const char *path)

   Find a stream by its full dotted path.

.. c:function:: const char *trl_rstream_path(const trl_rstream_t *s)

.. c:function:: uint32_t trl_rstream_inst_id(const trl_rstream_t *s)

   The stream instance id, as in :c:member:`trl_txn_hdr_t.stream_inst_id`.

Schemas
~~~~~~~

.. c:function:: const trl_rschema_t *trl_rdb_schema_find(trl_rdb_t *db, const char *qualified_name)

   Find a schema by name. If the name was registered more than once (an
   ad-hoc type that grew), return the newest version.

.. c:function:: const trl_rschema_t *trl_rdb_schema_by_id(trl_rdb_t *db, uint32_t schema_id)

   The schema with id ``schema_id``, as in
   :c:member:`trl_txn_hdr_t.txn_type_id`, or ``NULL``.

.. c:function:: const char *trl_rschema_name(const trl_rschema_t *s)

.. c:function:: uint32_t trl_rschema_num_fields(const trl_rschema_t *s)

.. c:function:: int32_t trl_rschema_field_index(const trl_rschema_t *s, const char *name)

   Index of field ``name``, or -1.

.. c:function:: const char *trl_rschema_field_name(const trl_rschema_t *s, uint32_t f)

   Name of field ``f``, or ``NULL`` if out of range.

.. c:function:: trl_field_type_t trl_rschema_field_type(const trl_rschema_t *s, uint32_t f)

   Type of field ``f``; :c:enumerator:`TRL_FT_U8` if out of range.

Cursor
~~~~~~

.. c:struct:: trl_txn_link_t

   A link from a transaction.

   .. c:member:: uint8_t link_type

      Link type code.

   .. c:member:: uint64_t target

      The transaction at the other end.

   .. c:member:: uint32_t label_sid

      Label string id, or 0.

.. c:struct:: trl_txn_meta_t

   A key/value pair attached to a transaction.

   .. c:member:: uint32_t key_sid
   .. c:member:: uint32_t val_sid

.. c:struct:: trl_txn_hdr_t

   One joined transaction. The pointers stay valid until the cursor is
   freed.

   .. c:member:: uint64_t txn_id
   .. c:member:: uint32_t stream_inst_id
   .. c:member:: uint32_t txn_type_id

      Schema id; resolve it with :c:func:`trl_rdb_schema_by_id`.

   .. c:member:: uint64_t start
   .. c:member:: uint64_t end

      ``UINT64_MAX`` if the file has no end record for the transaction.

   .. c:member:: uint64_t parent

      Parent transaction id, or 0.

   .. c:member:: uint32_t attr_count
   .. c:member:: const trl_attr_t *attrs

      Field values, in the order they were recorded.

   .. c:member:: uint32_t link_count
   .. c:member:: const trl_txn_link_t *links

      Links whose source is this transaction.

   .. c:member:: uint32_t meta_count
   .. c:member:: const trl_txn_meta_t *metas

.. c:function:: trl_cursor_t *trl_cursor_new(trl_rdb_t *db, trl_rstream_t *stream, uint64_t t_from, uint64_t t_to)

   Create a cursor over the transactions of ``stream``, or of every stream
   if ``stream`` is ``NULL``. A transaction is selected when
   ``start < t_to`` and it is open or ``end >= t_from``.

.. c:function:: trl_status_t trl_cursor_add_stream(trl_cursor_t *c, trl_rstream_t *stream)

   Add a stream to the selection. On a cursor created with ``stream ==
   NULL`` the first call replaces "every stream" with this one. Returns
   :c:enumerator:`TRL_ERR_INVALID` after the first
   :c:func:`trl_cursor_next`.

.. c:function:: int trl_cursor_next(trl_cursor_t *c, trl_txn_hdr_t *out)

   Fill ``out`` with the next transaction and return 1, or return 0 at the
   end. Transactions come in order of start time, then id. The first call
   reads and joins every selected block.

.. c:function:: void trl_cursor_free(trl_cursor_t *c)

   Free the cursor and every transaction it returned. ``NULL`` is accepted.


trl/trl_codec.h: codecs
-----------------------

The codec registry, codec vtables and the storage SPI a codec uses. These
declarations have ``extern "C"`` linkage. See
:doc:`../guide/writing-a-codec` for how codecs work.

Constants
~~~~~~~~~

.. c:macro:: TRL_CODEC_ABI_VERSION

   Version of the codec SPI and vtable ABI (1).

.. c:macro:: TRL_FLAG_STRUCT_CODEC

   Block flag (0x80): the payload was produced by the stream type's
   structural codec rather than the built-in path.

.. c:macro:: TRL_TYPE_TAG_STREAM_V2

   Type-registry entry tag (0x05) that carries a stream type's codec id and
   parameters.

.. c:macro:: TRL_CODEC_CORE_VALUECHANGE

   ``"org.fvutils.trlog.core.valuechange"``, the built-in value-change
   codec.

.. c:macro:: TRL_CODEC_CORE_RECORD

   ``"org.fvutils.trlog.core.record"``, the built-in transaction record
   codec. The ``org.fvutils.trlog.`` prefix is reserved for built-ins.

.. c:enum:: trl_codec_caps_t

   Codec capability bits.

   .. c:enumerator:: TRL_CAP_NONE
   .. c:enumerator:: TRL_CAP_RANDOM_ACCESS
   .. c:enumerator:: TRL_CAP_NEEDS_INPUT_STREAMS
   .. c:enumerator:: TRL_CAP_HAS_RAW_FALLBACK
   .. c:enumerator:: TRL_CAP_LOSSLESS
   .. c:enumerator:: TRL_CAP_MATERIALIZED

.. c:type:: trl_store

   Opaque handle the core passes to a codec: the storage services and the
   trace they are bound to.

.. c:type:: trl_blkout

   Opaque append buffer for one output block.

Vtables
~~~~~~~

.. c:struct:: trl_vc_codec

   A value-change codec; ``typedef``'d as ``trl_vc_codec_t``. ``self`` is
   the per-stream state returned by ``open_writer`` or ``open_reader``
   (``NULL`` for the stateless built-ins). Functions returning ``int``
   return 0 on success.

   .. c:member:: const char *codec_id

      Reverse-DNS codec id.

   .. c:member:: uint16_t version

      Codec-internal format version.

   .. c:member:: uint32_t caps

      :c:enum:`trl_codec_caps_t` bits.

   .. c:member:: void *(*open_writer)(trl_store *st, uint32_t stream_id, const void *params, size_t param_len)
   .. c:member:: void *(*open_reader)(trl_store *st, uint32_t stream_id, const void *params, size_t param_len)
   .. c:member:: void (*close)(void *self)
   .. c:member:: int (*finalize)(void *self, trl_store *st)

      Called once per writer at trace close, after the last
      ``encode_block`` and before the index.

   .. c:member:: int (*encode_block)(void *self, trl_store *st, trl_blkout *out)

      Write the current block's payload into ``out``. The core compresses
      and indexes it.

   .. c:member:: int (*decode_block)(void *self, trl_store *st, const uint8_t *payload, size_t len, uint8_t flags, trl_vc_cb emit, void *user)

      Decode a decompressed payload and call ``emit`` for each value change.

   .. c:member:: int (*input_streams)(void *self, trl_store *st, uint32_t **out_ids, size_t *n)

      Stream ids that must be materialized first, for derived codecs.

.. c:struct:: trl_txn_codec

   A transaction codec; ``typedef``'d as ``trl_txn_codec_t``. Same slots as
   :c:struct:`trl_vc_codec`, except that ``decode_block`` takes a
   :c:type:`trl_txn_cb_t`.

   .. c:member:: const char *codec_id
   .. c:member:: uint16_t version
   .. c:member:: uint32_t caps
   .. c:member:: void *(*open_writer)(trl_store *st, uint32_t stream_id, const void *params, size_t param_len)
   .. c:member:: void *(*open_reader)(trl_store *st, uint32_t stream_id, const void *params, size_t param_len)
   .. c:member:: void (*close)(void *self)
   .. c:member:: int (*finalize)(void *self, trl_store *st)
   .. c:member:: int (*encode_block)(void *self, trl_store *st, trl_blkout *out)
   .. c:member:: int (*decode_block)(void *self, trl_store *st, const uint8_t *payload, size_t len, uint8_t flags, trl_txn_cb_t emit, void *user)
   .. c:member:: int (*input_streams)(void *self, trl_store *st, uint32_t **out_ids, size_t *n)

Registry
~~~~~~~~

Codecs are keyed by ``codec_id``. The built-in codecs register when the
library loads. Register other codecs before :c:func:`trl_writer_open` (or
after :c:func:`trl_reader_open`). The registry stores the pointer, so the
vtable must outlive every trace that uses it.

.. c:function:: int trl_register_vc_codec(const trl_vc_codec_t *codec)
                int trl_register_txn_codec(const trl_txn_codec_t *codec)

   Register a codec. Returns 0 on success, non-zero for an invalid id or
   out of memory.

.. c:function:: const trl_vc_codec_t *trl_lookup_vc_codec(const char *codec_id)
                const trl_txn_codec_t *trl_lookup_txn_codec(const char *codec_id)

   Return the registered codec, or ``NULL``.

.. c:function:: int trl_writer_set_vc_codec(trl_writer_t *w, const char *codec_id)

   Use the registered value-change codec ``codec_id`` for the writer's
   following blocks. Its ``open_writer`` is called now and its state is
   passed to ``encode_block``, ``finalize`` and ``close``. ``NULL``
   reverts to the built-in codec. Returns non-zero if the id is unknown.

Storage SPI
~~~~~~~~~~~

.. c:function:: uint32_t trl_store_intern(trl_store *st, const char *s)

   Intern a string in the trace's string table (write side) and return its
   id.

.. c:function:: int trl_blkout_append(trl_blkout *out, const uint8_t *data, size_t len)

   Append bytes to a block-output buffer.

.. c:enum:: trl_vc_change_kind_t

   Value kinds reported by :c:func:`trl_store_vc_change_at`.

   .. c:enumerator:: TRL_VC_KIND_U64
   .. c:enumerator:: TRL_VC_KIND_BYTES
   .. c:enumerator:: TRL_VC_KIND_STRING
   .. c:enumerator:: TRL_VC_KIND_REAL

The following are valid only inside a value-change codec's
``encode_block``. They expose the current block's changes, ordered by
variable.

.. c:function:: size_t trl_store_vc_change_count(trl_store *st)

.. c:function:: uint64_t trl_store_vc_start_time(trl_store *st)

.. c:function:: uint64_t trl_store_vc_end_time(trl_store *st)

.. c:function:: int trl_store_vc_change_at(trl_store *st, size_t i, uint32_t *var_id, uint64_t *time, uint32_t *kind, uint64_t *u64val)

   Fetch change ``i``. Any out-parameter may be ``NULL``. ``u64val`` is set
   only for :c:enumerator:`TRL_VC_KIND_U64`. Returns non-zero if ``i`` is out
   of range.


trl/trl_qscan.h: query scan engine
----------------------------------

The C back end of the ``trlog.query`` Python package
(``python/src/trlog/query/_cengine.py``). It is an accelerator for the
pure-Python engine and must give the same answers, so the Python side
supplies every semantic choice: the block plan, the decoder field types,
the field catalog, and the filter compiled to bytecode. The bytecode is
defined in ``c/src/qscan.c`` and mirrored in ``_cengine.py``. Other
programs should use ``trl_query.h`` instead. These declarations have
``extern "C"`` linkage.

Filter results are three-valued: a row is kept, dropped, or *deferred*,
meaning the engine cannot be sure of Python's answer (strings, floats,
integers beyond int64) and the caller evaluates the filter on it.

Constants
~~~~~~~~~

.. c:macro:: TRL_QSCAN_ABI

   ABI version (2), changed on any change to the bytecode, the output
   records or the calls. Python uses the engine only when
   :c:func:`trl_qscan_abi_version` returns the version it knows.

.. c:macro:: TRL_QS_OK
             TRL_QS_FALLBACK

   :c:func:`trl_qscan_run` results: success (0), or the file holds
   something the engine does not decode exactly as Python does (100); the
   caller reruns the query in Python.

.. c:macro:: TRL_QS_WANT_LINKS
             TRL_QS_WANT_META
             TRL_QS_HAS_T_HI
             TRL_QS_FILTER
             TRL_QS_HAS_LO
             TRL_QS_HAS_HI
             TRL_QS_HAS_MAX
             TRL_QS_PROJECT

   Flags for :c:func:`trl_qscan_set_options`: attach links (both ends);
   attach metadata; ``t_hi`` is set (skip blocks that start after it unless
   they can hold transactions still open at ``t_hi``); apply the window and
   filter; ``lo`` is set; ``hi`` is set; stop after the block in which the
   ``max_txns``-th transaction started; send fully decided rows to the
   projection columns.

.. c:macro:: TRL_QS_NORM_PLAIN
             TRL_QS_NORM_BITVEC
             TRL_QS_NORM_LOGICVEC
             TRL_QS_NORM_ENUM

   Field value normalisation codes for :c:func:`trl_qscan_set_catalog`.

.. c:macro:: TRL_QS_ROW_OPEN
             TRL_QS_ROW_DEFER

   :c:member:`trl_qs_row_t.flags` bits: no end record was seen (``end`` is
   0); the caller must evaluate the filter.

.. c:macro:: TRL_QS_V_MISS
             TRL_QS_V_INT
             TRL_QS_V_ENUM
             TRL_QS_V_BOOL

   Projected value tags: missing, integer, enum (``aux`` is
   ``(type_id << 32) | field_idx``), boolean.

Output records
~~~~~~~~~~~~~~

The records are little-endian; Python reads them with the ``struct``
formats given below.

.. c:type:: trl_qscan_t

   Opaque engine handle.

.. c:struct:: trl_qs_row_t

   One output transaction (72 bytes, ``'<qqqqIIIIIIIIII'``). The ``*_first``
   and ``*_n`` members select a range of the attribute, metadata and link
   arrays.

   .. c:member:: int64_t id
   .. c:member:: int64_t start
   .. c:member:: int64_t end
   .. c:member:: int64_t parent

      0 means none.

   .. c:member:: uint32_t stream
   .. c:member:: uint32_t type
   .. c:member:: uint32_t attr_first
   .. c:member:: uint32_t attr_n
   .. c:member:: uint32_t meta_first
   .. c:member:: uint32_t meta_n
   .. c:member:: uint32_t link_first
   .. c:member:: uint32_t link_n
   .. c:member:: uint32_t flags

      ``TRL_QS_ROW_*`` bits.

   .. c:member:: uint32_t pad

.. c:struct:: trl_qs_attr_t

   One attribute (32 bytes, ``'<IIQIIII'``).

   .. c:member:: uint32_t field_idx
   .. c:member:: uint32_t ft

      The :c:enum:`trl_field_type_t` it was decoded as.

   .. c:member:: uint64_t v

      Integer value (raw bits), or the bits of a float.

   .. c:member:: uint32_t off
   .. c:member:: uint32_t len
   .. c:member:: uint32_t width

      For ``STRING``, ``BYTES``, ``BITVEC`` and ``LOGICVEC``: the slice of
      the heap (:c:func:`trl_qscan_heap`) that holds the value, and its bit
      width.

   .. c:member:: uint32_t pad

.. c:struct:: trl_qs_meta_t

   One metadata pair (8 bytes, ``'<II'``).

   .. c:member:: uint32_t key_sid
   .. c:member:: uint32_t val_sid

.. c:struct:: trl_qs_link_t

   One link (16 bytes, ``'<qIBBH'``).

   .. c:member:: int64_t other

      The transaction at the other end.

   .. c:member:: uint32_t label_sid
   .. c:member:: uint8_t kind

      Link type code.

   .. c:member:: uint8_t dir

      0 if this transaction is the source, 1 if it is the target.

   .. c:member:: uint16_t pad

.. c:struct:: trl_qs_proj_t

   Projection columns (with :c:macro:`TRL_QS_PROJECT`). Row ``i`` is
   ``id[i]``, ``start[i]`` and so on; value ``k`` of row ``i`` is
   ``vtag``/``val``/``aux[i * nvals + k]``. ``end`` is 0 for an open
   transaction.

   .. c:member:: const int64_t *id
   .. c:member:: const int64_t *start
   .. c:member:: const int64_t *end
   .. c:member:: const int64_t *parent
   .. c:member:: const uint32_t *stream
   .. c:member:: const uint32_t *type
   .. c:member:: const uint8_t *open
   .. c:member:: const uint8_t *vtag
   .. c:member:: const int64_t *val
   .. c:member:: const uint64_t *aux

Functions
~~~~~~~~~

.. c:function:: int trl_qscan_abi_version(void)

   Return :c:macro:`TRL_QSCAN_ABI`.

.. c:function:: trl_qscan_t *trl_qscan_new(const char *path)

   Open ``path`` for scanning. Returns ``NULL`` if it cannot be opened.

.. c:function:: void trl_qscan_free(trl_qscan_t *q)

.. c:function:: trl_status_t trl_qscan_set_schema(trl_qscan_t *q, uint32_t type_id, uint32_t n, const uint8_t *field_types)

   Set the decoder field types of transaction type ``type_id`` (how
   ``TR_FULL`` attributes are decoded). Kept across queries.

.. c:function:: void trl_qscan_reset(trl_qscan_t *q)

   Clear the per-query settings and results. Schemas are kept.

.. c:function:: void trl_qscan_trim(trl_qscan_t *q)

   Free the results and scan buffers once the caller has copied what it
   needs. Per-query settings and schemas are kept.

.. c:function:: trl_status_t trl_qscan_set_catalog(trl_qscan_t *q, uint32_t type_id, uint32_t n, const int32_t *slots, const uint8_t *norms)

   Per query, for transaction type ``type_id``: each field's name slot (-1
   if the program does not use it) and ``TRL_QS_NORM_*`` normalisation.
   Attributes of types with no catalog entry, or with ``field_idx >= n``,
   are *unmapped*.

.. c:function:: trl_status_t trl_qscan_set_plan(trl_qscan_t *q, size_t n, const int64_t *block_starts, const uint64_t *offsets, const uint32_t *stream_ids)

   The transaction blocks to read, in visit order: file offsets, with each
   block's declared start time and stream id.

.. c:function:: trl_status_t trl_qscan_set_streams(trl_qscan_t *q, size_t n, const uint32_t *ids)
                trl_status_t trl_qscan_set_types(trl_qscan_t *q, size_t n, const uint32_t *ids)

   Build only transactions of these streams or types. Without a call, all
   are built.

.. c:function:: trl_status_t trl_qscan_set_program(trl_qscan_t *q, size_t ncode, const int64_t *code, size_t nsets, const int64_t *sets, int strict_unmapped)

   Set the bytecode and the label and id sets it refers to. With
   ``strict_unmapped``, an unmapped attribute makes field loads and
   presence tests defer, because the program names a field (such as
   ``f3``) that could match it.

.. c:function:: trl_status_t trl_qscan_set_entries(trl_qscan_t *q, int64_t filter_entry, size_t nvals, const int64_t *val_entries)

   Entry points into the program: the filter (-1 for none), and the value
   expressions projected with :c:macro:`TRL_QS_PROJECT`.

.. c:function:: void trl_qscan_set_options(trl_qscan_t *q, uint32_t flags, int64_t t_hi, int64_t lo, int64_t hi, int64_t max_txns)

   Set the ``TRL_QS_*`` flags and the values they refer to. With
   :c:macro:`TRL_QS_FILTER`, a transaction is dropped if it starts after
   ``hi`` (with ``TRL_QS_HAS_HI``) or ended before ``lo`` (with
   ``TRL_QS_HAS_LO``).

.. c:function:: int trl_qscan_run(trl_qscan_t *q)

   Read the planned blocks, join and filter. Returns
   :c:macro:`TRL_QS_OK`, :c:macro:`TRL_QS_FALLBACK`, or a
   :c:type:`trl_status_t` error.

.. c:function:: const char *trl_qscan_fallback_reason(const trl_qscan_t *q)

   Why the last run returned :c:macro:`TRL_QS_FALLBACK`.

.. c:function:: size_t trl_qscan_rows(const trl_qscan_t *q, const trl_qs_row_t **out)
                size_t trl_qscan_attrs(const trl_qscan_t *q, const trl_qs_attr_t **out)
                size_t trl_qscan_metas(const trl_qscan_t *q, const trl_qs_meta_t **out)
                size_t trl_qscan_links(const trl_qscan_t *q, const trl_qs_link_t **out)
                size_t trl_qscan_heap(const trl_qscan_t *q, const uint8_t **out)

   Point ``*out`` at an output array and return its length.

.. c:function:: size_t trl_qscan_proj(const trl_qscan_t *q, trl_qs_proj_t *out)

   With :c:macro:`TRL_QS_PROJECT`: fill ``out`` with the rows the engine
   decided in full (filter true, every value exact) and return their
   count. The other rows are in :c:func:`trl_qscan_rows`.

.. c:function:: void trl_qscan_counts(const trl_qscan_t *q, int64_t out[4])

   ``out[0]`` blocks read, ``out[1]`` duplicate ids, ``out[2]`` rows kept,
   ``out[3]`` rows deferred.
