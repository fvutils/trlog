// trl_uvm_pkg.sv — UVM transaction recording through trlog (L3 adapter).
//
// Install the database once, before any component records, for example in the
// test's build_phase:
//
//     trl_uvm_pkg::trl_uvm_tr_database::install();
//
// and turn on recording as usual (``recording_detail``). ``begin_tr``/``end_tr``
// then write each transaction into the trace opened by ``+trlog_file=``. Streams
// follow the component hierarchy: ``uvm_test_top.env.mon`` / ``main``.
//
// Classes woven by the ``trlog-record`` sv-weave plugin record through a typed fast
// path: their ``do_record`` asks the recorder for a transaction of the class's own
// trace type (:sv:func:`trl_uvm_recorder::open_typed`) and sets every field
// directly. Any other object goes through UVM's generic ``record_field`` calls,
// which become fields of the stream's ad-hoc type, named ``<object>.<field>`` for
// nested objects.
//
// The recorder opens its trace transaction late, when it knows the type: at
// ``open_typed``, or at close for generic records. Values recorded before a typed
// open (``begin_tr``'s ``label`` and ``desc``) become transaction metadata.
// Parent/child links (``begin_tr`` with a parent, sequence items of a sequence) are
// kept: as the ``parent`` of the child when the parent is already open, as a
// parent-to-child link otherwise.

