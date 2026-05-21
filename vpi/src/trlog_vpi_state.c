/*
 * trlog_vpi_state.c — global state lifecycle and bookkeeping helpers.
 */

#include "trlog_vpi_internal.h"

trlog_vpi_state_t g_trlog;

/* -------------------------------------------------------------------------
 * Initialise / destroy
 * ---------------------------------------------------------------------- */

void trlog_vpi_state_init(void) {
    memset(&g_trlog, 0, sizeof(g_trlog));
    g_trlog.flush_interval = TRLOG_VPI_DEFAULT_FLUSH_INTERVAL;
    g_trlog.compress       = TRL_COMPRESS_ZLIB;
}

static void free_patterns(void) {
    for (int i = 0; i < g_trlog.pattern_count; ++i) {
        free(g_trlog.patterns[i]);
        g_trlog.patterns[i] = NULL;
    }
    g_trlog.pattern_count = 0;
}

static void free_pending(void) {
    for (size_t i = 0; i < g_trlog.pending_count; ++i) {
        if (g_trlog.pending[i].heap_data)
            free(g_trlog.pending[i].heap_data);
    }
    free(g_trlog.pending);
    g_trlog.pending       = NULL;
    g_trlog.pending_count = 0;
    g_trlog.pending_cap   = 0;
}

void trlog_vpi_state_destroy(void) {
    /* Free per-signal callback structs before nulling the arrays. */
    if (g_trlog.sig_cb_times) {
        for (size_t i = 0; i < g_trlog.sig_count; ++i)
            free(g_trlog.sig_cb_times[i]);
    }
    if (g_trlog.sig_cb_values) {
        for (size_t i = 0; i < g_trlog.sig_count; ++i)
            free(g_trlog.sig_cb_values[i]);
    }
    free(g_trlog.var_ids);
    free(g_trlog.sig_handles);
    free(g_trlog.sig_type_ids);
    free(g_trlog.sig_encs);
    free(g_trlog.sig_widths);
    free(g_trlog.sig_scope_ids);
    free(g_trlog.sig_det_enc);
    free(g_trlog.sig_cb_times);
    free(g_trlog.sig_cb_values);
    free(g_trlog.sig_changed);
    free(g_trlog.type_cache);
    free_pending();
    free_patterns();
    memset(&g_trlog, 0, sizeof(g_trlog));
}

/* -------------------------------------------------------------------------
 * Signal registration helpers
 * ---------------------------------------------------------------------- */

/* Grow all parallel signal arrays to at least (g_trlog.sig_count + 1).
 * Returns 0 on success, -1 on allocation failure. */
static int ensure_sig_cap(void) {
    if (g_trlog.sig_count < g_trlog.sig_cap)
        return 0;

    size_t new_cap = g_trlog.sig_cap ? g_trlog.sig_cap * 2 : 64;

#define GROW(field, type) \
    do { \
        type *_p = realloc(g_trlog.field, new_cap * sizeof(type)); \
        if (!_p) return -1; \
        g_trlog.field = _p; \
    } while (0)

    GROW(var_ids,        uint32_t);
    GROW(sig_handles,    vpiHandle);
    GROW(sig_type_ids,   uint32_t);
    GROW(sig_encs,       uint8_t);
    GROW(sig_widths,     uint32_t);
    GROW(sig_scope_ids,  uint32_t);
    GROW(sig_det_enc,    uint8_t);
    GROW(sig_cb_times,   s_vpi_time *);
    GROW(sig_cb_values,  s_vpi_value *);
    GROW(sig_changed,    uint8_t);

#undef GROW

    /* Initialise the detection-encoding for newly allocated slots. */
    memset(g_trlog.sig_det_enc + g_trlog.sig_cap, 0xFF,
           (new_cap - g_trlog.sig_cap) * sizeof(uint8_t));
    /* Zero the new callback-struct pointer and de-bounce slots. */
    memset(g_trlog.sig_cb_times  + g_trlog.sig_cap, 0,
           (new_cap - g_trlog.sig_cap) * sizeof(s_vpi_time *));
    memset(g_trlog.sig_cb_values + g_trlog.sig_cap, 0,
           (new_cap - g_trlog.sig_cap) * sizeof(s_vpi_value *));
    memset(g_trlog.sig_changed   + g_trlog.sig_cap, 0,
           (new_cap - g_trlog.sig_cap) * sizeof(uint8_t));

    g_trlog.sig_cap = new_cap;
    return 0;
}

/* Append a signal to the registration arrays.
 * Returns the local_idx on success, or -1 on allocation failure. */
int trlog_vpi_state_add_signal(vpiHandle handle, uint32_t var_id,
                               uint32_t sig_type_id, uint8_t enc,
                               uint32_t bit_width, uint32_t scope_id) {
    if (ensure_sig_cap() < 0)
        return -1;

    int idx = (int)g_trlog.sig_count++;
    g_trlog.var_ids       [idx] = var_id;
    g_trlog.sig_handles   [idx] = handle;
    g_trlog.sig_type_ids  [idx] = sig_type_id;
    g_trlog.sig_encs      [idx] = enc;
    g_trlog.sig_widths    [idx] = bit_width;
    g_trlog.sig_scope_ids [idx] = scope_id;
    g_trlog.sig_det_enc   [idx] = 0xFF;  /* not yet detected */
    g_trlog.sig_cb_times  [idx] = NULL;  /* filled by register_vc_callback */
    g_trlog.sig_cb_values [idx] = NULL;
    g_trlog.sig_changed   [idx] = 0;
    return idx;
}

/* -------------------------------------------------------------------------
 * Signal type dedup cache
 * ---------------------------------------------------------------------- */

/* Return the sig_type_id for (encoding, bit_width), creating one if needed.
 * Returns 0 on allocation failure (0 is never a valid sig_type_id). */
uint32_t trlog_vpi_state_find_or_create_type(uint8_t encoding,
                                             uint32_t bit_width) {
    /* Linear scan: typically < 20 distinct types per design. */
    for (size_t i = 0; i < g_trlog.type_cache_count; ++i) {
        if (g_trlog.type_cache[i].encoding  == encoding &&
            g_trlog.type_cache[i].bit_width == bit_width)
            return g_trlog.type_cache[i].sig_type_id;
    }

    /* Not found — register a new signal type with libtrl. */
    if (!g_trlog.writer)
        return 0;

    uint8_t radix = 0; /* RX_HEX=0 for all types; real viewer handles display */
    uint32_t new_id = trl_add_signal_type(g_trlog.writer, encoding,
                                          bit_width, radix);
    if (new_id == 0)
        return 0;

    /* Grow cache if needed. */
    if (g_trlog.type_cache_count >= g_trlog.type_cache_cap) {
        size_t new_cap = g_trlog.type_cache_cap ? g_trlog.type_cache_cap * 2 : 16;
        trlog_vpi_type_entry_t *p = realloc(g_trlog.type_cache,
                                            new_cap * sizeof(*p));
        if (!p) return 0;
        g_trlog.type_cache     = p;
        g_trlog.type_cache_cap = new_cap;
    }

    size_t idx = g_trlog.type_cache_count++;
    g_trlog.type_cache[idx].encoding   = encoding;
    g_trlog.type_cache[idx].bit_width  = bit_width;
    g_trlog.type_cache[idx].sig_type_id = new_id;
    return new_id;
}
