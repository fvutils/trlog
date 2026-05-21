/*
 * trlog_vpi_value.c — value-change callbacks, de-bounce, packing, and snapshot.
 *
 * De-bounce pattern
 * -----------------
 * VPI simulators fire cbValueChange at every signal assignment, including
 * intra-time-step delta cycles.  Recording after each callback produces
 * duplicate timestamps and corrupt delta values in the encoded blocks.
 *
 * Instead we use a two-stage approach:
 *   1. cbValueChange: mark the signal as changed; on the first change in a
 *      new time step register a cbAfterDelay(0) callback.
 *   2. cbAfterDelay(0) (the "delta callback"): fires at the same sim-time
 *      but after the active delta region has settled.  At this point we
 *      read the current (final) value of every marked signal and write it
 *      to the TRLOG writer, then clear the marks.
 *
 * If further delta cycles occur after the delta callback fires, new
 * cbValueChange notifications generate a fresh cbAfterDelay(0), ensuring
 * we always capture the final settled value.
 */

#include "trlog_vpi_internal.h"

/* -------------------------------------------------------------------------
 * Value packing helpers
 * ---------------------------------------------------------------------- */

/* Pack aval words into a uint64_t (2-state, width <= 64).
 * Words are in ascending order: word[0] = bits[31:0]. */
static uint64_t pack_2state_u64(const s_vpi_vecval *vec, int nwords) {
    uint64_t out = 0;
    for (int i = 0; i < nwords && i < 2; ++i)
        out |= (uint64_t)(uint32_t)vec[i].aval << (32 * i);
    return out;
}

/* Pack aval words big-endian into a byte buffer (2-state, width > 64).
 * buf must be ceil(bit_width/8) bytes. */
static void pack_2state_bytes(const s_vpi_vecval *vec, int nwords,
                              uint8_t *buf, int nbytes) {
    memset(buf, 0, (size_t)nbytes);
    for (int i = 0; i < nwords; ++i) {
        uint32_t a = (uint32_t)vec[i].aval;
        int base   = i * 4;
        for (int b = 0; b < 4 && base + b < nbytes; ++b)
            buf[nbytes - 1 - (base + b)] = (uint8_t)(a >> (8 * b));
    }
}

/* Pack aval+bval words into the 2-bits-per-bit TRL_SE_4STATE encoding.
 * buf must be ceil(bit_width*2/8) bytes. */
static void pack_4state_bytes(const s_vpi_vecval *vec, int nwords,
                              uint8_t *buf, int nbytes) {
    memset(buf, 0, (size_t)nbytes);
    int total_bits = nwords * 32;
    for (int bit = 0; bit < total_bits; ++bit) {
        int word  = bit / 32;
        int shift = bit % 32;
        int a = ((uint32_t)vec[word].aval >> shift) & 1;
        int b = ((uint32_t)vec[word].bval >> shift) & 1;
        int enc_bit   = bit * 2;
        int byte_idx  = nbytes - 1 - enc_bit / 8;
        int bit_shift = enc_bit % 8;
        if (byte_idx >= 0)
            buf[byte_idx] |= (uint8_t)(((b << 1) | a) << bit_shift);
    }
}

/* -------------------------------------------------------------------------
 * Dump-limit check (approximate: uses change count as a size proxy)
 * ---------------------------------------------------------------------- */

#define TRLOG_BYTES_PER_CHANGE_ESTIMATE 16ULL

static void check_dump_limit(void) {
    if (!g_trlog.dump_limit || !g_trlog.writer || g_trlog.paused) return;
    uint64_t estimated = g_trlog.vc_change_count * TRLOG_BYTES_PER_CHANGE_ESTIMATE;
    if (estimated >= g_trlog.dump_limit) {
        s_vpi_time vt; vt.type = vpiSimTime;
        vpi_get_time(NULL, &vt);
        uint64_t t = ((uint64_t)vt.high << 32) | (uint64_t)vt.low;
        if (g_trlog.scope_grouped)
            trlog_vpi_flush_pending(t);
        else if (g_trlog.vc_active) {
            trl_vc_flush(g_trlog.writer);
            g_trlog.vc_active       = 0;
            g_trlog.vc_change_count = 0;
        }
        g_trlog.paused = 1;
        vpi_printf("trlog: dump limit reached; tracing paused\n");
    }
}

/* -------------------------------------------------------------------------
 * Pending change buffer (scope-grouped mode)
 * ---------------------------------------------------------------------- */

