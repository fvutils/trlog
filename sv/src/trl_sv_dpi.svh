// trl_sv_dpi.svh — L1: raw DPI-C imports onto c/src/trl_sv.c.
// Included by trl_pkg.sv; not a standalone file (the package is useless
// without these imports, so they live in it rather than a separate package).
//
// Everything here is a 1:1, non-context import. Handles are chandle;
// status values are int (0 = TRL_OK in trl_status_t). The header comment
// of c/src/trl_sv.c lists what this phase trims from the full §6.1
// surface in sv_api_design.md (no enum types, no nested-struct/array fields, no wide
// vector fields, no cursor seek).

// ---- database ----

import "DPI-C" function chandle trl_sv_db_open(string name, string path,
                    int timescale_exp, int compress, int block_kb, int flags);
import "DPI-C" function chandle trl_sv_db_find(string name);
import "DPI-C" function int     trl_sv_db_close(chandle db);
import "DPI-C" function int     trl_sv_intern(chandle db, string s);
import "DPI-C" function string  trl_sv_last_error();

// ---- types (write side) ----

import "DPI-C" function chandle trl_sv_type_new(chandle db, string name, chandle scope);
import "DPI-C" function chandle trl_sv_type_find(chandle db, string name, chandle scope);
import "DPI-C" function int     trl_sv_type_add_field(chandle t, string name, int kind, int width);
import "DPI-C" function int     trl_sv_type_field_get_or_add(chandle t, string name, int kind, int width);
import "DPI-C" function int     trl_sv_type_seal(chandle t);
import "DPI-C" function string  trl_sv_type_name(chandle t);
import "DPI-C" function int     trl_sv_type_num_fields(chandle t);
import "DPI-C" function int     trl_sv_type_field_index(chandle t, string name);
import "DPI-C" function string  trl_sv_type_field_name(chandle t, int f);
import "DPI-C" function int     trl_sv_type_field_kind(chandle t, int f);
import "DPI-C" function int     trl_sv_type_field_width(chandle t, int f);

// ---- streams ----

import "DPI-C" function chandle trl_sv_stream_new(chandle db, chandle parent, string name,
                    string kind, chandle dflt_type, output int enabled_out);
import "DPI-C" function chandle trl_sv_stream_find(chandle db, string path);
import "DPI-C" function chandle trl_sv_stream_adhoc_type(chandle s);
import "DPI-C" function int     trl_sv_stream_inst_id(chandle s);

// ---- recording ----

import "DPI-C" function longint unsigned trl_sv_record(chandle s, chandle t,
                    longint unsigned start, longint unsigned end_t, longint unsigned parent,
                    input longint unsigned v[], int n);
import "DPI-C" function chandle trl_sv_begin(chandle s, chandle t,
                    longint unsigned start, longint unsigned parent);
import "DPI-C" function longint unsigned trl_sv_txn_id(chandle x);
import "DPI-C" function void    trl_sv_set_int (chandle x, int f, longint unsigned v);
import "DPI-C" function void    trl_sv_set_real(chandle x, int f, real v);
import "DPI-C" function void    trl_sv_set_str (chandle x, int f, string v);
import "DPI-C" function void    trl_sv_set_str_id(chandle x, int f, int sid);
import "DPI-C" function void    trl_sv_link (chandle x, longint unsigned tgt, int ltype, int label_sid);
import "DPI-C" function void    trl_sv_link_id(chandle db, longint unsigned src,
                    longint unsigned tgt, int ltype, int label_sid);
import "DPI-C" function void    trl_sv_meta(chandle x, int key_sid, int val_sid);
import "DPI-C" function void    trl_sv_end (chandle x, longint unsigned end_t);

// ---- reading ----

import "DPI-C" function chandle trl_sv_rdb_open(string name, string path);
import "DPI-C" function chandle trl_sv_rdb_find(string name);
import "DPI-C" function int     trl_sv_rdb_close(chandle r);
import "DPI-C" function int     trl_sv_rdb_num_streams(chandle r);
import "DPI-C" function chandle trl_sv_rdb_stream_at(chandle r, int i);
import "DPI-C" function chandle trl_sv_rdb_stream_find(chandle r, string path);
import "DPI-C" function string  trl_sv_rstream_path(chandle rs);
import "DPI-C" function int     trl_sv_rstream_inst_id(chandle rs);
import "DPI-C" function chandle trl_sv_rdb_schema_find(chandle r, string name);
import "DPI-C" function string  trl_sv_rschema_name(chandle sch);
import "DPI-C" function int     trl_sv_rschema_num_fields(chandle sch);
import "DPI-C" function int     trl_sv_rschema_field_index(chandle sch, string name);
import "DPI-C" function string  trl_sv_rschema_field_name(chandle sch, int f);
import "DPI-C" function int     trl_sv_rschema_field_kind(chandle sch, int f);
import "DPI-C" function string  trl_sv_string_lookup(chandle r, int sid);

// ---- cursor ----

import "DPI-C" function chandle trl_sv_cursor_new(chandle r, chandle rs /*null=all*/,
                    longint unsigned t_from, longint unsigned t_to);
import "DPI-C" function int     trl_sv_cursor_add_stream(chandle c, chandle rs);
import "DPI-C" function int     trl_sv_cursor_next(chandle c,
                    output longint unsigned txn_id, output int stream_inst_id,
                    output int txn_type_id, output longint unsigned start,
                    output longint unsigned end_t, output longint unsigned parent,
                    output int attr_count, output int link_count, output int meta_count);
import "DPI-C" function int     trl_sv_cur_has (chandle c, int f);
import "DPI-C" function longint unsigned trl_sv_cur_int(chandle c, int f);
import "DPI-C" function real    trl_sv_cur_real(chandle c, int f);
import "DPI-C" function string  trl_sv_cur_str (chandle c, int f);
import "DPI-C" function int     trl_sv_cur_link(chandle c, int i,
                    output longint unsigned tgt, output int ltype, output int label_sid);
import "DPI-C" function int     trl_sv_cur_meta_at(chandle c, int i,
                    output int key_sid, output int val_sid);
import "DPI-C" function void    trl_sv_cursor_free(chandle c);
