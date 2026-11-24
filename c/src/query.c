/* query.c — the L0 reading session (trl/trl_query.h, phase 3). See the
 * header for the scope this phase trims from the design. */

#include "varint_internal.h"
#include "trl/trl_query.h"

#include <stdarg.h>

/* ----------------------------------------------------------------------- *
 * Structs
 * ----------------------------------------------------------------------- */

struct trl_rstream {
    trl_rdb_t *db;
    uint32_t stream_inst_id;
    char *path;
};

struct trl_rschema {
    trl_rdb_t *db;
    uint32_t schema_id;
    char *name;
    uint32_t field_count;
    char **field_names;
    trl_field_type_t *field_types;
};

struct trl_rdb {
    char *name;
    trl_reader_t *r;
    trl_rstream_t *streams;
    size_t stream_count;
    trl_rschema_t *schemas;      /* schemas[i] has schema_id == i+1 */
    size_t schema_count;
    trl_rdb_t *next;
};

/* A joined transaction, built up during the cursor's merge pass. */
typedef struct {
    uint64_t txn_id;
    uint32_t stream_inst_id;
    uint32_t txn_type_id;
    uint64_t start;
    uint64_t end;
    uint64_t parent;
    trl_attr_t *attrs;         size_t attr_count, attr_cap;
    trl_txn_link_t *links;     size_t link_count, link_cap;
    trl_txn_meta_t *metas;     size_t meta_count, meta_cap;
} q_txn_t;

struct trl_cursor {
    trl_rdb_t *db;
    uint64_t t_from, t_to;
    uint32_t *stream_ids;
    size_t stream_count, stream_cap;
    bool all_streams;
    bool built;
    q_txn_t *txns;
    size_t count;
    size_t pos;
};

static trl_rdb_t *g_rdbs = NULL;

/* ----------------------------------------------------------------------- *
 * trl_rdb_t: open/close, stream + schema registries
 * ----------------------------------------------------------------------- */

trl_rdb_t *trl_rdb_find(const char *name) {
    for (trl_rdb_t *db = g_rdbs; db; db = db->next) {
        if (strcmp(db->name, name) == 0) return db;
    }
    return NULL;
}

static trl_status_t read_block_at(trl_reader_t *r, uint64_t offset, uint8_t *type, uint8_t *flags,
                                  uint8_t **payload, size_t *payload_len) {
    uint8_t hdr[TRL_BLOCK_HEADER_SIZE];
    if (trl_reader_src_seek(r, offset) != 0) return TRL_ERR_IO;
    if (trl_reader_src_read(r, hdr, sizeof(hdr)) != sizeof(hdr)) return TRL_ERR_IO;
    *type = hdr[0];
    *flags = hdr[1];
    uint64_t total_len = trl_read_le64(hdr + 2);
    if (total_len < TRL_BLOCK_HEADER_SIZE) return TRL_ERR_CORRUPT;
    size_t plen = (size_t)(total_len - TRL_BLOCK_HEADER_SIZE);
    uint8_t *p = plen ? (uint8_t *)malloc(plen) : NULL;
    if (plen && !p) return TRL_ERR_OOM;
    if (plen && trl_reader_src_read(r, p, plen) != plen) { free(p); return TRL_ERR_IO; }
    *payload = p;
    *payload_len = plen;
    return TRL_OK;
}

static void rstream_free(trl_rstream_t *s) { free(s->path); }

static void rschema_free(trl_rschema_t *s) {
    for (uint32_t i = 0; i < s->field_count; ++i) free(s->field_names[i]);
    free(s->field_names);
    free(s->field_types);
    free(s->name);
}

static trl_rdb_t *rdb_from_reader(const char *name, trl_reader_t *r);

trl_rdb_t *trl_rdb_open(const char *name, const char *path) {
    if (trl_rdb_find(name)) return NULL;
    trl_reader_t *r = trl_reader_open(path);
    if (!r) return NULL;
    return rdb_from_reader(name, r);
}

trl_rdb_t *trl_rdb_open_mem(const char *name, const void *buf, size_t len) {
    if (trl_rdb_find(name)) return NULL;
    trl_reader_t *r = trl_reader_open_mem(buf, len);
    if (!r) return NULL;
    return rdb_from_reader(name, r);
}

