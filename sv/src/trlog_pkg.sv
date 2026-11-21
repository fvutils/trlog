// trlog_pkg.sv — L2: SV classes over trlog_dpi_pkg (sv_api_design.md §6.2).
//
// Scope trimmed from the design doc for this phase (see sv_impl_status.md):
//   - read-side schema introspection is a separate class, `schema_h`, not
//     a shared `type_h`. This mirrors trl_query.h's `trl_rschema_t` vs
//     trl_rec.h's `trl_rtype_t` split (phase 3's reasoning: two distinct C
//     structs behind one SV class name would let a write-side handle flow
//     silently into a read-side accessor).
//   - no `enum_h`, no `type_h::add_struct/add_array` (nested types), no
//     `set_bits/set_logic/set_bytes` on `txn`, no matching `cursor`
//     `get_bits/get_logic/get_bytes` — trl_sv.c doesn't export any of
//     these yet (see its header comment).
//   - no `cursor::seek` (trl_query.h doesn't implement it either).
//   - lazy init only reads the plusargs L0/L1 can act on: `+trlog_file`,
//     `+trlog_off`, `+trlog_compress`, `+trlog_block_kb`, `+trlog_read[_name]`,
//     `+trlog_verbose`. `+trlog_streams[_off]`, `+trlog_buf_mb`,
//     `+trlog_max_age` and `+trlog_check` are deferred: trl_sv_db_open's
//     fixed argument list (matching the design's own §6.1 signature) has
//     no slot for stream globs, and trl_rec.h doesn't implement a memory
//     cap, a max-age flush or handle-checking yet.
//   - `stream::field()` (tier 3, ad-hoc by name) only supports the scalar
//     kinds `trl_sv_type_field_get_or_add` can register (no width-bearing
//     vector kinds, consistent with the set_bits/set_logic/set_bytes trim).

