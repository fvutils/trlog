/*
 * rec.c — the L0 recording session (trl/trl_rec.h, sv_api_design.md §7.1).
 *
 * Layered on top of the block-level writer in trl.h/trl_internal.h. Each
 * stream owns its own trl_txn_block_t, flushed as its own single-stream
 * block (phase 1's per-stream index, trl_txn_begin_block_stream) once it
 * reaches the configured byte threshold or the db closes — there is no
 * shared "active block" to contend over, so streams interleave freely.
 *
 * See the scope note at the top of trl_rec.h for what phase 2 trims from
 * the design (enum labels, scope-nested hierarchy placement, the §7.2
 * stream-set index section, nested-type introspection).
 */

#include <fnmatch.h>
#include <stdarg.h>

#include "trl/trl_rec.h"
#include "trl_internal.h"

#define TRL_DEFAULT_BLOCK_BYTES (64u * 1024u)
#define TRL_HK_BEHAVIORAL 0x01

typedef struct trl_field_slot {
    char *name;
    trl_field_type_t type;
    uint32_t width;
    trl_renum_t *renum;
} trl_field_slot_t;

struct trl_rtype {
    trl_db_t *db;
    char *name;
    bool sealed;
    bool adhoc;
    uint32_t schema_id;
    trl_field_slot_t *fields;
    size_t count, cap;
    trl_rtype_t *next;
};

typedef struct trl_enum_item {
    char *label;
    int64_t value;
} trl_enum_item_t;

struct trl_renum {
    trl_db_t *db;
    char *name;
    trl_enum_item_t *items;
    size_t count, cap;
    trl_renum_t *next;
};

struct trl_stream {
    trl_db_t *db;
    trl_stream_t *parent;
    char *name;
    char *path;
    char *kind;
    trl_rtype_t *dflt;
    uint32_t stream_inst_id;
    int enabled;
    bool declared;
    bool blk_active;
    trl_txn_block_t blk;
    trl_stream_t *next;
    trl_stream_t *next_pending;
};

struct trl_txn {
    trl_db_t *db;
    trl_stream_t *stream;
    trl_rtype_t *rtype;
    uint64_t txn_id;
    uint64_t start;       /* re-activation baseline if the stream's block
                            * auto-flushed between this begin and a later
                            * set/link/meta/end call on the same txn */
    trl_txn_t *next_free;
};

struct trl_db {
    char *name;
    trl_writer_t *w;
    uint32_t block_bytes;
    char *include;
    char *exclude;
    uint64_t next_txn_id;
    uint32_t next_stream_inst_id;
    uint32_t next_hier_id;
    trl_rtype_t *types;
    trl_renum_t *enums;
    trl_stream_t *streams;
    trl_stream_t *pending;
    trl_txn_block_t link_blk;
    bool link_blk_active;
    trl_txn_t *txn_free;
    trl_db_t *next;
};

static trl_db_t *g_dbs = NULL;
static char g_last_error[256] = "";

static void set_error(const char *fmt, ...) {
    va_list ap;
    va_start(ap, fmt);
    vsnprintf(g_last_error, sizeof(g_last_error), fmt, ap);
    va_end(ap);
}

const char *trl_last_error(void) {
    return g_last_error;
}

/* ----------------------------------------------------------------------- *
 * Stream-path glob filter (plusarg-style include/exclude, §5.1)
 * ----------------------------------------------------------------------- */

static int glob_list_match(const char *list, const char *path) {
    if (!list || !*list) {
        return 0;
    }
    char buf[1024];
    size_t len = strlen(list);
    if (len >= sizeof(buf)) {
        len = sizeof(buf) - 1;
    }
    memcpy(buf, list, len);
    buf[len] = '\0';
    char *tok = buf;
    while (tok && *tok) {
        char *comma = strchr(tok, ',');
        if (comma) {
            *comma = '\0';
        }
        if (*tok && fnmatch(tok, path, 0) == 0) {
            return 1;
        }
        tok = comma ? comma + 1 : NULL;
    }
    return 0;
}

