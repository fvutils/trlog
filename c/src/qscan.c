/* qscan.c: the query scan engine (trl/trl_qscan.h, query CLI phase Q4).
 *
 * The reference for every rule here is the pure-Python engine:
 *   - record decoding:  python/src/trlog/_txn_data.py (_decode_payload,
 *                       _decode_record, _decode_attrs) and _codec.decode_field
 *   - the join:         python/src/trlog/query/_engine.py Trace.scan
 *   - window + filter:  Trace._matches and query/_expr.py
 * Where this file cannot be sure to match it, it either defers a row to the
 * caller (DEFER) or gives up on the whole scan (TRL_QS_FALLBACK).
 */

#include "varint_internal.h"
#include "trl/trl_qscan.h"

#include <stdarg.h>
#include <stdio.h>

#if defined(_WIN32)
#define qs_fseek _fseeki64
#else
#define qs_fseek fseeko
#endif

/* ----------------------------------------------------------------------- *
 * Bytecode (mirrored in python/src/trlog/query/_cengine.py)
 * ----------------------------------------------------------------------- */

enum {
    OP_END = 0, OP_INT = 1, OP_MISS = 2, OP_DEFER = 3, OP_LOAD = 4, OP_HAS = 5,
    OP_BUILTIN = 6, OP_UN = 7, OP_BIN = 8, OP_CMP = 9, OP_AND = 10, OP_OR = 11,
    OP_LABEL = 12, OP_IDTEST = 13, OP_MINMAX = 14, OP_BIT = 15, OP_BITS = 16,
    OP_LOADT = 17, OP_HAST = 18
};
enum { B_ID = 0, B_START, B_END, B_DUR, B_PARENT, B_OPEN, B_NLINKS };
enum { U_NEG = 0, U_POS, U_INV, U_ABS, U_NOT, U_NOTMISS };
enum { A_ADD = 0, A_SUB, A_MUL, A_DIV, A_MOD, A_AND, A_OR, A_XOR, A_SHL, A_SHR };
enum { C_EQ = 0, C_NE, C_LT, C_LE, C_GT, C_GE };

/* Value tags. DEFER is a value C cannot represent exactly (string, float,
 * int beyond int64, tuple); every operation on it yields DEFER unless
 * Python's answer does not depend on it. */
enum { V_MISS = 0, V_INT, V_ENUM, V_LXZ, V_DEFER, V_BOOL };   /* values: trl_qscan.h TRL_QS_V_* */

/* _expr.MAX_SHIFT: a wider bits() mask (or longer left shift) is MISSING. */
#define QS_MAX_SHIFT ((int64_t)1 << 16)

#define TRI_FALSE 0
#define TRI_TRUE  1
#define TRI_DEFER 2

typedef struct {
    uint8_t  tag;
    int64_t  v;
    uint64_t aux;      /* V_ENUM: (type_id << 32) | field_idx, for label sets */
} qs_val_t;

/* ----------------------------------------------------------------------- *
 * State
 * ----------------------------------------------------------------------- */

typedef struct {
    int64_t  id, start, end, parent;
    uint32_t stream, type;
    uint8_t  open;
    int32_t  a_head, a_tail, m_head, m_tail, l_head, l_tail;
} qs_txn_t;

typedef struct { trl_qs_attr_t a; int32_t next; } qs_attr_node_t;
typedef struct { trl_qs_meta_t m; int32_t next; } qs_meta_node_t;
typedef struct { trl_qs_link_t l; int32_t next; } qs_link_node_t;

typedef struct {
    uint32_t n;
    uint8_t *fts;
} qs_schema_t;

typedef struct {
    uint32_t n;
    int32_t *slots;
    uint8_t *norms;
} qs_catalog_t;

/* Open-addressing map from int64 key to int64 value. */
typedef struct {
    int64_t *keys;
    int64_t *vals;
    uint8_t *used;
    size_t   cap, count;
} qs_map_t;

struct trl_qscan {
    FILE *fp;

    qs_schema_t  *schemas;   size_t schema_cap;     /* indexed by type id */
    qs_catalog_t *catalog;   size_t catalog_cap;    /* indexed by type id */

    /* plan */
    size_t    plan_n;
    int64_t  *plan_start;
    uint64_t *plan_off;
    uint32_t *plan_sid;

    qs_map_t  streams_sel;  bool streams_all;
    qs_map_t  types_sel;    bool types_all;

    /* program */
    int64_t  *code;  size_t ncode;
    int64_t  *sets;  size_t nsets;
    int       strict_unmapped;
    int64_t   filter_entry;                       /* -1: no filter */
    int64_t  *val_entries; size_t nvals, val_cap; /* projected values */
    qs_val_t *stack; size_t stack_cap;
    bool      err_possible;

    uint32_t  flags;
    int64_t   t_hi, lo, hi, max_txns;

    /* scan state */
    qs_txn_t *txns;  size_t ntxn, txn_cap;
    qs_map_t  by_id;
    qs_map_t  pending;          /* stream id -> open txns started <= t_hi */
    qs_attr_node_t *attrs; size_t nattr, attr_cap;
    qs_meta_node_t *metas; size_t nmeta, meta_cap;
    qs_link_node_t *links; size_t nlink, link_cap;
    uint8_t  *heap;  size_t heap_len, heap_cap;
    int64_t   blocks_read, duplicate_ids;

    /* block buffers */
    uint8_t *raw;  size_t raw_cap;
    uint8_t *dec;  size_t dec_cap;

    /* output */
    trl_qs_row_t  *out_rows;  size_t out_nrow, out_row_cap;
    trl_qs_attr_t *out_attrs; size_t out_nattr, out_attr_cap;
    trl_qs_meta_t *out_metas; size_t out_nmeta, out_meta_cap;
    trl_qs_link_t *out_links; size_t out_nlink, out_link_cap;
    int64_t n_keep, n_defer;

    /* projection (TRL_QS_PROJECT): one column per field, row-major values */
    size_t    p_n, p_cap;
    int64_t  *p_id, *p_start, *p_end, *p_parent;
    uint32_t *p_stream, *p_type;
    uint8_t  *p_open;
    uint8_t  *p_vtag; int64_t *p_val; uint64_t *p_aux; size_t p_vcap;

    char reason[160];
};

/* A FALLBACK is not an error: the caller reruns on the Python engine. */
#define FALLBACK(q, ...) do { snprintf((q)->reason, sizeof((q)->reason), __VA_ARGS__); \
                              return TRL_QS_FALLBACK; } while (0)

/* ----------------------------------------------------------------------- *
 * Small containers
 * ----------------------------------------------------------------------- */

static bool grow(void **p, size_t *cap, size_t need, size_t elem) {
    if (need <= *cap) return true;
    size_t nc = *cap ? *cap : 16;
    while (nc < need) nc *= 2;
    void *np = realloc(*p, nc * elem);
    if (!np) return false;
    *p = np;
    *cap = nc;
    return true;
}

static uint64_t mix64(uint64_t x) {
    x ^= x >> 33; x *= 0xff51afd7ed558ccdULL;
    x ^= x >> 33; x *= 0xc4ceb9fe1a85ec53ULL;
    x ^= x >> 33;
    return x;
}

static void map_free(qs_map_t *m) {
    free(m->keys); free(m->vals); free(m->used);
    memset(m, 0, sizeof(*m));
}

static void map_clear(qs_map_t *m) {
    if (m->used) memset(m->used, 0, m->cap);
    m->count = 0;
}

static int64_t *map_find(const qs_map_t *m, int64_t key) {
    if (!m->cap) return NULL;
    size_t mask = m->cap - 1;
    size_t i = (size_t)mix64((uint64_t)key) & mask;
    while (m->used[i]) {
        if (m->keys[i] == key) return &m->vals[i];
        i = (i + 1) & mask;
    }
    return NULL;
}

static bool map_rehash(qs_map_t *m, size_t ncap) {
    qs_map_t n;
    n.cap = ncap; n.count = 0;
    n.keys = (int64_t *)malloc(ncap * sizeof(int64_t));
    n.vals = (int64_t *)malloc(ncap * sizeof(int64_t));
    n.used = (uint8_t *)calloc(ncap, 1);
    if (!n.keys || !n.vals || !n.used) { free(n.keys); free(n.vals); free(n.used); return false; }
    for (size_t i = 0; i < m->cap; ++i) {
        if (!m->used[i]) continue;
        size_t j = (size_t)mix64((uint64_t)m->keys[i]) & (ncap - 1);
        while (n.used[j]) j = (j + 1) & (ncap - 1);
        n.used[j] = 1; n.keys[j] = m->keys[i]; n.vals[j] = m->vals[i];
        n.count++;
    }
    map_free(m);
    *m = n;
    return true;
}

