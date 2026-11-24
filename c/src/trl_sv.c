/* trl_sv.c — DPI-C glue for the SystemVerilog API (sv_api_design.md §6.1,
 * phase 4, L1). Every exported name is a thin, non-context wrapper around
 * trl_rec.h (write side) and trl_query.h (read side); no state lives here.
 *
 * Scope trimmed from the design doc for this phase (see sv_impl_status.md):
 *   - no enum types (trl_enum_new/trl_enum_add) and no nested-struct/array
 *     field declaration (trl_type_add_struct/add_array). trl_rec.h supports
 *     them, but the SV-side convenience for walking a nested type is L2
 *     work that didn't fit this phase; a flat field list covers the smoke
 *     test and the benchmarked tiers in §3.2.
 *   - no wide-vector fields: trl_sv_set_bits/set_logic/set_bytes and the
 *     matching cursor accessors are not exported. These need an SV open
 *     array or packed-vector argument (`svGetArrayPtr`/`svBitVecVal`) that
 *     is easy to get wrong without a simulator to test against interactively;
 *     scalar (set_int/set_real/set_str) and TR_LINK/TR_META cover this
 *     phase's smoke test.
 *   - no VPI sharing (§5.4): this build does not dlsym vpi_* or attempt to
 *     join trlog_vpi's database registry directly. `trl_sv_db_open` opens
 *     through trl_db_open's existing process-wide name registry, which is
 *     already enough for DPI and VPI to share one database: whichever side
 *     opens "default" first wins, and the other finds it by name.
 *   - `trl_sv_cursor_seek` is not exported; trl_query.h doesn't implement
 *     it either (phase 3's trim).
 */

#include "trl/trl.h"
#include "trl/trl_rec.h"
#include "trl/trl_query.h"

#include <stdlib.h>
#include <string.h>

/* DPI-C maps `chandle` to void*, `string` to char*, and status ints to
 * plain `int`; none of that needs svdpi.h, so this file has no dependency
 * on any particular simulator's DPI headers. */

/* Every trl_sv_* function is a DPI-C import, so the simulator must find it
 * in the library's export table (the library builds with hidden visibility). */
#define TRL_SV_API TRL_DLL_EXPORT

/* ----------------------------------------------------------------------- *
 * Database
 * ----------------------------------------------------------------------- */

TRL_SV_API void *trl_sv_db_open(const char *name, const char *path, int timescale_exp,
                      int compress, int block_kb, int flags) {
    (void)flags;
    trl_db_cfg_t cfg;
    memset(&cfg, 0, sizeof(cfg));
    cfg.path = path;
    cfg.timescale_exp = timescale_exp;
    cfg.compress = (compress != 0) ? TRL_COMPRESS_ZLIB : TRL_COMPRESS_NONE;
    cfg.block_bytes = (block_kb > 0) ? (uint32_t)block_kb * 1024u : 0u;
    return trl_db_open(name, &cfg);
}

TRL_SV_API void *trl_sv_db_find(const char *name) { return trl_db_find(name); }

TRL_SV_API int trl_sv_db_close(void *db) { return (int)trl_db_close((trl_db_t *)db); }

TRL_SV_API int trl_sv_intern(void *db, const char *s) {
    return (int)trl_db_intern((trl_db_t *)db, s);
}

TRL_SV_API const char *trl_sv_last_error(void) { return trl_last_error(); }

/* ----------------------------------------------------------------------- *
 * Types
 * ----------------------------------------------------------------------- */

TRL_SV_API void *trl_sv_type_new(void *db, const char *name, void *scope) {
    return trl_type_new((trl_db_t *)db, name, (trl_stream_t *)scope);
}

TRL_SV_API void *trl_sv_type_find(void *db, const char *name, void *scope) {
    return trl_type_find((trl_db_t *)db, name, (trl_stream_t *)scope);
}

TRL_SV_API int trl_sv_type_add_field(void *t, const char *name, int kind, int width) {
    return trl_type_add_field((trl_rtype_t *)t, name, (trl_field_type_t)kind,
                               (uint32_t)width, NULL);
}