static int stream_path_enabled(trl_db_t *db, const char *path) {
    int included = db->include ? glob_list_match(db->include, path) : 1;
    if (!included) {
        return 0;
    }
    if (db->exclude && glob_list_match(db->exclude, path)) {
        return 0;
    }
    return 1;
}

/* ----------------------------------------------------------------------- *
 * trl_db_t
 * ----------------------------------------------------------------------- */

trl_db_t *trl_db_open(const char *name, const trl_db_cfg_t *cfg) {
    if (!name || !cfg || !cfg->path) {
        set_error("trl_db_open: name and cfg->path are required");
        return NULL;
    }
    if (trl_db_find(name)) {
        set_error("trl_db_open: a db named '%s' is already open", name);
        return NULL;
    }
    trl_db_t *db = (trl_db_t *)calloc(1, sizeof(*db));
    if (!db) {
        set_error("trl_db_open: out of memory");
        return NULL;
    }
    db->name = trl_strdup_len(name, strlen(name));
    db->w = trl_writer_open(cfg->path, cfg->timescale_exp, cfg->compress);
    if (!db->w) {
        set_error("trl_db_open: could not open '%s' for writing", cfg->path);
        free(db->name);
        free(db);
        return NULL;
    }
    if (cfg->timezero != 0 && trl_writer_set_timezero(db->w, cfg->timezero) != TRL_OK) {
        set_error("trl_db_open: could not write timezero to '%s'", cfg->path);
        trl_writer_close(db->w);
        free(db->name);
        free(db);
        return NULL;
    }
    db->block_bytes = cfg->block_bytes ? cfg->block_bytes : TRL_DEFAULT_BLOCK_BYTES;
    db->include = cfg->stream_include ? trl_strdup_len(cfg->stream_include, strlen(cfg->stream_include)) : NULL;
    db->exclude = cfg->stream_exclude ? trl_strdup_len(cfg->stream_exclude, strlen(cfg->stream_exclude)) : NULL;
    db->next_txn_id = 1;
    db->next_stream_inst_id = 1;
    db->next_hier_id = 0x10000;
    trl_txn_block_init(&db->link_blk);
    db->next = g_dbs;
    g_dbs = db;
    return db;
}

trl_db_t *trl_db_find(const char *name) {
    for (trl_db_t *d = g_dbs; d; d = d->next) {
        if (strcmp(d->name, name) == 0) {
            return d;
        }
    }
    return NULL;
}

trl_writer_t *trl_db_writer(trl_db_t *db) {
    return db->w;
}

uint32_t trl_db_intern(trl_db_t *db, const char *s) {
    return trl_intern(db->w, s);
}

static trl_status_t flush_pending_streams(trl_db_t *db) {
    if (!db->pending) {
        return TRL_OK;
    }
    uint32_t hier_id = db->next_hier_id++;
    trl_status_t st = trl_begin_hierarchy(db->w, hier_id, TRL_HK_BEHAVIORAL, trl_intern(db->w, "streams"));
    for (trl_stream_t *s = db->pending; st == TRL_OK && s; s = s->next_pending) {
        st = trl_hierarchy_add_stream_explicit(&db->w->hierarchy, s->stream_inst_id, 0,
                                               trl_intern(db->w, s->path));
    }
    if (st == TRL_OK) {
        st = trl_flush_hierarchy(db->w);
    }
    for (trl_stream_t *s = db->pending; s; ) {
        trl_stream_t *n = s->next_pending;
        s->declared = true;
        s->next_pending = NULL;
        s = n;
    }
    db->pending = NULL;
    return st;
}

static trl_status_t flush_stream_block(trl_stream_t *s) {
    if (!s->blk_active || s->blk.count == 0) {
        return TRL_OK;
    }
    trl_buf_t block;
    trl_buf_init(&block);
    trl_status_t st = trl_txn_block_encode(&s->blk, s->db->w->compress, &block);
    if (st == TRL_OK) {
        uint64_t off = (uint64_t)ftell(s->db->w->fp);
        st = trl_index_add_txn(&s->db->w->index, s->blk.start_time, s->blk.end_time, off, s->stream_inst_id);
        if (st == TRL_OK && fwrite(block.data, 1, block.size, s->db->w->fp) != block.size) {
            st = TRL_ERR_IO;
        }
    }
    trl_buf_free(&block);
    s->blk_active = false;
    return st;
}

