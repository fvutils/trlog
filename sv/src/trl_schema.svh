// trl_schema.svh — read-side schema introspection. Included by trl_pkg.sv.
//
// Distinct from the write-side `trl_type`; see trl_pkg.sv's header comment.

// A transaction type in a database opened for reading.
//
// The read-side counterpart of :sv:class:`trl_type`. Use :sv:func:`index
// <trl_schema::index>` to map a field name to the index
// that :sv:class:`trl_cursor`'s ``get_*`` functions take.
class trl_schema;
    chandle h;      // DPI handle (internal).

    // :returns: the type name
    function string name();
        return (h == null) ? "" : trl_sv_rschema_name(h);
    endfunction

    // :returns: the number of fields
    function int num_fields();
        return (h == null) ? 0 : trl_sv_rschema_num_fields(h);
    endfunction

    // Look up a field index by name.
    //
    // :param name: field name
    // :returns: the field index, or ``-1`` if there is no such field
    function int index(string name);
        return (h == null) ? -1 : trl_sv_rschema_field_index(h, name);
    endfunction

    // Describe field ``f``.
    //
    // :param f: field index
    // :param nm: receives the field name
    // :param k: receives the field kind
    function void field_info(int f, output string nm, output trl_field_type_e k);
        nm = trl_sv_rschema_field_name(h, f);
        k  = trl_field_type_e'(trl_sv_rschema_field_kind(h, f));
    endfunction
endclass