/* Takes ownership of r (closed on failure). */
static trl_rdb_t *rdb_from_reader(const char *name, trl_reader_t *r) {
    trl_rdb_t *db = (trl_rdb_t *)calloc(1, sizeof(*db));
    if (!db) { trl_reader_close(r); return NULL; }
    db->name = trl_strdup_len(name, strlen(name));
    db->r = r;

    /* Streams: every H_STREAM entry across every hierarchy block. Phase 2's
     * writer bakes the qualified dotted path directly into name_id, so no
     * scope-tree walk is needed to recover it. */
    trl_hier_stream_entry_t *tmp = NULL;
    size_t tmp_count = 0, tmp_cap = 0;
    for (size_t i = 0; i < r->index.hier_count; ++i) {
        uint8_t type, flags;
        uint8_t *payload; size_t payload_len;
        if (read_block_at(r, r->index.hier_offsets[i], &type, &flags, &payload, &payload_len) != TRL_OK) continue;
        if (type == TRL_BLK_HIERARCHY) {
            trl_hierarchy_decode_streams(payload, payload_len, flags, &tmp, &tmp_count, &tmp_cap);
        }
        free(payload);
    }
    if (tmp_count) {
        db->streams = (trl_rstream_t *)calloc(tmp_count, sizeof(trl_rstream_t));
        if (db->streams) {
            for (size_t i = 0; i < tmp_count; ++i) {
                const char *nm = trl_string_table_lookup(&r->strtab, tmp[i].name_id);
                db->streams[db->stream_count].db = db;
                db->streams[db->stream_count].stream_inst_id = tmp[i].stream_inst_id;
                db->streams[db->stream_count].path = trl_strdup_len(nm ? nm : "", nm ? strlen(nm) : 0);
                db->stream_count++;
            }
        }
    }
    free(tmp);

    /* Schemas: the type registry already holds every schema version ever
     * registered (including superseded ad-hoc growths), ids 1..txn_count. */
    if (r->typereg.txn_count) {
        db->schemas = (trl_rschema_t *)calloc(r->typereg.txn_count, sizeof(trl_rschema_t));
        if (db->schemas) {
            for (uint32_t id = 1; id <= (uint32_t)r->typereg.txn_count; ++id) {
                const trl_txn_schema_entry_t *e = trl_type_registry_get_txn_schema(&r->typereg, id);
                trl_rschema_t *s = &db->schemas[id - 1];
                s->db = db;
                s->schema_id = id;
                if (!e) continue;
                const char *nm = trl_string_table_lookup(&r->strtab, e->name_id);
                s->name = trl_strdup_len(nm ? nm : "", nm ? strlen(nm) : 0);
                s->field_count = e->field_count;
                if (e->field_count) {
                    s->field_names = (char **)calloc(e->field_count, sizeof(char *));
                    s->field_types = (trl_field_type_t *)calloc(e->field_count, sizeof(trl_field_type_t));
                    for (uint32_t f = 0; s->field_names && s->field_types && f < e->field_count; ++f) {
                        const char *fn = trl_string_table_lookup(&r->strtab, e->fields[f].name_str_id);
                        s->field_names[f] = trl_strdup_len(fn ? fn : "", fn ? strlen(fn) : 0);
                        s->field_types[f] = e->fields[f].field_type;
                    }
                }
            }
            db->schema_count = r->typereg.txn_count;
        }
    }

    db->next = g_rdbs;
    g_rdbs = db;
    return db;
}

trl_status_t trl_rdb_close(trl_rdb_t *db) {
    if (!db) return TRL_OK;
    trl_rdb_t **pp = &g_rdbs;
    while (*pp && *pp != db) pp = &(*pp)->next;
    if (*pp == db) *pp = db->next;

    for (size_t i = 0; i < db->stream_count; ++i) rstream_free(&db->streams[i]);
    free(db->streams);
    for (size_t i = 0; i < db->schema_count; ++i) rschema_free(&db->schemas[i]);
    free(db->schemas);
    trl_reader_close(db->r);
    free(db->name);
    free(db);
    return TRL_OK;
}

uint32_t trl_rdb_num_streams(trl_rdb_t *db) { return (uint32_t)db->stream_count; }

trl_rstream_t *trl_rdb_stream_at(trl_rdb_t *db, uint32_t i) {
    return (i < db->stream_count) ? &db->streams[i] : NULL;
}

trl_rstream_t *trl_rdb_stream_find(trl_rdb_t *db, const char *path) {
    for (size_t i = 0; i < db->stream_count; ++i) {
        if (strcmp(db->streams[i].path, path) == 0) return &db->streams[i];
    }
    return NULL;
}

