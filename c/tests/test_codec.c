/*
 * test_codec.c — Phase 1b unit tests for the C codec registry + ABI surface.
 *
 * Verifies that the built-in core codecs self-register, lookup of a known id
 * returns the vtable, lookup of an unknown id returns NULL, and a third-party
 * codec can register and be found. Uses the same lightweight TEST/EXPECT style
 * as vpi/tests.
 */

#include <stdio.h>
#include <string.h>

#include "trl/trl_codec.h"

static int g_failures = 0;

#define EXPECT(cond) do { \
    if (!(cond)) { \
        fprintf(stderr, "    EXPECT failed: %s  (%s:%d)\n", #cond, __FILE__, __LINE__); \
        g_failures++; \
    } \
} while (0)

/* A trivial third-party VC codec used to exercise registration. */
static int dummy_encode(void *self, trl_store *st, trl_blkout *out) {
    (void)self; (void)st; (void)out; return 0;
}
static int dummy_decode(void *self, trl_store *st, const uint8_t *p, size_t n,
                        uint8_t f, trl_vc_cb e, void *u) {
    (void)self; (void)st; (void)p; (void)n; (void)f; (void)e; (void)u; return 0;
}
static const trl_vc_codec_t DUMMY = {
    .codec_id = "com.example.codec.dummy",
    .version = 3,
    .caps = TRL_CAP_LOSSLESS,
    .encode_block = dummy_encode,
    .decode_block = dummy_decode,
};

int main(void) {
    /* Core codecs self-registered at library init. */
    const trl_vc_codec_t *vc = trl_lookup_vc_codec(TRL_CODEC_CORE_VALUECHANGE);
    EXPECT(vc != NULL);
    if (vc) {
        EXPECT(strcmp(vc->codec_id, TRL_CODEC_CORE_VALUECHANGE) == 0);
        EXPECT(vc->version == 1);
        EXPECT((vc->caps & TRL_CAP_LOSSLESS) != 0);
        EXPECT(vc->encode_block != NULL && vc->decode_block != NULL);
        EXPECT(vc->finalize != NULL);   /* full lifecycle surface present */
    }

    const trl_txn_codec_t *tx = trl_lookup_txn_codec(TRL_CODEC_CORE_RECORD);
    EXPECT(tx != NULL);
    if (tx) EXPECT(strcmp(tx->codec_id, TRL_CODEC_CORE_RECORD) == 0);

    /* Unknown id -> NULL (the skip-unknown precondition, design §4.2). */
    EXPECT(trl_lookup_vc_codec("com.example.does.not-exist") == NULL);
    EXPECT(trl_lookup_txn_codec("com.example.does.not-exist") == NULL);

    /* Third-party registration round-trip. */
    EXPECT(trl_register_vc_codec(&DUMMY) == 0);
    const trl_vc_codec_t *got = trl_lookup_vc_codec("com.example.codec.dummy");
    EXPECT(got == &DUMMY);
    if (got) EXPECT(got->version == 3);

    /* NULL / malformed registration is rejected. */
    EXPECT(trl_register_vc_codec(NULL) != 0);

    printf("test_codec: %s (failures=%d)\n", g_failures ? "FAIL" : "PASS", g_failures);
    return g_failures ? 1 : 0;
}