static trl_status_t flush_link_blk(trl_db_t *db) {
    if (!db->link_blk_active || db->link_blk.count == 0) {
        return TRL_OK;
    }
    trl_buf_t block;
    trl_buf_init(&block);
    trl_status_t st = trl_txn_block_encode(&db->link_blk, db->w->compress, &block);
    if (st == TRL_OK) {
        uint64_t off = (uint64_t)ftell(db->w->fp);
        st = trl_index_add_txn(&db->w->index, db->link_blk.start_time, db->link_blk.end_time, off, TRL_STREAM_MIXED);
        if (st == TRL_OK && fwrite(block.data, 1, block.size, db->w->fp) != block.size) {
            st = TRL_ERR_IO;
        }
    }
    trl_buf_free(&block);
    db->link_blk_active = false;
    return st;
}

static void ensure_stream_active(trl_stream_t *s, uint64_t start_time) {
    if (!s->declared) {
        flush_pending_streams(s->db);
    }
    if (!s->blk_active) {
        trl_txn_block_begin(&s->blk, start_time, s->stream_inst_id);
        s->blk_active = true;
    }
}

static void auto_flush_stream(trl_stream_t *s) {
    if (s->blk_active && s->blk.recs.size >= s->db->block_bytes) {
        flush_stream_block(s);
    }
}

static void ensure_link_blk_active(trl_db_t *db) {
    if (!db->link_blk_active) {
        trl_txn_block_begin(&db->link_blk, 0, TRL_STREAM_MIXED);
        db->link_blk_active = true;
    }
}

static void auto_flush_link_blk(trl_db_t *db) {
    if (db->link_blk_active && db->link_blk.recs.size >= db->block_bytes) {
        flush_link_blk(db);
    }
}

trl_status_t trl_db_close(trl_db_t *db) {
    if (!db) {
        return TRL_OK;
    }
    trl_status_t st = TRL_OK;
    for (trl_stream_t *s = db->streams; s; s = s->next) {
        trl_status_t fst = flush_stream_block(s);
        if (fst != TRL_OK) st = fst;
    }
    {
        trl_status_t fst = flush_link_blk(db);
        if (fst != TRL_OK) st = fst;
    }
    {
        trl_status_t fst = flush_pending_streams(db);
        if (fst != TRL_OK) st = fst;
    }
    {
        trl_status_t fst = trl_writer_close(db->w);
        if (fst != TRL_OK) st = fst;
    }

    if (g_dbs == db) {
        g_dbs = db->next;
    } else {
        for (trl_db_t *d = g_dbs; d; d = d->next) {
            if (d->next == db) { d->next = db->next; break; }
        }
    }

    for (trl_rtype_t *t = db->types; t; ) {
        trl_rtype_t *n = t->next;
        for (size_t i = 0; i < t->count; ++i) free(t->fields[i].name);
        free(t->fields);
        free(t->name);
        free(t);
        t = n;
    }
    for (trl_renum_t *e = db->enums; e; ) {
        trl_renum_t *n = e->next;
        for (size_t i = 0; i < e->count; ++i) free(e->items[i].label);
        free(e->items);
        free(e->name);
        free(e);
        e = n;
    }
    for (trl_stream_t *s = db->streams; s; ) {
        trl_stream_t *n = s->next;
        trl_txn_block_free(&s->blk);
        free(s->name);
        free(s->path);
        free(s->kind);
        free(s);
        s = n;
    }
    for (trl_txn_t *t = db->txn_free; t; ) {
        trl_txn_t *n = t->next_free;
        free(t);
        t = n;
    }
    trl_txn_block_free(&db->link_blk);
    free(db->include);
    free(db->exclude);
    free(db->name);
    free(db);
    return st;
}

/* ----------------------------------------------------------------------- *
 * Types
 * ----------------------------------------------------------------------- */

