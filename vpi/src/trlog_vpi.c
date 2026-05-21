/*
 * trlog_vpi.c — VPI bootstrap: system task registration and callbacks.
 *
 * Entry points:
 *   trlog_vpi_register()      — called by the simulator at elaboration.
 *   vlog_startup_routines[]   — standard startup table scanned at load time.
 */

#include "trlog_vpi_internal.h"
#include "trlog_vpi.h"

/* -------------------------------------------------------------------------
 * Forward declarations (static helpers)
 * ---------------------------------------------------------------------- */

static PLI_INT32 trlog_dumpfile_cb(PLI_BYTE8 *user);
static PLI_INT32 trlog_dumpvars_cb(PLI_BYTE8 *user);
static PLI_INT32 trlog_dumpoff_cb(PLI_BYTE8 *user);
static PLI_INT32 trlog_dumpon_cb(PLI_BYTE8 *user);
static PLI_INT32 trlog_dumpflush_cb(PLI_BYTE8 *user);
static PLI_INT32 trlog_dumplimit_cb(PLI_BYTE8 *user);
static PLI_INT32 trlog_dumpall_cb(PLI_BYTE8 *user);
static PLI_INT32 trlog_eos_callback(p_cb_data cb);
static PLI_INT32 trlog_sos_callback(p_cb_data cb);

/* -------------------------------------------------------------------------
 * Plusarg scanning
 *
 * We scan argv from vpi_get_vlog_info.  Returns 1 if the plusarg was found,
 * 0 otherwise.  When found, *value_out (if non-NULL) points to the value
 * string after '=' (not a copy; points into the argv array).
 * ---------------------------------------------------------------------- */

static int scan_plusarg(const char *name, const char **value_out) {
    s_vpi_vlog_info info;
    if (!vpi_get_vlog_info(&info))
        return 0;

    size_t name_len = strlen(name);
    for (int i = 0; i < info.argc; ++i) {
        const char *arg = info.argv[i];
        if (arg[0] != '+')
            continue;
        arg++;  /* skip '+' */
        if (strncmp(arg, name, name_len) != 0)
            continue;
        /* Match: arg starts with name, followed by '\0' or '=' */
        if (arg[name_len] == '\0') {
            if (value_out) *value_out = arg + name_len;
            return 1;
        }
        if (arg[name_len] == '=') {
            if (value_out) *value_out = arg + name_len + 1;
            return 1;
        }
    }
    return 0;
}

/* Parse +trlog_signals=pat1,pat2,... into g_trlog.patterns[]. */
static void parse_signal_patterns(const char *val) {
    if (!val || val[0] == '\0')
        return;

    char *copy = strdup(val);
    if (!copy) return;

    char *tok = strtok(copy, ",");
    while (tok && g_trlog.pattern_count < TRLOG_VPI_MAX_PATTERNS) {
        char *pat = strdup(tok);
        if (pat)
            g_trlog.patterns[g_trlog.pattern_count++] = pat;
        tok = strtok(NULL, ",");
    }
    free(copy);
}

/* Read all recognised plusargs and store results in g_trlog. */
static void apply_plusargs(void) {
    const char *val = NULL;

    if (scan_plusarg("trlog_2state", NULL))
        g_trlog.force_2state = 1;

    if (scan_plusarg("trlog_flush_interval", &val) && val && *val)
        g_trlog.flush_interval = (uint64_t)strtoull(val, NULL, 10);

    if (scan_plusarg("trlog_compress", &val) && val) {
        if (strcmp(val, "none") == 0)
            g_trlog.compress = TRL_COMPRESS_NONE;
        else
            g_trlog.compress = TRL_COMPRESS_ZLIB;
    }

    if (scan_plusarg("trlog_scope_grouped", NULL))
        g_trlog.scope_grouped = 1;

    if (scan_plusarg("trlog_signals", &val) && val)
        parse_signal_patterns(val);
}

/* -------------------------------------------------------------------------
 * Registration
 * ---------------------------------------------------------------------- */

