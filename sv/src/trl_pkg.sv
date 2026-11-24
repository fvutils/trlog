// Transaction recording and reading for SystemVerilog, over the trlog C
// library. Import it with ``import trl_pkg::*;``; every name it exports
// starts with ``trl_`` or ``TRL_``.
package trl_pkg;
    // Implementation notes: SV recording/query API over c/src/trl_sv.c
    // (sv_api_design.md §6).
    //
    // Users typically `import trl_pkg::*;`, so every package-scope name
    // (class, enum, enum literal, function) carries a `trl_`/`TRL_` prefix to
    // stay clear of user and UVM names. One class per .svh file; the DPI-C
    // imports (trl_sv_*) are included directly rather than kept in a separate
    // package, since this package is useless without them.
    //
    // Scope trimmed from the design doc for this phase (see sv_impl_status.md):
    //   - read-side schema introspection is a separate class, `trl_schema`, not
    //     a shared `trl_type`. This mirrors trl_query.h's `trl_rschema_t` vs
    //     trl_rec.h's `trl_rtype_t` split (phase 3's reasoning: two distinct C
    //     structs behind one SV class name would let a write-side handle flow
    //     silently into a read-side accessor).
    //   - no enum types, no `trl_type::add_struct/add_array` (nested types), no
    //     `set_bits/set_logic/set_bytes` on `trl_txn`, no matching `trl_cursor`
    //     `get_bits/get_logic/get_bytes` — trl_sv.c doesn't export any of
    //     these yet (see its header comment).
    //   - no `trl_cursor::seek` (trl_query.h doesn't implement it either).
    //   - lazy init only reads the plusargs L0/L1 can act on: `+trlog_file`,
    //     `+trlog_off`, `+trlog_compress`, `+trlog_block_kb`, `+trlog_read[_name]`.
    //     `+trlog_verbose`, `+trlog_streams[_off]`, `+trlog_buf_mb`,
    //     `+trlog_max_age` and `+trlog_check` are deferred: trl_sv_db_open's
    //     fixed argument list (matching the design's own §6.1 signature) has
    //     no slot for stream globs, and trl_rec.h doesn't implement a memory
    //     cap, a max-age flush or handle-checking yet.
    //   - `trl_stream::field()` (tier 3, ad-hoc by name) only supports the
    //     scalar kinds `trl_sv_type_field_get_or_add` can register (no
    //     width-bearing vector kinds, consistent with the set_bits trim).

    `include "trl_macros.svh"
    `include "trl_sv_dpi.svh"

    // Field kinds, for :sv:func:`trl_type::add_field` and
    // for :sv:func:`trl_stream::field`. Values match ``trl_field_type_t``
    // in the C library's ``trl.h``.
    typedef enum int {
        TRL_FT_U8=0, TRL_FT_U16, TRL_FT_U32, TRL_FT_U64,
        TRL_FT_I8, TRL_FT_I16, TRL_FT_I32, TRL_FT_I64,
        TRL_FT_F32, TRL_FT_F64, TRL_FT_BOOL, TRL_FT_STRING,
        TRL_FT_BITVEC, TRL_FT_BYTES, TRL_FT_TIME, TRL_FT_ENUM, TRL_FT_LOGICVEC
    } trl_field_type_e;

    // Kinds of link between two transactions, for :sv:func:`trl_txn::link`.
    typedef enum int {
        TRL_LINK_CAUSE=0, TRL_LINK_PARENT_CHILD=1, TRL_LINK_RELATED=2, TRL_LINK_CUSTOM=255
    } trl_link_e;

    typedef class trl_db;
    typedef class trl_stream;   // trl_type::create/find take a stream scope

    // :returns: the current simulation time (``$time``), the default for every start/end time argument in the package
    function automatic longint unsigned trl_now();
        return $time;
    endfunction

    // Recording side
    `include "trl_db.svh"
    `include "trl_type.svh"
    `include "trl_txn.svh"
    `include "trl_stream.svh"

    // Reading side
    `include "trl_rstream.svh"
    `include "trl_schema.svh"
    `include "trl_reader.svh"
    `include "trl_cursor.svh"

    // True when the ``"default"`` database is open for recording.
    //
    // Triggers lazy init on first call. See :sv:func:`trl_db::enabled`.
    function automatic bit trl_enabled();
        return trl_db::enabled();
    endfunction

    // Close every recording database. Call it at the end of the test (for
    // example from ``final_phase``), or instantiate :sv:mod:`trl_final`.
    function automatic void trl_shutdown();
        trl_db::close_all();
    endfunction

endpackage : trl_pkg
