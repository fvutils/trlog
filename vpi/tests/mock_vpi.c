/*
 * mock_vpi.c — stub implementations of the VPI functions used by the unit
 * test build.  Not a simulator; just enough to satisfy the linker and let
 * tests call state-management and packing functions directly.
 */

#include "mock_vpi.h"
#include <stdarg.h>

/* --- Static test state ---------------------------------------------------- */

static PLI_INT32 s_argc = 0;
static PLI_BYTE8 *s_argv_buf[64];
static PLI_BYTE8 **s_argv = s_argv_buf;

/* Allow tests to inject plusargs. */
void mock_vpi_set_args(int argc, char **argv) {
    s_argc = (PLI_INT32)argc;
    s_argv = (PLI_BYTE8 **)argv;
}

/* --- VPI stubs ------------------------------------------------------------ */

vpiHandle mock_vpi_register_systf(p_vpi_systf_data data) {
    (void)data; return NULL;
}

vpiHandle mock_vpi_register_cb(p_cb_data cb) {
    (void)cb; return NULL;
}

PLI_INT32 mock_vpi_get(PLI_INT32 prop, vpiHandle obj) {
    (void)obj;
    if (prop == vpiTimeUnit) return -9;  /* 1 ns default */
    return 0;
}

PLI_BYTE8 *mock_vpi_get_str(PLI_INT32 prop, vpiHandle obj) {
    (void)prop; (void)obj; return (PLI_BYTE8 *)"mock";
}

void mock_vpi_get_value(vpiHandle obj, p_vpi_value val) {
    (void)obj;
    /* Return a zeroed value by default; tests override per-signal. */
    if (val->format == vpiRealVal)
        val->value.real = 0.0;
    else if (val->format == vpiStringVal)
        val->value.str = (PLI_BYTE8 *)"";
    else {
        /* Return a single zero word for vector requests. */
        static s_vpi_vecval zero_vec = { 0, 0 };
        val->value.vector = &zero_vec;
    }
}

void mock_vpi_get_time(vpiHandle obj, p_vpi_time t) {
    (void)obj;
    t->type = vpiSimTime;
    t->high = 0;
    t->low  = 100;  /* arbitrary non-zero time for tests */
}

PLI_INT32 mock_vpi_get_vlog_info(p_vpi_vlog_info info) {
    info->argc    = s_argc;
    info->argv    = s_argv;
    info->product = (PLI_BYTE8 *)"mock";
    info->version = (PLI_BYTE8 *)"0.0";
    return 1;
}

void mock_vpi_printf(const char *fmt, ...) {
    va_list ap;
    va_start(ap, fmt);
    vfprintf(stderr, fmt, ap);
    va_end(ap);
}

PLI_INT32 mock_vpi_control(PLI_INT32 op, ...) {
    (void)op; return 0;
}

/* ---- Stubs for trlog_vpi_value.c symbols (not compiled in unit test) ----- */
#include "../src/trlog_vpi_internal.h"

PLI_INT32 trlog_vc_callback(p_cb_data cb) {
    (void)cb; return 0;
}

void trlog_snapshot_all(uint64_t sim_time) {
    (void)sim_time;
}

void trlog_vpi_flush_pending(uint64_t sim_time) {
    (void)sim_time;
}