const char *trl_rstream_path(const trl_rstream_t *s) { return s->path; }
uint32_t trl_rstream_inst_id(const trl_rstream_t *s) { return s->stream_inst_id; }

const char *trl_rdb_string_lookup(trl_rdb_t *db, uint32_t sid) {
    return trl_string_table_lookup(&db->r->strtab, sid);
}

const trl_rschema_t *trl_rdb_schema_find(trl_rdb_t *db, const char *qualified_name) {
    const trl_rschema_t *found = NULL;
    for (size_t i = 0; i < db->schema_count; ++i) {
        if (db->schemas[i].name && strcmp(db->schemas[i].name, qualified_name) == 0) found = &db->schemas[i];
    }
    return found;
}

const trl_rschema_t *trl_rdb_schema_by_id(trl_rdb_t *db, uint32_t schema_id) {
    if (schema_id < 1 || schema_id > db->schema_count) return NULL;
    return &db->schemas[schema_id - 1];
}

const char *trl_rschema_name(const trl_rschema_t *s) { return s->name; }
uint32_t trl_rschema_num_fields(const trl_rschema_t *s) { return s->field_count; }

int32_t trl_rschema_field_index(const trl_rschema_t *s, const char *name) {
    for (uint32_t i = 0; i < s->field_count; ++i) {
        if (strcmp(s->field_names[i], name) == 0) return (int32_t)i;
    }
    return -1;
}

const char *trl_rschema_field_name(const trl_rschema_t *s, uint32_t f) {
    return (f < s->field_count) ? s->field_names[f] : NULL;
}

trl_field_type_t trl_rschema_field_type(const trl_rschema_t *s, uint32_t f) {
    return (f < s->field_count) ? s->field_types[f] : TRL_FT_U8;
}

/* ----------------------------------------------------------------------- *
 * Transaction-record decode (mirrors txn_data.c's encoder, in reverse)
 * ----------------------------------------------------------------------- */

static trl_status_t decode_attr_value(const uint8_t *data, size_t len, size_t *offset,
                                      trl_field_type_t ft, trl_attr_t *out) {
    out->field_type = ft;
    switch (ft) {
    case TRL_FT_U8:
    case TRL_FT_I8: {
        if (*offset >= len) return TRL_ERR_CORRUPT;
        uint8_t b = data[(*offset)++];
        if (ft == TRL_FT_U8) out->value.u64 = b; else out->value.i64 = (int8_t)b;
        return TRL_OK;
    }
    case TRL_FT_U16:
    case TRL_FT_I16: {
        if (*offset + 2 > len) return TRL_ERR_CORRUPT;
        uint16_t v = (uint16_t)(data[*offset] | ((uint16_t)data[*offset + 1] << 8));
        *offset += 2;
        if (ft == TRL_FT_U16) out->value.u64 = v; else out->value.i64 = (int16_t)v;
        return TRL_OK;
    }
    case TRL_FT_U32:
    case TRL_FT_I32: {
        if (*offset + 4 > len) return TRL_ERR_CORRUPT;
        uint32_t v = trl_read_le32(data + *offset);
        *offset += 4;
        if (ft == TRL_FT_U32) out->value.u64 = v; else out->value.i64 = (int32_t)v;
        return TRL_OK;
    }
    case TRL_FT_U64:
    case TRL_FT_TIME:
    case TRL_FT_I64: {
        if (*offset + 8 > len) return TRL_ERR_CORRUPT;
        uint64_t v = trl_read_le64(data + *offset);
        *offset += 8;
        if (ft == TRL_FT_I64) out->value.i64 = (int64_t)v; else out->value.u64 = v;
        return TRL_OK;
    }
    case TRL_FT_ENUM: {
        uint64_t v;
        trl_status_t st = trl_decode_uvarint(data, len, offset, &v);
        out->value.u64 = v;
        return st;
    }
    case TRL_FT_F32: {
        if (*offset + 4 > len) return TRL_ERR_CORRUPT;
        float f; memcpy(&f, data + *offset, 4);
        *offset += 4;
        out->value.f64 = (double)f;
        return TRL_OK;
    }
    case TRL_FT_F64: {
        if (*offset + 8 > len) return TRL_ERR_CORRUPT;
        double d; memcpy(&d, data + *offset, 8);
        *offset += 8;
        out->value.f64 = d;
        return TRL_OK;
    }
    case TRL_FT_BOOL: {
        if (*offset >= len) return TRL_ERR_CORRUPT;
        out->value.u64 = data[(*offset)++] ? 1 : 0;
        return TRL_OK;
    }
    case TRL_FT_STRING: {
        uint64_t slen;
        trl_status_t st = trl_decode_uvarint(data, len, offset, &slen);
        if (st != TRL_OK) return st;
        if (*offset + (size_t)slen > len) return TRL_ERR_CORRUPT;
        char *s = (char *)malloc((size_t)slen + 1);
        if (!s) return TRL_ERR_OOM;
        memcpy(s, data + *offset, (size_t)slen);
        s[slen] = '\0';
        *offset += (size_t)slen;
        out->value.str = s;
        return TRL_OK;
    }
    case TRL_FT_BYTES: {
        uint64_t blen;
        trl_status_t st = trl_decode_uvarint(data, len, offset, &blen);
        if (st != TRL_OK) return st;
        if (*offset + (size_t)blen > len) return TRL_ERR_CORRUPT;
        uint8_t *b = blen ? (uint8_t *)malloc((size_t)blen) : NULL;
        if (blen && !b) return TRL_ERR_OOM;
        if (blen) memcpy(b, data + *offset, (size_t)blen);
        *offset += (size_t)blen;
        out->value.vec.data = b;
        out->value.vec.len = (uint32_t)blen;
        out->value.vec.width = 0;
        return TRL_OK;
    }
    case TRL_FT_BITVEC:
    case TRL_FT_LOGICVEC: {
        uint64_t width;
        trl_status_t st = trl_decode_uvarint(data, len, offset, &width);
        if (st != TRL_OK) return st;
        size_t nbytes = ((size_t)width + 7) / 8;
        if (ft == TRL_FT_LOGICVEC) nbytes *= 2;
        if (*offset + nbytes > len) return TRL_ERR_CORRUPT;
        uint8_t *b = nbytes ? (uint8_t *)malloc(nbytes) : NULL;
        if (nbytes && !b) return TRL_ERR_OOM;
        if (nbytes) memcpy(b, data + *offset, nbytes);
        *offset += nbytes;
        out->value.vec.data = b;
        out->value.vec.len = (uint32_t)nbytes;
        out->value.vec.width = (uint32_t)width;
        return TRL_OK;
    }
    default:
        return TRL_ERR_INVALID;
    }
}

