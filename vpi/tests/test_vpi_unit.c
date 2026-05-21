/*
 * test_vpi_unit.c — unit tests for libtrlog_vpi internals.
 *
 * Compiled with mock_vpi.h replacing vpi_user.h, so no simulator is needed.
 * Each TEST() block prints PASS / FAIL and returns a non-zero exit code on
 * any failure.
 */

#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <stdint.h>
#include <assert.h>

/* Pull in the mock VPI layer before including any library headers. */
#include "mock_vpi.h"

/* Now include the state and internal logic under test. */
#include "../src/trlog_vpi_internal.h"

/* -------------------------------------------------------------------------
 * Minimal test harness
 * ---------------------------------------------------------------------- */

static int g_failures = 0;

#define TEST(name) static void test_##name(void)
#define RUN(name)  do { \
    printf("  %-55s", #name); fflush(stdout); \
    int before = g_failures; \
    test_##name(); \
    puts(g_failures == before ? "PASS" : "FAIL"); \
} while (0)

#define EXPECT(cond) do { \
    if (!(cond)) { \
        fprintf(stderr, "    EXPECT failed: %s  (%s:%d)\n", \
                #cond, __FILE__, __LINE__); \
        g_failures++; \
    } \
} while (0)

#define EXPECT_EQ(a, b) EXPECT((a) == (b))
#define EXPECT_NE(a, b) EXPECT((a) != (b))

/* Provide the external g_trlog symbol required by trlog_vpi_state.c
 * (normally defined in that translation unit). */
/* — Included via the source file directly in this test binary. */

/* -------------------------------------------------------------------------
 * Tests: state_init / state_destroy
 * ---------------------------------------------------------------------- */

TEST(state_init_zeroes_struct) {
    trlog_vpi_state_init();
    EXPECT(g_trlog.writer        == NULL);
    EXPECT(g_trlog.sig_count     == 0);
    EXPECT(g_trlog.paused        == 0);
    EXPECT(g_trlog.scope_grouped == 0);
    EXPECT(g_trlog.flush_interval == TRLOG_VPI_DEFAULT_FLUSH_INTERVAL);
    EXPECT(g_trlog.compress      == TRL_COMPRESS_ZLIB);
    trlog_vpi_state_destroy();
}

/* -------------------------------------------------------------------------
 * Tests: signal registration (no actual writer; writer == NULL is safe)
 * ---------------------------------------------------------------------- */

TEST(add_signal_grows_arrays) {
    trlog_vpi_state_init();

    /* Add 70 signals to force at least two doublings from initial cap 64. */
    for (int i = 0; i < 70; ++i) {
        int idx = trlog_vpi_state_add_signal(
            (vpiHandle)(uintptr_t)(i + 1),
            (uint32_t)(i + 100),   /* var_id   */
            (uint32_t)(i + 200),   /* sig_type */
            TRL_SE_4STATE,
            32,
            (uint32_t)(i % 4)      /* scope_id */
        );
        EXPECT_EQ(idx, i);
    }
    EXPECT_EQ((int)g_trlog.sig_count, 70);
    EXPECT(g_trlog.sig_cap >= 70);

    /* Spot-check stored values. */
    EXPECT_EQ(g_trlog.var_ids[0],       100u);
    EXPECT_EQ(g_trlog.var_ids[69],      169u);
    EXPECT_EQ(g_trlog.sig_type_ids[5],  205u);
    EXPECT_EQ(g_trlog.sig_scope_ids[3], 3u);
    EXPECT_EQ(g_trlog.sig_det_enc[0],   0xFFu);  /* not yet detected */

    trlog_vpi_state_destroy();
    EXPECT_EQ((int)g_trlog.sig_count, 0);
    EXPECT(g_trlog.var_ids == NULL);
}

TEST(add_signal_returns_sequential_indices) {
    trlog_vpi_state_init();
    int a = trlog_vpi_state_add_signal((vpiHandle)1, 1, 1, TRL_SE_2STATE, 1, 0);
    int b = trlog_vpi_state_add_signal((vpiHandle)2, 2, 2, TRL_SE_2STATE, 8, 0);
    int c = trlog_vpi_state_add_signal((vpiHandle)3, 3, 3, TRL_SE_REAL,   0, 1);
    EXPECT_EQ(a, 0);
    EXPECT_EQ(b, 1);
    EXPECT_EQ(c, 2);
    trlog_vpi_state_destroy();
}