static char *qualified_type_name(const char *name, trl_stream_t *scope) {
    if (!scope) {
        return trl_strdup_len(name, strlen(name));
    }
    size_t len = strlen(scope->path) + 2 + strlen(name);
    char *buf = (char *)malloc(len + 1);
    if (!buf) return NULL;
    snprintf(buf, len + 1, "%s::%s", scope->path, name);
    return buf;
}

static trl_status_t rtype_ensure_cap(trl_rtype_t *t, size_t need) {
    if (need <= t->cap) return TRL_OK;
    size_t new_cap = t->cap ? t->cap : 8;
    while (new_cap < need) new_cap *= 2;
    trl_field_slot_t *n = (trl_field_slot_t *)realloc(t->fields, new_cap * sizeof(*n));
    if (!n) return TRL_ERR_OOM;
    memset(n + t->cap, 0, (new_cap - t->cap) * sizeof(*n));
    t->fields = n;
    t->cap = new_cap;
    return TRL_OK;
}

trl_rtype_t *trl_type_new(trl_db_t *db, const char *name, trl_stream_t *scope) {
    trl_rtype_t *t = (trl_rtype_t *)calloc(1, sizeof(*t));
    if (!t) return NULL;
    t->db = db;
    t->name = qualified_type_name(name, scope);
    t->next = db->types;
    db->types = t;
    return t;
}

trl_rtype_t *trl_type_find(trl_db_t *db, const char *name, trl_stream_t *scope) {
    char *q = qualified_type_name(name, scope);
    trl_rtype_t *found = NULL;
    for (trl_rtype_t *t = db->types; t; t = t->next) {
        if (strcmp(t->name, q) == 0) { found = t; break; }
    }
    free(q);
    return found;
}

static trl_status_t register_schema(trl_rtype_t *t) {
    trl_field_def_t *defs = NULL;
    if (t->count) {
        defs = (trl_field_def_t *)malloc(t->count * sizeof(*defs));
        if (!defs) return TRL_ERR_OOM;
        for (size_t i = 0; i < t->count; ++i) {
            defs[i].name_str_id = trl_intern(t->db->w, t->fields[i].name);
            defs[i].field_type = t->fields[i].type;
        }
    }
    uint32_t name_id = trl_intern(t->db->w, t->name);
    uint32_t id = trl_add_txn_schema(t->db->w, name_id, (uint32_t)t->count, defs);
    free(defs);
    if (!id) return TRL_ERR_OOM;
    t->schema_id = id;
    t->sealed = true;
    return TRL_OK;
}

int32_t trl_type_add_field(trl_rtype_t *t, const char *name, trl_field_type_t ft, uint32_t width, trl_renum_t *renum) {
    if (t->sealed && !t->adhoc) {
        set_error("trl_type_add_field: '%s' is sealed", t->name);
        return -1;
    }
    if (rtype_ensure_cap(t, t->count + 1) != TRL_OK) return -1;
    size_t idx = t->count++;
    t->fields[idx].name = trl_strdup_len(name, strlen(name));
    t->fields[idx].type = ft;
    t->fields[idx].width = width;
    t->fields[idx].renum = renum;
    return (int32_t)idx;
}

int32_t trl_type_add_struct(trl_rtype_t *t, const char *name, trl_rtype_t *sub) {
    if (!sub || !sub->sealed) {
        set_error("trl_type_add_struct: sub-type must be sealed first");
        return -1;
    }
    if (t->sealed && !t->adhoc) {
        set_error("trl_type_add_struct: '%s' is sealed", t->name);
        return -1;
    }
    if (rtype_ensure_cap(t, t->count + sub->count) != TRL_OK) return -1;
    int32_t base = (int32_t)t->count;
    for (size_t i = 0; i < sub->count; ++i) {
        size_t idx = t->count++;
        size_t len = strlen(name) + 1 + strlen(sub->fields[i].name);
        char *qn = (char *)malloc(len + 1);
        snprintf(qn, len + 1, "%s.%s", name, sub->fields[i].name);
        t->fields[idx].name = qn;
        t->fields[idx].type = sub->fields[i].type;
        t->fields[idx].width = sub->fields[i].width;
        t->fields[idx].renum = sub->fields[i].renum;
    }
    return base;
}