`include "uvm_macros.svh"

package trl_uvm_pkg;
    import uvm_pkg::*;
    import trl_pkg::*;

    typedef class trl_uvm_tr_stream;
    typedef class trl_uvm_recorder;

    // A ``uvm_tr_database`` that records into a trlog database.
    class trl_uvm_tr_database extends uvm_tr_database;
        `uvm_object_utils(trl_uvm_tr_database)

        trl_db db;      // the trlog database; ``null`` selects ``"default"``
        local trl_stream m_scopes[string];

        function new(string name = "trl_uvm_tr_database");
            super.new(name);
        endfunction

        // Create a database and make it the default of every component.
        //
        // :param d: trlog database; ``null`` selects ``"default"`` (``+trlog_file=``)
        // :returns: the new database
        static function trl_uvm_tr_database install(trl_db d = null);
            uvm_coreservice_t cs = uvm_coreservice_t::get();
            trl_uvm_tr_database t = new;
            t.db = d;
            cs.set_default_tr_database(t);
            return t;
        endfunction

        // The stream for a component scope (``a.b.c``), created with its parents.
        function trl_stream scope_stream(string scope);
            trl_stream parent;
            string leaf;
            int dot;
            if (scope == "") return null;
            if (m_scopes.exists(scope)) return m_scopes[scope];
            dot = -1;
            for (int i = scope.len() - 1; i >= 0; i--)
                if (scope[i] == ".") begin dot = i; break; end
            parent = (dot < 0) ? null : scope_stream(scope.substr(0, dot - 1));
            leaf = (dot < 0) ? scope : scope.substr(dot + 1, scope.len() - 1);
            m_scopes[scope] = trl_stream::create(leaf, parent, null, "uvm_component", db);
            return m_scopes[scope];
        endfunction

        protected virtual function bit do_open_db();
            return 1;
        endfunction

        protected virtual function bit do_close_db();
            return 1;
        endfunction

        protected virtual function uvm_tr_stream do_open_stream(string name, string scope,
                                                                string type_name);
            return trl_uvm_tr_stream::type_id::create(name);
        endfunction

        protected virtual function void do_establish_link(uvm_link_base link);
            trl_uvm_recorder lhs, rhs;
            uvm_parent_child_link pc;
            if (!$cast(lhs, link.get_lhs()) || !$cast(rhs, link.get_rhs())) return;
            if ($cast(pc, link)) rhs.set_parent(lhs);
            else lhs.add_related(rhs);
        endfunction
    endclass

    // A ``uvm_tr_stream`` backed by a :sv:class:`trl_stream`.
    class trl_uvm_tr_stream extends uvm_tr_stream;
        `uvm_object_utils(trl_uvm_tr_stream)

        trl_stream trl;     // the trlog stream; disabled (``en == 0``) when recording is off

        function new(string name = "trl_uvm_tr_stream");
            super.new(name);
        endfunction

        protected virtual function void do_open(uvm_tr_database db, string scope,
                                                string stream_type_name);
            trl_uvm_tr_database d;
            trl_db tdb;
            trl_stream parent;
            if ($cast(d, db)) begin
                tdb = d.db;
                parent = d.scope_stream(scope);
            end
            trl = trl_stream::create(get_name(), parent, null, stream_type_name, tdb);
        endfunction

        protected virtual function uvm_recorder do_open_recorder(string name, time open_time,
                                                                 string type_name);
            return trl_uvm_recorder::type_id::create(name);
        endfunction
    endclass

    // A ``uvm_recorder`` that writes one trlog transaction per recorder.
    class trl_uvm_recorder extends uvm_recorder;
        `uvm_object_utils(trl_uvm_recorder)

        local trl_stream       m_s;
        local trl_txn          m_x;          // the open transaction; null until opened
        local bit              m_typed;      // m_x was opened by open_typed
        local longint unsigned m_start;
        local longint unsigned m_id;
        local trl_uvm_recorder m_parent;
        local trl_uvm_recorder m_related[$];
        local longint unsigned m_children[$];  // opened before this one was
        local int              m_depth;      // record_object nesting
        local string           m_scope[$];   // names of the nested objects
        // Generic values recorded before the transaction opened.
        local string           m_pn[$];
        local trl_field_type_e m_pk[$];
        local longint unsigned m_pi[$];
        local real             m_pr[$];
        local string           m_ps[$];

        function new(string name = "trl_uvm_recorder");
            super.new(name);
        endfunction

        // Open this recorder's transaction with type ``t``, for a woven ``do_record``.
        //
        // :returns: the transaction to set fields on; a no-op transaction when
        //     recording is off; ``null`` when the recorder cannot take a typed record
        //     (already open, or inside a nested object), in which case the caller
        //     records generically
        function trl_txn open_typed(trl_type t);
            if (m_x != null || m_depth > 1) return null;
            if (m_s == null || !m_s.en || t == null) return trl_txn::null_txn();
            m_open(t);
            m_typed = 1;
            foreach (m_pn[i]) m_meta(m_pn[i], m_str(m_pk[i], m_pi[i], m_pr[i], m_ps[i]));
            m_pn.delete(); m_pk.delete(); m_pi.delete(); m_pr.delete(); m_ps.delete();
            return m_x;
        endfunction

        // :returns: the trace transaction ID; ``0`` until the transaction opens
        function longint unsigned trl_id();
            return m_id;
        endfunction

        function void set_parent(trl_uvm_recorder p);
            m_parent = p;
        endfunction

        function void add_related(trl_uvm_recorder r);
            m_related.push_back(r);
        endfunction

        local function void m_open(trl_type t);
            longint unsigned pid;
            pid = (m_parent != null) ? m_parent.m_id : 0;
            m_x = m_s.open_txn(t, pid, m_start);
            m_id = m_x.id();
            if (m_parent != null && pid == 0 && m_id != 0) m_parent.m_children.push_back(m_id);
            foreach (m_children[i]) m_x.link(m_children[i], TRL_LINK_PARENT_CHILD);
        endfunction

        local function void m_meta(string k, string v);
            trl_db d = trl_db::get();
            trl_uvm_tr_stream s;
            trl_uvm_tr_database db;
            if ($cast(s, get_stream()) && $cast(db, s.get_db()) && db.db != null) d = db.db;
            if (d != null) m_x.meta(d.intern(k), d.intern(v));
        endfunction

        local static function string m_str(trl_field_type_e k, longint unsigned iv, real rv,
                                           string sv);
            case (k)
                TRL_FT_STRING: return sv;
                TRL_FT_F64:    return $sformatf("%g", rv);
                TRL_FT_I8, TRL_FT_I16, TRL_FT_I32, TRL_FT_I64:
                    return $sformatf("%0d", longint'(iv));
                default:       return $sformatf("%0d", iv);
            endcase
        endfunction

        local function string m_name(string name);
            string s = "";
            foreach (m_scope[i]) s = {s, m_scope[i], "."};
            return {s, name};
        endfunction

        local function void m_put(string name, trl_field_type_e k, longint unsigned iv,
                                  real rv, string sv);
            if (m_s == null || !m_s.en) return;
            name = m_name(name);
            if (m_x == null) begin
                m_pn.push_back(name);
                m_pk.push_back(k);
                m_pi.push_back(iv);
                m_pr.push_back(rv);
                m_ps.push_back(sv);
                return;
            end
            if (m_typed) m_meta(name, m_str(k, iv, rv, sv));
        endfunction

        local static function trl_field_type_e m_int_kind(int size, bit signed_);
            if (size == 1 && !signed_) return TRL_FT_BOOL;
            if (size <= 8)  return signed_ ? TRL_FT_I8  : TRL_FT_U8;
            if (size <= 16) return signed_ ? TRL_FT_I16 : TRL_FT_U16;
            if (size <= 32) return signed_ ? TRL_FT_I32 : TRL_FT_U32;
            return signed_ ? TRL_FT_I64 : TRL_FT_U64;
        endfunction

        local function void m_put_int(string name, uvm_integral_t value, int size,
                                      uvm_radix_enum radix);
            longint unsigned v = 64'(value);
            bit sgn = (radix == UVM_DEC);
            if (size <= 0 || size > 64) size = 64;
            if (sgn && size < 64 && v[size - 1]) v = v | ~((64'd1 << size) - 1);
            else if (size < 64) v = v & ((64'd1 << size) - 1);
            m_put(name, m_int_kind(size, sgn), v, 0.0, "");
        endfunction

        protected virtual function void do_open(uvm_tr_stream stream, time open_time,
                                                string type_name);
            trl_uvm_tr_stream s;
            m_s = $cast(s, stream) ? s.trl : null;
            m_start = open_time;
            m_x = null;
            m_typed = 0;
            m_id = 0;
            m_parent = null;
            m_related.delete();
            m_children.delete();
            m_depth = 0;
            m_scope.delete();
            m_pn.delete(); m_pk.delete(); m_pi.delete(); m_pr.delete(); m_ps.delete();
        endfunction

        protected virtual function void do_close(time close_time);
            if (m_s == null || !m_s.en) return;
            if (m_x == null) begin
                // Generic record: the pending values are the fields. Declare them
                // first: the transaction takes the ad-hoc type's schema as of its open.
                int f[$];
                foreach (m_pn[i]) f.push_back(m_s.field(m_pn[i], m_pk[i]));
                m_open(m_s.adhoc_type());
                foreach (m_pn[i]) begin
                    case (m_pk[i])
                        TRL_FT_STRING: m_x.set_str(f[i], m_ps[i]);
                        TRL_FT_F64:    m_x.set_real(f[i], m_pr[i]);
                        default:       m_x.set_int(f[i], m_pi[i]);
                    endcase
                end
                m_pn.delete(); m_pk.delete(); m_pi.delete(); m_pr.delete(); m_ps.delete();
            end
            foreach (m_related[i])
                if (m_related[i].m_id != 0) m_x.link(m_related[i].m_id, TRL_LINK_RELATED);
            m_x.close(close_time);
        endfunction

        protected virtual function void do_free();
            m_x = null;
            m_parent = null;
            m_related.delete();
            m_children.delete();
        endfunction

        protected virtual function void do_record_field(string name, uvm_bitstream_t value,
                                                        int size, uvm_radix_enum radix);
            if (size <= 64) m_put_int(name, value[63:0], size, radix);
            else m_put(name, TRL_FT_STRING, 0, 0.0, $sformatf("'h%0h", value));
        endfunction

        protected virtual function void do_record_field_int(string name, uvm_integral_t value,
                                                            int size, uvm_radix_enum radix);
            m_put_int(name, value, size, radix);
        endfunction

        protected virtual function void do_record_field_real(string name, real value);
            m_put(name, TRL_FT_F64, 0, value, "");
        endfunction

        protected virtual function void do_record_object(string name, uvm_object value);
            if (value == null || get_recursion_policy() == UVM_REFERENCE) begin
                if (m_depth > 0)
                    m_put(name, TRL_FT_STRING, 0, 0.0, (value == null) ? "<null>" : value.get_name());
                return;
            end
            if (m_depth > 0) m_scope.push_back(name);
            m_depth++;
            super.do_record_object(name, value);
            m_depth--;
            if (m_depth > 0) void'(m_scope.pop_back());
        endfunction

        protected virtual function void do_record_string(string name, string value);
            m_put(name, TRL_FT_STRING, 0, 0.0, value);
        endfunction

        protected virtual function void do_record_time(string name, time value);
            m_put(name, TRL_FT_TIME, value, 0.0, "");
        endfunction

        protected virtual function void do_record_generic(string name, string value,
                                                          string type_name);
            m_put(name, TRL_FT_STRING, 0, 0.0, value);
        endfunction
    endclass

endpackage : trl_uvm_pkg