/* Returns the value slot for key, inserting `init` if absent. */
static int64_t *map_put(qs_map_t *m, int64_t key, int64_t init, bool *inserted) {
    if ((m->count + 1) * 2 > m->cap) {
        if (!map_rehash(m, m->cap ? m->cap * 2 : 64)) return NULL;
    }
    size_t mask = m->cap - 1;
    size_t i = (size_t)mix64((uint64_t)key) & mask;
    while (m->used[i]) {
        if (m->keys[i] == key) { if (inserted) *inserted = false; return &m->vals[i]; }
        i = (i + 1) & mask;
    }
    m->used[i] = 1; m->keys[i] = key; m->vals[i] = init;
    m->count++;
    if (inserted) *inserted = true;
    return &m->vals[i];
}

/* ----------------------------------------------------------------------- *
 * Lifecycle and configuration
 * ----------------------------------------------------------------------- */

int trl_qscan_abi_version(void) { return TRL_QSCAN_ABI; }

_Static_assert(V_MISS == TRL_QS_V_MISS && V_INT == TRL_QS_V_INT && V_ENUM == TRL_QS_V_ENUM &&
               V_BOOL == TRL_QS_V_BOOL, "value tags match trl_qscan.h");

trl_qscan_t *trl_qscan_new(const char *path) {
    FILE *fp = fopen(path, "rb");
    if (!fp) return NULL;
    trl_qscan_t *q = (trl_qscan_t *)calloc(1, sizeof(*q));
    if (!q) { fclose(fp); return NULL; }
    q->fp = fp;
    q->streams_all = q->types_all = true;
    q->filter_entry = -1;
    return q;
}

static void clear_results(trl_qscan_t *q) {
    q->ntxn = 0;
    map_clear(&q->by_id);
    map_clear(&q->pending);
    q->nattr = q->nmeta = q->nlink = 0;
    q->heap_len = 0;
    q->blocks_read = q->duplicate_ids = 0;
    q->out_nrow = q->out_nattr = q->out_nmeta = q->out_nlink = 0;
    q->n_keep = q->n_defer = 0;
    q->p_n = 0;
    q->reason[0] = 0;
}

void trl_qscan_reset(trl_qscan_t *q) {
    if (!q) return;
    for (size_t i = 0; i < q->catalog_cap; ++i) {
        free(q->catalog[i].slots); free(q->catalog[i].norms);
        q->catalog[i].slots = NULL; q->catalog[i].norms = NULL; q->catalog[i].n = 0;
    }
    q->plan_n = 0;
    map_clear(&q->streams_sel); q->streams_all = true;
    map_clear(&q->types_sel);   q->types_all = true;
    q->ncode = q->nsets = 0;
    q->strict_unmapped = 0;
    q->filter_entry = -1;
    q->nvals = 0;
    q->flags = 0;
    q->t_hi = q->lo = q->hi = q->max_txns = 0;
    clear_results(q);
}

static void free_proj(trl_qscan_t *q) {
    free(q->p_id); free(q->p_start); free(q->p_end); free(q->p_parent);
    free(q->p_stream); free(q->p_type); free(q->p_open);
    free(q->p_vtag); free(q->p_val); free(q->p_aux);
    q->p_id = q->p_start = q->p_end = q->p_parent = NULL;
    q->p_stream = q->p_type = NULL; q->p_open = NULL;
    q->p_vtag = NULL; q->p_val = NULL; q->p_aux = NULL;
    q->p_n = q->p_cap = q->p_vcap = 0;
}

void trl_qscan_trim(trl_qscan_t *q) {
    if (!q) return;
    clear_results(q);
    free(q->txns); q->txns = NULL; q->txn_cap = 0;
    map_free(&q->by_id);
    map_free(&q->pending);
    free(q->attrs); q->attrs = NULL; q->attr_cap = 0;
    free(q->metas); q->metas = NULL; q->meta_cap = 0;
    free(q->links); q->links = NULL; q->link_cap = 0;
    free(q->heap);  q->heap = NULL;  q->heap_cap = 0;
    free(q->raw);   q->raw = NULL;   q->raw_cap = 0;
    free(q->dec);   q->dec = NULL;   q->dec_cap = 0;
    free(q->out_rows);  q->out_rows = NULL;  q->out_row_cap = 0;
    free(q->out_attrs); q->out_attrs = NULL; q->out_attr_cap = 0;
    free(q->out_metas); q->out_metas = NULL; q->out_meta_cap = 0;
    free(q->out_links); q->out_links = NULL; q->out_link_cap = 0;
    free_proj(q);
}

void trl_qscan_free(trl_qscan_t *q) {
    if (!q) return;
    trl_qscan_reset(q);
    if (q->fp) fclose(q->fp);
    for (size_t i = 0; i < q->schema_cap; ++i) free(q->schemas[i].fts);
    free(q->schemas);
    free(q->catalog);
    free(q->plan_start); free(q->plan_off); free(q->plan_sid);
    map_free(&q->streams_sel); map_free(&q->types_sel);
    free(q->code); free(q->sets); free(q->stack); free(q->val_entries);
    free(q->txns);
    map_free(&q->by_id); map_free(&q->pending);
    free(q->attrs); free(q->metas); free(q->links); free(q->heap);
    free(q->raw); free(q->dec);
    free(q->out_rows); free(q->out_attrs); free(q->out_metas); free(q->out_links);
    free(q);
}

trl_status_t trl_qscan_set_schema(trl_qscan_t *q, uint32_t type_id, uint32_t n,
                                  const uint8_t *fts) {
    if (!q) return TRL_ERR_INVALID;
    if (type_id >= q->schema_cap) {
        size_t nc = (size_t)type_id + 1;
        qs_schema_t *ns = (qs_schema_t *)realloc(q->schemas, nc * sizeof(*ns));
        if (!ns) return TRL_ERR_OOM;
        memset(ns + q->schema_cap, 0, (nc - q->schema_cap) * sizeof(*ns));
        q->schemas = ns; q->schema_cap = nc;
    }
    qs_schema_t *s = &q->schemas[type_id];
    free(s->fts);
    s->fts = NULL; s->n = 0;
    if (n) {
        s->fts = (uint8_t *)malloc(n);
        if (!s->fts) return TRL_ERR_OOM;
        memcpy(s->fts, fts, n);
    }
    s->n = n;
    return TRL_OK;
}

trl_status_t trl_qscan_set_catalog(trl_qscan_t *q, uint32_t type_id, uint32_t n,
                                   const int32_t *slots, const uint8_t *norms) {
    if (!q) return TRL_ERR_INVALID;
    if (type_id >= q->catalog_cap) {
        size_t nc = (size_t)type_id + 1;
        qs_catalog_t *ns = (qs_catalog_t *)realloc(q->catalog, nc * sizeof(*ns));
        if (!ns) return TRL_ERR_OOM;
        memset(ns + q->catalog_cap, 0, (nc - q->catalog_cap) * sizeof(*ns));
        q->catalog = ns; q->catalog_cap = nc;
    }
    qs_catalog_t *c = &q->catalog[type_id];
    free(c->slots); free(c->norms);
    c->slots = (int32_t *)malloc((n ? n : 1) * sizeof(int32_t));
    c->norms = (uint8_t *)malloc(n ? n : 1);
    if (!c->slots || !c->norms) return TRL_ERR_OOM;
    if (n) { memcpy(c->slots, slots, n * sizeof(int32_t)); memcpy(c->norms, norms, n); }
    c->n = n;
    /* n == 0 still marks the type as catalogued (a type with no fields). */
    return TRL_OK;
}

trl_status_t trl_qscan_set_plan(trl_qscan_t *q, size_t n, const int64_t *starts,
                                const uint64_t *offsets, const uint32_t *sids) {
    if (!q) return TRL_ERR_INVALID;
    int64_t *a = (int64_t *)realloc(q->plan_start, (n ? n : 1) * sizeof(int64_t));
    if (!a) return TRL_ERR_OOM;
    q->plan_start = a;
    uint64_t *b = (uint64_t *)realloc(q->plan_off, (n ? n : 1) * sizeof(uint64_t));
    if (!b) return TRL_ERR_OOM;
    q->plan_off = b;
    uint32_t *c = (uint32_t *)realloc(q->plan_sid, (n ? n : 1) * sizeof(uint32_t));
    if (!c) return TRL_ERR_OOM;
    q->plan_sid = c;
    if (n) {
        memcpy(a, starts, n * sizeof(int64_t));
        memcpy(b, offsets, n * sizeof(uint64_t));
        memcpy(c, sids, n * sizeof(uint32_t));
    }
    q->plan_n = n;
    return TRL_OK;
}

static trl_status_t set_ids(qs_map_t *m, bool *all, size_t n, const uint32_t *ids) {
    map_clear(m);
    for (size_t i = 0; i < n; ++i)
        if (!map_put(m, (int64_t)ids[i], 1, NULL)) return TRL_ERR_OOM;
    *all = false;
    return TRL_OK;
}

trl_status_t trl_qscan_set_streams(trl_qscan_t *q, size_t n, const uint32_t *ids) {
    if (!q) return TRL_ERR_INVALID;
    return set_ids(&q->streams_sel, &q->streams_all, n, ids);
}

