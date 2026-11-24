// trl_txn.svh — an open transaction. Included by trl_pkg.sv.

// An open transaction, returned by :sv:func:`trl_stream::open_txn`.
//
// Set field values with the ``set_*`` functions, then :sv:func:`close
// <trl_txn::close>` it. The object is a thin handle: when the stream is
// disabled the handle is ``null``, and every function is a single SV ``null``
// check with no DPI call. Code can therefore record unconditionally.
//
// To avoid one object allocation per transaction, keep a ``trl_txn`` and pass
// it as the ``into`` argument of :sv:func:`trl_stream::open_txn`.
class trl_txn;
    chandle h;      // DPI handle (internal); ``null`` when disabled or closed.
    local static trl_txn m_null;

    // :returns: the shared no-op transaction that a disabled stream hands out
    static function trl_txn null_txn();
        if (m_null == null) m_null = new;
        return m_null;
    endfunction

    // Detach this object from its transaction without closing it.
    //
    // :returns: this object
    function trl_txn clear();
        h = null;
        return this;
    endfunction

    // :returns: the transaction ID, unique within the database; ``0`` when disabled. Use it as a link target or as a ``parent`` argument.
    function longint unsigned id();
        return (h == null) ? 0 : trl_sv_txn_id(h);
    endfunction

    // Set an integer, boolean or enum field.
    //
    // :param f: field index from :sv:func:`trl_type::add_field` or :sv:func:`trl_stream::field`
    // :param v: value; narrower kinds use the low-order bits
    function void set_int(int f, longint unsigned v);
        if (h != null) trl_sv_set_int(h, f, v);
    endfunction

    // Set a floating-point field.
    //
    // :param f: field index
    // :param v: value
    function void set_real(int f, real v);
        if (h != null) trl_sv_set_real(h, f, v);
    endfunction

    // Set a string field, passing the string across DPI.
    //
    // :param f: field index
    // :param v: value
    function void set_str(int f, string v);
        if (h != null) trl_sv_set_str(h, f, v);
    endfunction

    // Set a string field from an interned string ID.
    //
    // Cheaper than :sv:func:`set_str <trl_txn::set_str>` for repeated values.
    //
    // :param f: field index
    // :param sid: string ID from :sv:func:`trl_db::intern`
    function void set_str_id(int f, int sid);
        if (h != null) trl_sv_set_str_id(h, f, sid);
    endfunction

    // Add a link from this transaction to another.
    //
    // :param tgt: target transaction ID (see :sv:func:`id <trl_txn::id>`)
    // :param k: link kind
    // :param label_sid: optional label, as an interned string ID; ``0`` for none
    function void link(longint unsigned tgt, trl_link_e k = TRL_LINK_RELATED, int label_sid = 0);
        if (h != null) trl_sv_link(h, tgt, k, label_sid);
    endfunction

    // Attach a key/value metadata pair to this transaction.
    //
    // :param key_sid: key, as an interned string ID
    // :param val_sid: value, as an interned string ID
    function void meta(int key_sid, int val_sid);
        if (h != null) trl_sv_meta(h, key_sid, val_sid);
    endfunction

    // End the transaction and hand it to the writer.
    //
    // The object can be reused afterwards via the ``into`` argument
    // of :sv:func:`trl_stream::open_txn`.
    //
    // :param end_t: end time; defaults to the current simulation time
    function void close(longint unsigned end_t = trl_now());
        if (h != null) begin
            trl_sv_end(h, end_t);
            h = null;
        end
    endfunction
endclass
