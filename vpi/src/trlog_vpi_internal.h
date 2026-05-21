#pragma once
/*
 * trlog_vpi_internal.h — shared types and declarations for libtrlog_vpi.so
 *
 * Not part of the public API.  Include only from within the vpi/src tree.
 */

#include <stddef.h>
#include <stdint.h>
#include <stdlib.h>
#include <string.h>
#include <stdio.h>
#include <fnmatch.h>

/* Include the simulator VPI header unless the mock test layer is in use. */
#ifndef VPI_USER_H
#include <vpi_user.h>
#endif
#include <trl/trl.h>

/* -------------------------------------------------------------------------
 * Limits and defaults
 * ---------------------------------------------------------------------- */

#define TRLOG_VPI_DEFAULT_FLUSH_INTERVAL  100000U
#define TRLOG_VPI_MAX_PATTERNS            64
#define TRLOG_VPI_MAX_PATH                2048  /* bytes for full signal path */

/* -------------------------------------------------------------------------
 * Pending change entry (used in scope-grouped flush mode)
 *
 * Changes are accumulated in a flat array and sorted by scope_id before
 * flushing so each scope emits a separate BLK_VC_DATA block.
 * ---------------------------------------------------------------------- */

#define TRLOG_PENDING_FLAG_BYTES  0x01  /* heap_data holds byte array  */
#define TRLOG_PENDING_FLAG_STR    0x02  /* heap_data holds C string    */
#define TRLOG_PENDING_FLAG_REAL   0x04  /* real_val is valid           */

typedef struct {
    uint32_t  var_id;
    uint32_t  scope_id;
    uint64_t  time;
    uint8_t   enc;     /* TRL_SE_* encoding for this variable */
    uint8_t   flags;   /* TRLOG_PENDING_FLAG_* bitmask        */
    uint64_t  u64;     /* 2-state / 4-state width <= 64       */
    double    real_val;
    uint8_t  *heap_data;  /* malloc'd; non-NULL when BYTES or STR set */
    uint32_t  heap_len;
} trlog_vpi_pending_t;

/* -------------------------------------------------------------------------
 * Signal type cache entry
 *
 * Maps (encoding, bit_width) → sig_type_id.  Linear scan; typically < 20.
 * ---------------------------------------------------------------------- */

typedef struct {
    uint8_t  encoding;
    uint32_t bit_width;
    uint32_t sig_type_id;
} trlog_vpi_type_entry_t;

/* -------------------------------------------------------------------------
 * Library-wide state
 * ---------------------------------------------------------------------- */

typedef struct {
    trl_writer_t   *writer;     /* NULL until $trlog_dumpfile succeeds */

    /* --- Signal registration arrays (parallel, indexed by local_idx) --- */
    uint32_t       *var_ids;        /* var_ids[local_idx]      = trl var_id    */
    vpiHandle      *sig_handles;    /* sig_handles[local_idx]  = VPI handle    */
    uint32_t       *sig_type_ids;   /* sig_type_ids[local_idx] = trl type id   */
    uint8_t        *sig_encs;       /* sig_encs[local_idx]     = TRL_SE_*      */
    uint32_t       *sig_widths;     /* sig_widths[local_idx]   = bit width     */
    uint32_t       *sig_scope_ids;  /* sig_scope_ids[local_idx] = scope group  */
    uint8_t        *sig_det_enc;    /* 0xFF = not yet detected                 */
    /* Persistent heap storage for cbValueChange registration structs.
     * Some simulators write the live time/value into these pointers when
     * firing the callback, so they must outlive register_vc_callback(). */
    s_vpi_time    **sig_cb_times;
    s_vpi_value   **sig_cb_values;
    /* De-bounce: per-signal flag, set on cbValueChange, cleared in the
     * delta callback after all delta cycles for a time step drain. */
    uint8_t        *sig_changed;
    /* Delta-callback de-bounce state. */
    int             delta_cb_pending;  /* 1 = cbAfterDelay(0) registered */
    uint64_t        delta_cb_time;     /* sim time of pending delta CB   */
    size_t          sig_count;
    size_t          sig_cap;

    /* --- Signal type dedup cache ---------------------------------------- */
    trlog_vpi_type_entry_t *type_cache;
    size_t                  type_cache_count;
    size_t                  type_cache_cap;

    /* --- VC block state (direct-call mode) ------------------------------ */
    int             vc_active;
    uint64_t        vc_start_time;
    uint64_t        vc_change_count;

    /* --- Pending change buffer (scope-grouped mode) --------------------- */
    trlog_vpi_pending_t *pending;
    size_t               pending_count;
    size_t               pending_cap;

    /* --- Scope ID counter (incremented per scope during hierarchy walk) -- */
    uint32_t        scope_id_counter;

    /* --- Control flags -------------------------------------------------- */
    int             paused;          /* set by $trlog_dumpoff             */
    uint64_t        dump_limit;      /* 0 = unlimited                     */
    uint64_t        flush_interval;  /* changes between auto-flushes      */
    int             scope_grouped;   /* 1 = emit per-scope BLK_VC_DATA    */
    int             force_2state;    /* +trlog_2state: skip X/Z detection */
    trl_compress_t  compress;        /* compression mode for VC blocks    */

    /* --- Timescale (from vpi_get at $trlog_dumpfile time) --------------- */
    int             timescale_exp;

    /* --- Signal filter patterns from +trlog_signals --------------------- */
    char           *patterns[TRLOG_VPI_MAX_PATTERNS];
    int             pattern_count;
} trlog_vpi_state_t;

/* Process-global state; zero-initialised at library load. */
extern trlog_vpi_state_t g_trlog;

/* -------------------------------------------------------------------------
 * trlog_vpi_state.c — lifecycle and bookkeeping
 * ---------------------------------------------------------------------- */
void     trlog_vpi_state_init(void);
void     trlog_vpi_state_destroy(void);
int      trlog_vpi_state_add_signal(vpiHandle handle, uint32_t var_id,
                                    uint32_t sig_type_id, uint8_t enc,
                                    uint32_t bit_width, uint32_t scope_id);
uint32_t trlog_vpi_state_find_or_create_type(uint8_t encoding,
                                             uint32_t bit_width);

/* -------------------------------------------------------------------------
 * trlog_vpi_walk.c — hierarchy walker
 * ---------------------------------------------------------------------- */
void trlog_vpi_walk_scope(vpiHandle scope, int depth_remaining,
                          uint32_t scope_id, char *path_buf, int path_len);
int  trlog_vpi_match_signal(const char *full_path);

/* -------------------------------------------------------------------------
 * trlog_vpi_value.c — value-change callbacks and packing
 * ---------------------------------------------------------------------- */
PLI_INT32 trlog_vc_callback(p_cb_data cb);
void      trlog_snapshot_all(uint64_t sim_time);
void      trlog_vpi_flush_pending(uint64_t sim_time);