void trlog_vpi_register(void) {
    trlog_vpi_state_init();
    apply_plusargs();

    /* Register the seven system tasks. */
    static s_vpi_systf_data tasks[] = {
        { vpiSysTask, 0, "$trlog_dumpfile",  trlog_dumpfile_cb,  NULL, NULL, NULL },
        { vpiSysTask, 0, "$trlog_dumpvars",  trlog_dumpvars_cb,  NULL, NULL, NULL },
        { vpiSysTask, 0, "$trlog_dumpoff",   trlog_dumpoff_cb,   NULL, NULL, NULL },
        { vpiSysTask, 0, "$trlog_dumpon",    trlog_dumpon_cb,    NULL, NULL, NULL },
        { vpiSysTask, 0, "$trlog_dumpflush", trlog_dumpflush_cb, NULL, NULL, NULL },
        { vpiSysTask, 0, "$trlog_dumplimit", trlog_dumplimit_cb, NULL, NULL, NULL },
        { vpiSysTask, 0, "$trlog_dumpall",   trlog_dumpall_cb,   NULL, NULL, NULL },
    };
    for (size_t i = 0; i < sizeof(tasks) / sizeof(tasks[0]); ++i)
        vpi_register_systf(&tasks[i]);

    /* End-of-simulation: flush and close the writer. */
    /* Fields: reason, cb_rtn, obj, time, value, index (int), user_data */
    s_cb_data eos = { cbEndOfSimulation, trlog_eos_callback,
                      NULL, NULL, NULL, 0, NULL };
    vpi_register_cb(&eos);

    /* Start-of-simulation: handle +trlog_file plusarg tracing. */
    s_cb_data sos = { cbStartOfSimulation, trlog_sos_callback,
                      NULL, NULL, NULL, 0, NULL };
    vpi_register_cb(&sos);
}

/* Standard startup routine table. */
void (*vlog_startup_routines[])(void) = {
    trlog_vpi_register,
    NULL
};

/* -------------------------------------------------------------------------
 * Helpers shared by multiple callbacks
 * ---------------------------------------------------------------------- */

/* Return the current simulation time as a uint64_t. */
static uint64_t current_sim_time(void) {
    s_vpi_time t;
    t.type = vpiSimTime;
    vpi_get_time(NULL, &t);
    return ((uint64_t)t.high << 32) | (uint64_t)t.low;
}

/* Flush any open VC block (direct-call mode) or pending buffer
 * (scope-grouped mode) and mark the block as inactive. */
static void do_flush(uint64_t sim_time) {
    if (!g_trlog.writer) return;
    if (g_trlog.scope_grouped) {
        if (g_trlog.pending_count > 0)
            trlog_vpi_flush_pending(sim_time);
    } else {
        if (g_trlog.vc_active) {
            trl_vc_flush(g_trlog.writer);
            g_trlog.vc_active       = 0;
            g_trlog.vc_change_count = 0;
        }
    }
}

/* -------------------------------------------------------------------------
 * System task callbacks
 * ---------------------------------------------------------------------- */

static PLI_INT32 trlog_dumpfile_cb(PLI_BYTE8 *user) {
    (void)user;

    /* Read the filename argument. */
    vpiHandle task_call = vpi_handle(vpiSysTfCall, NULL);
    vpiHandle arg_iter  = vpi_iterate(vpiArgument, task_call);
    if (!arg_iter) {
        vpi_printf("$trlog_dumpfile: missing filename argument\n");
        return 0;
    }
    vpiHandle arg = vpi_scan(arg_iter);
    if (!arg) {
        vpi_printf("$trlog_dumpfile: missing filename argument\n");
        return 0;
    }

    s_vpi_value val;
    val.format = vpiStringVal;
    vpi_get_value(arg, &val);
    const char *filename = val.value.str;
    if (!filename || filename[0] == '\0') {
        vpi_printf("$trlog_dumpfile: empty filename\n");
        return 0;
    }

    /* Read the module timescale. */
    int ts = vpi_get(vpiTimeUnit, NULL);
    g_trlog.timescale_exp = ts;

    g_trlog.writer = trl_writer_open(filename, ts, g_trlog.compress);
    if (!g_trlog.writer) {
        vpi_printf("$trlog_dumpfile: cannot open '%s' for writing\n", filename);
    }
    return 0;
}

static PLI_INT32 trlog_dumpvars_cb(PLI_BYTE8 *user) {
    (void)user;
    if (!g_trlog.writer) return 0;

    vpiHandle task_call = vpi_handle(vpiSysTfCall, NULL);
    vpiHandle arg_iter  = vpi_iterate(vpiArgument, task_call);

    int depth = 0;
    vpiHandle scope_handle = NULL;

    if (arg_iter) {
        vpiHandle depth_arg = vpi_scan(arg_iter);
        if (depth_arg) {
            s_vpi_value v; v.format = vpiIntVal;
            vpi_get_value(depth_arg, &v);
            depth = v.value.integer;
        }
        vpiHandle scope_arg = vpi_scan(arg_iter);
        if (scope_arg)
            scope_handle = scope_arg;
    }

    if (!scope_handle) {
        /* Default to the first top-level module. */
        vpiHandle top_iter = vpi_iterate(vpiModule, NULL);
        if (top_iter) {
            scope_handle = vpi_scan(top_iter);
        }
    }

    if (!scope_handle) {
        vpi_printf("$trlog_dumpvars: cannot resolve scope\n");
        return 0;
    }

    /* Begin a hierarchy block covering the whole design. */
    uint32_t design_name_id = trl_intern(g_trlog.writer, "design");
    trl_begin_hierarchy(g_trlog.writer, 1, 0 /* HK_DESIGN */, design_name_id);

    char path_buf[TRLOG_VPI_MAX_PATH];
    path_buf[0] = '\0';
    uint32_t scope_id = ++g_trlog.scope_id_counter;
    trlog_vpi_walk_scope(scope_handle, depth, scope_id, path_buf, 0);

    trl_flush_hierarchy(g_trlog.writer);
    return 0;
}

