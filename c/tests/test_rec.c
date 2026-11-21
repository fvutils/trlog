/*
 * test_rec.c — phase 2 harness for the L0 recording session (trl/trl_rec.h).
 *
 * Builds a small trace through the session API (db/type/stream registration,
 * trl_rec_full and the begin/set/end/link/meta incremental path, an ad-hoc
 * stream, and a disabled stream via stream_exclude) and checks the API's own
 * bookkeeping. The written file is also read back by a Python integration
 * test (tests/integration/test_rec_session.py) for an independent check that
 * the bytes this session layer produces decode correctly.
 *
 * Usage: test_rec <out_path>
 */

#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include "trl/trl_rec.h"

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

    trl_db_cfg_t cfg = {0};
    cfg.path = argv[1];
    cfg.timescale_exp = -9;
    cfg.compress = TRL_COMPRESS_NONE;
    cfg.block_bytes = 4096;          /* small, to also exercise auto-flush */
    cfg.stream_exclude = "env.quiet";

    trl_db_t *db = trl_db_open("phase2", &cfg);
    EXPECT(db != NULL, "db_open");
    if (!db) return 1;

    /* A flat type for the one-shot (Tier 1) path. */
    trl_rtype_t *axi_t = trl_type_new(db, "axi_rd", NULL);
    int32_t f_addr = trl_type_add_field(axi_t, "addr", TRL_FT_U64, 0, NULL);
    int32_t f_len  = trl_type_add_field(axi_t, "len",  TRL_FT_U8,  0, NULL);
    EXPECT(f_addr == 0 && f_len == 1, "axi_rd field indices");
    EXPECT(trl_type_seal(axi_t) == TRL_OK, "seal axi_rd");
    EXPECT(trl_type_find(db, "axi_rd", NULL) == axi_t, "type_find axi_rd");

    /* A nested type (struct + array), for the incremental (Tier 2) path. */
    trl_rtype_t *hdr_t = trl_type_new(db, "pkt_hdr", NULL);
    int32_t h_len  = trl_type_add_field(hdr_t, "len", TRL_FT_U16, 0, NULL);
    int32_t h_kind = trl_type_add_field(hdr_t, "kind", TRL_FT_ENUM, 0, NULL);
    EXPECT(h_len == 0 && h_kind == 1, "pkt_hdr field indices");
    EXPECT(trl_type_seal(hdr_t) == TRL_OK, "seal pkt_hdr");

    trl_rtype_t *pkt_t = trl_type_new(db, "pkt", NULL);
    int32_t p_hdr = trl_type_add_struct(pkt_t, "hdr", hdr_t);
    int32_t p_tag = trl_type_add_array(pkt_t, "tag", TRL_FT_U8, 0, 3);
    int32_t p_pay = trl_type_add_field(pkt_t, "payload", TRL_FT_BYTES, 0, NULL);
    EXPECT(p_hdr == 0, "pkt base index for hdr");
    EXPECT(p_tag == 2, "pkt base index for tag[]");
    EXPECT(p_pay == 5, "pkt index for payload");
    EXPECT(strcmp(trl_type_field_name(pkt_t, 0), "hdr.len") == 0, "pkt flattened hdr.len");
    EXPECT(strcmp(trl_type_field_name(pkt_t, 1), "hdr.kind") == 0, "pkt flattened hdr.kind");
    EXPECT(strcmp(trl_type_field_name(pkt_t, 3), "tag[1]") == 0, "pkt flattened tag[1]");
    EXPECT(trl_type_seal(pkt_t) == TRL_OK, "seal pkt");

    /* Streams, one nested under another, and one excluded by glob. */
    int axi_enabled = 0;
    trl_stream_t *env = trl_stream_new(db, NULL, "env", "scope", NULL, NULL);
    trl_stream_t *axi = trl_stream_new(db, env, "axi", "axi", axi_t, &axi_enabled);
    EXPECT(axi_enabled, "axi stream enabled by default");
    int quiet_enabled = 1;
    trl_stream_t *quiet = trl_stream_new(db, env, "quiet", NULL, NULL, &quiet_enabled);
    EXPECT(!quiet_enabled, "env.quiet excluded by stream_exclude");
    EXPECT(trl_stream_find(db, "env.axi") == axi, "stream_find env.axi");

    /* Tier 1: one-shot trl_rec_full. */
    uint64_t vals[2];
    for (int i = 0; i < 20; ++i) {
        vals[f_addr] = 0x1000 + (uint64_t)i * 4;
        vals[f_len]  = (uint64_t)(i % 8);
        uint64_t id = trl_rec_full(axi, axi_t, (uint64_t)i * 10, (uint64_t)i * 10 + 5, 0, vals, 2);
        EXPECT(id == (uint64_t)(i + 1), "axi txn id sequence");
    }

    /* Tier 2: the incremental begin/set/end path, plus a link and metadata. */
    trl_stream_t *pkt_s = trl_stream_new(db, env, "pkt", "pkt", NULL, NULL);
    trl_txn_t *t1 = trl_rec_begin(pkt_s, pkt_t, 1000, 0);
    uint64_t t1_id = trl_rec_id(t1);
    trl_rec_set_u64(t1, (uint32_t)h_len, 64);
    trl_rec_set_u64(t1, (uint32_t)h_kind, 3);
    uint8_t payload[4] = {0xDE, 0xAD, 0xBE, 0xEF};
    trl_rec_set_bytes(t1, (uint32_t)p_pay, payload, sizeof(payload));
    trl_rec_meta(t1, trl_db_intern(db, "trlog.origin.component"), trl_db_intern(db, "seq1"));
    trl_rec_end(t1, 1050);

    trl_txn_t *t2 = trl_rec_begin(pkt_s, pkt_t, 1100, t1_id);
    uint64_t t2_id = trl_rec_id(t2);
    trl_rec_set_u64(t2, (uint32_t)h_len, 8);
    trl_rec_link(t2, t1_id, 1 /* LT_PARENT_CHILD */, 0);
    trl_rec_end(t2, 1120);
    EXPECT(t2_id == t1_id + 1, "pkt txn id sequence");

    /* Ad-hoc stream: fields declared lazily by name. */
    trl_stream_t *dbg = trl_stream_new(db, NULL, "dbg", NULL, NULL, NULL);
    trl_rtype_t *dbg_t = trl_stream_adhoc_type(dbg);
    int32_t f_msg = trl_type_field_get_or_add(dbg_t, "msg", TRL_FT_STRING, 0);
    int32_t f_lvl = trl_type_field_get_or_add(dbg_t, "lvl", TRL_FT_U8, 0);
    trl_txn_t *d1 = trl_rec_begin(dbg, dbg_t, 2000, 0);
    trl_rec_set_str(d1, (uint32_t)f_msg, "hello");
    trl_rec_set_u64(d1, (uint32_t)f_lvl, 2);
    trl_rec_end(d1, 2000);
    /* Re-declaring the same ad-hoc field with the same type/width is fine. */
    EXPECT(trl_type_field_get_or_add(dbg_t, "msg", TRL_FT_STRING, 0) == f_msg, "adhoc field_get_or_add idempotent");

    EXPECT(trl_db_close(db) == TRL_OK, "db_close");
    EXPECT(trl_db_find("phase2") == NULL, "db removed from registry after close");

    /* The written file must at least round-trip through the C reader. */
    trl_reader_t *r = trl_reader_open(argv[1]);
    EXPECT(r != NULL, "reader_open on the session output");
    if (r) trl_reader_close(r);

    if (g_failures) {
        fprintf(stderr, "test_rec: %d failure(s)\n", g_failures);
        return 1;
    }
    printf("test_rec: OK (%s)\n", argv[1]);
    return 0;
}
