// trl_stream.svh — a recording stream. Included by trl_pkg.sv.

// A recording stream: a named, hierarchical channel of transactions.
//
// Typically one per monitor or interface. Transactions are recorded in one of
// three ways, from fastest to most convenient:
//
// 1. :sv:func:`record <trl_stream::record>` writes a complete transaction
//    of integer fields in one call.
// 2. :sv:func:`open_txn <trl_stream::open_txn>` returns a transaction
//    object, :sv:class:`trl_txn`, to fill in and close.
// 3. :sv:func:`field <trl_stream::field>` declares fields by name on the
//    fly, on the stream's ad-hoc type, with no schema set up in advance.
//
// Test :sv:prop:`en <trl_stream::en>` (or use the ``TRL_IF`` macro) to skip
// computing field values when the stream is disabled.
class trl_stream;
    chandle  h;          // DPI handle (internal); ``null`` when disabled.
    bit      en;         // 1 when the stream is recording. Checking it costs no DPI call.
    string   path;       // Stream name as passed to :sv:func:`create <trl_stream::create>`.
    trl_type dflt_type;  // Type used when a recording call passes ``t = null``.
    local int      m_fidx[string];   // ad-hoc name -> index cache (tier 3)
    local trl_type m_adhoc;

    // Create a stream.
    //
    // When recording is off this still returns an object, with
    // ``en == 0``, so callers need no ``null`` check.
    //
    // :param name: stream name, relative to ``parent``
    // :param parent: parent stream, or ``null`` for a top-level stream
    // :param dflt: default transaction type for this stream
    // :param kind: free-form stream kind recorded in the trace
    // :param d: database; ``null`` selects the ``"default"`` database
    // :returns: the stream
    static function trl_stream create(string name, trl_stream parent = null,
                                      trl_type dflt = null, string kind = "", trl_db d = null);
        trl_stream s = new;
        int enabled_out;
        chandle parent_h, dflt_h;
        if (d == null) d = trl_db::get();
        if (d == null) begin
            s.h = null;
            s.en = 1'b0;
            return s;
        end
        s.path = name;
        s.dflt_type = dflt;
        parent_h = null;
        if (parent != null) parent_h = parent.h;
        dflt_h = null;
        if (dflt != null) dflt_h = dflt.h;
        s.h = trl_sv_stream_new(d.h, parent_h, name, kind, dflt_h, enabled_out);
        s.en = (s.h != null) && (enabled_out != 0);
        return s;
    endfunction

    // Look up an existing stream by its full path.
    //
    // :param path: hierarchical stream path
    // :param d: database; ``null`` selects the ``"default"`` database
    // :returns: the stream, or ``null`` when not found or recording is off
    static function trl_stream find(string path, trl_db d = null);
        trl_stream s;
        chandle hh;
        if (d == null) d = trl_db::get();
        if (d == null) return null;
        hh = trl_sv_stream_find(d.h, path);
        if (hh == null) return null;
        s = new;
        s.h = hh;
        s.path = path;
        s.en = 1'b1;
        return s;
    endfunction

    // Implementation note: record() is composed from open_txn/set_int/close
    // rather than one trl_sv_record DPI call (k+2 crossings, not the design's 1): Verilator cannot
    // pass a dynamic SV array (what an unsized `v[]` formal becomes
    // once a caller's actual array is bound to it) through a DPI open
    // array argument ("Passing dynamic array or queue as actual
    // argument to DPI open array is not yet supported", verilator
    // 5.041). trl_sv_record itself still exists in trl_sv.c/
    // trl_sv_dpi.svh and is covered by c/tests/test_sv.c's plain-C-ABI
    // check, for simulators that don't share this limitation.

    // Record a complete transaction whose fields are all integers.
    //
    // ``v[i]`` is the value of field ``i``.
    //
    // :param start: start time
    // :param end_t: end time
    // :param v: field values, in field-index order
    // :param t: transaction type; ``null`` selects :sv:prop:`dflt_type <trl_stream::dflt_type>`
    // :param parent: parent transaction ID, or ``0``
    // :returns: the transaction ID, or ``0`` when disabled or when there is no type
    function longint unsigned record(longint unsigned start, longint unsigned end_t,
                                     longint unsigned v[], trl_type t = null,
                                     longint unsigned parent = 0);
        trl_txn x;
        longint unsigned tid;
        if (!en) return 0;
        x = open_txn(t, parent, start);
        foreach (v[i]) x.set_int(i, v[i]);
        tid = x.id();
        x.close(end_t);
        return tid;
    endfunction

    // Open a transaction to be filled in and closed by the caller.
    //
    // (Named ``open_txn`` because ``begin`` is an SV keyword.)
    //
    // :param t: transaction type; ``null`` selects :sv:prop:`dflt_type <trl_stream::dflt_type>`.
    //     If both are ``null``, a ``$warning`` is printed and nothing is recorded.
    // :param parent: parent transaction ID, or ``0``
    // :param start: start time; defaults to the current simulation time
    // :param into: caller-owned object to reuse instead of allocating one
    // :returns: the transaction; a no-op transaction (ID ``0``) when disabled
    //     or when there is no type
    function trl_txn open_txn(trl_type t = null, longint unsigned parent = 0,
                              longint unsigned start = trl_now(), trl_txn into = null);
        trl_txn x;
        if (!en) return (into != null) ? into.clear() : trl_txn::null_txn();
        if (t == null && dflt_type == null) begin
            $warning("trl_stream '%s' has no default type: pass a type to open_txn/record; nothing recorded",
                     path);
            return (into != null) ? into.clear() : trl_txn::null_txn();
        end
        if (into != null) x = into;
        else x = new;
        x.h = trl_sv_begin(h, (t == null) ? dflt_type.h : t.h, start, parent);
        return x;
    endfunction

    // Open a transaction as a child of ``parent``.
    //
    // Same as :sv:func:`open_txn <trl_stream::open_txn>` with ``parent.id()``
    // as the parent ID.
    //
    // :param parent: parent transaction
    // :param t: transaction type; ``null`` selects :sv:prop:`dflt_type <trl_stream::dflt_type>`
    // :param start: start time; defaults to the current simulation time
    // :param into: caller-owned object to reuse
    // :returns: the transaction
    function trl_txn open_child(trl_txn parent, trl_type t = null,
                                longint unsigned start = trl_now(), trl_txn into = null);
        return open_txn(t, parent.id(), start, into);
    endfunction

    // :returns: this stream's ad-hoc type, which :sv:func:`field <trl_stream::field>` adds to. Pass it as ``t`` to :sv:func:`open_txn <trl_stream::open_txn>` when recording ad-hoc fields.
    function trl_type adhoc_type();
        if (m_adhoc == null) begin
            m_adhoc = new;
            if (h != null) m_adhoc.h = trl_sv_stream_adhoc_type(h);
        end
        return m_adhoc;
    endfunction

    // Return the index of an ad-hoc field, creating it on first use.
    //
    // Repeat lookups hit an SV associative array and cost no DPI call.
    // Only scalar kinds are supported.
    //
    // :param name: field name
    // :param k: field kind
    // :param width: bit width; ``0`` for scalar kinds
    // :returns: the field index, or ``-1`` when disabled
    function int field(string name, trl_field_type_e k, int width = 0);
        if (!en) return -1;
        if (!m_fidx.exists(name))
            m_fidx[name] = trl_sv_type_field_get_or_add(adhoc_type().h, name, k, width);
        return m_fidx[name];
    endfunction
endclass