trl_status_t trl_qscan_set_types(trl_qscan_t *q, size_t n, const uint32_t *ids) {
    if (!q) return TRL_ERR_INVALID;
    return set_ids(&q->types_sel, &q->types_all, n, ids);
}

trl_status_t trl_qscan_set_program(trl_qscan_t *q, size_t ncode, const int64_t *code,
                                   size_t nsets, const int64_t *sets, int strict_unmapped) {
    if (!q) return TRL_ERR_INVALID;
    int64_t *c = (int64_t *)realloc(q->code, (ncode ? ncode : 1) * sizeof(int64_t));
    if (!c) return TRL_ERR_OOM;
    q->code = c;
    if (ncode) memcpy(c, code, ncode * sizeof(int64_t));
    int64_t *s = (int64_t *)realloc(q->sets, (nsets ? nsets : 1) * sizeof(int64_t));
    if (!s) return TRL_ERR_OOM;
    q->sets = s;
    if (nsets) memcpy(s, sets, nsets * sizeof(int64_t));
    q->ncode = ncode;
    q->nsets = nsets;
    q->strict_unmapped = strict_unmapped;
    /* The stack never holds more values than there are code words. */
    if (!grow((void **)&q->stack, &q->stack_cap, ncode + 1, sizeof(qs_val_t))) return TRL_ERR_OOM;
    return TRL_OK;
}

trl_status_t trl_qscan_set_entries(trl_qscan_t *q, int64_t filter_entry, size_t nvals,
                                   const int64_t *val_entries) {
    if (!q) return TRL_ERR_INVALID;
    if (filter_entry >= (int64_t)q->ncode) return TRL_ERR_INVALID;
    if (!grow((void **)&q->val_entries, &q->val_cap, nvals + 1, sizeof(int64_t))) return TRL_ERR_OOM;
    for (size_t i = 0; i < nvals; ++i) {
        if (val_entries[i] < 0 || val_entries[i] >= (int64_t)q->ncode) return TRL_ERR_INVALID;
        q->val_entries[i] = val_entries[i];
    }
    q->filter_entry = filter_entry;
    q->nvals = nvals;
    return TRL_OK;
}

size_t trl_qscan_proj(const trl_qscan_t *q, trl_qs_proj_t *out) {
    out->id = q->p_id; out->start = q->p_start; out->end = q->p_end;
    out->parent = q->p_parent; out->stream = q->p_stream; out->type = q->p_type;
    out->open = q->p_open; out->vtag = q->p_vtag; out->val = q->p_val; out->aux = q->p_aux;
    return q->p_n;
}

void trl_qscan_set_options(trl_qscan_t *q, uint32_t flags, int64_t t_hi,
                           int64_t lo, int64_t hi, int64_t max_txns) {
    if (!q) return;
    q->flags = flags;
    q->t_hi = t_hi;
    q->lo = lo;
    q->hi = hi;
    q->max_txns = max_txns;
}

const char *trl_qscan_fallback_reason(const trl_qscan_t *q) { return q ? q->reason : ""; }

size_t trl_qscan_rows(const trl_qscan_t *q, const trl_qs_row_t **out) { *out = q->out_rows; return q->out_nrow; }
size_t trl_qscan_attrs(const trl_qscan_t *q, const trl_qs_attr_t **out) { *out = q->out_attrs; return q->out_nattr; }
size_t trl_qscan_metas(const trl_qscan_t *q, const trl_qs_meta_t **out) { *out = q->out_metas; return q->out_nmeta; }
size_t trl_qscan_links(const trl_qscan_t *q, const trl_qs_link_t **out) { *out = q->out_links; return q->out_nlink; }
size_t trl_qscan_heap(const trl_qscan_t *q, const uint8_t **out) { *out = q->heap; return q->heap_len; }

void trl_qscan_counts(const trl_qscan_t *q, int64_t out[4]) {
    out[0] = q->blocks_read;
    out[1] = q->duplicate_ids;
    out[2] = q->n_keep;
    out[3] = q->n_defer;
}

/* ----------------------------------------------------------------------- *
 * Decoding helpers (exactly _codec.decode_uvarint / decode_field)
 * ----------------------------------------------------------------------- */

/* Like _codec.decode_uvarint, but a 10th byte that would carry bits past
 * 64 (Python keeps them, C cannot) is reported as unrepresentable. */
static int rd_uvarint(trl_qscan_t *q, const uint8_t *d, size_t len, size_t *off, uint64_t *out) {
    uint64_t r = 0;
    unsigned shift = 0;
    for (;;) {
        if (*off >= len) FALLBACK(q, "truncated varint");
        uint8_t b = d[(*off)++];
        if (shift == 63 && (b & 0x7Eu)) FALLBACK(q, "varint wider than 64 bits");
        r |= ((uint64_t)(b & 0x7Fu)) << shift;
        if (!(b & 0x80u)) { *out = r; return TRL_QS_OK; }
        shift += 7;
        if (shift >= 64) FALLBACK(q, "varint too long");
    }
}

static int rd_svarint(trl_qscan_t *q, const uint8_t *d, size_t len, size_t *off, int64_t *out) {
    uint64_t raw;
    int rc = rd_uvarint(q, d, len, off, &raw);
    if (rc) return rc;
    *out = (int64_t)((raw >> 1) ^ (~(raw & 1) + 1));
    return TRL_QS_OK;
}

/* A u64 that Python holds as a plain int; C keeps int64 and gives up
 * outside it. */
static int as_i64(trl_qscan_t *q, uint64_t v, int64_t *out, const char *what) {
    if (v > (uint64_t)INT64_MAX) FALLBACK(q, "%s beyond int64", what);
    *out = (int64_t)v;
    return TRL_QS_OK;
}

static int add_i64(trl_qscan_t *q, int64_t a, int64_t b, int64_t *out) {
    if (__builtin_add_overflow(a, b, out)) FALLBACK(q, "time or id beyond int64");
    return TRL_QS_OK;
}

/* Strict UTF-8, as bytes.decode('utf-8') accepts it: no overlongs, no
 * surrogates, nothing above U+10FFFF. */
static bool utf8_ok(const uint8_t *s, size_t n) {
    size_t i = 0;
    while (i < n) {
        uint8_t c = s[i];
        if (c < 0x80) { i++; continue; }
        size_t k;
        uint32_t cp, min;
        if ((c & 0xE0) == 0xC0) { k = 1; cp = c & 0x1F; min = 0x80; }
        else if ((c & 0xF0) == 0xE0) { k = 2; cp = c & 0x0F; min = 0x800; }
        else if ((c & 0xF8) == 0xF0) { k = 3; cp = c & 0x07; min = 0x10000; }
        else return false;
        if (i + k >= n) return false;          /* needs bytes i+1 .. i+k */
        for (size_t j = 1; j <= k; ++j) {
            if ((s[i + j] & 0xC0) != 0x80) return false;
            cp = (cp << 6) | (s[i + j] & 0x3F);
        }
        if (cp < min || cp > 0x10FFFF || (cp >= 0xD800 && cp <= 0xDFFF)) return false;
        i += k + 1;
    }
    return true;
}

static bool valid_ft(uint32_t ft) { return ft <= TRL_FT_LOGICVEC; }

/* Decodes one field value. `keep` says whether the value is attached to a
 * selected txn (variable-length data then goes to the heap). */
static int rd_field(trl_qscan_t *q, const uint8_t *d, size_t len, size_t *off,
                    uint32_t ft, bool keep, trl_qs_attr_t *a) {
    a->ft = ft;
    a->v = 0; a->off = a->len = a->width = 0; a->pad = 0;
    size_t fixed = 0;
    switch (ft) {
    case TRL_FT_U8: case TRL_FT_I8: case TRL_FT_BOOL: fixed = 1; break;
    case TRL_FT_U16: case TRL_FT_I16: fixed = 2; break;
    case TRL_FT_U32: case TRL_FT_I32: case TRL_FT_F32: fixed = 4; break;
    case TRL_FT_U64: case TRL_FT_I64: case TRL_FT_F64: case TRL_FT_TIME: fixed = 8; break;
    default: break;
    }
    if (fixed) {
        if (*off + fixed > len) FALLBACK(q, "truncated field");
        const uint8_t *p = d + *off;
        uint64_t v = 0;
        for (size_t i = 0; i < fixed; ++i) v |= (uint64_t)p[i] << (8 * i);
        switch (ft) {
        case TRL_FT_I8:  v = (uint64_t)(int64_t)(int8_t)v; break;
        case TRL_FT_I16: v = (uint64_t)(int64_t)(int16_t)v; break;
        case TRL_FT_I32: v = (uint64_t)(int64_t)(int32_t)v; break;
        case TRL_FT_F32: { float f; uint32_t u = (uint32_t)v; memcpy(&f, &u, 4);
                           double dd = (double)f; memcpy(&v, &dd, 8); break; }
        default: break;
        }
        a->v = v;
        *off += fixed;
        return TRL_QS_OK;
    }
    int rc;
    uint64_t n;
    switch (ft) {
    case TRL_FT_ENUM:
        return rd_uvarint(q, d, len, off, &a->v);
    case TRL_FT_STRING:
    case TRL_FT_BYTES:
        if ((rc = rd_uvarint(q, d, len, off, &n))) return rc;
        if (n > len - *off) FALLBACK(q, "truncated string");
        if (ft == TRL_FT_STRING && !utf8_ok(d + *off, (size_t)n)) FALLBACK(q, "invalid UTF-8");
        a->len = (uint32_t)n;
        break;
    case TRL_FT_BITVEC:
    case TRL_FT_LOGICVEC:
        if ((rc = rd_uvarint(q, d, len, off, &n))) return rc;
        if (n > 0xFFFFFFFFull) FALLBACK(q, "vector too wide");
        a->width = (uint32_t)n;
        n = (n + 7) / 8;
        if (ft == TRL_FT_LOGICVEC) n *= 2;
        if (n > len - *off) FALLBACK(q, "truncated vector");
        a->len = (uint32_t)n;
        break;
    default:
        FALLBACK(q, "unknown field type %u", (unsigned)ft);
    }
    if (keep && a->len) {
        if (!grow((void **)&q->heap, &q->heap_cap, q->heap_len + a->len, 1)) return TRL_ERR_OOM;
        if (q->heap_len + a->len > 0xFFFFFFFFull) FALLBACK(q, "heap beyond 4 GB");
        memcpy(q->heap + q->heap_len, d + *off, a->len);
        a->off = (uint32_t)q->heap_len;
        q->heap_len += a->len;
    }
    *off += a->len;
    return TRL_QS_OK;
}

