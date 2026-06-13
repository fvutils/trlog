/*
 * test_golden.c — Phase 0 C-side parity harness.
 *
 * Mirrors the Python golden corpus (tests/golden/corpus.py) using the public
 * libtrl writer API, then:
 *
 *   1. round-trips each produced file through the C reader (must pass today),
 *   2. compares the bytes against the checked-in Python golden fixture and
 *      reports MATCH / DIVERGE *informationally*.
 *
 * Per the implementation plan (§0 strategy: "pure-Python reference first,
 * freeze golden, then make C match the fixtures"), the C writer is NOT yet
 * byte-identical to the Python reference, so byte-divergence is expected and
 * does not fail the test. As later phases bring the C writer onto the golden
 * bytes, the DIVERGE lines flip to MATCH and the comparison can be made a hard
 * assertion (see ASSERT_BYTE_MATCH below).
 *
 * Usage: test_golden <out_dir> [<fixture_dir>]
 */

#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <stdint.h>

#include "trl/trl.h"

/* Set to 1 in a later phase once the C writer matches the golden fixtures to
 * turn byte-divergence into a hard failure. */
#ifndef ASSERT_BYTE_MATCH
#define ASSERT_BYTE_MATCH 0
#endif

static int g_failures = 0;

#define EXPECT(cond, msg) do { \
    if (!(cond)) { \
        fprintf(stderr, "    EXPECT failed: %s  (%s:%d)\n", msg, __FILE__, __LINE__); \
        g_failures++; \
    } \
} while (0)

/* ----------------------------------------------------------------------- *
 * Corpus builders — the native-expressible subset of tests/golden/corpus.py *
 * ----------------------------------------------------------------------- */

static void build_empty(const char *path) {
    trl_writer_t *w = trl_writer_open(path, -9, TRL_COMPRESS_NONE);
    trl_writer_close(w);
}

static void build_vc_2state_1bit(const char *path) {
    trl_writer_t *w = trl_writer_open(path, -9, TRL_COMPRESS_NONE);
    uint32_t st = trl_add_signal_type(w, TRL_SE_2STATE, 1, 0);
    trl_begin_hierarchy(w, 1, 0, trl_intern(w, "design"));
    trl_hier_begin_scope(w, 0, trl_intern(w, "top"), 0, 0, 0);
    uint32_t clk = trl_hier_add_var(w, trl_intern(w, "clk"), st, 0, 0, 0);
    trl_hier_end_scope(w);
    trl_flush_hierarchy(w);
    trl_vc_begin(w, 0);
    for (int i = 0; i < 64; ++i) trl_vc_change_u64(w, clk, (uint64_t)i * 10, (uint64_t)(i % 2));
    trl_vc_flush(w);
    trl_writer_close(w);
}

static void build_vc_2state_32bit(const char *path) {
    trl_writer_t *w = trl_writer_open(path, -9, TRL_COMPRESS_NONE);
    uint32_t st = trl_add_signal_type(w, TRL_SE_2STATE, 32, 0);
    trl_begin_hierarchy(w, 1, 0, trl_intern(w, "design"));
    trl_hier_begin_scope(w, 0, trl_intern(w, "top"), 0, 0, 0);
    uint32_t bus = trl_hier_add_var(w, trl_intern(w, "count"), st, 0, 0, 0);
    trl_hier_end_scope(w);
    trl_flush_hierarchy(w);
    trl_vc_begin(w, 0);
    for (int i = 0; i < 50; ++i)
        trl_vc_change_u64(w, bus, (uint64_t)i * 10, (uint64_t)((i * 0x1234) & 0xFFFFFFFF));
    trl_vc_flush(w);
    trl_writer_close(w);
}

