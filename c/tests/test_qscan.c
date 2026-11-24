/*
 * test_qscan.c — the query scan engine (trl/trl_qscan.h, query CLI Q4) on
 * its own, without Python. The Python differential tests
 * (tests/query/test_cengine.py) are what check it against the reference
 * engine; this harness checks the C API end to end: plan from the index,
 * cross-block join, a filter program (keep/defer), and the fallback status.
 *
 * Usage: test_qscan <out_path>
 */

#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include "trl/trl_rec.h"
#include "trl/trl_qscan.h"
#include "trl_internal.h"

static int g_failures = 0;

#define EXPECT(cond, msg) do { \
    if (!(cond)) { \
        fprintf(stderr, "    EXPECT failed: %s  (%s:%d)\n", msg, __FILE__, __LINE__); \
        g_failures++; \
    } \
} while (0)

/* Bytecode words (qscan.c / query/_cengine.py) */
enum { OP_END = 0, OP_INT = 1, OP_DEFER = 3, OP_LOAD = 4, OP_CMP = 9 };
enum { C_GT = 4 };

static int cmp_off(const void *a, const void *b) {
    const trl_index_entry_t *x = a, *y = b;
    return x->offset < y->offset ? -1 : x->offset > y->offset;
}

/* Index entries in file order, as Trace._block_plan gives them. */
static void set_plan(trl_qscan_t *q, trl_reader_t *r) {
    size_t n = r->index.txn_count;
    trl_index_entry_t *e = malloc(n * sizeof(*e));
    memcpy(e, r->index.txn_entries, n * sizeof(*e));
    qsort(e, n, sizeof(*e), cmp_off);
    int64_t *st = malloc(n * sizeof(int64_t));
    uint64_t *off = malloc(n * sizeof(uint64_t));
    uint32_t *sid = malloc(n * sizeof(uint32_t));
    for (size_t i = 0; i < n; ++i) {
        st[i] = (int64_t)e[i].start; off[i] = e[i].offset; sid[i] = e[i].stream_inst_id;
    }
    EXPECT(trl_qscan_set_plan(q, n, st, off, sid) == TRL_OK, "set_plan");
    free(e); free(st); free(off); free(sid);
}

static void configure(trl_qscan_t *q, trl_reader_t *r, uint32_t type_id) {
    trl_qscan_reset(q);
    set_plan(q, r);
    int32_t slots[3] = {0, -1, -1};          /* field a is slot 0 */
    uint8_t norms[3] = {0, 0, 0};
    EXPECT(trl_qscan_set_catalog(q, type_id, 3, slots, norms) == TRL_OK, "set_catalog");
}