/* ----------------------------------------------------------------------- *
 * The join (Trace.scan)
 * ----------------------------------------------------------------------- */

static qs_txn_t *find_txn(trl_qscan_t *q, int64_t id) {
    int64_t *slot = map_find(&q->by_id, id);
    return slot ? &q->txns[*slot] : NULL;
}

static bool selected(trl_qscan_t *q, uint32_t stream, uint32_t type) {
    if (!q->streams_all && !map_find(&q->streams_sel, (int64_t)stream)) return false;
    if (!q->types_all && !map_find(&q->types_sel, (int64_t)type)) return false;
    return true;
}

/* new_txn: a repeated id keeps its first position (dict semantics) but
 * starts over with the new record's content. */
static int new_txn(trl_qscan_t *q, int64_t id, uint32_t stream, uint32_t type,
                   int64_t start, int64_t end, bool open, int64_t parent, qs_txn_t **out) {
    bool inserted;
    int64_t *slot = map_put(&q->by_id, id, (int64_t)q->ntxn, &inserted);
    if (!slot) return TRL_ERR_OOM;
    qs_txn_t *t;
    if (inserted) {
        if (!grow((void **)&q->txns, &q->txn_cap, q->ntxn + 1, sizeof(qs_txn_t))) return TRL_ERR_OOM;
        t = &q->txns[q->ntxn++];
    } else {
        q->duplicate_ids++;
        t = &q->txns[*slot];
    }
    t->id = id; t->stream = stream; t->type = type;
    t->start = start; t->end = end; t->open = open ? 1 : 0;
    t->parent = parent;
    t->a_head = t->a_tail = t->m_head = t->m_tail = t->l_head = t->l_tail = -1;
    *out = t;
    return TRL_QS_OK;
}

/* add_attrs -> normalize_field: the combinations where Python raises
 * while building the txn. */
static bool norm_raises(uint8_t norm, uint32_t ft) {
    if (norm == TRL_QS_NORM_ENUM)
        return ft == TRL_FT_F32 || ft == TRL_FT_F64 || ft == TRL_FT_STRING ||
               ft == TRL_FT_BYTES || ft == TRL_FT_BITVEC || ft == TRL_FT_LOGICVEC;
    if (norm == TRL_QS_NORM_LOGICVEC) return ft == TRL_FT_BITVEC;
    return false;
}

static int push_attr(trl_qscan_t *q, qs_txn_t *t, const trl_qs_attr_t *a) {
    if (t->type < q->catalog_cap && q->catalog[t->type].slots && a->field_idx < q->catalog[t->type].n &&
        norm_raises(q->catalog[t->type].norms[a->field_idx], a->ft))
        FALLBACK(q, "field value does not fit its declared kind");
    if (q->nattr >= 0x7FFFFFFF) FALLBACK(q, "too many attrs");
    if (!grow((void **)&q->attrs, &q->attr_cap, q->nattr + 1, sizeof(qs_attr_node_t))) return TRL_ERR_OOM;
    int32_t i = (int32_t)q->nattr++;
    q->attrs[i].a = *a;
    q->attrs[i].next = -1;
    if (t->a_tail >= 0) q->attrs[t->a_tail].next = i; else t->a_head = i;
    t->a_tail = i;
    return TRL_QS_OK;
}

static int push_meta(trl_qscan_t *q, qs_txn_t *t, uint32_t key, uint32_t val) {
    if (q->nmeta >= 0x7FFFFFFF) FALLBACK(q, "too many metas");
    if (!grow((void **)&q->metas, &q->meta_cap, q->nmeta + 1, sizeof(qs_meta_node_t))) return TRL_ERR_OOM;
    int32_t i = (int32_t)q->nmeta++;
    q->metas[i].m.key_sid = key;
    q->metas[i].m.val_sid = val;
    q->metas[i].next = -1;
    if (t->m_tail >= 0) q->metas[t->m_tail].next = i; else t->m_head = i;
    t->m_tail = i;
    return TRL_QS_OK;
}

static int push_link(trl_qscan_t *q, qs_txn_t *t, int64_t other, uint32_t label,
                     uint8_t kind, uint8_t dir) {
    if (q->nlink >= 0x7FFFFFFF) FALLBACK(q, "too many links");
    if (!grow((void **)&q->links, &q->link_cap, q->nlink + 1, sizeof(qs_link_node_t))) return TRL_ERR_OOM;
    int32_t i = (int32_t)q->nlink++;
    q->links[i].l.other = other;
    q->links[i].l.label_sid = label;
    q->links[i].l.kind = kind;
    q->links[i].l.dir = dir;
    q->links[i].l.pad = 0;
    q->links[i].next = -1;
    if (t->l_tail >= 0) q->links[t->l_tail].next = i; else t->l_head = i;
    t->l_tail = i;
    return TRL_QS_OK;
}

static int pending_add(trl_qscan_t *q, uint32_t stream, int64_t delta) {
    int64_t *v = map_put(&q->pending, (int64_t)stream, 0, NULL);
    if (!v) return TRL_ERR_OOM;
    *v += delta;
    return TRL_QS_OK;
}

static bool pending_any(const qs_map_t *m) {
    for (size_t i = 0; i < m->cap; ++i)
        if (m->used[i] && m->vals[i] != 0) return true;
    return false;
}

static bool pending_get(const qs_map_t *m, uint32_t stream) {
    int64_t *v = map_find(m, (int64_t)stream);
    return v && *v != 0;
}

/* Decodes one attr list (TR_FULL: types from the decoder schema; TR_ATTR:
 * a type byte per attr) and attaches it to t when t is selected. */
static int rd_attrs(trl_qscan_t *q, const uint8_t *d, size_t len, size_t *off,
                    uint32_t type_id, bool typed, qs_txn_t *t) {
    uint64_t count;
    int rc = rd_uvarint(q, d, len, off, &count);
    if (rc) return rc;
    const qs_schema_t *s = (type_id < q->schema_cap) ? &q->schemas[type_id] : NULL;
    for (uint64_t i = 0; i < count; ++i) {
        uint64_t idx;
        if ((rc = rd_uvarint(q, d, len, off, &idx))) return rc;
        if (idx > 0xFFFFFFFFull) FALLBACK(q, "field index beyond 32 bits");
        uint32_t ft;
        if (typed) {
            if (*off >= len) FALLBACK(q, "truncated attr");
            ft = d[(*off)++];
            if (!valid_ft(ft)) FALLBACK(q, "unknown field type %u", (unsigned)ft);
        } else {
            ft = (s && idx < s->n) ? s->fts[idx] : TRL_FT_U64;
        }
        trl_qs_attr_t a;
        if ((rc = rd_field(q, d, len, off, ft, t != NULL, &a))) return rc;
        a.field_idx = (uint32_t)idx;
        if (t && (rc = push_attr(q, t, &a))) return rc;
    }
    return TRL_QS_OK;
}