static void free_attr_value(const trl_attr_t *a) {
    switch (a->field_type) {
    case TRL_FT_STRING: free((void *)a->value.str); break;
    case TRL_FT_BYTES:
    case TRL_FT_BITVEC:
    case TRL_FT_LOGICVEC: free((void *)a->value.vec.data); break;
    default: break;
    }
}

/* ----------------------------------------------------------------------- *
 * Cursor: merge pass that joins TR_BEGIN/ATTR/END/LINK/META back together.
 * ----------------------------------------------------------------------- */

static trl_status_t qtxn_push_attr(q_txn_t *t, const trl_attr_t *a) {
    if (t->attr_count >= t->attr_cap) {
        size_t nc = t->attr_cap ? t->attr_cap * 2 : 4;
        trl_attr_t *n = (trl_attr_t *)realloc(t->attrs, nc * sizeof(*n));
        if (!n) return TRL_ERR_OOM;
        t->attrs = n; t->attr_cap = nc;
    }
    t->attrs[t->attr_count++] = *a;
    return TRL_OK;
}

static trl_status_t qtxn_push_link(q_txn_t *t, const trl_txn_link_t *l) {
    if (t->link_count >= t->link_cap) {
        size_t nc = t->link_cap ? t->link_cap * 2 : 4;
        trl_txn_link_t *n = (trl_txn_link_t *)realloc(t->links, nc * sizeof(*n));
        if (!n) return TRL_ERR_OOM;
        t->links = n; t->link_cap = nc;
    }
    t->links[t->link_count++] = *l;
    return TRL_OK;
}

static trl_status_t qtxn_push_meta(q_txn_t *t, const trl_txn_meta_t *m) {
    if (t->meta_count >= t->meta_cap) {
        size_t nc = t->meta_cap ? t->meta_cap * 2 : 4;
        trl_txn_meta_t *n = (trl_txn_meta_t *)realloc(t->metas, nc * sizeof(*n));
        if (!n) return TRL_ERR_OOM;
        t->metas = n; t->meta_cap = nc;
    }
    t->metas[t->meta_count++] = *m;
    return TRL_OK;
}