static void build_vc_real(const char *path) {
    trl_writer_t *w = trl_writer_open(path, -9, TRL_COMPRESS_NONE);
    uint32_t st = trl_add_signal_type(w, TRL_SE_REAL, 0, 0);
    trl_begin_hierarchy(w, 1, 0, trl_intern(w, "design"));
    trl_hier_begin_scope(w, 0, trl_intern(w, "top"), 0, 0, 0);
    uint32_t sig = trl_hier_add_var(w, trl_intern(w, "v"), st, 0, 0, 0);
    trl_hier_end_scope(w);
    trl_flush_hierarchy(w);
    trl_vc_begin(w, 0);
    for (int i = 0; i < 20; ++i) trl_vc_change_real(w, sig, (uint64_t)i * 10, (double)i * 1.5);
    trl_vc_flush(w);
    trl_writer_close(w);
}

static void build_vc_string(const char *path) {
    static const char *vals[] = {"idle", "run", "stop", "\xe6\x97\xa5\xe6\x9c\xac\xe8\xaa\x9e"};
    trl_writer_t *w = trl_writer_open(path, -9, TRL_COMPRESS_NONE);
    uint32_t st = trl_add_signal_type(w, TRL_SE_STRING, 0, 0);
    trl_begin_hierarchy(w, 1, 0, trl_intern(w, "design"));
    trl_hier_begin_scope(w, 0, trl_intern(w, "top"), 0, 0, 0);
    uint32_t sig = trl_hier_add_var(w, trl_intern(w, "state"), st, 0, 0, 0);
    trl_hier_end_scope(w);
    trl_flush_hierarchy(w);
    trl_vc_begin(w, 0);
    for (int i = 0; i < 4; ++i) trl_vc_change_str(w, sig, (uint64_t)i * 10, vals[i]);
    trl_vc_flush(w);
    trl_writer_close(w);
}

static void build_hierarchy(const char *path) {
    trl_writer_t *w = trl_writer_open(path, -9, TRL_COMPRESS_NONE);
    uint32_t st1 = trl_add_signal_type(w, TRL_SE_2STATE, 1, 0);
    uint32_t st8 = trl_add_signal_type(w, TRL_SE_2STATE, 8, 0);
    trl_begin_hierarchy(w, 1, 0, trl_intern(w, "design"));
    trl_hier_begin_scope(w, 0, trl_intern(w, "top"), 0, 0, 0);
    uint32_t clk = trl_hier_add_var(w, trl_intern(w, "clk"), st1, 0, 0, 0);
    trl_hier_begin_scope(w, 0, trl_intern(w, "sub"), 0, 0, 0);
    uint32_t rst = trl_hier_add_var(w, trl_intern(w, "rst"), st1, 0, 0, 0);
    uint32_t data = trl_hier_add_var(w, trl_intern(w, "data"), st8, 0, 0, 0);
    trl_hier_end_scope(w);
    trl_hier_end_scope(w);
    trl_flush_hierarchy(w);
    trl_vc_begin(w, 0);
    for (int i = 0; i < 16; ++i) {
        trl_vc_change_u64(w, clk, (uint64_t)i * 10, (uint64_t)(i % 2));
        trl_vc_change_u64(w, rst, (uint64_t)i * 10, (uint64_t)(i < 2 ? 1 : 0));
        trl_vc_change_u64(w, data, (uint64_t)i * 10, (uint64_t)((i * 7) & 0xFF));
    }
    trl_vc_flush(w);
    trl_writer_close(w);
}

static void build_txn_row(const char *path) {
    trl_writer_t *w = trl_writer_open(path, -9, TRL_COMPRESS_NONE);
    trl_txn_begin_block(w, 0);
    for (int i = 0; i < 40; ++i)
        trl_txn_full(w, 1, 1, (uint64_t)i, (uint64_t)i * 10, (uint64_t)i * 10 + 5, 0, 0, NULL);
    trl_txn_end_block(w);
    trl_writer_close(w);
}