static int decode_rows(trl_qscan_t *q, const uint8_t *d, size_t len, bool delta,
                       int64_t *started) {
    if (len < 16) FALLBACK(q, "truncated TXN block");
    int rc;
    int64_t prev_time, prev_id = 0;
    if ((rc = as_i64(q, trl_read_le64(d), &prev_time, "block start"))) return rc;
    size_t off = 16;
    uint64_t count;
    if ((rc = rd_uvarint(q, d, len, &off, &count))) return rc;
    bool t_hi_on = (q->flags & TRL_QS_HAS_T_HI) != 0;
    bool want_links = (q->flags & TRL_QS_WANT_LINKS) != 0;
    bool want_meta = (q->flags & TRL_QS_WANT_META) != 0;

    for (uint64_t r = 0; r < count; ++r) {
        if (off >= len) FALLBACK(q, "truncated record");
        uint8_t tag = d[off++];
        switch (tag) {
        case TRL_TXN_FULL:
        case TRL_TXN_BEGIN: {
            uint64_t sid, tid, parent_u;
            int64_t id, start, end = 0, parent;
            if ((rc = rd_uvarint(q, d, len, &off, &sid))) return rc;
            if ((rc = rd_uvarint(q, d, len, &off, &tid))) return rc;
            if (sid > 0xFFFFFFFFull || tid > 0xFFFFFFFFull) FALLBACK(q, "stream or type id beyond 32 bits");
            if (delta) {
                int64_t did, dstart;
                if ((rc = rd_svarint(q, d, len, &off, &did))) return rc;
                if ((rc = rd_svarint(q, d, len, &off, &dstart))) return rc;
                if ((rc = add_i64(q, prev_id, did, &id))) return rc;
                if ((rc = add_i64(q, prev_time, dstart, &start))) return rc;
                if (tag == TRL_TXN_FULL) {
                    uint64_t dur;
                    int64_t dur_i;
                    if ((rc = rd_uvarint(q, d, len, &off, &dur))) return rc;
                    if ((rc = as_i64(q, dur, &dur_i, "duration"))) return rc;
                    if ((rc = add_i64(q, start, dur_i, &end))) return rc;
                }
                if ((rc = rd_uvarint(q, d, len, &off, &parent_u))) return rc;
            } else {
                size_t need = (tag == TRL_TXN_FULL) ? 32 : 24;
                if (off + need > len) FALLBACK(q, "truncated record");
                if ((rc = as_i64(q, trl_read_le64(d + off), &id, "txn id"))) return rc;
                if ((rc = as_i64(q, trl_read_le64(d + off + 8), &start, "time"))) return rc;
                if (tag == TRL_TXN_FULL) {
                    if ((rc = as_i64(q, trl_read_le64(d + off + 16), &end, "time"))) return rc;
                    parent_u = trl_read_le64(d + off + 24);
                } else {
                    parent_u = trl_read_le64(d + off + 16);
                }
                off += need;
            }
            if ((rc = as_i64(q, parent_u, &parent, "parent id"))) return rc;
            qs_txn_t *t = NULL;
            if (selected(q, (uint32_t)sid, (uint32_t)tid)) {
                if ((rc = new_txn(q, id, (uint32_t)sid, (uint32_t)tid, start, end,
                                  tag == TRL_TXN_BEGIN, parent, &t))) return rc;
                (*started)++;
                if (tag == TRL_TXN_BEGIN && t_hi_on && start <= q->t_hi)
                    if ((rc = pending_add(q, (uint32_t)sid, 1))) return rc;
            }
            if (tag == TRL_TXN_FULL) {
                if ((rc = rd_attrs(q, d, len, &off, (uint32_t)tid, false, t))) return rc;
                prev_id = id; prev_time = end;
            } else {
                prev_id = id; prev_time = start;
            }
            break;
        }
        case TRL_TXN_ATTR: {
            int64_t id;
            if (delta) {
                int64_t did;
                if ((rc = rd_svarint(q, d, len, &off, &did))) return rc;
                if ((rc = add_i64(q, prev_id, did, &id))) return rc;
            } else {
                if (off + 8 > len) FALLBACK(q, "truncated record");
                if ((rc = as_i64(q, trl_read_le64(d + off), &id, "txn id"))) return rc;
                off += 8;
            }
            qs_txn_t *t = find_txn(q, id);
            if ((rc = rd_attrs(q, d, len, &off, 0, true, t))) return rc;
            prev_id = id;
            break;
        }
        case TRL_TXN_END: {
            int64_t id, end;
            if (delta) {
                int64_t did, dend;
                if ((rc = rd_svarint(q, d, len, &off, &did))) return rc;
                if ((rc = rd_svarint(q, d, len, &off, &dend))) return rc;
                if ((rc = add_i64(q, prev_id, did, &id))) return rc;
                if ((rc = add_i64(q, prev_time, dend, &end))) return rc;
            } else {
                if (off + 16 > len) FALLBACK(q, "truncated record");
                if ((rc = as_i64(q, trl_read_le64(d + off), &id, "txn id"))) return rc;
                if ((rc = as_i64(q, trl_read_le64(d + off + 8), &end, "time"))) return rc;
                off += 16;
            }
            qs_txn_t *t = find_txn(q, id);
            if (t && t->open) {
                t->end = end;
                t->open = 0;
                if (t_hi_on && t->start <= q->t_hi)
                    if ((rc = pending_add(q, t->stream, -1))) return rc;
            }
            prev_id = id;
            if (delta) prev_time = end;
            break;
        }
        case TRL_TXN_LINK: {
            if (off >= len) FALLBACK(q, "truncated record");
            uint8_t kind = d[off++];
            if (kind != 0x00 && kind != 0x01 && kind != 0x02 && kind != 0xFF)
                FALLBACK(q, "unknown link type %u", (unsigned)kind);
            int64_t src, tgt;
            uint64_t label;
            if (delta) {
                int64_t dsrc;
                uint64_t tgt_u;
                if ((rc = rd_svarint(q, d, len, &off, &dsrc))) return rc;
                if ((rc = rd_uvarint(q, d, len, &off, &tgt_u))) return rc;
                if ((rc = add_i64(q, prev_id, dsrc, &src))) return rc;
                if ((rc = as_i64(q, tgt_u, &tgt, "txn id"))) return rc;
            } else {
                if (off + 16 > len) FALLBACK(q, "truncated record");
                if ((rc = as_i64(q, trl_read_le64(d + off), &src, "txn id"))) return rc;
                if ((rc = as_i64(q, trl_read_le64(d + off + 8), &tgt, "txn id"))) return rc;
                off += 16;
            }
            if ((rc = rd_uvarint(q, d, len, &off, &label))) return rc;
            if (want_links) {
                if (label > 0xFFFFFFFFull) FALLBACK(q, "string id beyond 32 bits");
                qs_txn_t *s = find_txn(q, src);
                if (s && (rc = push_link(q, s, tgt, (uint32_t)label, kind, 0))) return rc;
                qs_txn_t *g = find_txn(q, tgt);
                if (g && (rc = push_link(q, g, src, (uint32_t)label, kind, 1))) return rc;
            }
            prev_id = src;
            break;
        }
        case TRL_TXN_META: {
            int64_t id;
            uint64_t key, val;
            if (delta) {
                int64_t did;
                if ((rc = rd_svarint(q, d, len, &off, &did))) return rc;
                if ((rc = add_i64(q, prev_id, did, &id))) return rc;
            } else {
                if (off + 8 > len) FALLBACK(q, "truncated record");
                if ((rc = as_i64(q, trl_read_le64(d + off), &id, "txn id"))) return rc;
                off += 8;
            }
            if ((rc = rd_uvarint(q, d, len, &off, &key))) return rc;
            if ((rc = rd_uvarint(q, d, len, &off, &val))) return rc;
            if (want_meta) {
                qs_txn_t *t = find_txn(q, id);
                if (t) {
                    if (key > 0xFFFFFFFFull || val > 0xFFFFFFFFull)
                        FALLBACK(q, "string id beyond 32 bits");
                    if ((rc = push_meta(q, t, (uint32_t)key, (uint32_t)val))) return rc;
                }
            }
            prev_id = id;
            break;
        }
        default:
            FALLBACK(q, "unknown record tag 0x%02x", tag);
        }
    }
    return TRL_QS_OK;
}

static int read_block(trl_qscan_t *q, uint64_t offset, uint8_t *type, uint8_t *flags,
                      size_t *plen) {
    uint8_t hdr[TRL_BLOCK_HEADER_SIZE];
    if (offset > (uint64_t)INT64_MAX) FALLBACK(q, "block offset beyond int64");
    if (qs_fseek(q->fp, (off_t)offset, SEEK_SET) != 0) FALLBACK(q, "seek failed");
    if (fread(hdr, 1, sizeof(hdr), q->fp) != sizeof(hdr)) FALLBACK(q, "truncated block header");
    *type = hdr[0];
    *flags = hdr[1];
    uint64_t total = trl_read_le64(hdr + 2);
    if (total < TRL_BLOCK_HEADER_SIZE) FALLBACK(q, "bad block length");
    uint64_t n = total - TRL_BLOCK_HEADER_SIZE;
    if (n > ((uint64_t)1 << 40)) FALLBACK(q, "block too large");
    if (!grow((void **)&q->raw, &q->raw_cap, (size_t)n + 1, 1)) return TRL_ERR_OOM;
    if (n && fread(q->raw, 1, (size_t)n, q->fp) != (size_t)n) FALLBACK(q, "truncated block");
    *plen = (size_t)n;
    return TRL_QS_OK;
}