static PLI_INT32 trlog_dumpoff_cb(PLI_BYTE8 *user) {
    (void)user;
    if (!g_trlog.writer) return 0;
    uint64_t t = current_sim_time();
    do_flush(t);
    g_trlog.paused = 1;
    return 0;
}

static PLI_INT32 trlog_dumpon_cb(PLI_BYTE8 *user) {
    (void)user;
    if (!g_trlog.writer) return 0;
    g_trlog.paused = 0;
    uint64_t t = current_sim_time();
    trlog_snapshot_all(t);
    return 0;
}

static PLI_INT32 trlog_dumpflush_cb(PLI_BYTE8 *user) {
    (void)user;
    if (!g_trlog.writer) return 0;
    uint64_t t = current_sim_time();
    do_flush(t);
    return 0;
}

static PLI_INT32 trlog_dumplimit_cb(PLI_BYTE8 *user) {
    (void)user;
    vpiHandle task_call = vpi_handle(vpiSysTfCall, NULL);
    vpiHandle arg_iter  = vpi_iterate(vpiArgument, task_call);
    if (!arg_iter) return 0;
    vpiHandle arg = vpi_scan(arg_iter);
    if (!arg) return 0;
    s_vpi_value v; v.format = vpiIntVal;
    vpi_get_value(arg, &v);
    g_trlog.dump_limit = (uint64_t)(uint32_t)v.value.integer;
    return 0;
}

static PLI_INT32 trlog_dumpall_cb(PLI_BYTE8 *user) {
    (void)user;
    if (!g_trlog.writer || g_trlog.paused) return 0;
    uint64_t t = current_sim_time();
    trlog_snapshot_all(t);
    return 0;
}

/* -------------------------------------------------------------------------
 * End-of-simulation callback
 * ---------------------------------------------------------------------- */

static PLI_INT32 trlog_eos_callback(p_cb_data cb) {
    (void)cb;
    if (!g_trlog.writer) { trlog_vpi_state_destroy(); return 0; }

    uint64_t t = current_sim_time();
    do_flush(t);
    trl_writer_close(g_trlog.writer);
    g_trlog.writer = NULL;
    trlog_vpi_state_destroy();
    return 0;
}

/* -------------------------------------------------------------------------
 * Start-of-simulation callback — handles +trlog_file plusarg
 * ---------------------------------------------------------------------- */

static PLI_INT32 trlog_sos_callback(p_cb_data cb) {
    (void)cb;
    const char *filepath = NULL;
    if (!scan_plusarg("trlog_file", &filepath) || !filepath || !filepath[0])
        return 0;

    /* Open the writer. */
    int ts = vpi_get(vpiTimeUnit, NULL);
    g_trlog.timescale_exp = ts;
    g_trlog.writer = trl_writer_open(filepath, ts, g_trlog.compress);
    if (!g_trlog.writer) {
        vpi_printf("trlog: cannot open '%s' (from +trlog_file)\n", filepath);
        return 0;
    }

    /* Walk the entire design hierarchy at unlimited depth. */
    uint32_t design_name_id = trl_intern(g_trlog.writer, "design");
    trl_begin_hierarchy(g_trlog.writer, 1, 0 /* HK_DESIGN */, design_name_id);

    char path_buf[TRLOG_VPI_MAX_PATH];
    vpiHandle top_iter = vpi_iterate(vpiModule, NULL);
    if (top_iter) {
        vpiHandle mod;
        while ((mod = vpi_scan(top_iter)) != NULL) {
            path_buf[0] = '\0';
            uint32_t sid = ++g_trlog.scope_id_counter;
            trlog_vpi_walk_scope(mod, 0 /* unlimited */, sid, path_buf, 0);
        }
    }
    trl_flush_hierarchy(g_trlog.writer);
    return 0;
}