TRL_SV_API int trl_sv_type_field_get_or_add(void *t, const char *name, int kind, int width) {
    return trl_type_field_get_or_add((trl_rtype_t *)t, name, (trl_field_type_t)kind,
                                      (uint32_t)width);
}

TRL_SV_API int trl_sv_type_seal(void *t) { return (int)trl_type_seal((trl_rtype_t *)t); }

TRL_SV_API const char *trl_sv_type_name(void *t) { return trl_type_name((const trl_rtype_t *)t); }

TRL_SV_API int trl_sv_type_num_fields(void *t) { return (int)trl_type_num_fields((const trl_rtype_t *)t); }

TRL_SV_API int trl_sv_type_field_index(void *t, const char *name) {
    return trl_type_field_index((const trl_rtype_t *)t, name);
}

TRL_SV_API const char *trl_sv_type_field_name(void *t, int f) {
    return trl_type_field_name((const trl_rtype_t *)t, (uint32_t)f);
}

TRL_SV_API int trl_sv_type_field_kind(void *t, int f) {
    return (int)trl_type_field_type((const trl_rtype_t *)t, (uint32_t)f);
}

TRL_SV_API int trl_sv_type_field_width(void *t, int f) {
    return (int)trl_type_field_width((const trl_rtype_t *)t, (uint32_t)f);
}

/* ----------------------------------------------------------------------- *
 * Streams
 * ----------------------------------------------------------------------- */

TRL_SV_API void *trl_sv_stream_new(void *db, void *parent, const char *name, const char *kind,
                         void *dflt_type, int *enabled_out) {
    int enabled = 1;
    trl_stream_t *s = trl_stream_new((trl_db_t *)db, (trl_stream_t *)parent, name, kind,
                                      (trl_rtype_t *)dflt_type, &enabled);
    if (enabled_out) *enabled_out = enabled;
    return s;
}

TRL_SV_API void *trl_sv_stream_find(void *db, const char *path) {
    return trl_stream_find((trl_db_t *)db, path);
}

TRL_SV_API void *trl_sv_stream_adhoc_type(void *s) { return trl_stream_adhoc_type((trl_stream_t *)s); }

TRL_SV_API int trl_sv_stream_inst_id(void *s) { return (int)trl_stream_inst_id((const trl_stream_t *)s); }

/* ----------------------------------------------------------------------- *
 * Recording
 * ----------------------------------------------------------------------- */

TRL_SV_API long long trl_sv_record(void *s, void *t, long long start, long long end, long long parent,
                         const long long *v, int n) {
    return (long long)trl_rec_full((trl_stream_t *)s, (trl_rtype_t *)t,
                                    (uint64_t)start, (uint64_t)end, (uint64_t)parent,
                                    (const uint64_t *)v, (uint32_t)n);
}

TRL_SV_API void *trl_sv_begin(void *s, void *t, long long start, long long parent) {
    return trl_rec_begin((trl_stream_t *)s, (trl_rtype_t *)t, (uint64_t)start, (uint64_t)parent);
}

TRL_SV_API long long trl_sv_txn_id(void *x) { return (long long)trl_rec_id((const trl_txn_t *)x); }

TRL_SV_API void trl_sv_set_int(void *x, int f, long long v) {
    trl_rec_set_u64((trl_txn_t *)x, (uint32_t)f, (uint64_t)v);
}

TRL_SV_API void trl_sv_set_real(void *x, int f, double v) {
    trl_rec_set_f64((trl_txn_t *)x, (uint32_t)f, v);
}

TRL_SV_API void trl_sv_set_str(void *x, int f, const char *v) {
    trl_rec_set_str((trl_txn_t *)x, (uint32_t)f, v);
}

TRL_SV_API void trl_sv_set_str_id(void *x, int f, int sid) {
    trl_rec_set_str_id((trl_txn_t *)x, (uint32_t)f, (uint32_t)sid);
}

TRL_SV_API void trl_sv_link(void *x, long long tgt, int ltype, int label_sid) {
    trl_rec_link((trl_txn_t *)x, (uint64_t)tgt, (uint8_t)ltype, (uint32_t)label_sid);
}