static int inflate_block(trl_qscan_t *q, size_t plen, const uint8_t **data, size_t *len) {
    size_t cap = plen * 8 + 256;
    for (;;) {
        if (!grow((void **)&q->dec, &q->dec_cap, cap, 1)) return TRL_ERR_OOM;
        uLongf actual = (uLongf)q->dec_cap;
        int zrc = uncompress(q->dec, &actual, q->raw, (uLong)plen);
        if (zrc == Z_OK) { *data = q->dec; *len = (size_t)actual; return TRL_QS_OK; }
        if (zrc != Z_BUF_ERROR) FALLBACK(q, "zlib error %d", zrc);
        cap = q->dec_cap * 2;
    }
}

/* ----------------------------------------------------------------------- *
 * Filter evaluation
 * ----------------------------------------------------------------------- */

static const qs_catalog_t *cat_of(const trl_qscan_t *q, uint32_t type) {
    if (type < q->catalog_cap && q->catalog[type].slots) return &q->catalog[type];
    return NULL;
}

static qs_val_t mk(uint8_t tag, int64_t v) { qs_val_t r; r.tag = tag; r.v = v; r.aux = 0; return r; }

/* Big-endian bytes to int64, or false when the value is >= 2^63. */
static bool be_i64(const uint8_t *p, size_t n, int64_t *out) {
    uint64_t v = 0;
    for (size_t i = 0; i < n; ++i) {
        if (v >> 56) return false;
        v = (v << 8) | p[i];
    }
    if (v > (uint64_t)INT64_MAX) return false;
    *out = (int64_t)v;
    return true;
}

static bool be_zero(const uint8_t *p, size_t n) {
    for (size_t i = 0; i < n; ++i) if (p[i]) return false;
    return true;
}

/* The query value of an attr (normalize_field), as a C value. A value
 * Python holds as a tuple (a raw bitvec/logicvec) sets err_possible: unary
 * operators, min/max and bit() raise on it in Python, so the row must not
 * be dropped by a sibling term. */
static qs_val_t classify(trl_qscan_t *q, const qs_txn_t *t, const trl_qs_attr_t *a, uint8_t norm) {
    int64_t v;
    switch (a->ft) {
    case TRL_FT_U8: case TRL_FT_U16: case TRL_FT_U32: case TRL_FT_U64:
    case TRL_FT_TIME: case TRL_FT_ENUM:
        if (a->v > (uint64_t)INT64_MAX) return mk(V_DEFER, 0);
        v = (int64_t)a->v;
        break;
    case TRL_FT_I8: case TRL_FT_I16: case TRL_FT_I32: case TRL_FT_I64:
        v = (int64_t)a->v;
        break;
    case TRL_FT_BOOL:
        v = a->v ? 1 : 0;
        if (norm != TRL_QS_NORM_ENUM) return mk(V_BOOL, v);    /* Python bool */
        break;
    case TRL_FT_BITVEC:
        if (norm != TRL_QS_NORM_BITVEC) { q->err_possible = true; return mk(V_DEFER, 0); }
        if (!be_i64(q->heap + a->off, a->len, &v)) return mk(V_DEFER, 0);
        return mk(V_INT, v);
    case TRL_FT_LOGICVEC: {
        size_t h = a->len / 2;
        const uint8_t *av = q->heap + a->off, *bv = av + h;
        if (norm == TRL_QS_NORM_BITVEC) {
            if (!be_i64(av, h, &v)) return mk(V_DEFER, 0);
            return mk(V_INT, v);
        }
        if (norm != TRL_QS_NORM_LOGICVEC) { q->err_possible = true; return mk(V_DEFER, 0); }
        if (!be_zero(bv, h)) return mk(V_LXZ, 0);
        if (!be_i64(av, h, &v)) return mk(V_DEFER, 0);
        return mk(V_INT, v);
    }
    default:
        return mk(V_DEFER, 0);      /* floats, strings, bytes */
    }
    if (norm == TRL_QS_NORM_ENUM) {
        qs_val_t r = mk(V_ENUM, v);
        r.aux = ((uint64_t)t->type << 32) | a->field_idx;
        return r;
    }
    return mk(V_INT, v);
}

static int id_test(const trl_qscan_t *q, int64_t setoff, uint32_t id);

/* LOAD/HAS: the last attr whose field maps to `slot` (dict overwrite).
 * typeset >= 0 (LOADT/HAST, `T.f`): MISSING unless the txn's type is in it. */
static qs_val_t load_slot(trl_qscan_t *q, const qs_txn_t *t, int64_t slot, bool has,
                          int64_t typeset) {
    if (typeset >= 0 && id_test(q, typeset, t->type) != TRI_TRUE)
        return has ? mk(V_BOOL, 0) : mk(V_MISS, 0);
    const qs_catalog_t *c = cat_of(q, t->type);
    const trl_qs_attr_t *hit = NULL;
    uint8_t norm = 0;
    for (int32_t i = t->a_head; i >= 0; i = q->attrs[i].next) {
        const trl_qs_attr_t *a = &q->attrs[i].a;
        if (!c || a->field_idx >= c->n) {
            if (q->strict_unmapped) { q->err_possible = true; return mk(V_DEFER, 0); }
            continue;
        }
        if (c->slots[a->field_idx] == slot) { hit = a; norm = c->norms[a->field_idx]; }
    }
    if (has) return mk(V_BOOL, hit != NULL);
    if (!hit) return mk(V_MISS, 0);
    return classify(q, t, hit, norm);
}

static int truth(const qs_val_t *v) {
    switch (v->tag) {
    case V_MISS:  return TRI_FALSE;
    case V_INT:
    case V_BOOL:
    case V_ENUM:  return v->v != 0;
    case V_LXZ:   return TRI_TRUE;       /* LogicVal has no __bool__ */
    default:      return TRI_DEFER;
    }
}

/* Comparisons, not/and/or, has() give Python bools. */
static qs_val_t tri_val(int t) { return t == TRI_DEFER ? mk(V_DEFER, 0) : mk(V_BOOL, t); }

/* _num(): MISSING for MISSING, LogicVal (and str/bytes, which are DEFER here). */
static bool is_num_missing(const qs_val_t *v) { return v->tag == V_MISS || v->tag == V_LXZ; }

static int64_t floordiv(int64_t a, int64_t b) {
    int64_t qt = a / b, r = a % b;
    if (r != 0 && ((r < 0) != (b < 0))) qt--;
    return qt;
}

static int64_t pymod(int64_t a, int64_t b) {
    if (b == -1) return 0;
    int64_t r = a % b;
    if (r != 0 && ((r < 0) != (b < 0))) r += b;
    return r;
}

static int64_t asr(int64_t x, int64_t n) {
    if (n >= 63) return x < 0 ? -1 : 0;
    return x < 0 ? ~((~x) >> n) : x >> n;
}

static qs_val_t arith(int op, qs_val_t a, qs_val_t b) {
    if (is_num_missing(&a) || is_num_missing(&b)) return mk(V_MISS, 0);
    if (a.tag == V_DEFER || b.tag == V_DEFER) return mk(V_DEFER, 0);
    int64_t x = a.v, y = b.v, r;
    switch (op) {
    case A_ADD: if (__builtin_add_overflow(x, y, &r)) return mk(V_DEFER, 0); return mk(V_INT, r);
    case A_SUB: if (__builtin_sub_overflow(x, y, &r)) return mk(V_DEFER, 0); return mk(V_INT, r);
    case A_MUL: if (__builtin_mul_overflow(x, y, &r)) return mk(V_DEFER, 0); return mk(V_INT, r);
    case A_DIV:
        if (y == 0) return mk(V_MISS, 0);
        if (x == INT64_MIN && y == -1) return mk(V_DEFER, 0);
        return mk(V_INT, floordiv(x, y));
    case A_MOD:
        if (y == 0) return mk(V_MISS, 0);
        return mk(V_INT, pymod(x, y));
    case A_AND: return mk(V_INT, x & y);
    case A_OR:  return mk(V_INT, x | y);
    case A_XOR: return mk(V_INT, x ^ y);
    case A_SHL:
        if (y < 0) return mk(V_MISS, 0);
        if (x == 0) return mk(V_INT, 0);
        if (y >= 63) return mk(V_DEFER, 0);
        if (x > (INT64_MAX >> y) || x < (INT64_MIN >> y)) return mk(V_DEFER, 0);
        return mk(V_INT, (int64_t)((uint64_t)x << y));
    case A_SHR:
        if (y < 0) return mk(V_MISS, 0);
        return mk(V_INT, asr(x, y));
    }
    return mk(V_DEFER, 0);
}

