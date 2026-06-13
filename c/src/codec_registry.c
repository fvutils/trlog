/*
 * codec_registry.c — codec registry + the re-homed built-in core codecs.
 *
 * Phase 1 (design §4.2/§4.3, §5.1/§5.3): the existing VC/TXN encoders are
 * registered as `org.fvutils.trlog.core.valuechange` / `…core.record` and
 * reached through the codec vtable like any other codec. The vtable
 * encode/decode slots wrap the existing internal functions unchanged, so the
 * default path is byte-for-byte identical (the c_golden harness stays green).
 *
 * The registry is a plain, append-only, string-keyed table. It is not
 * thread-safe — codecs are expected to be registered at library init (the core
 * ones via the constructor below) or single-threaded setup before any
 * writer/reader runs, which matches the design's lifecycle.
 */

#include "varint_internal.h"   /* pulls in trl_internal.h + trl/trl_codec.h */

/* ----------------------------------------------------------------------- *
 * Registry storage
 * ----------------------------------------------------------------------- */

#define TRL_MAX_CODECS 64

static const trl_vc_codec_t  *g_vc_codecs[TRL_MAX_CODECS];
static size_t                 g_vc_count;
static const trl_txn_codec_t *g_txn_codecs[TRL_MAX_CODECS];
static size_t                 g_txn_count;

int trl_register_vc_codec(const trl_vc_codec_t *codec) {
    if (!codec || !codec->codec_id) return -1;
    /* Replace if an id is registered twice (last registration wins). */
    for (size_t i = 0; i < g_vc_count; ++i) {
        if (strcmp(g_vc_codecs[i]->codec_id, codec->codec_id) == 0) {
            g_vc_codecs[i] = codec;
            return 0;
        }
    }
    if (g_vc_count >= TRL_MAX_CODECS) return -1;
    g_vc_codecs[g_vc_count++] = codec;
    return 0;
}

int trl_register_txn_codec(const trl_txn_codec_t *codec) {
    if (!codec || !codec->codec_id) return -1;
    for (size_t i = 0; i < g_txn_count; ++i) {
        if (strcmp(g_txn_codecs[i]->codec_id, codec->codec_id) == 0) {
            g_txn_codecs[i] = codec;
            return 0;
        }
    }
    if (g_txn_count >= TRL_MAX_CODECS) return -1;
    g_txn_codecs[g_txn_count++] = codec;
    return 0;
}

const trl_vc_codec_t *trl_lookup_vc_codec(const char *codec_id) {
    if (!codec_id) return NULL;
    for (size_t i = 0; i < g_vc_count; ++i) {
        if (strcmp(g_vc_codecs[i]->codec_id, codec_id) == 0) return g_vc_codecs[i];
    }
    return NULL;
}

const trl_txn_codec_t *trl_lookup_txn_codec(const char *codec_id) {
    if (!codec_id) return NULL;
    for (size_t i = 0; i < g_txn_count; ++i) {
        if (strcmp(g_txn_codecs[i]->codec_id, codec_id) == 0) return g_txn_codecs[i];
    }
    return NULL;
}

/* ----------------------------------------------------------------------- *
 * Storage-SPI surface
 * ----------------------------------------------------------------------- */

uint32_t trl_store_intern(trl_store *st, const char *s) {
    if (!st || !st->writer) return 0;
    return trl_string_table_intern(&st->writer->strtab, s);
}

int trl_blkout_append(trl_blkout *out, const uint8_t *data, size_t len) {
    if (!out || !out->buf) return -1;
    return trl_buf_append(out->buf, data, len) == TRL_OK ? 0 : -1;
}

/* ----------------------------------------------------------------------- *
 * Phase 3.5 — writer codec selection + the value-change change-accessor.
 * ----------------------------------------------------------------------- */