TRL_SV_API void trl_sv_link_id(void *db, long long src, long long tgt, int ltype, int label_sid) {
    trl_rec_link_ids((trl_db_t *)db, (uint64_t)src, (uint64_t)tgt, (uint8_t)ltype, (uint32_t)label_sid);
}

TRL_SV_API void trl_sv_meta(void *x, int key_sid, int val_sid) {
    trl_rec_meta((trl_txn_t *)x, (uint32_t)key_sid, (uint32_t)val_sid);
}

TRL_SV_API void trl_sv_end(void *x, long long end) {
    trl_rec_end((trl_txn_t *)x, (uint64_t)end);
}

/* ----------------------------------------------------------------------- *
 * Reading
 * ----------------------------------------------------------------------- */

TRL_SV_API void *trl_sv_rdb_open(const char *name, const char *path) { return trl_rdb_open(name, path); }
TRL_SV_API void *trl_sv_rdb_find(const char *name) { return trl_rdb_find(name); }
TRL_SV_API int trl_sv_rdb_close(void *r) { return (int)trl_rdb_close((trl_rdb_t *)r); }

TRL_SV_API int trl_sv_rdb_num_streams(void *r) { return (int)trl_rdb_num_streams((trl_rdb_t *)r); }

TRL_SV_API void *trl_sv_rdb_stream_at(void *r, int i) {
    return trl_rdb_stream_at((trl_rdb_t *)r, (uint32_t)i);
}

TRL_SV_API void *trl_sv_rdb_stream_find(void *r, const char *path) {
    return trl_rdb_stream_find((trl_rdb_t *)r, path);
}

TRL_SV_API const char *trl_sv_rstream_path(void *rs) { return trl_rstream_path((const trl_rstream_t *)rs); }

TRL_SV_API int trl_sv_rstream_inst_id(void *rs) {
    return (int)trl_rstream_inst_id((const trl_rstream_t *)rs);
}

TRL_SV_API void *trl_sv_rdb_schema_find(void *r, const char *name) {
    return (void *)trl_rdb_schema_find((trl_rdb_t *)r, name);
}

TRL_SV_API const char *trl_sv_rschema_name(void *sch) { return trl_rschema_name((const trl_rschema_t *)sch); }

TRL_SV_API int trl_sv_rschema_num_fields(void *sch) {
    return (int)trl_rschema_num_fields((const trl_rschema_t *)sch);
}

TRL_SV_API int trl_sv_rschema_field_index(void *sch, const char *name) {
    return trl_rschema_field_index((const trl_rschema_t *)sch, name);
}

TRL_SV_API const char *trl_sv_rschema_field_name(void *sch, int f) {
    return trl_rschema_field_name((const trl_rschema_t *)sch, (uint32_t)f);
}

TRL_SV_API int trl_sv_rschema_field_kind(void *sch, int f) {
    return (int)trl_rschema_field_type((const trl_rschema_t *)sch, (uint32_t)f);
}

TRL_SV_API const char *trl_sv_string_lookup(void *r, int sid) {
    return trl_rdb_string_lookup((trl_rdb_t *)r, (uint32_t)sid);
}

/* ----------------------------------------------------------------------- *
 * Cursor
 * ----------------------------------------------------------------------- *
 * trl_sv_cursor_next caches the last joined txn_hdr_t it returned in the
 * cursor-owned handle so the per-field accessors below don't need a second
 * DPI argument to carry it; this mirrors the design's "2 DPI calls per
 * transaction" read budget (next + one accessor call per field actually
 * read), at the cost of accessors only being valid until the next
 * trl_sv_cursor_next call on the same cursor (same lifetime trl_query.h
 * gives the C-side trl_txn_hdr_t itself).
 */

struct trl_sv_cursor_state {
    trl_cursor_t *c;
    trl_txn_hdr_t hdr;
    int have_hdr;
};

TRL_SV_API void *trl_sv_cursor_new(void *r, void *rs, long long t_from, long long t_to) {
    struct trl_sv_cursor_state *st =
        (struct trl_sv_cursor_state *)calloc(1, sizeof(*st));
    if (!st) return NULL;
    st->c = trl_cursor_new((trl_rdb_t *)r, (trl_rstream_t *)rs, (uint64_t)t_from, (uint64_t)t_to);
    if (!st->c) { free(st); return NULL; }
    return st;
}

