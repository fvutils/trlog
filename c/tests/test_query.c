/*
 * test_query.c — phase 3 harness for the L0 reading session (trl/trl_query.h).
 *
 * Writes a small trace with trl_rec.h (phase 2), deliberately forcing a
 * stream's buffer to auto-flush between a begin and its later attrs/end (a
 * block_bytes of 1 flushes after almost every record), so the cursor's
 * cross-block join is actually exercised. Then reopens it read-only with
 * trl_query.h and checks name lookup, schema introspection, and the joined
 * transactions the cursor produces.
 *
 * Usage: test_query <out_path>
 */

#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include "trl/trl_rec.h"
#include "trl/trl_query.h"

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

    uint32_t key_sid, val_sid;
    uint64_t t1_id, t2_id, t3_id;

    /* --- write --- */
    {
        trl_db_cfg_t cfg = {0};
        cfg.path = path;
        cfg.timescale_exp = -9;
        cfg.compress = TRL_COMPRESS_NONE;
        cfg.block_bytes = 1;   /* force an auto-flush after nearly every record */

        trl_db_t *db = trl_db_open("phase3", &cfg);
        EXPECT(db != NULL, "db_open");
        if (!db) return 1;

        trl_rtype_t *t = trl_type_new(db, "pkt", NULL);
        int32_t f_a = trl_type_add_field(t, "a", TRL_FT_U32, 0, NULL);
        int32_t f_b = trl_type_add_field(t, "b", TRL_FT_U32, 0, NULL);
        int32_t f_c = trl_type_add_field(t, "c", TRL_FT_U32, 0, NULL);
        EXPECT(trl_type_seal(t) == TRL_OK, "seal pkt");

        trl_stream_t *s = trl_stream_new(db, NULL, "s", NULL, t, NULL);

        /* t1: begin/set/set/link/meta/end, spanning several auto-flushed blocks. */
        trl_txn_t *x1 = trl_rec_begin(s, t, 100, 0);
        t1_id = trl_rec_id(x1);
        trl_rec_set_u64(x1, (uint32_t)f_a, 1);
        trl_rec_set_u64(x1, (uint32_t)f_b, 2);
        trl_rec_set_u64(x1, (uint32_t)f_c, 3);
        key_sid = trl_db_intern(db, "trlog.origin.component");
        val_sid = trl_db_intern(db, "seq1");
        trl_rec_meta(x1, key_sid, val_sid);
        trl_rec_end(x1, 150);

        /* t2: a child of t1, linked back to it. */
        trl_txn_t *x2 = trl_rec_begin(s, t, 120, t1_id);
        t2_id = trl_rec_id(x2);
        trl_rec_set_u64(x2, (uint32_t)f_a, 9);
        trl_rec_link(x2, t1_id, 1 /* LT_PARENT_CHILD */, 0);
        trl_rec_end(x2, 200);

        /* t3: one-shot, starts before t2 ends — out-of-block-order on disk,
         * but must still come back in start order. */
        uint64_t vals[3] = {0};
        vals[f_a] = 42;
        t3_id = trl_rec_full(s, t, 130, 140, 0, vals, 3);

        EXPECT(trl_db_close(db) == TRL_OK, "db_close");
    }

    /* --- read --- */
    trl_rdb_t *rdb = trl_rdb_open("phase3q", path);
    EXPECT(rdb != NULL, "rdb_open");
    if (!rdb) {
        fprintf(stderr, "test_query: %d failure(s)\n", g_failures);
        return 1;
    }

    EXPECT(trl_rdb_num_streams(rdb) == 1, "one stream declared");
    trl_rstream_t *s = trl_rdb_stream_find(rdb, "s");
    EXPECT(s != NULL, "stream_find s");
    if (s) EXPECT(strcmp(trl_rstream_path(s), "s") == 0, "stream path");

    const trl_rschema_t *schema = trl_rdb_schema_find(rdb, "pkt");
    EXPECT(schema != NULL, "schema_find pkt");
    if (schema) {
        EXPECT(trl_rschema_num_fields(schema) == 3, "pkt has 3 fields");
        EXPECT(trl_rschema_field_index(schema, "b") == 1, "field_index b");
        EXPECT(trl_rschema_field_type(schema, 0) == TRL_FT_U32, "field_type a");
    }

    trl_cursor_t *c = trl_cursor_new(rdb, NULL, 0, UINT64_MAX);
    EXPECT(c != NULL, "cursor_new");

    trl_txn_hdr_t hdr;
    int n = 0;
    uint64_t seen_start[8];
    uint64_t seen_id[8];
    while (trl_cursor_next(c, &hdr) && n < 8) {
        seen_start[n] = hdr.start;
        seen_id[n] = hdr.txn_id;

        if (hdr.txn_id == t1_id) {
            EXPECT(hdr.start == 100 && hdr.end == 150, "t1 start/end");
            EXPECT(hdr.attr_count == 3, "t1 has 3 joined attrs");
            int seen_a = 0, seen_b = 0, seen_c = 0;
            for (uint32_t i = 0; i < hdr.attr_count; ++i) {
                if (hdr.attrs[i].field_idx == 0 && hdr.attrs[i].value.u64 == 1) seen_a = 1;
                if (hdr.attrs[i].field_idx == 1 && hdr.attrs[i].value.u64 == 2) seen_b = 1;
                if (hdr.attrs[i].field_idx == 2 && hdr.attrs[i].value.u64 == 3) seen_c = 1;
            }
            EXPECT(seen_a && seen_b && seen_c, "t1 attrs a/b/c all joined across flushes");
            EXPECT(hdr.meta_count == 1 && hdr.metas[0].key_sid == key_sid && hdr.metas[0].val_sid == val_sid,
                   "t1 meta joined");
        } else if (hdr.txn_id == t2_id) {
            EXPECT(hdr.parent == t1_id, "t2 parent is t1");
            EXPECT(hdr.link_count == 1 && hdr.links[0].target == t1_id && hdr.links[0].link_type == 1,
                   "t2 link to t1 joined");
        } else if (hdr.txn_id == t3_id) {
            EXPECT(hdr.start == 130 && hdr.end == 140, "t3 full record start/end");
        }
        n++;
    }
    EXPECT(n == 3, "three transactions total");
    for (int i = 1; i < n; ++i) {
        EXPECT(seen_start[i - 1] <= seen_start[i], "cursor emits in start order");
    }

    trl_cursor_free(c);
    EXPECT(trl_rdb_close(rdb) == TRL_OK, "rdb_close");

    if (g_failures) {
        fprintf(stderr, "test_query: %d failure(s)\n", g_failures);
        return 1;
    }
    printf("test_query: OK (%s)\n", path);
    return 0;
}