static int ensure_pending_cap(void) {
    if (g_trlog.pending_count < g_trlog.pending_cap) return 0;
    size_t new_cap = g_trlog.pending_cap ? g_trlog.pending_cap * 2 : 256;
    trlog_vpi_pending_t *p = realloc(g_trlog.pending,
                                     new_cap * sizeof(*p));
    if (!p) return -1;
    g_trlog.pending     = p;
    g_trlog.pending_cap = new_cap;
    return 0;
}

static int cmp_pending(const void *a, const void *b) {
    const trlog_vpi_pending_t *pa = (const trlog_vpi_pending_t *)a;
    const trlog_vpi_pending_t *pb = (const trlog_vpi_pending_t *)b;
    if (pa->scope_id != pb->scope_id)
        return (pa->scope_id < pb->scope_id) ? -1 : 1;
    if (pa->time != pb->time)
        return (pa->time < pb->time) ? -1 : 1;
    return 0;
}

static void emit_pending_entry(const trlog_vpi_pending_t *p) {
    if (p->flags & TRLOG_PENDING_FLAG_REAL) {
        trl_vc_change_real(g_trlog.writer, p->var_id, p->time, p->real_val);
    } else if (p->flags & TRLOG_PENDING_FLAG_STR) {
        trl_vc_change_str(g_trlog.writer, p->var_id, p->time,
                          (const char *)p->heap_data);
    } else if (p->flags & TRLOG_PENDING_FLAG_BYTES) {
        trl_vc_change_bytes(g_trlog.writer, p->var_id, p->time,
                            p->heap_data, p->heap_len);
    } else {
        trl_vc_change_u64(g_trlog.writer, p->var_id, p->time, p->u64);
    }
}

void trlog_vpi_flush_pending(uint64_t sim_time) {
    if (!g_trlog.pending_count) return;
    qsort(g_trlog.pending, g_trlog.pending_count,
          sizeof(*g_trlog.pending), cmp_pending);

    uint32_t cur_scope = UINT32_MAX;
    for (size_t i = 0; i < g_trlog.pending_count; ++i) {
        trlog_vpi_pending_t *p = &g_trlog.pending[i];
        if (p->scope_id != cur_scope) {
            if (cur_scope != UINT32_MAX)
                trl_vc_flush(g_trlog.writer);
            trl_vc_begin(g_trlog.writer, g_trlog.vc_start_time);
            cur_scope = p->scope_id;
        }
        emit_pending_entry(p);
        if (p->heap_data) { free(p->heap_data); p->heap_data = NULL; }
    }
    if (cur_scope != UINT32_MAX)
        trl_vc_flush(g_trlog.writer);

    g_trlog.pending_count   = 0;
    g_trlog.vc_change_count = 0;
    g_trlog.vc_start_time   = sim_time;
}

/* -------------------------------------------------------------------------
 * Write one signal's current value to the writer (shared by delta CB
 * and snapshot).  The value is read directly from the simulator at the
 * point of call so it reflects the fully settled state.
 * ---------------------------------------------------------------------- */

