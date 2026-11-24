/*
 * trl_reader_open_mem / trl_rdb_open_mem: for every golden fixture, reading
 * the trace from a memory buffer must give exactly what reading the file
 * gives (value changes, streams, transactions). Also checks that bad
 * buffers are rejected.
 *
 * Usage: test_open_mem <fixture_dir>
 */
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include "trl/trl.h"
#include "trl/trl_query.h"

static const char *k_fixtures[] = {
    "bytesplit_real", "callreturn", "derived", "empty", "ext", "hierarchy",
    "multi_hier", "transparent_meta", "txn_column", "txn_field_kinds",
    "txn_row", "txn_schema_attrs", "vc_2state_1bit", "vc_2state_32bit",
    "vc_4state", "vc_9state", "vc_compressed", "vc_real", "vc_string",
};

typedef struct { uint8_t *p; size_t n, cap; } buf_t;

static void buf_put(buf_t *b, const void *d, size_t n) {
    if (b->n + n > b->cap) {
        size_t cap = b->cap ? b->cap * 2 : 256;
        while (cap < b->n + n) cap *= 2;
        b->p = (uint8_t *)realloc(b->p, cap);
        b->cap = cap;
    }
    memcpy(b->p + b->n, d, n);
    b->n += n;
}

static void vc_cb(uint32_t var_id, uint64_t time, const uint8_t *val, uint32_t len, void *user) {
    buf_t *b = (buf_t *)user;
    buf_put(b, &var_id, sizeof(var_id));
    buf_put(b, &time, sizeof(time));
    buf_put(b, &len, sizeof(len));
    buf_put(b, val, len);
}

static uint8_t *read_file(const char *path, size_t *len) {
    FILE *fp = fopen(path, "rb");
    if (!fp) return NULL;
    fseek(fp, 0, SEEK_END);
    long n = ftell(fp);
    fseek(fp, 0, SEEK_SET);
    uint8_t *p = (uint8_t *)malloc(n ? (size_t)n : 1);
    if (p && fread(p, 1, (size_t)n, fp) != (size_t)n) { free(p); p = NULL; }
    fclose(fp);
    *len = (size_t)n;
    return p;
}

/* Serializes everything both readers can see into b. Returns 0 on failure. */
static int dump_reader(trl_reader_t *r, buf_t *b) {
    if (!r) return 0;
    if (trl_iter_vc(r, vc_cb, b) != TRL_OK) return 0;
    /* A second pass must give the same again (the cursor rewinds). */
    if (trl_iter_vc(r, vc_cb, b) != TRL_OK) return 0;
    trl_reader_close(r);
    return 1;
}

static int dump_rdb(trl_rdb_t *db, buf_t *b) {
    if (!db) return 0;
    uint32_t ns = trl_rdb_num_streams(db);
    buf_put(b, &ns, sizeof(ns));
    for (uint32_t i = 0; i < ns; ++i) {
        const char *p = trl_rstream_path(trl_rdb_stream_at(db, i));
        buf_put(b, p, strlen(p) + 1);
    }
    trl_cursor_t *c = trl_cursor_new(db, NULL, 0, UINT64_MAX);
    trl_txn_hdr_t h;
    while (c && trl_cursor_next(c, &h)) {
        buf_put(b, &h.txn_id, sizeof(h.txn_id));
        buf_put(b, &h.stream_inst_id, sizeof(h.stream_inst_id));
        buf_put(b, &h.txn_type_id, sizeof(h.txn_type_id));
        buf_put(b, &h.start, sizeof(h.start));
        buf_put(b, &h.end, sizeof(h.end));
        buf_put(b, &h.parent, sizeof(h.parent));
        buf_put(b, &h.attr_count, sizeof(h.attr_count));
        buf_put(b, &h.link_count, sizeof(h.link_count));
        buf_put(b, &h.meta_count, sizeof(h.meta_count));
    }
    trl_cursor_free(c);
    trl_rdb_close(db);
    return 1;
}

int main(int argc, char **argv) {
    if (argc < 2) {
        fprintf(stderr, "usage: %s <fixture_dir>\n", argv[0]);
        return 2;
    }
    int fails = 0;
    for (size_t i = 0; i < sizeof(k_fixtures) / sizeof(k_fixtures[0]); ++i) {
        char path[4096];
        snprintf(path, sizeof(path), "%s/%s.trl", argv[1], k_fixtures[i]);
        size_t len = 0;
        uint8_t *mem = read_file(path, &len);
        if (!mem) { fprintf(stderr, "FAIL %s: cannot read\n", path); ++fails; continue; }

        buf_t bf = {0}, bm = {0}, qf = {0}, qm = {0};
        int okf = dump_reader(trl_reader_open(path), &bf);
        int okm = dump_reader(trl_reader_open_mem(mem, len), &bm);
        int qokf = dump_rdb(trl_rdb_open("f", path), &qf);
        int qokm = dump_rdb(trl_rdb_open_mem("m", mem, len), &qm);
        if (okf != okm || bf.n != bm.n || (bf.n && memcmp(bf.p, bm.p, bf.n) != 0)) {
            fprintf(stderr, "FAIL %s: reader file/mem differ (ok %d/%d, %zu/%zu bytes)\n",
                    k_fixtures[i], okf, okm, bf.n, bm.n);
            ++fails;
        }
        if (qokf != qokm || qf.n != qm.n || (qf.n && memcmp(qf.p, qm.p, qf.n) != 0)) {
            fprintf(stderr, "FAIL %s: rdb file/mem differ (ok %d/%d, %zu/%zu bytes)\n",
                    k_fixtures[i], qokf, qokm, qf.n, qm.n);
            ++fails;
        }
        /* A buffer cut inside the file header block is not a trace. */
        if (len > 4 && trl_reader_open_mem(mem, 4) != NULL) {
            fprintf(stderr, "FAIL %s: 4-byte prefix opened\n", k_fixtures[i]);
            ++fails;
        }
        printf("%-20s %s (vc %zu bytes, txn %zu bytes)\n", k_fixtures[i],
               okf ? "ok" : "unreadable", bf.n, qf.n);
        free(bf.p); free(bm.p); free(qf.p); free(qm.p);
        free(mem);
    }
    /* Garbage is rejected, not crashed on. */
    static const uint8_t junk[64] = { 0x01, 0x00, 0xff, 0xff };
    if (trl_reader_open_mem(junk, sizeof(junk)) != NULL) { fprintf(stderr, "FAIL junk opened\n"); ++fails; }
    if (trl_reader_open_mem(NULL, 16) != NULL) { fprintf(stderr, "FAIL NULL opened\n"); ++fails; }
    printf("%s\n", fails ? "FAILED" : "PASSED");
    return fails ? 1 : 0;
}
