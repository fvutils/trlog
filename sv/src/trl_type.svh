// trl_type.svh — write-side transaction type (schema being built).
// Included by trl_pkg.sv.

// A transaction type (schema) on the recording side.
//
// Create it, add fields, then :sv:func:`seal <trl_type::seal>` it before the first
// transaction of that type is recorded::
//
//     trl_type t = trl_type::create("pkt");
//     int f_addr = t.add_field("addr", TRL_FT_U32);
//     int f_data = t.add_field("data", TRL_FT_U64);
//     t.seal();
//
// The field index that :sv:func:`add_field <trl_type::add_field>` returns is
// what the ``set_*`` functions of :sv:class:`trl_txn` take.
//
// Types live either in the database's global namespace or in a stream's
// scope. On the reading side, the same schema is described by
// a :sv:class:`trl_schema`.
class trl_type;
    chandle h;      // DPI handle (internal); ``null`` when recording is off.

    // Create a new, empty type.
    //
    // :param name: type name
    // :param scope: stream whose namespace the type lives in, or ``null`` for the database's global namespace
    // :param d: database; ``null`` selects the ``"default"`` database
    // :returns: the type, or ``null`` when recording is off
    static function trl_type create(string name, trl_stream scope = null, trl_db d = null);
        trl_type t = new;
        chandle scope_h;
        if (d == null) d = trl_db::get();
        if (d == null) return null;
        scope_h = null;
        if (scope != null) scope_h = scope.h;
        t.h = trl_sv_type_new(d.h, name, scope_h);
        return t;
    endfunction

    // Look up an existing type by name.
    //
    // With a ``scope``, the stream and its ancestors are searched before
    // the global namespace.
    //
    // :param name: type name
    // :param scope: stream to start the search at, or ``null``
    // :param d: database; ``null`` selects the ``"default"`` database
    // :returns: the type, or ``null`` when not found or recording is off
    static function trl_type find(string name, trl_stream scope = null, trl_db d = null);
        trl_type t;
        chandle hh, scope_h;
        if (d == null) d = trl_db::get();
        if (d == null) return null;
        scope_h = null;
        if (scope != null) scope_h = scope.h;
        hh = trl_sv_type_find(d.h, name, scope_h);
        if (hh == null) return null;
        t = new;
        t.h = hh;
        return t;
    endfunction

    // Append a field to an unsealed type.
    //
    // :param name: field name
    // :param k: field kind
    // :param width: bit width, for the vector kinds; ``0`` otherwise
    // :returns: the field index, or ``-1`` on error or when recording is off
    function int add_field(string name, trl_field_type_e k, int width = 0);
        return (h == null) ? -1 : trl_sv_type_add_field(h, name, k, width);
    endfunction

    // Freeze the field list. Required before the type is recorded.
    function void seal();
        if (h != null) void'(trl_sv_type_seal(h));
    endfunction

    // :returns: the type name, or ``""`` when recording is off
    function string name();
        return (h == null) ? "" : trl_sv_type_name(h);
    endfunction

    // :returns: the number of fields
    function int num_fields();
        return (h == null) ? 0 : trl_sv_type_num_fields(h);
    endfunction

    // Look up a field index by name.
    //
    // :param path: field name
    // :returns: the field index, or ``-1`` if there is no such field
    function int index(string path);
        return (h == null) ? -1 : trl_sv_type_field_index(h, path);
    endfunction

    // Describe field ``f``.
    //
    // :param f: field index, ``0`` to ``num_fields()-1``
    // :param nm: receives the field name
    // :param k: receives the field kind
    // :param width: receives the bit width (``0`` for fixed-size kinds)
    function void field_info(int f, output string nm, output trl_field_type_e k, output int width);
        nm = trl_sv_type_field_name(h, f);
        k  = trl_field_type_e'(trl_sv_type_field_kind(h, f));
        width = trl_sv_type_field_width(h, f);
    endfunction
endclass