int32_t trl_type_add_array(trl_rtype_t *t, const char *name, trl_field_type_t ft, uint32_t width, uint32_t n) {
    if (t->sealed && !t->adhoc) {
        set_error("trl_type_add_array: '%s' is sealed", t->name);
        return -1;
    }
    if (rtype_ensure_cap(t, t->count + n) != TRL_OK) return -1;
    int32_t base = (int32_t)t->count;
    for (uint32_t i = 0; i < n; ++i) {
        size_t idx = t->count++;
        size_t len = strlen(name) + 12;
        char *qn = (char *)malloc(len);
        snprintf(qn, len, "%s[%u]", name, i);
        t->fields[idx].name = qn;
        t->fields[idx].type = ft;
        t->fields[idx].width = width;
        t->fields[idx].renum = NULL;
    }
    return base;
}

int32_t trl_type_field_get_or_add(trl_rtype_t *t, const char *name, trl_field_type_t ft, uint32_t width) {
    t->adhoc = true;
    for (size_t i = 0; i < t->count; ++i) {
        if (strcmp(t->fields[i].name, name) == 0) {
            if (t->fields[i].type != ft || t->fields[i].width != width) {
                set_error("trl_type_field_get_or_add: '%s' re-declared with a different type", name);
                return -1;
            }
            return (int32_t)i;
        }
    }
    if (rtype_ensure_cap(t, t->count + 1) != TRL_OK) return -1;
    size_t idx = t->count++;
    t->fields[idx].name = trl_strdup_len(name, strlen(name));
    t->fields[idx].type = ft;
    t->fields[idx].width = width;
    t->fields[idx].renum = NULL;
    if (register_schema(t) != TRL_OK) return -1;
    return (int32_t)idx;
}

trl_status_t trl_type_seal(trl_rtype_t *t) {
    if (t->sealed && !t->adhoc) return TRL_OK;
    return register_schema(t);
}

const char *trl_type_name(const trl_rtype_t *t) { return t->name; }
uint32_t trl_type_num_fields(const trl_rtype_t *t) { return (uint32_t)t->count; }

int32_t trl_type_field_index(const trl_rtype_t *t, const char *name) {
    for (size_t i = 0; i < t->count; ++i) {
        if (strcmp(t->fields[i].name, name) == 0) return (int32_t)i;
    }
    return -1;
}

const char *trl_type_field_name(const trl_rtype_t *t, uint32_t f) {
    return f < t->count ? t->fields[f].name : NULL;
}

trl_field_type_t trl_type_field_type(const trl_rtype_t *t, uint32_t f) {
    return f < t->count ? t->fields[f].type : TRL_FT_U8;
}

uint32_t trl_type_field_width(const trl_rtype_t *t, uint32_t f) {
    return f < t->count ? t->fields[f].width : 0;
}

/* ----------------------------------------------------------------------- *
 * Enums (in-memory only in phase 2 — see the scope note in trl_rec.h)
 * ----------------------------------------------------------------------- */

trl_renum_t *trl_enum_new(trl_db_t *db, const char *name) {
    trl_renum_t *e = (trl_renum_t *)calloc(1, sizeof(*e));
    if (!e) return NULL;
    e->db = db;
    e->name = trl_strdup_len(name, strlen(name));
    e->next = db->enums;
    db->enums = e;
    return e;
}

trl_status_t trl_enum_add(trl_renum_t *e, const char *label, int64_t value) {
    if (e->count >= e->cap) {
        size_t new_cap = e->cap ? e->cap * 2 : 8;
        trl_enum_item_t *n = (trl_enum_item_t *)realloc(e->items, new_cap * sizeof(*n));
        if (!n) return TRL_ERR_OOM;
        e->items = n;
        e->cap = new_cap;
    }
    e->items[e->count].label = trl_strdup_len(label, strlen(label));
    e->items[e->count].value = value;
    e->count++;
    return TRL_OK;
}