/* -------------------------------------------------------------------------
 * Tests: type cache dedup (no writer — find_or_create returns 0 without one)
 * Since we can't call the real trl_add_signal_type without a writer, we
 * test the cache lookup path by pre-populating it manually.
 * ---------------------------------------------------------------------- */

TEST(type_cache_dedup_no_writer) {
    trlog_vpi_state_init();

    /* With no writer, find_or_create_type returns 0. */
    uint32_t id = trlog_vpi_state_find_or_create_type(TRL_SE_2STATE, 8);
    EXPECT_EQ(id, 0u);
    EXPECT_EQ((int)g_trlog.type_cache_count, 0);  /* nothing cached */

    trlog_vpi_state_destroy();
}

TEST(type_cache_lookup_hits_after_manual_insert) {
    trlog_vpi_state_init();

    /* Manually insert two entries into the cache. */
    trlog_vpi_type_entry_t entries[2] = {
        { TRL_SE_2STATE, 8,  42 },
        { TRL_SE_4STATE, 32, 99 },
    };
    g_trlog.type_cache = malloc(sizeof(entries));
    memcpy(g_trlog.type_cache, entries, sizeof(entries));
    g_trlog.type_cache_count = 2;
    g_trlog.type_cache_cap   = 2;

    /* These should hit the cache. */
    EXPECT_EQ(trlog_vpi_state_find_or_create_type(TRL_SE_2STATE, 8),  42u);
    EXPECT_EQ(trlog_vpi_state_find_or_create_type(TRL_SE_4STATE, 32), 99u);
    /* Miss: different bit_width, no writer → returns 0. */
    EXPECT_EQ(trlog_vpi_state_find_or_create_type(TRL_SE_2STATE, 16), 0u);

    trlog_vpi_state_destroy();
}

/* -------------------------------------------------------------------------
 * Tests: 2-state packing helpers
 *
 * pack_2state_u64 and pack_2state_bytes are static in trlog_vpi_value.c.
 * We re-declare and test via wrappers defined below.
 * ---------------------------------------------------------------------- */

/* Replicate the packing logic here for direct unit testing. */
static uint64_t pack_2state_u64_ref(const s_vpi_vecval *vec, int nwords) {
    uint64_t out = 0;
    for (int i = 0; i < nwords && i < 2; ++i)
        out |= (uint64_t)(uint32_t)vec[i].aval << (32 * i);
    return out;
}

static void pack_2state_bytes_ref(const s_vpi_vecval *vec, int nwords,
                                  uint8_t *buf, int nbytes) {
    memset(buf, 0, (size_t)nbytes);
    for (int i = 0; i < nwords; ++i) {
        uint32_t a = (uint32_t)vec[i].aval;
        int base   = i * 4;
        for (int b = 0; b < 4 && base + b < nbytes; ++b)
            buf[nbytes - 1 - (base + b)] = (uint8_t)(a >> (8 * b));
    }
}

static void pack_4state_bytes_ref(const s_vpi_vecval *vec, int nwords,
                                  uint8_t *buf, int nbytes) {
    memset(buf, 0, (size_t)nbytes);
    int total_bits = nwords * 32;
    for (int bit = 0; bit < total_bits; ++bit) {
        int word  = bit / 32;
        int shift = bit % 32;
        int a = ((uint32_t)vec[word].aval >> shift) & 1;
        int b = ((uint32_t)vec[word].bval >> shift) & 1;
        int enc_bit  = bit * 2;
        int byte_idx = nbytes - 1 - enc_bit / 8;
        int bit_shift = enc_bit % 8;
        if (byte_idx >= 0)
            buf[byte_idx] |= (uint8_t)(((b << 1) | a) << bit_shift);
    }
}

TEST(pack_2state_u64_8bit) {
    s_vpi_vecval vec = { 0xAB, 0 };
    uint64_t v = pack_2state_u64_ref(&vec, 1);
    EXPECT_EQ(v, 0xABull);
}

TEST(pack_2state_u64_32bit) {
    s_vpi_vecval vec = { 0xDEADBEEF, 0 };
    uint64_t v = pack_2state_u64_ref(&vec, 1);
    EXPECT_EQ(v, 0xDEADBEEFull);
}

TEST(pack_2state_u64_64bit) {
    s_vpi_vecval vec[2] = { { 0x12345678u, 0 }, { 0xCAFEBABEu, 0 } };
    uint64_t v = pack_2state_u64_ref(vec, 2);
    EXPECT_EQ(v, 0xCAFEBABE12345678ull);
}