/* The in-progress merge state: transactions begun but not yet ended, and the
 * ones already closed (TR_FULL, or TR_BEGIN joined with its TR_END). Lookup
 * by txn_id is linear — fine at the scale this phase targets; see the note
 * at the top of trl_query.h. */
typedef struct {
    q_txn_t *open; size_t open_count, open_cap;
    q_txn_t *done; size_t done_count, done_cap;
} merge_state_t;

static q_txn_t *merge_open_find(merge_state_t *m, uint64_t txn_id) {
    for (size_t i = 0; i < m->open_count; ++i) {
        if (m->open[i].txn_id == txn_id) return &m->open[i];
    }
    return NULL;
}

static trl_status_t merge_open_push(merge_state_t *m, const q_txn_t *t) {
    if (m->open_count >= m->open_cap) {
        size_t nc = m->open_cap ? m->open_cap * 2 : 8;
        q_txn_t *n = (q_txn_t *)realloc(m->open, nc * sizeof(*n));
        if (!n) return TRL_ERR_OOM;
        m->open = n; m->open_cap = nc;
    }
    m->open[m->open_count++] = *t;
    return TRL_OK;
}

static trl_status_t merge_done_push(merge_state_t *m, const q_txn_t *t) {
    if (m->done_count >= m->done_cap) {
        size_t nc = m->done_cap ? m->done_cap * 2 : 8;
        q_txn_t *n = (q_txn_t *)realloc(m->done, nc * sizeof(*n));
        if (!n) return TRL_ERR_OOM;
        m->done = n; m->done_cap = nc;
    }
    m->done[m->done_count++] = *t;
    return TRL_OK;
}

static void merge_open_remove_at(merge_state_t *m, size_t idx) {
    m->open[idx] = m->open[--m->open_count];
}

/* Decodes one already-decompressed BLK_TXN_DATA payload and folds its
 * records into *m (open/done). */
