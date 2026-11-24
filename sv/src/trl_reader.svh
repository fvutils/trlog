// trl_reader.svh — a database opened for reading. Included by trl_pkg.sv.

// A trace database opened for reading, for replay or checking in simulation.
//
// Walk its transactions with a :sv:class:`trl_cursor`.
class trl_reader;
    chandle h;      // DPI handle (internal).
    local static trl_reader m_rdbs[string];

    // Return the read database registered as ``name``, opening it from a
    // plusarg on first use.
    //
    // ``"default"`` reads ``+trlog_read=<path>``; any other name reads
    // ``+trlog_read_<name>=<path>``.
    //
    // :param name: registry name
    // :returns: the reader, or ``null`` if the plusarg is absent or the file could not be opened
    static function trl_reader get(string name = "default");
        string path;
        string plusarg;
        if (m_rdbs.exists(name)) return m_rdbs[name];
        plusarg = (name == "default") ? "trlog_read" : $sformatf("trlog_read_%s", name);
        if (!$value$plusargs({plusarg, "=%s"}, path)) return null;
        return open(name, path);
    endfunction

    // Open a trace file for reading and register it as ``name``.
    //
    // :param name: registry name
    // :param path: trace file path
    // :returns: the reader, or ``null`` if the file could not be opened
    static function trl_reader open(string name, string path);
        trl_reader r = new;
        r.h = trl_sv_rdb_open(name, path);
        if (r.h == null) return null;
        m_rdbs[name] = r;
        return r;
    endfunction

    // :returns: the number of streams in the database
    function int num_streams();
        return (h == null) ? 0 : trl_sv_rdb_num_streams(h);
    endfunction

    // :param i: stream position, ``0`` to ``num_streams()-1``
    // :returns: the stream at position ``i``, or ``null``
    function trl_rstream stream_at(int i);
        trl_rstream s;
        chandle hh;
        if (h == null) return null;
        hh = trl_sv_rdb_stream_at(h, i);
        if (hh == null) return null;
        s = new;
        s.h = hh;
        s.m_path = trl_sv_rstream_path(hh);
        return s;
    endfunction

    // :param path: full hierarchical stream path
    // :returns: the stream, or ``null`` if not found
    function trl_rstream find_stream(string path);
        trl_rstream s;
        chandle hh;
        if (h == null) return null;
        hh = trl_sv_rdb_stream_find(h, path);
        if (hh == null) return null;
        s = new;
        s.h = hh;
        s.m_path = path;
        return s;
    endfunction

    // :param name: type name (stream-scoped types are named ``<stream path>::<name>``)
    // :returns: the schema, or ``null`` if not found
    function trl_schema find_schema(string name);
        trl_schema sc;
        chandle hh;
        if (h == null) return null;
        hh = trl_sv_rdb_schema_find(h, name);
        if (hh == null) return null;
        sc = new;
        sc.h = hh;
        return sc;
    endfunction

    // Resolve an interned string ID from this database.
    //
    // :param sid: string ID
    // :returns: the string, or ``""`` if unknown
    function string string_lookup(int sid);
        return (h == null) ? "" : trl_sv_string_lookup(h, sid);
    endfunction
endclass