static int compare(int op, const qs_val_t *a, const qs_val_t *b) {
    if (op == C_EQ || op == C_NE) {
        if (a->tag == V_MISS || b->tag == V_MISS) return TRI_FALSE;
        if (a->tag == V_DEFER || b->tag == V_DEFER) return TRI_DEFER;
        int eq;
        if (a->tag == V_LXZ || b->tag == V_LXZ) {
            if (a->tag == V_LXZ && b->tag == V_LXZ) return TRI_DEFER;
            eq = 0;
        } else {
            eq = a->v == b->v;
        }
        return op == C_EQ ? eq : !eq;
    }
    if (is_num_missing(a) || is_num_missing(b)) return TRI_FALSE;
    if (a->tag == V_DEFER || b->tag == V_DEFER) return TRI_DEFER;
    switch (op) {
    case C_LT: return a->v < b->v;
    case C_LE: return a->v <= b->v;
    case C_GT: return a->v > b->v;
    default:   return a->v >= b->v;
    }
}

/* Label set: [n, r_int, r_lxz, r_miss, r_unknown, (key, raw, res) * n] */
static int label_test(const trl_qscan_t *q, int64_t setoff, const qs_val_t *v) {
    const int64_t *s = q->sets + setoff;
    switch (v->tag) {
    case V_MISS:  return (int)s[3];
    case V_LXZ:   return (int)s[2];
    case V_INT:
    case V_BOOL:  return (int)s[1];
    case V_DEFER: return TRI_DEFER;
    default: break;
    }
    int64_t n = s[0];
    for (int64_t i = 0; i < n; ++i) {
        const int64_t *e = s + 5 + 3 * i;
        if ((uint64_t)e[0] == v->aux && e[1] == v->v) return (int)e[2];
    }
    return (int)s[4];
}

/* Id set: [n, (id, res) * n]; an id Python did not list is DEFER (a
 * LOADT type set lists only its own ids; others are then not TRUE). */
static int id_test(const trl_qscan_t *q, int64_t setoff, uint32_t id) {
    const int64_t *s = q->sets + setoff;
    int64_t n = s[0];
    for (int64_t i = 0; i < n; ++i)
        if (s[1 + 2 * i] == (int64_t)id) return (int)s[2 + 2 * i];
    return TRI_DEFER;
}

/* Runs the program at `entry` on t and returns its value (DEFER when
 * Python's answer is in doubt). */
static qs_val_t run_prog(trl_qscan_t *q, const qs_txn_t *t, int64_t entry) {
    const int64_t *c = q->code;
    qs_val_t *st = q->stack;
    size_t sp = 0, pc = (size_t)entry;
    q->err_possible = false;
    while (pc < q->ncode) {
        int64_t op = c[pc++];
        switch (op) {
        case OP_END: {
            if (q->err_possible || sp != 1) return mk(V_DEFER, 0);
            return st[0];
        }
        case OP_INT:   st[sp++] = mk(V_INT, c[pc++]); break;
        case OP_MISS:  st[sp++] = mk(V_MISS, 0); break;
        case OP_DEFER: st[sp++] = mk(V_DEFER, 0); break;
        case OP_LOAD:  st[sp] = load_slot(q, t, c[pc++], false, -1); sp++; break;
        case OP_HAS:   st[sp] = load_slot(q, t, c[pc++], true, -1); sp++; break;
        case OP_LOADT:
        case OP_HAST: {
            int64_t slot = c[pc++], ts = c[pc++];
            st[sp] = load_slot(q, t, slot, op == OP_HAST, ts);
            sp++;
            break;
        }
        case OP_BUILTIN: {
            int64_t b = c[pc++];
            qs_val_t v = mk(V_INT, 0);
            switch (b) {
            case B_ID:     v.v = t->id; break;
            case B_START:  v.v = t->start; break;
            case B_END:    if (t->open) v = mk(V_MISS, 0); else v.v = t->end; break;
            case B_DUR:
                if (t->open) v = mk(V_MISS, 0);
                else if (__builtin_sub_overflow(t->end, t->start, &v.v)) v = mk(V_DEFER, 0);
                break;
            case B_PARENT: if (t->parent == 0) v = mk(V_MISS, 0); else v.v = t->parent; break;
            case B_OPEN:   v = mk(V_BOOL, t->open); break;
            case B_NLINKS: {
                int64_t n = 0;
                for (int32_t i = t->l_head; i >= 0; i = q->links[i].next) n++;
                v.v = n;
                break;
            }
            default: v = mk(V_DEFER, 0); break;
            }
            st[sp++] = v;
            break;
        }
        case OP_UN: {
            int64_t u = c[pc++];
            qs_val_t *a = &st[sp - 1];
            switch (u) {
            case U_NOT: { int tv = truth(a); *a = tri_val(tv == TRI_DEFER ? TRI_DEFER : !tv); break; }
            case U_NOTMISS:
                *a = a->tag == V_MISS ? mk(V_BOOL, 0) : a->tag == V_DEFER ? mk(V_DEFER, 0) : mk(V_BOOL, 1);
                break;
            default:
                if (is_num_missing(a)) { *a = mk(V_MISS, 0); break; }
                if (a->tag == V_DEFER) break;
                if (u == U_POS) break;                     /* keeps an EnumVal or bool */
                if (u == U_INV) { *a = mk(V_INT, ~a->v); break; }
                if (a->v == INT64_MIN) { *a = mk(V_DEFER, 0); break; }
                if (u == U_NEG) *a = mk(V_INT, -a->v);
                else *a = mk(V_INT, a->v < 0 ? -a->v : a->v);   /* U_ABS */
                break;
            }
            break;
        }
        case OP_BIN: {
            int64_t o = c[pc++];
            qs_val_t b = st[--sp];
            st[sp - 1] = arith((int)o, st[sp - 1], b);
            break;
        }
        case OP_CMP: {
            int64_t o = c[pc++];
            qs_val_t b = st[--sp];
            st[sp - 1] = tri_val(compare((int)o, &st[sp - 1], &b));
            break;
        }
        case OP_AND:
        case OP_OR: {
            int y = truth(&st[--sp]);
            int x = truth(&st[sp - 1]);
            int r;
            if (op == OP_AND)
                r = (x == TRI_FALSE || y == TRI_FALSE) ? TRI_FALSE
                  : (x == TRI_TRUE && y == TRI_TRUE) ? TRI_TRUE : TRI_DEFER;
            else
                r = (x == TRI_TRUE || y == TRI_TRUE) ? TRI_TRUE
                  : (x == TRI_FALSE && y == TRI_FALSE) ? TRI_FALSE : TRI_DEFER;
            st[sp - 1] = tri_val(r);
            break;
        }
        case OP_LABEL: {
            int64_t so = c[pc++];
            st[sp - 1] = tri_val(label_test(q, so, &st[sp - 1]));
            break;
        }
        case OP_IDTEST: {
            int64_t which = c[pc++], so = c[pc++];
            st[sp++] = tri_val(id_test(q, so, which == 0 ? t->stream : t->type));
            break;
        }
        case OP_MINMAX: {
            int64_t ismax = c[pc++], n = c[pc++];
            qs_val_t *vs = &st[sp - n];
            qs_val_t r = vs[0];
            bool miss = false, defer = false;
            for (int64_t i = 0; i < n; ++i) {
                if (is_num_missing(&vs[i])) miss = true;
                else if (vs[i].tag == V_DEFER) defer = true;
            }
            if (miss) r = mk(V_MISS, 0);
            else if (defer) r = mk(V_DEFER, 0);
            else
                for (int64_t i = 1; i < n; ++i)
                    if (ismax ? vs[i].v > r.v : vs[i].v < r.v) r = vs[i];
            sp -= (size_t)n;
            st[sp++] = r;
            break;
        }
        case OP_BIT: {
            qs_val_t n = st[--sp], x = st[sp - 1];
            if (is_num_missing(&x) || is_num_missing(&n)) { st[sp - 1] = mk(V_MISS, 0); break; }
            if (x.tag == V_DEFER || n.tag == V_DEFER || n.v < 0) {
                q->err_possible = true;               /* Python may raise here */
                st[sp - 1] = mk(V_DEFER, 0);
                break;
            }
            st[sp - 1] = mk(V_INT, asr(x.v, n.v) & 1);
            break;
        }
        case OP_BITS: {
            qs_val_t l = st[--sp], h = st[--sp], x = st[sp - 1];
            if (is_num_missing(&x) || is_num_missing(&h) || is_num_missing(&l)) {
                st[sp - 1] = mk(V_MISS, 0); break;
            }
            if (x.tag == V_DEFER || h.tag == V_DEFER || l.tag == V_DEFER) {
                q->err_possible = true;
                st[sp - 1] = mk(V_DEFER, 0); break;
            }
            if (h.v < l.v) { st[sp - 1] = mk(V_MISS, 0); break; }
            int64_t w;
            if (l.v < 0 || __builtin_sub_overflow(h.v, l.v, &w) || w >= INT64_MAX - 1) {
                q->err_possible = true;
                st[sp - 1] = mk(V_DEFER, 0); break;
            }
            w += 1;
            if (w > QS_MAX_SHIFT) { st[sp - 1] = mk(V_MISS, 0); break; }   /* _expr.MAX_SHIFT */
            int64_t sh = asr(x.v, l.v);
            if (w >= 63) st[sp - 1] = sh >= 0 ? mk(V_INT, sh) : mk(V_DEFER, 0);
            else st[sp - 1] = mk(V_INT, sh & (int64_t)(((uint64_t)1 << w) - 1));
            break;
        }
        default:
            return mk(V_DEFER, 0);
        }
    }
    return mk(V_DEFER, 0);
}