static trl_status_t decode_txn_payload(trl_rdb_t *db, const uint8_t *data, size_t len, merge_state_t *m) {
    size_t offset = 0;
    if (len < 8 + 8) return TRL_ERR_CORRUPT;
    uint64_t block_start = trl_read_le64(data);
    offset += 16;  /* block_start, block_end — aggregate, not needed per-record */
    uint64_t count = 0;
    trl_status_t st = trl_decode_uvarint(data, len, &offset, &count);
    int64_t prev_txn_id = 0;
    int64_t prev_time = (int64_t)block_start;

    for (uint64_t i = 0; st == TRL_OK && i < count; ++i) {
        if (offset >= len) { st = TRL_ERR_CORRUPT; break; }
        uint8_t tag = data[offset++];
        if (tag == TRL_TXN_FULL) {
            uint64_t sid = 0, tid = 0, duration = 0, parent = 0, attr_count = 0;
            int64_t txn_id_delta = 0, start_delta = 0;
            st = trl_decode_uvarint(data, len, &offset, &sid);
            if (st == TRL_OK) st = trl_decode_uvarint(data, len, &offset, &tid);
            if (st == TRL_OK) st = trl_decode_svarint(data, len, &offset, &txn_id_delta);
            if (st == TRL_OK) st = trl_decode_svarint(data, len, &offset, &start_delta);
            if (st == TRL_OK) st = trl_decode_uvarint(data, len, &offset, &duration);
            if (st == TRL_OK) st = trl_decode_uvarint(data, len, &offset, &parent);
            if (st == TRL_OK) st = trl_decode_uvarint(data, len, &offset, &attr_count);
            if (st != TRL_OK) break;
            q_txn_t t; memset(&t, 0, sizeof(t));
            t.txn_id = (uint64_t)(prev_txn_id + txn_id_delta);
            t.start = (uint64_t)(prev_time + start_delta);
            t.end = t.start + duration;
            t.parent = parent;
            t.stream_inst_id = (uint32_t)sid;
            t.txn_type_id = (uint32_t)tid;
            const trl_rschema_t *schema = trl_rdb_schema_by_id(db, (uint32_t)tid);
            for (uint64_t a = 0; st == TRL_OK && a < attr_count; ++a) {
                uint64_t field_idx = 0;
                st = trl_decode_uvarint(data, len, &offset, &field_idx);
                if (st != TRL_OK) break;
                trl_field_type_t ft = (schema && field_idx < schema->field_count)
                                           ? schema->field_types[field_idx] : TRL_FT_U64;
                trl_attr_t av; memset(&av, 0, sizeof(av));
                av.field_idx = (uint32_t)field_idx;
                st = decode_attr_value(data, len, &offset, ft, &av);
                if (st == TRL_OK) st = qtxn_push_attr(&t, &av);
            }
            if (st != TRL_OK) break;
            st = merge_done_push(m, &t);
            prev_txn_id = (int64_t)t.txn_id;
            prev_time = (int64_t)t.end;
        } else if (tag == TRL_TXN_BEGIN) {
            uint64_t sid = 0, tid = 0, parent = 0;
            int64_t txn_id_delta = 0, start_delta = 0;
            st = trl_decode_uvarint(data, len, &offset, &sid);
            if (st == TRL_OK) st = trl_decode_uvarint(data, len, &offset, &tid);
            if (st == TRL_OK) st = trl_decode_svarint(data, len, &offset, &txn_id_delta);
            if (st == TRL_OK) st = trl_decode_svarint(data, len, &offset, &start_delta);
            if (st == TRL_OK) st = trl_decode_uvarint(data, len, &offset, &parent);
            if (st != TRL_OK) break;
            q_txn_t t; memset(&t, 0, sizeof(t));
            t.txn_id = (uint64_t)(prev_txn_id + txn_id_delta);
            t.start = (uint64_t)(prev_time + start_delta);
            t.end = UINT64_MAX;
            t.parent = parent;
            t.stream_inst_id = (uint32_t)sid;
            t.txn_type_id = (uint32_t)tid;
            st = merge_open_push(m, &t);
            prev_txn_id = (int64_t)t.txn_id;
            prev_time = (int64_t)t.start;
        } else if (tag == TRL_TXN_ATTR) {
            uint64_t field_count = 0;
            int64_t txn_id_delta = 0;
            st = trl_decode_svarint(data, len, &offset, &txn_id_delta);
            if (st == TRL_OK) st = trl_decode_uvarint(data, len, &offset, &field_count);
            if (st != TRL_OK) break;
            uint64_t txn_id = (uint64_t)(prev_txn_id + txn_id_delta);
            for (uint64_t a = 0; st == TRL_OK && a < field_count; ++a) {
                uint64_t field_idx = 0;
                st = trl_decode_uvarint(data, len, &offset, &field_idx);
                if (st != TRL_OK) break;
                if (offset >= len) { st = TRL_ERR_CORRUPT; break; }
                trl_field_type_t ft = (trl_field_type_t)data[offset++];
                trl_attr_t av; memset(&av, 0, sizeof(av));
                av.field_idx = (uint32_t)field_idx;
                st = decode_attr_value(data, len, &offset, ft, &av);
                if (st != TRL_OK) break;
                q_txn_t *open = merge_open_find(m, txn_id);
                if (open) st = qtxn_push_attr(open, &av);
                else free_attr_value(&av);
            }
            prev_txn_id = (int64_t)txn_id;
        } else if (tag == TRL_TXN_END) {
            int64_t txn_id_delta = 0, end_delta = 0;
            st = trl_decode_svarint(data, len, &offset, &txn_id_delta);
            if (st == TRL_OK) st = trl_decode_svarint(data, len, &offset, &end_delta);
            if (st != TRL_OK) break;
            uint64_t txn_id = (uint64_t)(prev_txn_id + txn_id_delta);
            uint64_t end_time = (uint64_t)(prev_time + end_delta);
            for (size_t i = 0; i < m->open_count; ++i) {
                if (m->open[i].txn_id == txn_id) {
                    q_txn_t t = m->open[i];
                    t.end = end_time;
                    merge_open_remove_at(m, i);
                    st = merge_done_push(m, &t);
                    break;
                }
            }
            prev_txn_id = (int64_t)txn_id;
            prev_time = (int64_t)end_time;
        } else if (tag == TRL_TXN_LINK) {
            if (offset >= len) { st = TRL_ERR_CORRUPT; break; }
            uint8_t link_type = data[offset++];
            uint64_t tgt = 0, label_sid = 0;
            int64_t src_delta = 0;
            st = trl_decode_svarint(data, len, &offset, &src_delta);
            if (st == TRL_OK) st = trl_decode_uvarint(data, len, &offset, &tgt);
            if (st == TRL_OK) st = trl_decode_uvarint(data, len, &offset, &label_sid);
            if (st != TRL_OK) break;
            uint64_t src = (uint64_t)(prev_txn_id + src_delta);
            trl_txn_link_t lk = { link_type, tgt, (uint32_t)label_sid };
            q_txn_t *open = merge_open_find(m, src);
            if (open) {
                st = qtxn_push_link(open, &lk);
            } else {
                for (size_t i = 0; i < m->done_count; ++i) {
                    if (m->done[i].txn_id == src) { st = qtxn_push_link(&m->done[i], &lk); break; }
                }
            }
            prev_txn_id = (int64_t)src;
        } else if (tag == TRL_TXN_META) {
            uint64_t key_sid = 0, val_sid = 0;
            int64_t txn_id_delta = 0;
            st = trl_decode_svarint(data, len, &offset, &txn_id_delta);
            if (st == TRL_OK) st = trl_decode_uvarint(data, len, &offset, &key_sid);
            if (st == TRL_OK) st = trl_decode_uvarint(data, len, &offset, &val_sid);
            if (st != TRL_OK) break;
            uint64_t txn_id = (uint64_t)(prev_txn_id + txn_id_delta);
            trl_txn_meta_t mt = { (uint32_t)key_sid, (uint32_t)val_sid };
            q_txn_t *open = merge_open_find(m, txn_id);
            if (open) {
                st = qtxn_push_meta(open, &mt);
            } else {
                for (size_t i = 0; i < m->done_count; ++i) {
                    if (m->done[i].txn_id == txn_id) { st = qtxn_push_meta(&m->done[i], &mt); break; }
                }
            }
            prev_txn_id = (int64_t)txn_id;
        } else {
            st = TRL_ERR_CORRUPT;
        }
    }
    return st;
}

