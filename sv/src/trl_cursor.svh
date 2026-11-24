// trl_cursor.svh — time-ordered walk over a reader's transactions.
// Included by trl_pkg.sv.

// A time-ordered walk over the transactions of a :sv:class:`trl_reader`.
//
// Each successful :sv:func:`next <trl_cursor::next>` loads one transaction's header into the
// public properties (``id``, ``start``, ``end_t``, ...) in a single DPI
// call; field values are fetched by index with the ``get_*`` functions::
//
//     trl_cursor c = trl_cursor::create(r);
//     while (c.next()) begin
//         c.wait_start();                 // replay in simulation time
//         $display("%0d @%0t a=%0d", c.id, c.start, c.get_int(f_a));
//     end
//     c.free();
class trl_cursor;
    chandle    h;      // DPI handle (internal).
    trl_reader m_rdb;  // Reader this cursor walks (internal).

    longint unsigned id;      // Current transaction ID.
    longint unsigned start;   // Current transaction start time.
    longint unsigned end_t;   // Current transaction end time.
    longint unsigned parent;  // Parent transaction ID, or ``0``.
    int stream_inst_id;       // Stream instance ID; see :sv:func:`trl_rstream::inst_id`.
    int txn_type_id;          // Type ID of the current transaction.
    int attr_count;           // Number of field values present.
    int link_count;           // Number of outgoing links.
    int meta_count;           // Number of metadata pairs.

    // Create a cursor over a time window.
    //
    // :param r: reader to walk
    // :param s: walk only this stream; ``null`` walks every stream (add more with :sv:func:`add_stream <trl_cursor::add_stream>`)
    // :param t_from: start of the time window
    // :param t_to: end of the time window
    // :returns: the cursor, or ``null`` on error
    static function trl_cursor create(trl_reader r, trl_rstream s = null,
                                      longint unsigned t_from = 0,
                                      longint unsigned t_to = 64'hFFFF_FFFF_FFFF_FFFF);
        trl_cursor c = new;
        chandle s_h;
        if (r == null || r.h == null) return null;
        c.m_rdb = r;
        s_h = null;
        if (s != null) s_h = s.h;
        c.h = trl_sv_cursor_new(r.h, s_h, t_from, t_to);
        if (c.h == null) return null;
        return c;
    endfunction

    // Add a stream to the set this cursor walks.
    //
    // :param s: stream to add
    function void add_stream(trl_rstream s);
        if (h != null && s != null) void'(trl_sv_cursor_add_stream(h, s.h));
    endfunction

    // Advance to the next transaction in start-time order.
    //
    // :returns: ``1`` if a transaction was loaded, ``0`` at the end
    function bit next();
        if (h == null) return 1'b0;
        return trl_sv_cursor_next(h, id, stream_inst_id, txn_type_id, start, end_t, parent,
                                  attr_count, link_count, meta_count) != 0;
    endfunction

    // :param f: field index
    // :returns: ``1`` if the current transaction has a value for field ``f``
    function bit has(int f);
        return (h == null) ? 1'b0 : (trl_sv_cur_has(h, f) != 0);
    endfunction

    // :param f: field index
    // :returns: the integer value of field ``f``, or ``0`` if absent
    function longint unsigned get_int(int f);
        return (h == null) ? 0 : trl_sv_cur_int(h, f);
    endfunction

    // :param f: field index
    // :returns: the floating-point value of field ``f``, or ``0.0`` if absent
    function real get_real(int f);
        return (h == null) ? 0.0 : trl_sv_cur_real(h, f);
    endfunction

    // :param f: field index
    // :returns: the string value of field ``f``, or ``""`` if absent
    function string get_str(int f);
        return (h == null) ? "" : trl_sv_cur_str(h, f);
    endfunction

    // :returns: the number of links on the current transaction
    function int num_links();
        return link_count;
    endfunction

    // Read link ``i`` of the current transaction.
    //
    // :param i: link position, ``0`` to ``num_links()-1``
    // :param tgt: receives the target transaction ID
    // :param k: receives the link kind
    // :param label: receives the label, or ``""``
    // :returns: ``1`` on success
    function bit link(int i, output longint unsigned tgt, output trl_link_e k, output string label);
        int ltype, label_sid;
        if (h == null || trl_sv_cur_link(h, i, tgt, ltype, label_sid) == 0) return 1'b0;
        k = trl_link_e'(ltype);
        label = (m_rdb == null) ? "" : m_rdb.string_lookup(label_sid);
        return 1'b1;
    endfunction

    // :returns: the number of metadata pairs on the current transaction
    function int num_metas();
        return meta_count;
    endfunction

    // Read metadata pair ``i`` of the current transaction.
    //
    // :param i: position, ``0`` to ``num_metas()-1``
    // :param key: receives the key
    // :param val: receives the value
    // :returns: ``1`` on success
    function bit meta_at(int i, output string key, output string val);
        int key_sid, val_sid;
        if (h == null || trl_sv_cur_meta_at(h, i, key_sid, val_sid) == 0) return 1'b0;
        key = (m_rdb == null) ? "" : m_rdb.string_lookup(key_sid);
        val = (m_rdb == null) ? "" : m_rdb.string_lookup(val_sid);
        return 1'b1;
    endfunction

    // Block until simulation time reaches the current transaction's start.
    //
    // Use it to replay a trace in time.
    task wait_start();
        if (start > $time) #(start - $time);
    endtask

    // Release the cursor. It cannot be used afterwards.
    function void free();
        if (h != null) begin
            trl_sv_cursor_free(h);
            h = null;
        end
    endfunction
endclass