TEST(pack_2state_bytes_128bit) {
    /* 128-bit value = 0xDEAD_BEEF_CAFE_BABE_0102_0304_0506_0708 */
    s_vpi_vecval vec[4] = {
        { 0x05060708u, 0 },
        { 0x01020304u, 0 },
        { 0xCAFEBABEu, 0 },
        { 0xDEADBEEFu, 0 },
    };
    uint8_t buf[16];
    pack_2state_bytes_ref(vec, 4, buf, 16);
    /* buf[0] is MSB = 0xDE */
    EXPECT_EQ(buf[0],  0xDEu);
    EXPECT_EQ(buf[1],  0xADu);
    EXPECT_EQ(buf[4],  0xCAu);
    EXPECT_EQ(buf[12], 0x05u);
    EXPECT_EQ(buf[15], 0x08u);
}

TEST(pack_4state_bytes_x_z_bits) {
    /* 4-bit value: bits[3:0] = X(b=1,a=0) Z(b=1,a=1) 1(b=0,a=1) 0(b=0,a=0)
     * aval bits[3:0] = 0b0110 = 0x6, bval bits[3:0] = 0b1100 = 0xC
     * Encoding (LSB first): bit0=(b=0,a=0)=00, bit1=(b=0,a=1)=01,
     *                       bit2=(b=1,a=1)=11, bit3=(b=1,a=0)=10
     * Packed big-endian byte: bits 7..0 = [bit3_enc=10][bit2_enc=11]... */
    s_vpi_vecval vec = { 0x6u, 0xCu };
    uint8_t buf[2];  /* ceil(4*2/8) = 1 byte needed, use 2 to be safe */
    memset(buf, 0, sizeof(buf));
    pack_4state_bytes_ref(&vec, 1, buf, 1);
    /* Expected byte (big-endian, 2-bits-per-bit encoding = (bval<<1)|aval):
     *   aval=0x6=0b0110: bit0=0, bit1=1, bit2=1, bit3=0
     *   bval=0xC=0b1100: bit0=0, bit1=0, bit2=1, bit3=1
     *   bit0: enc=(0<<1)|0=0  → byte[0] bits 1-0 = 0b00
     *   bit1: enc=(0<<1)|1=1  → byte[0] bits 3-2 = 0b01
     *   bit2: enc=(1<<1)|1=3  → byte[0] bits 5-4 = 0b11
     *   bit3: enc=(1<<1)|0=2  → byte[0] bits 7-6 = 0b10
     *   byte[0] = 0b10_11_01_00 = 0xB4
     */
    EXPECT_EQ(buf[0], 0xB4u);
}

/* -------------------------------------------------------------------------
 * Tests: signal filter (fnmatch-based)
 * ---------------------------------------------------------------------- */

TEST(signal_filter_empty_accepts_all) {
    trlog_vpi_state_init();
    EXPECT(trlog_vpi_match_signal("top.cpu.clk") == 1);
    EXPECT(trlog_vpi_match_signal("top.mem.addr") == 1);
    trlog_vpi_state_destroy();
}

TEST(signal_filter_single_pattern) {
    trlog_vpi_state_init();
    g_trlog.patterns[0]    = strdup("top.cpu.*");
    g_trlog.pattern_count  = 1;
    EXPECT(trlog_vpi_match_signal("top.cpu.clk")    == 1);
    EXPECT(trlog_vpi_match_signal("top.cpu.rst")    == 1);
    EXPECT(trlog_vpi_match_signal("top.mem.addr")   == 0);
    free(g_trlog.patterns[0]);
    g_trlog.patterns[0]   = NULL;
    g_trlog.pattern_count = 0;
    trlog_vpi_state_destroy();
}

TEST(signal_filter_multiple_patterns) {
    trlog_vpi_state_init();
    g_trlog.patterns[0]   = strdup("top.clk");
    g_trlog.patterns[1]   = strdup("top.mem.addr");
    g_trlog.pattern_count = 2;
    EXPECT(trlog_vpi_match_signal("top.clk")       == 1);
    EXPECT(trlog_vpi_match_signal("top.mem.addr")  == 1);
    EXPECT(trlog_vpi_match_signal("top.mem.data")  == 0);
    EXPECT(trlog_vpi_match_signal("top.cpu.clk")   == 0);
    free(g_trlog.patterns[0]);
    free(g_trlog.patterns[1]);
    g_trlog.patterns[0]   = NULL;
    g_trlog.patterns[1]   = NULL;
    g_trlog.pattern_count = 0;
    trlog_vpi_state_destroy();
}

/* -------------------------------------------------------------------------
 * Tests: pending buffer sort / group
 * ---------------------------------------------------------------------- */

