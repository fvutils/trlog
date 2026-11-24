/*
 * test_capi.c — C API fixes (code fix task 9, issues #15 #18 #19).
 *
 * - trl_hier_add_attr links and writes H_ATTR entries (#15).
 * - trl_rec_full records BITVEC/LOGICVEC fields of up to 64 bits from the
 *   raw value, and reports BYTES fields instead of dropping them silently (#18).
 * - Records on a disabled stream are dropped by the library (#19).
 *
 * Writes <out_dir>/capi_hier.trl and <out_dir>/capi_rec.trl; both are read
 * back by tests/integration/test_c_api_fixes.py.
 *
 * Usage: test_capi <out_dir>
 */

#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include "trl/trl.h"
#include "trl/trl_rec.h"
#include "trl/trl_query.h"

static int g_failures = 0;

#define EXPECT(cond, msg) do { \
    if (!(cond)) { \
        fprintf(stderr, "    EXPECT failed: %s  (%s:%d)\n", msg, __FILE__, __LINE__); \
        g_failures++; \
    } \
} while (0)

static void test_hier_attr(const char *path) {
    trl_writer_t *w = trl_writer_open(path, -9, TRL_COMPRESS_NONE);
    EXPECT(w != NULL, "writer open");
    if (!w) return;
    uint32_t st8 = trl_add_signal_type(w, TRL_SE_2STATE, 8, 0);
    trl_begin_hierarchy(w, 1, 0, trl_intern(w, "design"));
    /* No node declared yet: nothing to attach to. */
    EXPECT(trl_hier_add_attr(w, trl_intern(w, "k0"), trl_intern(w, "v0")) == TRL_ERR_INVALID,
           "attr before any node is rejected");
    trl_hier_begin_scope(w, 0, trl_intern(w, "top"), 0, 0, 0);
    EXPECT(trl_hier_add_attr(w, trl_intern(w, "scope_key"), trl_intern(w, "scope_val")) == TRL_OK,
           "attr on scope");
    uint32_t v = trl_hier_add_var(w, trl_intern(w, "sig"), st8, 0, 0, 0);
    EXPECT(v != 0, "add var");
    EXPECT(trl_hier_add_attr(w, trl_intern(w, "var_key"), trl_intern(w, "var_val")) == TRL_OK,
           "attr on var");
    trl_hier_end_scope(w);
    EXPECT(trl_flush_hierarchy(w) == TRL_OK, "flush hierarchy");
    EXPECT(trl_writer_close(w) == TRL_OK, "writer close");
}

static void test_rec(const char *path) {
    trl_db_cfg_t cfg = {0};
    cfg.path = path;
    cfg.timescale_exp = -9;
    cfg.compress = TRL_COMPRESS_NONE;
    cfg.stream_exclude = "off";

    trl_db_t *db = trl_db_open("capi", &cfg);
    EXPECT(db != NULL, "db_open");
    if (!db) return;

    trl_rtype_t *t = trl_type_new(db, "vecs", NULL);
    trl_type_add_field(t, "n", TRL_FT_U8, 0, NULL);
    trl_type_add_field(t, "bv", TRL_FT_BITVEC, 12, NULL);
    trl_type_add_field(t, "lv", TRL_FT_LOGICVEC, 8, NULL);
    trl_type_add_field(t, "bv64", TRL_FT_BITVEC, 64, NULL);
    EXPECT(trl_type_seal(t) == TRL_OK, "seal vecs");

    trl_rtype_t *tb = trl_type_new(db, "blob", NULL);
    trl_type_add_field(tb, "n", TRL_FT_U8, 0, NULL);
    trl_type_add_field(tb, "data", TRL_FT_BYTES, 0, NULL);
    EXPECT(trl_type_seal(tb) == TRL_OK, "seal blob");

    int on_en = 0, off_en = 1;
    trl_stream_t *on = trl_stream_new(db, NULL, "on", NULL, t, &on_en);
    trl_stream_t *off = trl_stream_new(db, NULL, "off", NULL, t, &off_en);
    EXPECT(on_en && !off_en, "stream enable flags");

    /* bv gets 0xABC from 0xFABC (masked to 12 bits). */
    uint64_t vals[4] = { 7, 0xFABC, 0x5A, 0xDEADBEEFCAFEF00Dull };
    uint64_t id1 = trl_rec_full(on, t, 10, 20, 0, vals, 4);
    EXPECT(id1 == 1, "first record id is 1");

    /* Disabled stream: nothing recorded, no id consumed. */
    EXPECT(trl_rec_full(off, t, 30, 40, 0, vals, 4) == 0, "rec_full on disabled stream returns 0");
    trl_txn_t *x = trl_rec_begin(off, t, 30, 0);
    EXPECT(x != NULL, "rec_begin on disabled stream returns a handle");
    EXPECT(trl_rec_id(x) == 0, "disabled handle has id 0");
    trl_rec_set_u64(x, 0, 1);
    trl_rec_meta(x, trl_db_intern(db, "k"), trl_db_intern(db, "v"));
    trl_rec_link(x, id1, 0, 0);
    trl_rec_end(x, 50);

    /* BYTES cannot be carried in 64 bits: reported, rest recorded. */
    uint64_t bvals[2] = { 3, 0 };
    uint64_t id2 = trl_rec_full(on, tb, 60, 70, 0, bvals, 2);
    EXPECT(id2 == 2, "next record id is 2 (disabled records used none)");
    const char *err = trl_last_error();
    EXPECT(err && strstr(err, "data") != NULL, "BYTES field reported via trl_last_error");

    EXPECT(trl_db_close(db) == TRL_OK, "db close");
}

int main(int argc, char **argv) {
    if (argc < 2) {
        fprintf(stderr, "usage: %s <out_dir>\n", argv[0]);
        return 2;
    }
    char p1[4096], p2[4096];
    snprintf(p1, sizeof(p1), "%s/capi_hier.trl", argv[1]);
    snprintf(p2, sizeof(p2), "%s/capi_rec.trl", argv[1]);
    test_hier_attr(p1);
    test_rec(p2);
    if (g_failures) {
        fprintf(stderr, "test_capi: %d failure(s)\n", g_failures);
        return 1;
    }
    printf("test_capi: OK\n");
    return 0;
}
