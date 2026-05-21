/*
 * trlog_vpi_walk.c — VPI hierarchy walker and signal registration.
 *
 * walk_scope() performs a depth-first traversal of the VPI object tree,
 * mirroring the design hierarchy into the libtrl writer and registering
 * a cbValueChange callback for each discovered signal.
 */

#include "trlog_vpi_internal.h"

/* -------------------------------------------------------------------------
 * Scope-type mapping
 * ---------------------------------------------------------------------- */

/* Map a VPI object type to the trl scope-type byte.
 * Values match the TRL_H_SCOPE sub-type field. */
static uint8_t vpi_type_to_trl_scope(PLI_INT32 vpi_type) {
    switch (vpi_type) {
    case vpiModule:     return 1;   /* ST_MODULE   */
    case vpiTask:       return 2;   /* ST_TASK     */
    case vpiFunction:   return 3;   /* ST_FUNCTION */
    case vpiNamedBegin: return 4;   /* ST_BEGIN    */
    case vpiNamedFork:  return 5;   /* ST_FORK     */
    case vpiGenScope:   return 6;   /* ST_GENERATE */
    default:            return 1;   /* fallback    */
    }
}

/* -------------------------------------------------------------------------
 * Signal filter
 * ---------------------------------------------------------------------- */

/* Return 1 if full_path matches at least one pattern in g_trlog.patterns[],
 * or if no patterns are configured (accept all). */
int trlog_vpi_match_signal(const char *full_path) {
    if (g_trlog.pattern_count == 0)
        return 1;
    for (int i = 0; i < g_trlog.pattern_count; ++i) {
        if (fnmatch(g_trlog.patterns[i], full_path, 0) == 0)
            return 1;
    }
    return 0;
}

/* -------------------------------------------------------------------------
 * Signal encoding
 * ---------------------------------------------------------------------- */

/* Determine the TRL encoding for a VPI signal handle. */
static uint8_t vpi_type_to_trl_enc(PLI_INT32 vpi_type) {
    switch (vpi_type) {
    case vpiRealVar:
        return TRL_SE_REAL;
    default:
        /* Default to 4-state; detection may refine this to 2-state on the
         * first value change (unless +trlog_2state is set). */
        return g_trlog.force_2state ? TRL_SE_2STATE : TRL_SE_4STATE;
    }
}

/* -------------------------------------------------------------------------
 * cbValueChange registration
 * ---------------------------------------------------------------------- */

/* Register a value-change callback for sig_handle, storing local_idx as
 * the user_data so the callback can look up the signal. */
static void register_vc_callback(vpiHandle sig_handle, int local_idx) {
    /* Heap-allocate the time/value structs: some simulators write the
     * current time/value back into the registered pointers when firing
     * the callback, so the storage must outlive register_vc_callback(). */
    s_vpi_time  *tp = (s_vpi_time  *)calloc(1, sizeof(s_vpi_time));
    s_vpi_value *vp = (s_vpi_value *)calloc(1, sizeof(s_vpi_value));
    if (!tp || !vp) { free(tp); free(vp); vpi_control(vpiFinish, 1); return; }
    tp->type   = vpiSimTime;
    vp->format = vpiObjTypeVal;
    /* Fields: reason, cb_rtn, obj, time, value, index (PLI_INT32), user_data */
    s_cb_data   cb = {
        cbValueChange,
        trlog_vc_callback,
        sig_handle,
        tp,
        vp,
        0,
        (PLI_BYTE8 *)(uintptr_t)(uint32_t)local_idx
    };
    /* tp and vp are kept alive for the simulation lifetime; they are
     * freed in trlog_vpi_state_destroy() via sig_cb_times/sig_cb_values. */
    vpi_register_cb(&cb);
    /* Store pointers so state_destroy() can free them. */
    if ((size_t)local_idx < g_trlog.sig_count) {
        g_trlog.sig_cb_times [local_idx] = tp;
        g_trlog.sig_cb_values[local_idx] = vp;
    } else {
        free(tp); free(vp);
    }
}

/* -------------------------------------------------------------------------
 * Signal registration
 * ---------------------------------------------------------------------- */

static void register_signal(vpiHandle handle, uint32_t scope_id,
                            const char *sig_path) {
    /* Apply signal filter. */
    if (!trlog_vpi_match_signal(sig_path))
        return;

    PLI_INT32 vpi_type  = vpi_get(vpiType, handle);
    int       bit_width = vpi_get(vpiSize, handle);
    if (bit_width <= 0) bit_width = 1;

    uint8_t  enc = vpi_type_to_trl_enc(vpi_type);
    uint32_t stid = trlog_vpi_state_find_or_create_type(enc, (uint32_t)bit_width);
    if (stid == 0) {
        vpi_printf("trlog: failed to register signal type for '%s'\n", sig_path);
        return;
    }

    /* Intern the signal's local name. */
    const char *local_name = vpi_get_str(vpiName, handle);
    if (!local_name) local_name = "?";
    uint32_t name_id = trl_intern(g_trlog.writer, local_name);
    uint32_t var_id  = trl_hier_add_var(g_trlog.writer, name_id, stid,
                                        0 /* dir: none */, 0, 0);

    int local_idx = trlog_vpi_state_add_signal(handle, var_id, stid, enc,
                                               (uint32_t)bit_width, scope_id);
    if (local_idx < 0) {
        /* OOM: abort simulation. */
        vpi_control(vpiFinish, 1);
        return;
    }
    register_vc_callback(handle, local_idx);
}