/* The filter: TRI_FALSE / TRI_TRUE / TRI_DEFER. */
static int eval(trl_qscan_t *q, const qs_txn_t *t) {
    qs_val_t v = run_prog(q, t, q->filter_entry);
    return truth(&v);
}

/* ----------------------------------------------------------------------- *
 * Output
 * ----------------------------------------------------------------------- */

static int emit(trl_qscan_t *q, const qs_txn_t *t, bool defer) {
    if (!grow((void **)&q->out_rows, &q->out_row_cap, q->out_nrow + 1, sizeof(trl_qs_row_t)))
        return TRL_ERR_OOM;
    trl_qs_row_t *r = &q->out_rows[q->out_nrow++];
    memset(r, 0, sizeof(*r));
    r->id = t->id; r->start = t->start; r->end = t->open ? 0 : t->end; r->parent = t->parent;
    r->stream = t->stream; r->type = t->type;
    r->flags = (t->open ? TRL_QS_ROW_OPEN : 0) | (defer ? TRL_QS_ROW_DEFER : 0);
    r->attr_first = (uint32_t)q->out_nattr;
    for (int32_t i = t->a_head; i >= 0; i = q->attrs[i].next) {
        if (!grow((void **)&q->out_attrs, &q->out_attr_cap, q->out_nattr + 1, sizeof(trl_qs_attr_t)))
            return TRL_ERR_OOM;
        q->out_attrs[q->out_nattr++] = q->attrs[i].a;
    }
    r->attr_n = (uint32_t)(q->out_nattr - r->attr_first);
    r->meta_first = (uint32_t)q->out_nmeta;
    for (int32_t i = t->m_head; i >= 0; i = q->metas[i].next) {
        if (!grow((void **)&q->out_metas, &q->out_meta_cap, q->out_nmeta + 1, sizeof(trl_qs_meta_t)))
            return TRL_ERR_OOM;
        q->out_metas[q->out_nmeta++] = q->metas[i].m;
    }
    r->meta_n = (uint32_t)(q->out_nmeta - r->meta_first);
    r->link_first = (uint32_t)q->out_nlink;
    for (int32_t i = t->l_head; i >= 0; i = q->links[i].next) {
        if (!grow((void **)&q->out_links, &q->out_link_cap, q->out_nlink + 1, sizeof(trl_qs_link_t)))
            return TRL_ERR_OOM;
        q->out_links[q->out_nlink++] = q->links[i].l;
    }
    r->link_n = (uint32_t)(q->out_nlink - r->link_first);
    if (defer) q->n_defer++; else q->n_keep++;
    return TRL_QS_OK;
}

static int proj_push(trl_qscan_t *q, const qs_txn_t *t, const qs_val_t *vals) {
    size_t n = q->p_n, cap = q->p_cap;
    if (n + 1 > cap) {
        size_t nc = cap ? cap * 2 : 1024;
#define QS_GROW(f, T) do { T *np = (T *)realloc(q->f, nc * sizeof(T)); \
                           if (!np) return TRL_ERR_OOM; q->f = np; } while (0)
        QS_GROW(p_id, int64_t); QS_GROW(p_start, int64_t); QS_GROW(p_end, int64_t);
        QS_GROW(p_parent, int64_t); QS_GROW(p_stream, uint32_t); QS_GROW(p_type, uint32_t);
        QS_GROW(p_open, uint8_t);
#undef QS_GROW
        q->p_cap = nc;
    }
    q->p_id[n] = t->id; q->p_start[n] = t->start; q->p_end[n] = t->open ? 0 : t->end;
    q->p_parent[n] = t->parent; q->p_stream[n] = t->stream; q->p_type[n] = t->type;
    q->p_open[n] = t->open;
    if (q->nvals) {
        size_t need = (n + 1) * q->nvals;
        if (need > q->p_vcap) {
            size_t nc = q->p_vcap ? q->p_vcap * 2 : 1024 * q->nvals;
            while (nc < need) nc *= 2;
            uint8_t *a = (uint8_t *)realloc(q->p_vtag, nc);
            if (!a) return TRL_ERR_OOM;
            q->p_vtag = a;
            int64_t *b = (int64_t *)realloc(q->p_val, nc * sizeof(int64_t));
            if (!b) return TRL_ERR_OOM;
            q->p_val = b;
            uint64_t *c = (uint64_t *)realloc(q->p_aux, nc * sizeof(uint64_t));
            if (!c) return TRL_ERR_OOM;
            q->p_aux = c;
            q->p_vcap = nc;
        }
        for (size_t k = 0; k < q->nvals; ++k) {
            size_t j = n * q->nvals + k;
            q->p_vtag[j] = vals[k].tag;
            q->p_val[j] = vals[k].v;
            q->p_aux[j] = vals[k].aux;
        }
    }
    q->p_n = n + 1;
    q->n_keep++;
    return TRL_QS_OK;
}

/* ----------------------------------------------------------------------- *
 * Run
 * ----------------------------------------------------------------------- */

int trl_qscan_run(trl_qscan_t *q) {
    if (!q || !q->fp) return TRL_ERR_INVALID;
    clear_results(q);
    int rc;
    int64_t started = 0;
    bool skip_on = (q->flags & TRL_QS_HAS_T_HI) && !(q->flags & TRL_QS_WANT_LINKS);

    for (size_t b = 0; b < q->plan_n; ++b) {
        /* _iter_records: a block that starts after t_hi can only matter for
         * txns of its stream still open at t_hi. */
        if (skip_on && q->plan_start[b] > q->t_hi) {
            if (q->plan_sid[b] == TRL_STREAM_MIXED) {
                if (!pending_any(&q->pending)) continue;
            } else if (!pending_get(&q->pending, q->plan_sid[b])) {
                continue;
            }
        }
        uint8_t type, flags;
        size_t plen;
        if ((rc = read_block(q, q->plan_off[b], &type, &flags, &plen))) return rc;
        if (type != TRL_BLK_TXN_DATA) FALLBACK(q, "index points at block type 0x%02x", type);
        q->blocks_read++;
        if (flags & (TRL_FLAG_COMPRESS_ALG | TRL_FLAG_TXN_COLUMN))
            FALLBACK(q, "block layout not handled in C (flags 0x%02x)", flags);
        const uint8_t *data = q->raw;
        size_t len = plen;
        if (flags & TRL_FLAG_COMPRESSED)
            if ((rc = inflate_block(q, plen, &data, &len))) return rc;
        if ((rc = decode_rows(q, data, len, (flags & TRL_FLAG_TXN_DELTA) != 0, &started))) return rc;
        if ((q->flags & TRL_QS_HAS_MAX) && started >= q->max_txns) break;
    }

    bool filter = (q->flags & TRL_QS_FILTER) != 0;
    bool project = (q->flags & TRL_QS_PROJECT) != 0;
    qs_val_t vals[64];
    if (q->nvals > 64) FALLBACK(q, "too many projected values");
    for (size_t i = 0; i < q->ntxn; ++i) {
        const qs_txn_t *t = &q->txns[i];
        bool defer = false;
        if (filter) {
            if ((q->flags & TRL_QS_HAS_HI) && t->start > q->hi) continue;
            if ((q->flags & TRL_QS_HAS_LO) && !t->open && t->end < q->lo) continue;
            if (q->ncode && q->filter_entry >= 0) {
                int r = eval(q, t);
                if (r == TRI_FALSE) continue;
                defer = (r == TRI_DEFER);
            }
        }
        if (project && !defer) {
            /* A row whose values C cannot give exactly goes out in full;
             * the caller computes them (and the filter is already TRUE). */
            bool full = false;
            for (size_t k = 0; k < q->nvals; ++k) {
                vals[k] = run_prog(q, t, q->val_entries[k]);
                if (vals[k].tag == V_DEFER || vals[k].tag == V_LXZ) full = true;
            }
            if (!full) {
                if ((rc = proj_push(q, t, vals))) return rc;
                continue;
            }
        }
        if ((rc = emit(q, t, defer))) return rc;
    }
    /* The output arrays hold everything the caller reads (the heap is
     * shared); free the join state before the caller builds its objects. */
    free(q->txns);  q->txns = NULL;  q->txn_cap = 0;  q->ntxn = 0;
    free(q->attrs); q->attrs = NULL; q->attr_cap = 0; q->nattr = 0;
    free(q->metas); q->metas = NULL; q->meta_cap = 0; q->nmeta = 0;
    free(q->links); q->links = NULL; q->link_cap = 0; q->nlink = 0;
    map_free(&q->by_id);
    map_free(&q->pending);
    return TRL_QS_OK;
}