int trl_writer_set_vc_codec(trl_writer_t *w, const char *codec_id) {
    if (!w) return -1;
    if (!codec_id) {                       /* revert to the core default */
        if (w->vc_codec) w->vc_codec->close(w->vc_codec_self);
        w->vc_codec = NULL;
        w->vc_codec_self = NULL;
        return 0;
    }
    const trl_vc_codec_t *codec = trl_lookup_vc_codec(codec_id);
    if (!codec) return -1;
    if (w->vc_codec) w->vc_codec->close(w->vc_codec_self);
    trl_store store = { .writer = w, .reader = NULL };
    w->vc_codec = codec;
    w->vc_codec_self = codec->open_writer ?
        codec->open_writer(&store, 0, NULL, 0) : NULL;
    return 0;
}

/* Walk the flattened var-major change list to the i-th item. Returns the
 * change (and its owning var_id) or NULL if i is past the end. */
static const trl_vc_change_t *vc_change_lookup(trl_writer_t *w, size_t i,
                                               uint32_t *out_var_id) {
    size_t seen = 0;
    for (size_t v = 0; v < w->vc.var_count; ++v) {
        const trl_vc_var_changes_t *vc = &w->vc.vars[v];
        if (i < seen + vc->count) {
            if (out_var_id) *out_var_id = vc->var_id;
            return &vc->items[i - seen];
        }
        seen += vc->count;
    }
    return NULL;
}

size_t trl_store_vc_change_count(trl_store *st) {
    if (!st || !st->writer) return 0;
    size_t n = 0;
    for (size_t v = 0; v < st->writer->vc.var_count; ++v)
        n += st->writer->vc.vars[v].count;
    return n;
}

uint64_t trl_store_vc_start_time(trl_store *st) {
    return (st && st->writer) ? st->writer->vc.start_time : 0;
}

uint64_t trl_store_vc_end_time(trl_store *st) {
    return (st && st->writer) ? st->writer->vc.end_time : 0;
}

int trl_store_vc_change_at(trl_store *st, size_t i, uint32_t *var_id,
                           uint64_t *time, uint32_t *kind, uint64_t *u64val) {
    if (!st || !st->writer) return -1;
    uint32_t vid = 0;
    const trl_vc_change_t *c = vc_change_lookup(st->writer, i, &vid);
    if (!c) return -1;
    if (var_id) *var_id = vid;
    if (time)   *time   = c->time;
    if (kind)   *kind   = (uint32_t)c->kind;
    if (u64val) *u64val = (c->kind == TRL_VC_VALUE_U64) ? c->value.u64 : 0;
    return 0;
}

/* ----------------------------------------------------------------------- *
 * Built-in: org.fvutils.trlog.core.valuechange  (design §5.1)
 * ----------------------------------------------------------------------- */

static void *core_vc_open_writer(trl_store *st, uint32_t stream_id,
                                 const void *params, size_t param_len) {
    (void)st; (void)stream_id; (void)params; (void)param_len;
    return NULL;   /* stateless: state lives in the bound writer */
}

static void *core_vc_open_reader(trl_store *st, uint32_t stream_id,
                                 const void *params, size_t param_len) {
    (void)st; (void)stream_id; (void)params; (void)param_len;
    return NULL;
}

static void core_vc_close(void *self) { (void)self; }

static int core_vc_finalize(void *self, trl_store *st) {
    (void)self; (void)st;
    return 0;   /* no trace-wide accumulated metadata */
}

static int core_vc_encode_block(void *self, trl_store *st, trl_blkout *out) {
    (void)self;
    trl_writer_t *w = st->writer;
    trl_status_t rc = trl_vc_block_encode(&w->vc, w->global_var_sig_type_ids,
                                          w->global_var_cap, &w->typereg,
                                          w->compress, out->buf);
    return rc == TRL_OK ? 0 : (int)rc;
}