int main(int argc, char **argv) {
    if (argc < 2) {
        fprintf(stderr, "usage: %s <out_path>\n", argv[0]);
        return 2;
    }
    const char *path = argv[1];
    uint64_t long_id;

    /* --- write: 20 one-shot txns (a = 0..19) and one begin/set/end that
     *     auto-flush splits across blocks (a = 100). --- */
    {
        trl_db_cfg_t cfg = {0};
        cfg.path = path;
        cfg.timescale_exp = -9;
        cfg.compress = TRL_COMPRESS_ZLIB;
        cfg.block_bytes = 64;
        trl_db_t *db = trl_db_open("qscan", &cfg);
        EXPECT(db != NULL, "db_open");
        if (!db) return 1;
        trl_rtype_t *t = trl_type_new(db, "pkt", NULL);
        trl_type_add_field(t, "a", TRL_FT_U32, 0, NULL);
        trl_type_add_field(t, "b", TRL_FT_U32, 0, NULL);
        trl_type_add_field(t, "c", TRL_FT_U32, 0, NULL);
        EXPECT(trl_type_seal(t) == TRL_OK, "seal");
        trl_stream_t *s = trl_stream_new(db, NULL, "s", NULL, t, NULL);
        trl_txn_t *x = trl_rec_begin(s, t, 5, 0);
        long_id = trl_rec_id(x);
        for (uint64_t i = 0; i < 20; ++i) {
            uint64_t vals[3] = {i, 0, 0};
            trl_rec_full(s, t, 10 + i, 12 + i, 0, vals, 3);
        }
        trl_rec_set_u64(x, 0, 100);
        trl_rec_end(x, 500);
        EXPECT(trl_db_close(db) == TRL_OK, "db_close");
    }

    trl_reader_t *r = trl_reader_open(path);
    EXPECT(r != NULL, "reader_open");
    if (!r) return 1;
    EXPECT(r->index.txn_count > 1, "trace spans several TXN blocks");
    trl_qscan_t *q = trl_qscan_new(path);
    EXPECT(q != NULL, "qscan_new");
    uint32_t type_id = 0;
    for (uint32_t id = 1; id <= (uint32_t)r->typereg.txn_count; ++id) {
        const trl_txn_schema_entry_t *e = trl_type_registry_get_txn_schema(&r->typereg, id);
        if (!e) continue;
        uint8_t fts[16];
        for (uint32_t f = 0; f < e->field_count && f < 16; ++f) fts[f] = (uint8_t)e->fields[f].field_type;
        EXPECT(trl_qscan_set_schema(q, id, e->field_count, fts) == TRL_OK, "set_schema");
        const char *nm = trl_string_table_lookup(&r->strtab, e->name_id);
        if (nm && strcmp(nm, "pkt") == 0) type_id = id;
    }
    EXPECT(type_id != 0, "pkt schema");

    const trl_qs_row_t *rows;
    const trl_qs_attr_t *attrs;
    int64_t counts[4];

    /* --- no filter: every txn, the long one joined across blocks --- */
    configure(q, r, type_id);
    trl_qscan_set_options(q, 0, 0, 0, 0, 0);
    EXPECT(trl_qscan_run(q) == TRL_QS_OK, "run (all)");
    size_t n = trl_qscan_rows(q, &rows);
    EXPECT(n == 21, "21 txns");
    int found_long = 0;
    for (size_t i = 0; i < n; ++i) {
        if ((uint64_t)rows[i].id != long_id) continue;
        found_long = 1;
        EXPECT(!(rows[i].flags & TRL_QS_ROW_OPEN) && rows[i].end == 500, "long txn closed at 500");
        trl_qscan_attrs(q, &attrs);
        EXPECT(rows[i].attr_n == 1 && attrs[rows[i].attr_first].v == 100, "long txn a = 100");
    }
    EXPECT(found_long, "long txn present");

    /* --- a > 5: 14 short txns plus the long one, all decided in C --- */
    int64_t prog[] = {OP_LOAD, 0, OP_INT, 5, OP_CMP, C_GT, OP_END};
    configure(q, r, type_id);
    EXPECT(trl_qscan_set_program(q, 7, prog, 0, NULL, 0) == TRL_OK, "set_program");
    EXPECT(trl_qscan_set_entries(q, 0, 0, NULL) == TRL_OK, "set_entries");
    trl_qscan_set_options(q, TRL_QS_FILTER, 0, 0, 0, 0);
    EXPECT(trl_qscan_run(q) == TRL_QS_OK, "run (a > 5)");
    n = trl_qscan_rows(q, &rows);
    trl_qscan_counts(q, counts);
    EXPECT(n == 15 && counts[2] == 15 && counts[3] == 0, "15 kept, none deferred");

    /* --- inclusive window [15, 20]: txns overlapping it, a > 5 --- */
    configure(q, r, type_id);
    trl_qscan_set_program(q, 7, prog, 0, NULL, 0);
    trl_qscan_set_entries(q, 0, 0, NULL);
    trl_qscan_set_options(q, TRL_QS_FILTER | TRL_QS_HAS_LO | TRL_QS_HAS_HI, 0, 15, 20, 0);
    EXPECT(trl_qscan_run(q) == TRL_QS_OK, "run (window)");
    n = trl_qscan_rows(q, &rows);
    /* short txn i spans [10+i, 12+i]: overlaps [15,20] for i = 3..10, a > 5
     * leaves i = 6..10 (5 txns); the long txn [5, 500] overlaps too. */
    EXPECT(n == 6, "window keeps 6");

    /* --- a program C cannot decide: every row comes back deferred --- */
    int64_t defer[] = {OP_DEFER, OP_END};
    configure(q, r, type_id);
    trl_qscan_set_program(q, 2, defer, 0, NULL, 0);
    trl_qscan_set_entries(q, 0, 0, NULL);
    trl_qscan_set_options(q, TRL_QS_FILTER, 0, 0, 0, 0);
    EXPECT(trl_qscan_run(q) == TRL_QS_OK, "run (defer)");
    trl_qscan_counts(q, counts);
    EXPECT(counts[2] == 0 && counts[3] == 21, "all 21 deferred");
    trl_qscan_trim(q);
    EXPECT(trl_qscan_rows(q, &rows) == 0, "trim drops the results");

    /* --- projection: filter a > 5 at entry 0, value `a` at entry 7; rows
     *     come back as columns, none as full rows --- */
    int64_t proj_code[] = {OP_LOAD, 0, OP_INT, 5, OP_CMP, C_GT, OP_END,
                           OP_LOAD, 0, OP_END};
    int64_t val_entry = 7;
    configure(q, r, type_id);
    trl_qscan_set_program(q, 10, proj_code, 0, NULL, 0);
    EXPECT(trl_qscan_set_entries(q, 0, 1, &val_entry) == TRL_OK, "entries");
    trl_qscan_set_options(q, TRL_QS_FILTER | TRL_QS_PROJECT, 0, 0, 0, 0);
    EXPECT(trl_qscan_run(q) == TRL_QS_OK, "run (project)");
    trl_qs_proj_t pj;
    n = trl_qscan_proj(q, &pj);
    EXPECT(n == 15 && trl_qscan_rows(q, &rows) == 0, "15 projected, 0 full");
    int64_t sum = 0;
    for (size_t i = 0; i < n; ++i) {
        EXPECT(pj.vtag[i] == TRL_QS_V_INT, "value is an int");
        sum += pj.val[i];
    }
    EXPECT(sum == (6 + 19) * 14 / 2 + 100, "projected values a");
    EXPECT(trl_qscan_set_entries(q, 0, 1, (int64_t[]){99}) == TRL_ERR_INVALID,
           "entry past the code is rejected");

    /* --- a plan that points at a non-TXN block: fall back to Python --- */
    trl_qscan_reset(q);
    int64_t st0 = 0;
    uint64_t off0 = 0;                       /* the FILE_HDR block */
    uint32_t sid0 = 1;
    trl_qscan_set_plan(q, 1, &st0, &off0, &sid0);
    EXPECT(trl_qscan_run(q) == TRL_QS_FALLBACK, "fallback on a non-TXN block");
    EXPECT(strlen(trl_qscan_fallback_reason(q)) > 0, "fallback reason");

    trl_qscan_free(q);
    trl_reader_close(r);
    if (g_failures) {
        fprintf(stderr, "test_qscan: %d failure(s)\n", g_failures);
        return 1;
    }
    printf("test_qscan: ok\n");
    return 0;
}