static void write_signal_value(size_t idx, uint64_t sim_time) {
    uint8_t  enc       = g_trlog.sig_encs[idx];
    uint32_t var_id    = g_trlog.var_ids[idx];
    int      bit_width = (int)g_trlog.sig_widths[idx];
    int      nwords    = (bit_width + 31) / 32;

    s_vpi_value val;

    if (enc == TRL_SE_REAL) {
        val.format = vpiRealVal;
        vpi_get_value(g_trlog.sig_handles[idx], &val);
        if (g_trlog.scope_grouped) {
            if (ensure_pending_cap() < 0) { vpi_control(vpiFinish, 1); return; }
            trlog_vpi_pending_t *p = &g_trlog.pending[g_trlog.pending_count++];
            memset(p, 0, sizeof(*p));
            p->var_id   = var_id;
            p->scope_id = g_trlog.sig_scope_ids[idx];
            p->time     = sim_time;
            p->enc      = enc;
            p->flags    = TRLOG_PENDING_FLAG_REAL;
            p->real_val = val.value.real;
        } else {
            trl_vc_change_real(g_trlog.writer, var_id, sim_time,
                               val.value.real);
        }
    } else if (enc == TRL_SE_STRING) {
        val.format = vpiStringVal;
        vpi_get_value(g_trlog.sig_handles[idx], &val);
        const char *s = val.value.str ? val.value.str : "";
        if (g_trlog.scope_grouped) {
            if (ensure_pending_cap() < 0) { vpi_control(vpiFinish, 1); return; }
            trlog_vpi_pending_t *p = &g_trlog.pending[g_trlog.pending_count++];
            memset(p, 0, sizeof(*p));
            p->var_id   = var_id;
            p->scope_id = g_trlog.sig_scope_ids[idx];
            p->time     = sim_time;
            p->enc      = enc;
            p->flags    = TRLOG_PENDING_FLAG_STR;
            p->heap_data = (uint8_t *)strdup(s);
            p->heap_len  = p->heap_data ? (uint32_t)strlen(s) + 1 : 0;
        } else {
            trl_vc_change_str(g_trlog.writer, var_id, sim_time, s);
        }
    } else {
        /* Bit-vector: request vpiVectorVal so we get aval + bval. */
        val.format = vpiVectorVal;
        vpi_get_value(g_trlog.sig_handles[idx], &val);
        const s_vpi_vecval *vec = val.value.vector;

        if (g_trlog.scope_grouped) {
            if (ensure_pending_cap() < 0) { vpi_control(vpiFinish, 1); return; }
            trlog_vpi_pending_t *p = &g_trlog.pending[g_trlog.pending_count++];
            memset(p, 0, sizeof(*p));
            p->var_id   = var_id;
            p->scope_id = g_trlog.sig_scope_ids[idx];
            p->time     = sim_time;
            p->enc      = enc;
            if (enc == TRL_SE_2STATE && bit_width <= 64) {
                p->u64 = pack_2state_u64(vec, nwords);
            } else if (enc == TRL_SE_2STATE) {
                int nb = (bit_width + 7) / 8;
                p->flags     = TRLOG_PENDING_FLAG_BYTES;
                p->heap_data = malloc((size_t)nb);
                p->heap_len  = (uint32_t)nb;
                if (p->heap_data) pack_2state_bytes(vec, nwords, p->heap_data, nb);
            } else {
                int nb = (bit_width * 2 + 7) / 8;
                p->flags     = TRLOG_PENDING_FLAG_BYTES;
                p->heap_data = malloc((size_t)nb);
                p->heap_len  = (uint32_t)nb;
                if (p->heap_data) pack_4state_bytes(vec, nwords, p->heap_data, nb);
            }
        } else {
            if (enc == TRL_SE_2STATE && bit_width <= 64) {
                trl_vc_change_u64(g_trlog.writer, var_id, sim_time,
                                  pack_2state_u64(vec, nwords));
            } else if (enc == TRL_SE_2STATE) {
                int nb = (bit_width + 7) / 8;
                uint8_t buf[512];
                uint8_t *b = nb <= (int)sizeof(buf) ? buf : malloc((size_t)nb);
                if (!b) { vpi_control(vpiFinish, 1); return; }
                pack_2state_bytes(vec, nwords, b, nb);
                trl_vc_change_bytes(g_trlog.writer, var_id, sim_time, b, (uint32_t)nb);
                if (b != buf) free(b);
            } else {
                int nb = (bit_width * 2 + 7) / 8;
                uint8_t buf[512];
                uint8_t *b = nb <= (int)sizeof(buf) ? buf : malloc((size_t)nb);
                if (!b) { vpi_control(vpiFinish, 1); return; }
                pack_4state_bytes(vec, nwords, b, nb);
                trl_vc_change_bytes(g_trlog.writer, var_id, sim_time, b, (uint32_t)nb);
                if (b != buf) free(b);
            }
        }
    }
    g_trlog.vc_change_count++;
}

/* -------------------------------------------------------------------------
 * cbAfterDelay(0) callback — the de-bounce drain point
 *
 * Fires at the same sim-time as the triggering cbValueChange but after
 * the current delta cycle has settled.  Reads final values of all marked
 * signals and writes them to the TRLOG writer.
 * ---------------------------------------------------------------------- */