static int core_vc_decode_block(void *self, trl_store *st,
                                const uint8_t *payload, size_t len, uint8_t flags,
                                trl_vc_cb emit, void *user) {
    (void)self;
    trl_reader_t *r = st->reader;
    trl_status_t rc = trl_vc_iter_payload(payload, len, flags,
                                          r->var_sig_type_ids, r->var_sig_cap,
                                          &r->typereg, emit, user);
    return rc == TRL_OK ? 0 : (int)rc;
}

static int core_vc_input_streams(void *self, trl_store *st,
                                 uint32_t **out_ids, size_t *n) {
    (void)self; (void)st;
    if (out_ids) *out_ids = NULL;
    if (n) *n = 0;
    return 0;
}

static const trl_vc_codec_t CORE_VC_CODEC = {
    .codec_id = TRL_CODEC_CORE_VALUECHANGE,
    .version = 1,
    .caps = TRL_CAP_RANDOM_ACCESS | TRL_CAP_LOSSLESS,
    .open_writer = core_vc_open_writer,
    .open_reader = core_vc_open_reader,
    .close = core_vc_close,
    .finalize = core_vc_finalize,
    .encode_block = core_vc_encode_block,
    .decode_block = core_vc_decode_block,
    .input_streams = core_vc_input_streams,
};

/* ----------------------------------------------------------------------- *
 * Built-in: org.fvutils.trlog.core.record  (design §5.3)
 * ----------------------------------------------------------------------- */

static void *core_txn_open_writer(trl_store *st, uint32_t stream_id,
                                  const void *params, size_t param_len) {
    (void)st; (void)stream_id; (void)params; (void)param_len;
    return NULL;
}

static void *core_txn_open_reader(trl_store *st, uint32_t stream_id,
                                  const void *params, size_t param_len) {
    (void)st; (void)stream_id; (void)params; (void)param_len;
    return NULL;
}

static void core_txn_close(void *self) { (void)self; }

static int core_txn_finalize(void *self, trl_store *st) {
    (void)self; (void)st;
    return 0;
}

static int core_txn_encode_block(void *self, trl_store *st, trl_blkout *out) {
    (void)self;
    trl_writer_t *w = st->writer;
    trl_status_t rc = trl_txn_block_encode(&w->txn, w->compress, out->buf);
    return rc == TRL_OK ? 0 : (int)rc;
}

static int core_txn_decode_block(void *self, trl_store *st,
                                 const uint8_t *payload, size_t len, uint8_t flags,
                                 trl_txn_cb_t emit, void *user) {
    /* The C reader does not yet iterate transaction blocks (no public
     * trl_iter_txn); decode is a no-op placeholder until that lands. */
    (void)self; (void)st; (void)payload; (void)len; (void)flags;
    (void)emit; (void)user;
    return 0;
}

static int core_txn_input_streams(void *self, trl_store *st,
                                  uint32_t **out_ids, size_t *n) {
    (void)self; (void)st;
    if (out_ids) *out_ids = NULL;
    if (n) *n = 0;
    return 0;
}

static const trl_txn_codec_t CORE_TXN_CODEC = {
    .codec_id = TRL_CODEC_CORE_RECORD,
    .version = 1,
    .caps = TRL_CAP_RANDOM_ACCESS | TRL_CAP_LOSSLESS,
    .open_writer = core_txn_open_writer,
    .open_reader = core_txn_open_reader,
    .close = core_txn_close,
    .finalize = core_txn_finalize,
    .encode_block = core_txn_encode_block,
    .decode_block = core_txn_decode_block,
    .input_streams = core_txn_input_streams,
};

/* ----------------------------------------------------------------------- *
 * Self-registration at library init (design §4.2). Also callable explicitly
 * (idempotent) so static linkers that drop the constructor still work.
 * ----------------------------------------------------------------------- */

void trl_register_core_codecs(void) {
    trl_register_vc_codec(&CORE_VC_CODEC);
    trl_register_txn_codec(&CORE_TXN_CODEC);
}

__attribute__((constructor))
static void trl_register_core_codecs_ctor(void) {
    trl_register_core_codecs();
}
