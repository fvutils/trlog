// trl_db.svh — a recording database. Included by trl_pkg.sv.

// A transaction database open for recording.
//
// Most code never names a database: every ``create``/``find`` call defaults
// to the ``"default"`` database, which :sv:func:`get <trl_db::get>` opens
// lazily from the ``+trlog_file`` plusarg. Use :sv:func:`open <trl_db::open>`
// only to record into an additional file.
//
// A ``null`` return from :sv:func:`get <trl_db::get>` means recording is off;
// every other class in the package treats a ``null`` database as "do
// nothing".
class trl_db;
    chandle h;      // DPI handle onto the C session (internal).
    string  name;   // Registry name this database was opened under.
    local static trl_db m_dbs[string];
    local static bit    m_enabled;
    local static bit    m_inited;

    // Return the database registered as ``name``.
    //
    // For ``"default"``, the first call reads the plusargs and opens the
    // file: ``+trlog_file=<path>`` turns recording on, ``+trlog_off`` forces
    // it off, and ``+trlog_compress=none|zlib`` and ``+trlog_block_kb=<n>``
    // tune the writer. Later calls return the cached object with no DPI call.
    // Any other name must first be opened with :sv:func:`open
    // <trl_db::open>`.
    //
    // :param name: registry name; ``"default"`` triggers lazy init
    // :returns: the database, or ``null`` when recording is off or ``name`` was never opened
    static function trl_db get(string name = "default");
        string path;
        int compress_i;
        int block_kb;
        bit off;
        if (m_dbs.exists(name)) return m_dbs[name];
        if (name == "default") begin
            m_inited = 1'b1;
            if ($value$plusargs("trlog_off", off) && off) begin
                m_enabled = 1'b0;
                return null;
            end
            if (!$value$plusargs("trlog_file=%s", path)) begin
                m_enabled = 1'b0;
                return null;
            end
            compress_i = 1; // zlib default
            begin
                string compress_s;
                if ($value$plusargs("trlog_compress=%s", compress_s))
                    compress_i = (compress_s == "none") ? 0 : 1;
            end
            block_kb = 64;
            void'($value$plusargs("trlog_block_kb=%d", block_kb));
            return open(name, path, compress_i, block_kb);
        end
        return null;
    endfunction

    // Open an additional recording database and register it as ``name``.
    //
    // The file's timescale is fixed at 1 ns.
    //
    // :param name: registry name, later passed to :sv:func:`get <trl_db::get>` and to the ``d`` argument of the ``create``/``find`` functions
    // :param path: output file path
    // :param compress: ``1`` for zlib block compression, ``0`` for none
    // :param block_kb: target block payload size in KiB
    // :returns: the database, or ``null`` if the file could not be opened
    static function trl_db open(string name, string path, int compress = 1, int block_kb = 64);
        trl_db d = new;
        d.name = name;
        d.h = trl_sv_db_open(name, path, -9 /* ns */, compress, block_kb, 0);
        if (d.h == null) return null;
        m_dbs[name] = d;
        if (name == "default") m_enabled = 1'b1;
        return d;
    endfunction

    // True when the ``"default"`` database is open for recording.
    //
    // Forces lazy init on first call; afterwards it reads a static bit with
    // no DPI call. Also available as the package function ``trl_enabled()``.
    static function bit enabled();
        if (!m_inited) void'(get());
        return m_enabled;
    endfunction

    // Close every open database, flushing buffered records to disk.
    //
    // Called by ``trl_shutdown()`` and the :sv:mod:`trl_final` module. Safe
    // to call more than once.
    static function void close_all();
        foreach (m_dbs[nm]) begin
            if (m_dbs[nm].h != null) void'(trl_sv_db_close(m_dbs[nm].h));
        end
        m_dbs.delete();
        m_enabled = 1'b0;
    endfunction

    // Intern a string in this database's string table.
    //
    // Pass the returned ID to :sv:func:`trl_txn::set_str_id`,
    // to :sv:func:`trl_txn::meta` or to :sv:func:`trl_txn::link`, to avoid
    // passing a string across DPI on every transaction.
    //
    // :param s: string to intern
    // :returns: the string ID, or ``0`` when the database is not open
    function int intern(string s);
        return (h == null) ? 0 : trl_sv_intern(h, s);
    endfunction
endclass