/* ----------------------------------------------------------------------- *
 * Streams
 * ----------------------------------------------------------------------- */

trl_stream_t *trl_stream_new(trl_db_t *db, trl_stream_t *parent, const char *name,
                             const char *kind, trl_rtype_t *dflt, int *enabled) {
    trl_stream_t *s = (trl_stream_t *)calloc(1, sizeof(*s));
    if (!s) return NULL;
    s->db = db;
    s->parent = parent;
    s->name = trl_strdup_len(name, strlen(name));
    if (parent) {
        size_t len = strlen(parent->path) + 1 + strlen(name);
        s->path = (char *)malloc(len + 1);
        snprintf(s->path, len + 1, "%s.%s", parent->path, name);
    } else {
        s->path = trl_strdup_len(name, strlen(name));
    }
    s->kind = kind ? trl_strdup_len(kind, strlen(kind)) : NULL;
    s->dflt = dflt;
    s->stream_inst_id = db->next_stream_inst_id++;
    s->enabled = stream_path_enabled(db, s->path) && (!parent || parent->enabled);
    trl_txn_block_init(&s->blk);
    s->next = db->streams;
    db->streams = s;
    s->next_pending = db->pending;
    db->pending = s;
    if (enabled) *enabled = s->enabled;
    return s;
}

trl_stream_t *trl_stream_find(trl_db_t *db, const char *path) {
    for (trl_stream_t *s = db->streams; s; s = s->next) {
        if (strcmp(s->path, path) == 0) return s;
    }
    return NULL;
}

trl_rtype_t *trl_stream_adhoc_type(trl_stream_t *s) {
    if (!s->dflt) {
        size_t len = strlen(s->path) + 8;
        char *name = (char *)malloc(len);
        snprintf(name, len, "%s#adhoc", s->path);
        trl_rtype_t *t = trl_type_new(s->db, name, NULL);
        free(name);
        t->adhoc = true;
        s->dflt = t;
    }
    return s->dflt;
}

uint32_t trl_stream_inst_id(const trl_stream_t *s) { return s->stream_inst_id; }

/* ----------------------------------------------------------------------- *
 * Recording
 * ----------------------------------------------------------------------- */

static trl_attr_t make_fixed_attr(uint32_t field_idx, trl_field_type_t ft, uint64_t raw, trl_db_t *db) {
    trl_attr_t a;
    a.field_idx = field_idx;
    a.field_type = ft;
    switch (ft) {
        case TRL_FT_I8: case TRL_FT_I16: case TRL_FT_I32: case TRL_FT_I64:
            a.value.i64 = (int64_t)raw;
            break;
        case TRL_FT_F32: {
            uint32_t bits = (uint32_t)raw;
            float f;
            memcpy(&f, &bits, sizeof(f));
            a.value.f64 = (double)f;
            break;
        }
        case TRL_FT_F64: {
            double d;
            memcpy(&d, &raw, sizeof(d));
            a.value.f64 = d;
            break;
        }
        case TRL_FT_STRING: {
            const char *s = trl_string_table_lookup(&db->w->strtab, (uint32_t)raw);
            a.value.str = s ? s : "";
            break;
        }
        default:
            a.value.u64 = raw;
            break;
    }
    return a;
}

/* Fill a BITVEC/LOGICVEC attr of width <= 64 from the raw value: the value's
 * low `width` bits, big-endian, into scratch (16 bytes). A LOGICVEC gets
 * bval = 0 (no X/Z). Returns 0 when the field cannot be carried in 64 bits
 * (BYTES, or a vector wider than 64). */