static int cmp_offset(const void *a, const void *b) {
    const trl_index_entry_t *ea = *(const trl_index_entry_t * const *)a;
    const trl_index_entry_t *eb = *(const trl_index_entry_t * const *)b;
    if (ea->offset < eb->offset) return -1;
    if (ea->offset > eb->offset) return 1;
    return 0;
}

static int cmp_txn(const void *a, const void *b) {
    const q_txn_t *ta = (const q_txn_t *)a;
    const q_txn_t *tb = (const q_txn_t *)b;
    if (ta->start < tb->start) return -1;
    if (ta->start > tb->start) return 1;
    if (ta->txn_id < tb->txn_id) return -1;
    if (ta->txn_id > tb->txn_id) return 1;
    return 0;
}

static bool cursor_wants_stream(const trl_cursor_t *c, uint32_t stream_inst_id) {
    if (c->all_streams || stream_inst_id == TRL_STREAM_MIXED) return true;
    for (size_t i = 0; i < c->stream_count; ++i) {
        if (c->stream_ids[i] == stream_inst_id) return true;
    }
    return false;
}

static void cursor_build(trl_cursor_t *c) {
    c->built = true;
    trl_reader_t *r = c->db->r;

    const trl_index_entry_t **sel = NULL;
    size_t sel_count = 0;
    for (size_t i = 0; i < r->index.txn_count; ++i) {
        if (cursor_wants_stream(c, r->index.txn_entries[i].stream_inst_id)) {
            const trl_index_entry_t **n = (const trl_index_entry_t **)realloc(
                sel, (sel_count + 1) * sizeof(*sel));
            if (!n) break;
            sel = n;
            sel[sel_count++] = &r->index.txn_entries[i];
        }
    }
    if (sel_count) qsort(sel, sel_count, sizeof(*sel), cmp_offset);

    merge_state_t m; memset(&m, 0, sizeof(m));
    for (size_t i = 0; i < sel_count; ++i) {
        uint8_t type, flags;
        uint8_t *payload; size_t payload_len;
        if (read_block_at(r, sel[i]->offset, &type, &flags, &payload, &payload_len) != TRL_OK) continue;
        if (type != TRL_BLK_TXN_DATA) { free(payload); continue; }

        const uint8_t *data = payload;
        size_t len = payload_len;
        uint8_t *owned = NULL;
        if (flags & TRL_FLAG_COMPRESSED) {
            /* Unlike BLK_HIERARCHY (and the other metadata blocks), a
             * compressed BLK_TXN_DATA payload has no 8-byte uncompressed-
             * length prefix: trl_txn_block_encode just zlib-compresses the
             * row payload directly, matching the pre-existing Python
             * writer/reader convention for this block type (_txn_data.py's
             * read_block calls zlib.decompress(payload) with no prefix).
             * So the output size isn't known up front; grow a buffer and
             * retry, the same way trl_vc_iter_payload does for BLK_VC_DATA. */
            uLongf out_len = payload_len ? (uLongf)(payload_len * 16 + 64) : 64;
            for (;;) {
                uint8_t *tmp = (uint8_t *)realloc(owned, out_len);
                if (!tmp) { free(owned); owned = NULL; data = NULL; len = 0; break; }
                owned = tmp;
                uLongf actual = out_len;
                int rc = uncompress(owned, &actual, payload, (uLong)payload_len);
                if (rc == Z_OK) { data = owned; len = (size_t)actual; break; }
                if (rc != Z_BUF_ERROR) { free(owned); owned = NULL; data = NULL; len = 0; break; }
                out_len *= 2;
            }
        }
        if (data && (flags & TRL_FLAG_TXN_DELTA)) {
            decode_txn_payload(c->db, data, len, &m);
        }
        free(owned);
        free(payload);
    }
    free(sel);

    /* Anything still open at the end of the selected blocks never saw a
     * TR_END; report it as open (end == UINT64_MAX) rather than dropping it. */
    for (size_t i = 0; i < m.open_count; ++i) merge_done_push(&m, &m.open[i]);
    free(m.open);

    /* Apply the [t_from, t_to) window and the final (start, txn_id) sort. */
    size_t keep = 0;
    for (size_t i = 0; i < m.done_count; ++i) {
        const q_txn_t *t = &m.done[i];
        bool in_range = (t->start < c->t_to) && (t->end == UINT64_MAX || t->end >= c->t_from);
        if (in_range && cursor_wants_stream(c, t->stream_inst_id)) {
            m.done[keep++] = *t;
        } else {
            for (size_t a = 0; a < t->attr_count; ++a) free_attr_value(&t->attrs[a]);
            free(t->attrs); free(t->links); free(t->metas);
        }
    }
    m.done_count = keep;
    if (m.done_count) qsort(m.done, m.done_count, sizeof(*m.done), cmp_txn);

    c->txns = m.done;
    c->count = m.done_count;
}

