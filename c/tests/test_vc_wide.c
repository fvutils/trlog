/*
 * Value-change encoding of wide and multi-state values (code fix task 1):
 * 2-state values wider than 64 bits, and N-bit 4-state / 9-state values given
 * through trl_vc_change_u64 or trl_vc_change_bytes, must read back in full.
 *
 * Usage: test_vc_wide <out.trl>
 */
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <stdint.h>

#include "trl/trl.h"

static int g_failures = 0;

#define EXPECT(cond, msg) do { \
    if (!(cond)) { \
        fprintf(stderr, "    EXPECT failed: %s  (%s:%d)\n", msg, __FILE__, __LINE__); \
        g_failures++; \
    } \
} while (0)

#define MAX_SEEN 64

typedef struct {
    uint32_t var_id;
    uint64_t time;
    uint8_t  val[32];
    uint32_t len;
} seen_t;

static seen_t g_seen[MAX_SEEN];
static size_t g_nseen = 0;

static void on_vc(uint32_t var_id, uint64_t time, const uint8_t *val, uint32_t len, void *user) {
    (void)user;
    if (g_nseen >= MAX_SEEN || len > sizeof(g_seen[0].val)) { g_failures++; return; }
    g_seen[g_nseen].var_id = var_id;
    g_seen[g_nseen].time = time;
    memcpy(g_seen[g_nseen].val, val, len);
    g_seen[g_nseen].len = len;
    g_nseen++;
}

static const seen_t *find(uint32_t var_id, uint64_t time) {
    for (size_t i = 0; i < g_nseen; ++i)
        if (g_seen[i].var_id == var_id && g_seen[i].time == time) return &g_seen[i];
    return NULL;
}

static void expect_val(uint32_t var_id, uint64_t time, const uint8_t *want, uint32_t want_len, const char *what) {
    const seen_t *s = find(var_id, time);
    if (!s) {
        fprintf(stderr, "    %s: no change for var %u at %llu\n", what, var_id, (unsigned long long)time);
        g_failures++;
        return;
    }
    if (s->len != want_len || memcmp(s->val, want, want_len) != 0) {
        fprintf(stderr, "    %s: var %u at %llu: got", what, var_id, (unsigned long long)time);
        for (uint32_t i = 0; i < s->len; ++i) fprintf(stderr, " %02x", s->val[i]);
        fprintf(stderr, ", want");
        for (uint32_t i = 0; i < want_len; ++i) fprintf(stderr, " %02x", want[i]);
        fprintf(stderr, "\n");
        g_failures++;
    }
}

int main(int argc, char **argv) {
    if (argc < 2) {
        fprintf(stderr, "usage: %s <out.trl>\n", argv[0]);
        return 2;
    }
    const char *path = argv[1];

    static const uint8_t w72_a[9] = {0x01, 0x23, 0x45, 0x67, 0x89, 0xAB, 0xCD, 0xEF, 0x12};
    static const uint8_t w72_b[9] = {0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF};
    static const uint8_t w72_c[9] = {0x80, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x01};
    static const uint8_t w72_d[9] = {0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x05};
    static const uint8_t w128_a[16] = {0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0xDE, 0xAD, 0xBE, 0xEF};
    /* 4-state "1x0z1x0z" packed 2 bits per bit, MSB first: 01 10 00 11 = 0x63 */
    static const uint8_t l8_xz[2] = {0x63, 0x63};

    trl_writer_t *w = trl_writer_open(path, -9, TRL_COMPRESS_NONE);
    EXPECT(w != NULL, "writer opens");
    if (!w) return 1;
    uint32_t st72  = trl_add_signal_type(w, TRL_SE_2STATE, 72, 0);
    uint32_t st128 = trl_add_signal_type(w, TRL_SE_2STATE, 128, 0);
    uint32_t stl8  = trl_add_signal_type(w, TRL_SE_4STATE, 8, 0);
    uint32_t sts8  = trl_add_signal_type(w, TRL_SE_9STATE, 8, 0);
    trl_begin_hierarchy(w, 1, 0, trl_intern(w, "design"));
    trl_hier_begin_scope(w, 0, trl_intern(w, "top"), 0, 0, 0);
    uint32_t v72  = trl_hier_add_var(w, trl_intern(w, "w72"), st72, 0, 0, 0);
    uint32_t v128 = trl_hier_add_var(w, trl_intern(w, "w128"), st128, 0, 0, 0);
    uint32_t vl8  = trl_hier_add_var(w, trl_intern(w, "l8"), stl8, 0, 0, 0);
    uint32_t vl8x = trl_hier_add_var(w, trl_intern(w, "l8x"), stl8, 0, 0, 0);
    uint32_t vs8  = trl_hier_add_var(w, trl_intern(w, "s8"), sts8, 0, 0, 0);
    trl_hier_end_scope(w);
    trl_flush_hierarchy(w);

    trl_vc_begin(w, 0);
    trl_vc_change_bytes(w, v72, 0, w72_a, 9);
    trl_vc_change_bytes(w, v72, 10, w72_b, 9);
    trl_vc_change_bytes(w, v72, 20, w72_c, 9);
    trl_vc_change_u64(w, v72, 30, 5);
    trl_vc_change_u64(w, v128, 0, 0xDEADBEEFu);
    trl_vc_change_u64(w, vl8, 0, 0xA5);
    trl_vc_change_u64(w, vl8, 10, 0x3C);
    trl_vc_change_u64(w, vl8x, 0, 0x81);
    trl_vc_change_bytes(w, vl8x, 10, l8_xz, 2);
    trl_vc_change_u64(w, vs8, 0, 0x5A);
    trl_vc_change_u64(w, vs8, 10, 0xC3);
    EXPECT(trl_vc_flush(w) == TRL_OK, "vc flush");
    EXPECT(trl_writer_close(w) == TRL_OK, "writer close");

    trl_reader_t *r = trl_reader_open(path);
    EXPECT(r != NULL, "reader opens");
    if (!r) return 1;
    EXPECT(trl_iter_vc(r, on_vc, NULL) == TRL_OK, "iter_vc");
    trl_reader_close(r);

    printf("  2-state 72-bit\n");
    expect_val(v72, 0, w72_a, 9, "w72@0");
    expect_val(v72, 10, w72_b, 9, "w72@10");
    expect_val(v72, 20, w72_c, 9, "w72@20");
    expect_val(v72, 30, w72_d, 9, "w72@30 (u64)");
    printf("  2-state 128-bit\n");
    expect_val(v128, 0, w128_a, 16, "w128@0 (u64)");
    printf("  4-state 8-bit\n");
    { uint8_t b = 0xA5; expect_val(vl8, 0, &b, 1, "l8@0 (u64)"); }
    { uint8_t b = 0x3C; expect_val(vl8, 10, &b, 1, "l8@10 (u64)"); }
    { uint8_t b = 0x81; expect_val(vl8x, 0, &b, 1, "l8x@0 (u64)"); }
    expect_val(vl8x, 10, l8_xz, 2, "l8x@10 (packed X/Z)");
    printf("  9-state 8-bit\n");
    { uint8_t b = 0x5A; expect_val(vs8, 0, &b, 1, "s8@0 (u64)"); }
    { uint8_t b = 0xC3; expect_val(vs8, 10, &b, 1, "s8@10 (u64)"); }

    if (g_failures) {
        fprintf(stderr, "test_vc_wide: %d failure(s)\n", g_failures);
        return 1;
    }
    printf("test_vc_wide: OK\n");
    return 0;
}