/* -------------------------------------------------------------------------
 * Scope walker (recursive DFS)
 * ---------------------------------------------------------------------- */

/* Signal object types we iterate within a scope. */
static const PLI_INT32 k_sig_types[] = {
    vpiNet,
    vpiReg,
    vpiIntegerVar,
    vpiRealVar,
    vpiVariables,  /* SystemVerilog local variables */
};

/* Child scope types we descend into. */
static const PLI_INT32 k_child_types[] = {
    vpiModule,
    vpiGenScope,
    vpiTask,
    vpiFunction,
    vpiNamedBegin,
    vpiNamedFork,
};

void trlog_vpi_walk_scope(vpiHandle scope, int depth_remaining,
                          uint32_t scope_id, char *path_buf, int path_len) {
    /* --- Emit hierarchy scope record ------------------------------------ */
    PLI_INT32   scope_vpi_type = vpi_get(vpiType, scope);
    const char *scope_name     = vpi_get_str(vpiName, scope);
    if (!scope_name) scope_name = "?";

    uint32_t sname_id   = trl_intern(g_trlog.writer, scope_name);
    uint8_t  trl_stype  = vpi_type_to_trl_scope(scope_vpi_type);
    trl_hier_begin_scope(g_trlog.writer, trl_stype, sname_id, 0, 0, 0);

    /* Build the full path for this scope (used by signal filter). */
    int new_len = path_len;
    if (path_len > 0 && path_len < TRLOG_VPI_MAX_PATH - 1)
        path_buf[new_len++] = '.';
    int name_len = (int)strlen(scope_name);
    if (new_len + name_len < TRLOG_VPI_MAX_PATH) {
        memcpy(path_buf + new_len, scope_name, (size_t)name_len);
        new_len += name_len;
        path_buf[new_len] = '\0';
    }

    /* --- Register signals in this scope --------------------------------- */
    char sig_path[TRLOG_VPI_MAX_PATH];
    for (size_t ti = 0; ti < sizeof(k_sig_types) / sizeof(k_sig_types[0]); ++ti) {
        vpiHandle it = vpi_iterate(k_sig_types[ti], scope);
        if (!it) continue;
        vpiHandle sig;
        while ((sig = vpi_scan(it)) != NULL) {
            /* Build full signal path for filter matching. */
            const char *signame = vpi_get_str(vpiName, sig);
            if (!signame) signame = "?";
            if (new_len > 0)
                snprintf(sig_path, sizeof(sig_path), "%.*s.%s",
                         new_len, path_buf, signame);
            else
                snprintf(sig_path, sizeof(sig_path), "%s", signame);

            /* Avoid double-registration across iterators: check if this
             * handle was already registered in a previous loop iteration. */
            int already_seen = 0;
            for (size_t si = 0; si < g_trlog.sig_count && !already_seen; ++si)
                if (g_trlog.sig_handles[si] == sig) already_seen = 1;
            if (already_seen) continue;

            register_signal(sig, scope_id, sig_path);
        }
    }

    /* --- Handle multi-dimensional arrays (best-effort) ----------------- */
    {
        static const PLI_INT32 k_arr_types[] = { vpiRegArray, vpiNetArray };
        for (size_t ai = 0;
             ai < sizeof(k_arr_types) / sizeof(k_arr_types[0]); ++ai) {
            vpiHandle it = vpi_iterate(k_arr_types[ai], scope);
            if (!it) continue;
            vpiHandle arr;
            while ((arr = vpi_scan(it)) != NULL) {
                const char *arrname = vpi_get_str(vpiName, arr);
                if (!arrname) arrname = "?";
                /* Try to iterate elements; vpiMemory is used as a fallback. */
                vpiHandle eit = vpi_iterate(vpiMemory, arr);
                if (!eit) continue;
                vpiHandle elem;
                int elem_idx = 0;
                while ((elem = vpi_scan(eit)) != NULL) {
                    char elem_path[TRLOG_VPI_MAX_PATH];
                    if (new_len > 0)
                        snprintf(elem_path, sizeof(elem_path),
                                 "%.*s.%s[%d]",
                                 new_len, path_buf, arrname, elem_idx++);
                    else
                        snprintf(elem_path, sizeof(elem_path),
                                 "%s[%d]", arrname, elem_idx++);
                    register_signal(elem, scope_id, elem_path);
                }
            }
        }
    }

    /* --- Recurse into child scopes -------------------------------------- */
    if (depth_remaining != 1) {
        /* 0 = unlimited; any other value: decrement and recurse. */
        int child_depth = (depth_remaining == 0) ? 0 : depth_remaining - 1;
        for (size_t ci = 0;
             ci < sizeof(k_child_types) / sizeof(k_child_types[0]); ++ci) {
            vpiHandle it = vpi_iterate(k_child_types[ci], scope);
            if (!it) continue;
            vpiHandle child;
            while ((child = vpi_scan(it)) != NULL) {
                uint32_t child_sid = ++g_trlog.scope_id_counter;
                char child_path[TRLOG_VPI_MAX_PATH];
                memcpy(child_path, path_buf, (size_t)(new_len + 1));
                trlog_vpi_walk_scope(child, child_depth, child_sid,
                                     child_path, new_len);
            }
        }
    }

    trl_hier_end_scope(g_trlog.writer);
}