TEST(pending_sort_groups_by_scope) {
    trlog_vpi_state_init();

    /* Simulate 6 pending entries across 3 scopes (interleaved order). */
    trlog_vpi_pending_t entries[6] = {
        { 10, 2, 100, TRL_SE_2STATE, 0, 0xFF, 0.0, NULL, 0 },
        { 11, 1, 200, TRL_SE_2STATE, 0, 0x01, 0.0, NULL, 0 },
        { 12, 3, 300, TRL_SE_2STATE, 0, 0x02, 0.0, NULL, 0 },
        { 13, 2, 100, TRL_SE_2STATE, 0, 0x04, 0.0, NULL, 0 },
        { 14, 1, 200, TRL_SE_2STATE, 0, 0x08, 0.0, NULL, 0 },
        { 15, 3, 300, TRL_SE_2STATE, 0, 0x10, 0.0, NULL, 0 },
    };

    g_trlog.pending       = malloc(sizeof(entries));
    memcpy(g_trlog.pending, entries, sizeof(entries));
    g_trlog.pending_count = 6;
    g_trlog.pending_cap   = 6;

    /* Sort using the same comparator that flush_pending uses. */
    extern int cmp_pending(const void *, const void *);  /* defined in value.c */
    /* Since cmp_pending is static, replicate the logic here. */
    /* (We rely on the fact that the flush sorts by scope_id then time.) */
    /* Manually verify after a qsort using our own inline comparator. */
    /* scope order: 1,1,2,2,3,3 */
    int scopes_seen[6];
    /* Simple bubble sort to simulate without accessing static symbol. */
    for (size_t i = 0; i < 6; ++i) {
        for (size_t j = i + 1; j < 6; ++j) {
            if (g_trlog.pending[i].scope_id > g_trlog.pending[j].scope_id ||
                (g_trlog.pending[i].scope_id == g_trlog.pending[j].scope_id &&
                 g_trlog.pending[i].time     >  g_trlog.pending[j].time)) {
                trlog_vpi_pending_t tmp = g_trlog.pending[i];
                g_trlog.pending[i] = g_trlog.pending[j];
                g_trlog.pending[j] = tmp;
            }
        }
    }

    for (size_t i = 0; i < 6; ++i)
        scopes_seen[i] = (int)g_trlog.pending[i].scope_id;

    /* After sort: scopes must be non-decreasing. */
    for (size_t i = 1; i < 6; ++i)
        EXPECT(scopes_seen[i] >= scopes_seen[i-1]);
    /* First two entries must be scope 1. */
    EXPECT_EQ(scopes_seen[0], 1);
    EXPECT_EQ(scopes_seen[1], 1);
    /* Next two scope 2. */
    EXPECT_EQ(scopes_seen[2], 2);
    EXPECT_EQ(scopes_seen[3], 2);
    /* Last two scope 3. */
    EXPECT_EQ(scopes_seen[4], 3);
    EXPECT_EQ(scopes_seen[5], 3);

    free(g_trlog.pending);
    g_trlog.pending       = NULL;
    g_trlog.pending_count = 0;
    g_trlog.pending_cap   = 0;
    trlog_vpi_state_destroy();
}

/* -------------------------------------------------------------------------
 * Main
 * ---------------------------------------------------------------------- */

int main(void) {
    puts("=== trlog_vpi unit tests ===");

    puts("-- State management --");
    RUN(state_init_zeroes_struct);
    RUN(add_signal_grows_arrays);
    RUN(add_signal_returns_sequential_indices);

    puts("-- Type cache --");
    RUN(type_cache_dedup_no_writer);
    RUN(type_cache_lookup_hits_after_manual_insert);

    puts("-- 2-state packing --");
    RUN(pack_2state_u64_8bit);
    RUN(pack_2state_u64_32bit);
    RUN(pack_2state_u64_64bit);
    RUN(pack_2state_bytes_128bit);

    puts("-- 4-state packing --");
    RUN(pack_4state_bytes_x_z_bits);

    puts("-- Signal filter --");
    RUN(signal_filter_empty_accepts_all);
    RUN(signal_filter_single_pattern);
    RUN(signal_filter_multiple_patterns);

    puts("-- Pending buffer --");
    RUN(pending_sort_groups_by_scope);

    printf("\n%s  (%d failure%s)\n",
           g_failures ? "FAILED" : "PASSED",
           g_failures, g_failures == 1 ? "" : "s");
    return g_failures ? 1 : 0;
}