TRL_SV_API int trl_sv_cursor_add_stream(void *c, void *rs) {
    struct trl_sv_cursor_state *st = (struct trl_sv_cursor_state *)c;
    return (int)trl_cursor_add_stream(st->c, (trl_rstream_t *)rs);
}

TRL_SV_API int trl_sv_cursor_next(void *c, long long *txn_id, int *stream_inst_id, int *txn_type_id,
                        long long *start, long long *end, long long *parent,
                        int *attr_count, int *link_count, int *meta_count) {
    struct trl_sv_cursor_state *st = (struct trl_sv_cursor_state *)c;
    st->have_hdr = trl_cursor_next(st->c, &st->hdr);
    if (!st->have_hdr) return 0;
    *txn_id = (long long)st->hdr.txn_id;
    *stream_inst_id = (int)st->hdr.stream_inst_id;
    *txn_type_id = (int)st->hdr.txn_type_id;
    *start = (long long)st->hdr.start;
    *end = (long long)st->hdr.end;
    *parent = (long long)st->hdr.parent;
    *attr_count = (int)st->hdr.attr_count;
    *link_count = (int)st->hdr.link_count;
    *meta_count = (int)st->hdr.meta_count;
    return 1;
}

static const trl_attr_t *cur_find_attr(struct trl_sv_cursor_state *st, int f) {
    if (!st->have_hdr) return NULL;
    for (uint32_t i = 0; i < st->hdr.attr_count; ++i) {
        if (st->hdr.attrs[i].field_idx == (uint32_t)f) return &st->hdr.attrs[i];
    }
    return NULL;
}

TRL_SV_API int trl_sv_cur_has(void *c, int f) {
    return cur_find_attr((struct trl_sv_cursor_state *)c, f) != NULL;
}

TRL_SV_API long long trl_sv_cur_int(void *c, int f) {
    const trl_attr_t *a = cur_find_attr((struct trl_sv_cursor_state *)c, f);
    if (!a) return 0;
    return (a->field_type == TRL_FT_I8 || a->field_type == TRL_FT_I16 ||
            a->field_type == TRL_FT_I32 || a->field_type == TRL_FT_I64)
               ? (long long)a->value.i64
               : (long long)a->value.u64;
}

TRL_SV_API double trl_sv_cur_real(void *c, int f) {
    const trl_attr_t *a = cur_find_attr((struct trl_sv_cursor_state *)c, f);
    return a ? a->value.f64 : 0.0;
}

TRL_SV_API const char *trl_sv_cur_str(void *c, int f) {
    const trl_attr_t *a = cur_find_attr((struct trl_sv_cursor_state *)c, f);
    return (a && a->field_type == TRL_FT_STRING) ? a->value.str : "";
}

TRL_SV_API int trl_sv_cur_link(void *c, int i, long long *tgt, int *ltype, int *label_sid) {
    struct trl_sv_cursor_state *st = (struct trl_sv_cursor_state *)c;
    if (!st->have_hdr || i < 0 || (uint32_t)i >= st->hdr.link_count) return 0;
    const trl_txn_link_t *l = &st->hdr.links[i];
    *tgt = (long long)l->target;
    *ltype = (int)l->link_type;
    *label_sid = (int)l->label_sid;
    return 1;
}

TRL_SV_API int trl_sv_cur_meta_at(void *c, int i, int *key_sid, int *val_sid) {
    struct trl_sv_cursor_state *st = (struct trl_sv_cursor_state *)c;
    if (!st->have_hdr || i < 0 || (uint32_t)i >= st->hdr.meta_count) return 0;
    const trl_txn_meta_t *m = &st->hdr.metas[i];
    *key_sid = (int)m->key_sid;
    *val_sid = (int)m->val_sid;
    return 1;
}

TRL_SV_API void trl_sv_cursor_free(void *c) {
    struct trl_sv_cursor_state *st = (struct trl_sv_cursor_state *)c;
    if (!st) return;
    trl_cursor_free(st->c);
    free(st);
}
