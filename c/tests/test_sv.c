/*
 * test_sv.c — phase 4 harness for the DPI-C glue (trl_sv.c, L1).
 *
 * Calls the trl_sv_* exports directly from C (same ABI a SystemVerilog DPI
 * import would use — chandle is void*, string is char*, status ints are
 * int), so this exercises trl_sv.c itself without needing a simulator. The
 * Verilator smoke test (sv/) covers the actual DPI crossing; this test
 * covers trl_sv.c's logic being a correct pass-through to trl_rec.h/
 * trl_query.h.
 *
 * Usage: test_sv <out_path>
 */

#include <stdio.h>
#include <stdlib.h>
#include <string.h>

/* No svdpi.h needed: declare the handful of trl_sv_* exports we call with
 * their plain-C signatures (chandle -> void*), matching trl_sv.c exactly. */
void *trl_sv_db_open(const char *name, const char *path, int timescale_exp,
                      int compress, int block_kb, int flags);
int   trl_sv_db_close(void *db);
int   trl_sv_intern(void *db, const char *s);

void *trl_sv_type_new(void *db, const char *name, void *scope);
int   trl_sv_type_add_field(void *t, const char *name, int kind, int width);
int   trl_sv_type_seal(void *t);
int   trl_sv_type_field_index(void *t, const char *name);

void *trl_sv_stream_new(void *db, void *parent, const char *name, const char *kind,
                         void *dflt_type, int *enabled_out);

long long trl_sv_record(void *s, void *t, long long start, long long end, long long parent,
                         const long long *v, int n);
void *trl_sv_begin(void *s, void *t, long long start, long long parent);
long long trl_sv_txn_id(void *x);
void trl_sv_set_int(void *x, int f, long long v);
void trl_sv_meta(void *x, int key_sid, int val_sid);
void trl_sv_end(void *x, long long end);

void *trl_sv_rdb_open(const char *name, const char *path);
int   trl_sv_rdb_close(void *r);
int   trl_sv_rdb_num_streams(void *r);
void *trl_sv_rdb_schema_find(void *r, const char *name);
int   trl_sv_rschema_num_fields(void *sch);
int   trl_sv_rschema_field_index(void *sch, const char *name);

void      *trl_sv_cursor_new(void *r, void *rs, long long t_from, long long t_to);
int        trl_sv_cursor_next(void *c, long long *txn_id, int *stream_inst_id, int *txn_type_id,
                               long long *start, long long *end, long long *parent,
                               int *attr_count, int *link_count, int *meta_count);
int        trl_sv_cur_has(void *c, int f);
long long  trl_sv_cur_int(void *c, int f);
void       trl_sv_cursor_free(void *c);

static int g_failures = 0;
#define EXPECT(cond, msg) do { \
    if (!(cond)) { \
        fprintf(stderr, "    EXPECT failed: %s  (%s:%d)\n", msg, __FILE__, __LINE__); \
        g_failures++; \
    } \
} while (0)

int main(int argc, char **argv) {
    if (argc < 2) {
        fprintf(stderr, "usage: %s <out_path>\n", argv[0]);
        return 2;
    }
    const char *path = argv[1];
    long long t1_id;

    /* --- write, through trl_sv.c --- */
    {
        void *db = trl_sv_db_open("phase4", path, -9, 0 /* none */, 0, 0);
        EXPECT(db != NULL, "db_open");
        if (!db) return 1;

        void *t = trl_sv_type_new(db, "pkt", NULL);
        int f_a = trl_sv_type_add_field(t, "a", /*TRL_FT_U32*/ 0x02, 0);
        int f_b = trl_sv_type_add_field(t, "b", 0x02, 0);
        EXPECT(trl_sv_type_seal(t) == 0, "seal pkt");

        int enabled = 0;
        void *s = trl_sv_stream_new(db, NULL, "s", NULL, t, &enabled);
        EXPECT(s != NULL && enabled, "stream_new s, enabled by default");

        int key_sid = trl_sv_intern(db, "trlog.origin.component");
        int val_sid = trl_sv_intern(db, "sv1");

        void *x1 = trl_sv_begin(s, t, 100, 0);
        t1_id = trl_sv_txn_id(x1);
        trl_sv_set_int(x1, f_a, 7);
        trl_sv_set_int(x1, f_b, 8);
        trl_sv_meta(x1, key_sid, val_sid);
        trl_sv_end(x1, 150);

        long long vals[2] = {42, 43};
        long long t2_id = trl_sv_record(s, t, 200, 210, 0, vals, 2);
        EXPECT(t2_id != t1_id, "record() returns a distinct txn id");

        EXPECT(trl_sv_db_close(db) == 0, "db_close");
    }

    /* --- read, through trl_sv.c --- */
    void *rdb = trl_sv_rdb_open("phase4q", path);
    EXPECT(rdb != NULL, "rdb_open");
    if (!rdb) {
        fprintf(stderr, "test_sv: %d failure(s)\n", g_failures);
        return 1;
    }
    EXPECT(trl_sv_rdb_num_streams(rdb) == 1, "one stream");

    void *sch = trl_sv_rdb_schema_find(rdb, "pkt");
    EXPECT(sch != NULL, "schema_find pkt");
    if (sch) {
        EXPECT(trl_sv_rschema_num_fields(sch) == 2, "pkt has 2 fields");
        EXPECT(trl_sv_rschema_field_index(sch, "b") == 1, "field_index b");
    }

    void *c = trl_sv_cursor_new(rdb, NULL, 0, 0x7FFFFFFFFFFFFFFFLL);
    EXPECT(c != NULL, "cursor_new");

    long long txn_id, start, end, parent;
    int stream_inst_id, txn_type_id, attr_count, link_count, meta_count;
    int n = 0;
    while (trl_sv_cursor_next(c, &txn_id, &stream_inst_id, &txn_type_id, &start, &end, &parent,
                               &attr_count, &link_count, &meta_count)) {
        if (txn_id == t1_id) {
            EXPECT(start == 100 && end == 150, "t1 start/end via trl_sv_cursor_next");
            EXPECT(attr_count == 2, "t1 has 2 attrs");
            EXPECT(trl_sv_cur_has(c, 0) && trl_sv_cur_int(c, 0) == 7, "t1 field a via trl_sv_cur_int");
            EXPECT(trl_sv_cur_int(c, 1) == 8, "t1 field b via trl_sv_cur_int");
            EXPECT(meta_count == 1, "t1 meta joined");
        }
        n++;
    }
    EXPECT(n == 2, "two transactions total");

    trl_sv_cursor_free(c);
    EXPECT(trl_sv_rdb_close(rdb) == 0, "rdb_close");

    if (g_failures) {
        fprintf(stderr, "test_sv: %d failure(s)\n", g_failures);
        return 1;
    }
    printf("test_sv: OK (%s)\n", path);
    return 0;
}