package trlog_pkg;
    import trlog_dpi_pkg::*;
    `include "trlog_macros.svh"

    typedef enum int {
        K_U8=0, K_U16, K_U32, K_U64, K_I8, K_I16, K_I32, K_I64,
        K_F32, K_F64, K_BOOL, K_STRING, K_BITS, K_BYTES, K_TIME, K_ENUM, K_LOGIC
    } kind_e;

    typedef enum int { L_CAUSE=0, L_PARENT_CHILD=1, L_RELATED=2, L_CUSTOM=255 } link_e;

    typedef class stream;   // type_h::create/find take a stream scope, declared below

    bit g_enabled;
    bit g_inited;

    function automatic longint unsigned now();
        return $time;
    endfunction

    // ------------------------------------------------------------------ db
    class db;
        chandle h;
        string  name;
        local static db m_dbs[string];

        // Lazy init: the first call reads plusargs and opens "default" (or
        // does nothing if recording is off). Later calls with the same
        // name return the cached handle with no DPI call.
        static function db get(string name = "default");
            string path;
            int compress_i;
            int block_kb;
            bit off;
            if (m_dbs.exists(name)) return m_dbs[name];
            if (name == "default") begin
                g_inited = 1'b1;
                if ($value$plusargs("trlog_off", off) && off) begin
                    g_enabled = 1'b0;
                    return null;
                end
                if (!$value$plusargs("trlog_file=%s", path)) begin
                    g_enabled = 1'b0;
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

        // Explicit open, for extra databases beyond "default".
        static function db open(string name, string path, int compress = 1, int block_kb = 64);
            db d = new;
            d.name = name;
            d.h = trl_sv_db_open(name, path, -9 /* ns */, compress, block_kb, 0);
            if (d.h == null) return null;
            m_dbs[name] = d;
            if (name == "default") g_enabled = 1'b1;
            return d;
        endfunction

        static function void close_all();
            foreach (m_dbs[nm]) begin
                if (m_dbs[nm].h != null) void'(trl_sv_db_close(m_dbs[nm].h));
            end
            m_dbs.delete();
            g_enabled = 1'b0;
        endfunction

        function int intern(string s);
            return (h == null) ? 0 : trl_sv_intern(h, s);
        endfunction
    endclass

    function automatic bit enabled();
        if (!g_inited) void'(db::get());
        return g_enabled;
    endfunction

    function automatic void shutdown();
        db::close_all();
    endfunction

    // ------------------------------------------------------------------ type_h (write side)
    class type_h;
        chandle h;

        static function type_h create(string name, stream scope = null, db d = null);
            type_h t = new;
            chandle scope_h;
            if (d == null) d = db::get();
            if (d == null) return null;
            scope_h = null;
            if (scope != null) scope_h = scope.h;
            t.h = trl_sv_type_new(d.h, name, scope_h);
            return t;
        endfunction

        static function type_h find(string name, stream scope = null, db d = null);
            type_h t;
            chandle hh, scope_h;
            if (d == null) d = db::get();
            if (d == null) return null;
            scope_h = null;
            if (scope != null) scope_h = scope.h;
            hh = trl_sv_type_find(d.h, name, scope_h);
            if (hh == null) return null;
            t = new;
            t.h = hh;
            return t;
        endfunction

        function int add_field(string name, kind_e k, int width = 0);
            return (h == null) ? -1 : trl_sv_type_add_field(h, name, k, width);
        endfunction

        function void seal();
            if (h != null) void'(trl_sv_type_seal(h));
        endfunction

        function string name();
            return (h == null) ? "" : trl_sv_type_name(h);
        endfunction

        function int num_fields();
            return (h == null) ? 0 : trl_sv_type_num_fields(h);
        endfunction

        function int index(string path);
            return (h == null) ? -1 : trl_sv_type_field_index(h, path);
        endfunction

        function void field_info(int f, output string nm, output kind_e k, output int width);
            nm = trl_sv_type_field_name(h, f);
            k  = kind_e'(trl_sv_type_field_kind(h, f));
            width = trl_sv_type_field_width(h, f);
        endfunction
    endclass

    // ------------------------------------------------------------------ txn
    // Thin handle. h == null means disabled/no-op: every method below is a
    // single `if (h != null)` check with no DPI call when false.
    class txn;
        chandle h;
        local static txn m_null;

        static function txn null_txn();
            if (m_null == null) m_null = new;
            return m_null;
        endfunction

        function txn clear();
            h = null;
            return this;
        endfunction

        function longint unsigned id();
            return (h == null) ? 0 : trl_sv_txn_id(h);
        endfunction

        function void set_int(int f, longint unsigned v);
            if (h != null) trl_sv_set_int(h, f, v);
        endfunction

        function void set_real(int f, real v);
            if (h != null) trl_sv_set_real(h, f, v);
        endfunction

        function void set_str(int f, string v);
            if (h != null) trl_sv_set_str(h, f, v);
        endfunction

        function void set_str_id(int f, int sid);
            if (h != null) trl_sv_set_str_id(h, f, sid);
        endfunction

        function void link(longint unsigned tgt, link_e k = L_RELATED, int label_sid = 0);
            if (h != null) trl_sv_link(h, tgt, k, label_sid);
        endfunction

        function void meta(int key_sid, int val_sid);
            if (h != null) trl_sv_meta(h, key_sid, val_sid);
        endfunction

        function void close(longint unsigned end_t = now());
            if (h != null) begin
                trl_sv_end(h, end_t);
                h = null;
            end
        endfunction
    endclass

    // ------------------------------------------------------------------ stream
    class stream;
        chandle h;      // null when disabled
        bit     en;
        string  path;
        type_h  dflt_type;
        local int m_fidx[string];   // ad-hoc name -> index cache (tier 3)
        local type_h m_adhoc;

        static function stream create(string name, stream parent = null,
                                       type_h dflt = null, string kind = "", db d = null);
            stream s = new;
            int enabled_out;
            chandle parent_h, dflt_h;
            if (d == null) d = db::get();
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

        static function stream find(string path, db d = null);
            stream s;
            chandle hh;
            if (d == null) d = db::get();
            if (d == null) return null;
            hh = trl_sv_stream_find(d.h, path);
            if (hh == null) return null;
            s = new;
            s.h = hh;
            s.path = path;
            s.en = 1'b1;
            return s;
        endfunction

        // Tier 1: one DPI call. Returns the txn id (0 when disabled).
        // Composed from open_txn/set_int/close rather than one trl_sv_record
        // DPI call (k+2 crossings, not the design's 1): Verilator cannot
        // pass a dynamic SV array (what an unsized `v[]` formal becomes
        // once a caller's actual array is bound to it) through a DPI open
        // array argument ("Passing dynamic array or queue as actual
        // argument to DPI open array is not yet supported", verilator
        // 5.041). trl_sv_record itself still exists in trl_sv.c/
        // trlog_dpi_pkg and is covered by c/tests/test_sv.c's plain-C-ABI
        // check, for simulators that don't share this limitation.
        function longint unsigned record(longint unsigned start, longint unsigned end_t,
                                          longint unsigned v[], type_h t = null,
                                          longint unsigned parent = 0);
            txn x;
            longint unsigned tid;
            if (!en) return 0;
            x = open_txn(t, parent, start);
            foreach (v[i]) x.set_int(i, v[i]);
            tid = x.id();
            x.close(end_t);
            return tid;
        endfunction

        // Tier 2 (`begin` is an SV keyword, hence open_txn).
        function txn open_txn(type_h t = null, longint unsigned parent = 0,
                               longint unsigned start = now(), txn into = null);
            txn x;
            if (!en) return (into != null) ? into.clear() : txn::null_txn();
            if (into != null) x = into;
            else x = new;
            x.h = trl_sv_begin(h, (t == null) ? dflt_type.h : t.h, start, parent);
            return x;
        endfunction

        function txn open_child(txn parent, type_h t = null, longint unsigned start = now(),
                                 txn into = null);
            return open_txn(t, parent.id(), start, into);
        endfunction

        // Tier 3: ad-hoc, get-or-create index. A cache hit costs no DPI call.
        function type_h adhoc_type();
            if (m_adhoc == null) begin
                m_adhoc = new;
                if (h != null) m_adhoc.h = trl_sv_stream_adhoc_type(h);
            end
            return m_adhoc;
        endfunction

        function int field(string name, kind_e k, int width = 0);
            if (!en) return -1;
            if (!m_fidx.exists(name))
                m_fidx[name] = trl_sv_type_field_get_or_add(adhoc_type().h, name, k, width);
            return m_fidx[name];
        endfunction
    endclass

    // ------------------------------------------------------------------ reading
    class rstream;
        chandle h;
        string  m_path;

        function string path();
            return m_path;
        endfunction

        function int inst_id();
            return (h == null) ? -1 : trl_sv_rstream_inst_id(h);
        endfunction
    endclass

    // Read-side schema introspection (see the header comment for why this
    // is a distinct class from the write-side `type_h`).
    class schema_h;
        chandle h;

        function string name();
            return (h == null) ? "" : trl_sv_rschema_name(h);
        endfunction

        function int num_fields();
            return (h == null) ? 0 : trl_sv_rschema_num_fields(h);
        endfunction

        function int index(string name);
            return (h == null) ? -1 : trl_sv_rschema_field_index(h, name);
        endfunction

        function void field_info(int f, output string nm, output kind_e k);
            nm = trl_sv_rschema_field_name(h, f);
            k  = kind_e'(trl_sv_rschema_field_kind(h, f));
        endfunction
    endclass

    class reader;
        chandle h;
        local static reader m_rdbs[string];

        static function reader get(string name = "default");
            string path;
            string plusarg;
            reader r;
            if (m_rdbs.exists(name)) return m_rdbs[name];
            plusarg = (name == "default") ? "trlog_read" : $sformatf("trlog_read_%s", name);
            if (!$value$plusargs({plusarg, "=%s"}, path)) return null;
            return open(name, path);
        endfunction

        static function reader open(string name, string path);
            reader r = new;
            r.h = trl_sv_rdb_open(name, path);
            if (r.h == null) return null;
            m_rdbs[name] = r;
            return r;
        endfunction

        function int num_streams();
            return (h == null) ? 0 : trl_sv_rdb_num_streams(h);
        endfunction

        function rstream stream_at(int i);
            rstream s;
            chandle hh;
            if (h == null) return null;
            hh = trl_sv_rdb_stream_at(h, i);
            if (hh == null) return null;
            s = new;
            s.h = hh;
            s.m_path = trl_sv_rstream_path(hh);
            return s;
        endfunction

        function rstream find_stream(string path);
            rstream s;
            chandle hh;
            if (h == null) return null;
            hh = trl_sv_rdb_stream_find(h, path);
            if (hh == null) return null;
            s = new;
            s.h = hh;
            s.m_path = path;
            return s;
        endfunction

        function schema_h find_schema(string name);
            schema_h sc;
            chandle hh;
            if (h == null) return null;
            hh = trl_sv_rdb_schema_find(h, name);
            if (hh == null) return null;
            sc = new;
            sc.h = hh;
            return sc;
        endfunction

        function string string_lookup(int sid);
            return (h == null) ? "" : trl_sv_string_lookup(h, sid);
        endfunction
    endclass

    class cursor;
        chandle h;
        reader  m_rdb;

        // Current record header, filled in by next() in one crossing.
        longint unsigned id, start, end_t, parent;
        int stream_inst_id, txn_type_id, attr_count, link_count, meta_count;

        static function cursor create(reader r, rstream s = null,
                                       longint unsigned t_from = 0,
                                       longint unsigned t_to = 64'hFFFF_FFFF_FFFF_FFFF);
            cursor c = new;
            chandle s_h;
            if (r == null || r.h == null) return null;
            c.m_rdb = r;
            s_h = null;
            if (s != null) s_h = s.h;
            c.h = trl_sv_cursor_new(r.h, s_h, t_from, t_to);
            if (c.h == null) return null;
            return c;
        endfunction

        function void add_stream(rstream s);
            if (h != null && s != null) void'(trl_sv_cursor_add_stream(h, s.h));
        endfunction

        function bit next();
            if (h == null) return 1'b0;
            return trl_sv_cursor_next(h, id, stream_inst_id, txn_type_id, start, end_t, parent,
                                       attr_count, link_count, meta_count) != 0;
        endfunction

        function bit has(int f);
            return (h == null) ? 1'b0 : (trl_sv_cur_has(h, f) != 0);
        endfunction

        function longint unsigned get_int(int f);
            return (h == null) ? 0 : trl_sv_cur_int(h, f);
        endfunction

        function real get_real(int f);
            return (h == null) ? 0.0 : trl_sv_cur_real(h, f);
        endfunction

        function string get_str(int f);
            return (h == null) ? "" : trl_sv_cur_str(h, f);
        endfunction

        function int num_links();
            return link_count;
        endfunction

        function bit link(int i, output longint unsigned tgt, output link_e k, output string label);
            int ltype, label_sid;
            if (h == null || trl_sv_cur_link(h, i, tgt, ltype, label_sid) == 0) return 1'b0;
            k = link_e'(ltype);
            label = (m_rdb == null) ? "" : m_rdb.string_lookup(label_sid);
            return 1'b1;
        endfunction

        function int num_metas();
            return meta_count;
        endfunction

        function bit meta_at(int i, output string key, output string val);
            int key_sid, val_sid;
            if (h == null || trl_sv_cur_meta_at(h, i, key_sid, val_sid) == 0) return 1'b0;
            key = (m_rdb == null) ? "" : m_rdb.string_lookup(key_sid);
            val = (m_rdb == null) ? "" : m_rdb.string_lookup(val_sid);
            return 1'b1;
        endfunction

        task wait_start();
            if (start > $time) #(start - $time);
        endtask

        function void free();
            if (h != null) begin
                trl_sv_cursor_free(h);
                h = null;
            end
        endfunction
    endclass

endpackage : trlog_pkg