static PLI_INT32 trlog_delta_callback(p_cb_data cb) {
    /* Free the heap-allocated s_vpi_time registered for this callback. */
    if (cb && cb->user_data) free(cb->user_data);

    if (!g_trlog.writer || g_trlog.paused) {
        g_trlog.delta_cb_pending = 0;
        if (g_trlog.sig_changed)
            memset(g_trlog.sig_changed, 0, g_trlog.sig_count);
        return 0;
    }

    s_vpi_time vt; vt.type = vpiSimTime;
    vpi_get_time(NULL, &vt);
    uint64_t sim_time = ((uint64_t)vt.high << 32) | (uint64_t)vt.low;

    g_trlog.delta_cb_pending = 0;

    /* Open a VC block if needed (direct-call mode only; scope-grouped
     * tracks start time separately). */
    if (!g_trlog.scope_grouped) {
        if (!g_trlog.vc_active) {
            trl_vc_begin(g_trlog.writer, sim_time);
            g_trlog.vc_active     = 1;
            g_trlog.vc_start_time = sim_time;
        }
    } else {
        if (g_trlog.pending_count == 0)
            g_trlog.vc_start_time = sim_time;
    }

    /* Write the current (settled) value of every signal marked changed. */
    for (size_t i = 0; i < g_trlog.sig_count; ++i) {
        if (!g_trlog.sig_changed[i]) continue;
        g_trlog.sig_changed[i] = 0;
        write_signal_value(i, sim_time);
    }

    /* Auto-flush. */
    if (g_trlog.scope_grouped) {
        if (g_trlog.vc_change_count >= g_trlog.flush_interval)
            trlog_vpi_flush_pending(sim_time);
    } else {
        if (g_trlog.vc_change_count >= g_trlog.flush_interval) {
            trl_vc_flush(g_trlog.writer);
            g_trlog.vc_active       = 0;
            g_trlog.vc_change_count = 0;
        }
    }

    check_dump_limit();
    return 0;
}

/* -------------------------------------------------------------------------
 * Register a cbAfterDelay(0) if one is not already pending
 * ---------------------------------------------------------------------- */

static void ensure_delta_callback(void) {
    if (g_trlog.delta_cb_pending) return;

    /* cbReadWriteSynch fires at the read-write synchronisation point of the
     * current time step — after all delta cycles have settled but before
     * simulation time advances.  This is the safe point at which to read
     * final signal values.
     *
     * We heap-allocate the s_vpi_time so the pointer remains valid when
     * VCS writes the current time back into it on callback invocation. */
    s_vpi_time *tp = (s_vpi_time *)calloc(1, sizeof(s_vpi_time));
    if (!tp) return;
    tp->type = vpiSimTime;

    s_cb_data sync_cb;
    memset(&sync_cb, 0, sizeof(sync_cb));
    sync_cb.reason  = cbReadWriteSynch;
    sync_cb.cb_rtn  = trlog_delta_callback;
    sync_cb.time    = tp;
    /* user_data carries the tp pointer so the callback can free it. */
    sync_cb.user_data = (PLI_BYTE8 *)tp;

    vpi_register_cb(&sync_cb);
    g_trlog.delta_cb_pending = 1;
}

/* -------------------------------------------------------------------------
 * cbValueChange — mark changed; schedule de-bounce drain
 * ---------------------------------------------------------------------- */

PLI_INT32 trlog_vc_callback(p_cb_data cb) {
    if (!g_trlog.writer || g_trlog.paused) return 0;

    int local_idx = (int)(uint32_t)(uintptr_t)cb->user_data;
    if (local_idx < 0 || (size_t)local_idx >= g_trlog.sig_count) return 0;

    /* Mark the signal as changed; the delta callback does the actual write. */
    g_trlog.sig_changed[local_idx] = 1;

    /* Register a 0-time drain callback if one is not already pending. */
    ensure_delta_callback();

    return 0;
}

/* -------------------------------------------------------------------------
 * Snapshot: emit the current value of every registered signal
 * Used by $trlog_dumpon and $trlog_dumpall.
 * ---------------------------------------------------------------------- */

void trlog_snapshot_all(uint64_t sim_time) {
    if (!g_trlog.writer || !g_trlog.sig_count) return;

    /* Flush any pending buffered changes first. */
    if (g_trlog.scope_grouped) {
        if (g_trlog.pending_count)
            trlog_vpi_flush_pending(sim_time);
        g_trlog.vc_start_time = sim_time;
    } else {
        if (g_trlog.vc_active) {
            trl_vc_flush(g_trlog.writer);
            g_trlog.vc_active       = 0;
            g_trlog.vc_change_count = 0;
        }
        trl_vc_begin(g_trlog.writer, sim_time);
        g_trlog.vc_active     = 1;
        g_trlog.vc_start_time = sim_time;
    }

    for (size_t i = 0; i < g_trlog.sig_count; ++i)
        write_signal_value(i, sim_time);

    if (g_trlog.scope_grouped)
        trlog_vpi_flush_pending(sim_time);
    else
        trl_vc_flush(g_trlog.writer);

    g_trlog.vc_active       = 0;
    g_trlog.vc_change_count = 0;
}