static int make_vec_attr(trl_attr_t *a, uint32_t field_idx, const trl_field_slot_t *fd, uint64_t raw,
                         uint8_t *scratch) {
    uint32_t width = fd->width;
    if (fd->type == TRL_FT_BYTES || width == 0 || width > 64) return 0;
    uint32_t nbytes = (width + 7) / 8;
    if (width < 64) raw &= (((uint64_t)1 << width) - 1);
    for (uint32_t b = 0; b < nbytes; ++b) {
        scratch[nbytes - 1 - b] = (uint8_t)(raw >> (8 * b));
    }
    memset(a, 0, sizeof(*a));
    a->field_idx = field_idx;
    a->field_type = fd->type;
    a->value.vec.data = scratch;
    a->value.vec.len = nbytes;
    if (fd->type == TRL_FT_LOGICVEC) {
        memset(scratch + nbytes, 0, nbytes);
        a->value.vec.len = 2 * nbytes;
    }
    a->value.vec.width = width;
    return 1;
}

uint64_t trl_rec_full(trl_stream_t *s, trl_rtype_t *t, uint64_t start, uint64_t end,
                      uint64_t parent, const uint64_t *vals, uint32_t nvals) {
    if (!t->sealed) {
        set_error("trl_rec_full: type '%s' is not sealed", t->name);
        return 0;
    }
    if (!s->enabled) return 0;
    ensure_stream_active(s, start);
    uint32_t n = nvals < t->count ? nvals : (uint32_t)t->count;
    trl_attr_t *attrs = n ? (trl_attr_t *)malloc(n * sizeof(*attrs)) : NULL;
    uint8_t *scratch = n ? (uint8_t *)malloc((size_t)n * 16) : NULL;
    if (n && (!attrs || !scratch)) {
        free(attrs);
        free(scratch);
        set_error("trl_rec_full: out of memory");
        return 0;
    }
    uint32_t used = 0;
    for (uint32_t i = 0; i < n; ++i) {
        trl_field_type_t ft = t->fields[i].type;
        if (ft == TRL_FT_BITVEC || ft == TRL_FT_LOGICVEC || ft == TRL_FT_BYTES) {
            if (make_vec_attr(&attrs[used], i, &t->fields[i], vals[i], scratch + 16 * i)) {
                used++;
            } else {
                set_error("trl_rec_full: field '%s' does not fit in 64 bits; "
                          "set it with trl_rec_begin/trl_rec_set_*", t->fields[i].name);
            }
        } else {
            attrs[used++] = make_fixed_attr(i, ft, vals[i], s->db);
        }
    }
    uint64_t txn_id = s->db->next_txn_id++;
    trl_txn_block_add_full(&s->blk, s->stream_inst_id, t->schema_id, txn_id, start, end, parent, used, attrs);
    free(attrs);
    free(scratch);
    auto_flush_stream(s);
    return txn_id;
}

trl_txn_t *trl_rec_begin(trl_stream_t *s, trl_rtype_t *t, uint64_t start, uint64_t parent) {
    trl_txn_t *txn;
    /* An ad-hoc type has no schema until its first field; register the empty one. */
    if (s->enabled && t && t->adhoc && !t->sealed && register_schema(t) != TRL_OK) return NULL;
    if (s->db->txn_free) {
        txn = s->db->txn_free;
        s->db->txn_free = txn->next_free;
    } else {
        txn = (trl_txn_t *)calloc(1, sizeof(*txn));
        if (!txn) return NULL;
    }
    txn->db = s->db;
    txn->stream = s;
    txn->rtype = t;
    txn->start = start;
    if (!s->enabled) {
        /* Disabled stream: hand out a handle so callers need no check, but
         * record nothing. Id 0 marks the handle for set/link/meta/end. */
        txn->txn_id = 0;
        return txn;
    }
    ensure_stream_active(s, start);
    txn->txn_id = s->db->next_txn_id++;
    trl_txn_block_add_begin(&s->blk, s->stream_inst_id, t ? t->schema_id : 0, txn->txn_id, start, parent);
    auto_flush_stream(s);
    return txn;
}

uint64_t trl_rec_id(const trl_txn_t *txn) { return txn->txn_id; }

static void set_attr(trl_txn_t *txn, uint32_t f, trl_attr_t attr) {
    if (!txn->rtype || f >= txn->rtype->count) {
        set_error("trl_rec_set_*: field %u is out of range", f);
        return;
    }
    if (!txn->txn_id) return;
    attr.field_idx = f;
    attr.field_type = txn->rtype->fields[f].type;
    ensure_stream_active(txn->stream, txn->start);
    trl_txn_block_add_attr(&txn->stream->blk, txn->txn_id, &attr);
    auto_flush_stream(txn->stream);
}