static void build_txn_schema_attrs(const char *path) {
    trl_writer_t *w = trl_writer_open(path, -9, TRL_COMPRESS_NONE);
    trl_field_def_t fields[2];
    fields[0].name_str_id = trl_intern(w, "addr");
    fields[0].field_type = TRL_FT_U32;
    fields[1].name_str_id = trl_intern(w, "data");
    fields[1].field_type = TRL_FT_U64;
    uint32_t t = trl_add_txn_schema(w, trl_intern(w, "Access"), 2, fields);
    trl_txn_begin_block(w, 0);
    for (int i = 0; i < 32; ++i) {
        trl_attr_t attrs[2];
        attrs[0].field_idx = 0; attrs[0].field_type = TRL_FT_U32; attrs[0].value.u64 = (uint64_t)i * 4;
        attrs[1].field_idx = 1; attrs[1].field_type = TRL_FT_U64; attrs[1].value.u64 = (uint64_t)i * 0x100;
        trl_txn_full(w, 1, t, (uint64_t)i, (uint64_t)i * 10, (uint64_t)i * 10 + 5, 0, 2, attrs);
    }
    trl_txn_end_block(w);
    trl_writer_close(w);
}

typedef void (*build_fn)(const char *path);

struct entry { const char *name; build_fn build; };

static const struct entry ENTRIES[] = {
    {"empty",            build_empty},
    {"vc_2state_1bit",   build_vc_2state_1bit},
    {"vc_2state_32bit",  build_vc_2state_32bit},
    {"vc_real",          build_vc_real},
    {"vc_string",        build_vc_string},
    {"hierarchy",        build_hierarchy},
    {"txn_row",          build_txn_row},
    {"txn_schema_attrs", build_txn_schema_attrs},
};
static const int N_ENTRIES = (int)(sizeof(ENTRIES) / sizeof(ENTRIES[0]));

/* ----------------------------------------------------------------------- */

static uint8_t *read_file(const char *path, long *out_len) {
    FILE *f = fopen(path, "rb");
    if (!f) return NULL;
    fseek(f, 0, SEEK_END);
    long len = ftell(f);
    fseek(f, 0, SEEK_SET);
    uint8_t *buf = (uint8_t *)malloc((size_t)(len > 0 ? len : 1));
    if (buf && len > 0) {
        if (fread(buf, 1, (size_t)len, f) != (size_t)len) { free(buf); fclose(f); return NULL; }
    }
    fclose(f);
    *out_len = len;
    return buf;
}

int main(int argc, char **argv) {
    if (argc < 2) {
        fprintf(stderr, "usage: %s <out_dir> [<fixture_dir>]\n", argv[0]);
        return 2;
    }
    const char *out_dir = argv[1];
    const char *fixture_dir = (argc > 2) ? argv[2] : NULL;
    char path[4096], fxpath[4096];
    int matches = 0, diverges = 0, no_fixture = 0;

    for (int i = 0; i < N_ENTRIES; ++i) {
        const struct entry *e = &ENTRIES[i];
        snprintf(path, sizeof(path), "%s/%s.trl", out_dir, e->name);

        e->build(path);

        /* (1) round-trip: the C reader must reopen what the C writer produced. */
        trl_reader_t *r = trl_reader_open(path);
        EXPECT(r != NULL, e->name);
        if (r) trl_reader_close(r);

        /* (2) informational byte comparison against the Python golden fixture. */
        const char *verdict = "(no fixture dir)";
        if (fixture_dir) {
            snprintf(fxpath, sizeof(fxpath), "%s/%s.trl", fixture_dir, e->name);
            long flen = 0, clen = 0;
            uint8_t *fx = read_file(fxpath, &flen);
            uint8_t *cb = read_file(path, &clen);
            if (!fx) {
                verdict = "(no fixture)"; no_fixture++;
            } else if (cb && flen == clen && memcmp(fx, cb, (size_t)flen) == 0) {
                verdict = "MATCH"; matches++;
            } else {
                verdict = "DIVERGE"; diverges++;
#if ASSERT_BYTE_MATCH
                EXPECT(0, e->name);  /* later-phase: byte-match is mandatory */
#endif
            }
            free(fx); free(cb);
        }
        printf("  %-20s round-trip OK  byte:%s\n", e->name, verdict);
    }

    printf("golden: %d entries, byte MATCH=%d DIVERGE=%d no-fixture=%d, failures=%d\n",
           N_ENTRIES, matches, diverges, no_fixture, g_failures);
    return g_failures ? 1 : 0;
}