trl_cursor_t *trl_cursor_new(trl_rdb_t *db, trl_rstream_t *stream, uint64_t t_from, uint64_t t_to) {
    trl_cursor_t *c = (trl_cursor_t *)calloc(1, sizeof(*c));
    if (!c) return NULL;
    c->db = db;
    c->t_from = t_from;
    c->t_to = t_to;
    c->all_streams = (stream == NULL);
    if (stream) {
        c->stream_ids = (uint32_t *)malloc(sizeof(uint32_t));
        if (c->stream_ids) { c->stream_ids[0] = stream->stream_inst_id; c->stream_count = c->stream_cap = 1; }
    }
    return c;
}

trl_status_t trl_cursor_add_stream(trl_cursor_t *c, trl_rstream_t *stream) {
    if (c->built) return TRL_ERR_INVALID;
    c->all_streams = false;
    if (c->stream_count >= c->stream_cap) {
        size_t nc = c->stream_cap ? c->stream_cap * 2 : 4;
        uint32_t *n = (uint32_t *)realloc(c->stream_ids, nc * sizeof(uint32_t));
        if (!n) return TRL_ERR_OOM;
        c->stream_ids = n; c->stream_cap = nc;
    }
    c->stream_ids[c->stream_count++] = stream->stream_inst_id;
    return TRL_OK;
}

int trl_cursor_next(trl_cursor_t *c, trl_txn_hdr_t *out) {
    if (!c->built) cursor_build(c);
    if (c->pos >= c->count) return 0;
    const q_txn_t *t = &c->txns[c->pos++];
    out->txn_id = t->txn_id;
    out->stream_inst_id = t->stream_inst_id;
    out->txn_type_id = t->txn_type_id;
    out->start = t->start;
    out->end = t->end;
    out->parent = t->parent;
    out->attr_count = (uint32_t)t->attr_count;
    out->attrs = t->attrs;
    out->link_count = (uint32_t)t->link_count;
    out->links = t->links;
    out->meta_count = (uint32_t)t->meta_count;
    out->metas = t->metas;
    return 1;
}

void trl_cursor_free(trl_cursor_t *c) {
    if (!c) return;
    for (size_t i = 0; i < c->count; ++i) {
        for (size_t a = 0; a < c->txns[i].attr_count; ++a) free_attr_value(&c->txns[i].attrs[a]);
        free(c->txns[i].attrs);
        free(c->txns[i].links);
        free(c->txns[i].metas);
    }
    free(c->txns);
    free(c->stream_ids);
    free(c);
}