void trl_rec_set_u64(trl_txn_t *txn, uint32_t f, uint64_t v) {
    trl_attr_t a = {0}; a.value.u64 = v; set_attr(txn, f, a);
}

void trl_rec_set_i64(trl_txn_t *txn, uint32_t f, int64_t v) {
    trl_attr_t a = {0}; a.value.i64 = v; set_attr(txn, f, a);
}

void trl_rec_set_f64(trl_txn_t *txn, uint32_t f, double v) {
    trl_attr_t a = {0}; a.value.f64 = v; set_attr(txn, f, a);
}

void trl_rec_set_str(trl_txn_t *txn, uint32_t f, const char *s) {
    trl_attr_t a = {0}; a.value.str = s; set_attr(txn, f, a);
}

void trl_rec_set_str_id(trl_txn_t *txn, uint32_t f, uint32_t sid) {
    const char *s = trl_string_table_lookup(&txn->db->w->strtab, sid);
    trl_attr_t a = {0}; a.value.str = s ? s : ""; set_attr(txn, f, a);
}

void trl_rec_set_bits(trl_txn_t *txn, uint32_t f, uint32_t width, const uint8_t *be_bytes) {
    trl_attr_t a = {0};
    a.value.vec.data = be_bytes;
    a.value.vec.len = (width + 7) / 8;
    a.value.vec.width = width;
    set_attr(txn, f, a);
}

void trl_rec_set_logic(trl_txn_t *txn, uint32_t f, uint32_t width,
                       const uint8_t *aval_be, const uint8_t *bval_be) {
    size_t nbytes = (size_t)(width + 7) / 8;
    uint8_t *buf = (uint8_t *)malloc(2 * nbytes);
    if (!buf) { set_error("trl_rec_set_logic: out of memory"); return; }
    memcpy(buf, aval_be, nbytes);
    memcpy(buf + nbytes, bval_be, nbytes);
    trl_attr_t a = {0};
    a.value.vec.data = buf;
    a.value.vec.len = (uint32_t)(2 * nbytes);
    a.value.vec.width = width;
    set_attr(txn, f, a);
    free(buf);
}

void trl_rec_set_bytes(trl_txn_t *txn, uint32_t f, const uint8_t *p, uint32_t n) {
    trl_attr_t a = {0};
    a.value.vec.data = p;
    a.value.vec.len = n;
    a.value.vec.width = 0;
    set_attr(txn, f, a);
}

void trl_rec_link(trl_txn_t *txn, uint64_t tgt, uint8_t ltype, uint32_t label_sid) {
    if (!txn->txn_id) return;
    ensure_stream_active(txn->stream, txn->start);
    trl_txn_block_add_link(&txn->stream->blk, ltype, txn->txn_id, tgt, label_sid);
    auto_flush_stream(txn->stream);
}

void trl_rec_link_ids(trl_db_t *db, uint64_t src, uint64_t tgt, uint8_t ltype, uint32_t label_sid) {
    ensure_link_blk_active(db);
    trl_txn_block_add_link(&db->link_blk, ltype, src, tgt, label_sid);
    auto_flush_link_blk(db);
}

void trl_rec_meta(trl_txn_t *txn, uint32_t key_sid, uint32_t val_sid) {
    if (!txn->txn_id) return;
    ensure_stream_active(txn->stream, txn->start);
    trl_txn_block_add_meta(&txn->stream->blk, txn->txn_id, key_sid, val_sid);
    auto_flush_stream(txn->stream);
}

void trl_rec_end(trl_txn_t *txn, uint64_t end) {
    if (txn->txn_id) {
        ensure_stream_active(txn->stream, txn->start);
        trl_txn_block_add_end(&txn->stream->blk, txn->txn_id, end);
        auto_flush_stream(txn->stream);
    }
    txn->next_free = txn->db->txn_free;
    txn->db->txn_free = txn;
}
