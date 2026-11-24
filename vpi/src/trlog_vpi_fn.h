#pragma once
/*
 * trlog_vpi_fn.h — VPI functions looked up at runtime.
 *
 * libtrlog_vpi does not link against any simulator.  The VPI routines it
 * uses are found in the running process when the library is registered
 * (trlog_vpi_fn_load), and every vpi_xxx() call in vpi/src goes through
 * this table.  That keeps the library free of undefined symbols, which
 * Windows DLLs and macOS dylibs require.
 *
 * Include after vpi_user.h.  trlog_vpi_fn.c defines TRLOG_VPI_FN_NO_MACROS
 * so it can still name the real functions.
 */

typedef struct {
    vpiHandle  (*register_cb)(p_cb_data cb_data_p);
    vpiHandle  (*register_systf)(p_vpi_systf_data systf_data_p);
    vpiHandle  (*handle)(PLI_INT32 type, vpiHandle ref);
    vpiHandle  (*iterate)(PLI_INT32 type, vpiHandle ref);
    vpiHandle  (*scan)(vpiHandle iterator);
    PLI_INT32  (*get)(PLI_INT32 property, vpiHandle object);
    PLI_BYTE8 *(*get_str)(PLI_INT32 property, vpiHandle object);
    void       (*get_value)(vpiHandle expr, p_vpi_value value_p);
    void       (*get_time)(vpiHandle object, p_vpi_time time_p);
    PLI_INT32  (*printf)(PLI_BYTE8 *format, ...);
    PLI_INT32  (*get_vlog_info)(p_vpi_vlog_info vlog_info_p);
    PLI_INT32  (*control)(PLI_INT32 operation, ...);
} trlog_vpi_fn_t;

extern trlog_vpi_fn_t trlog_vpi_fn;

/* Fill trlog_vpi_fn from the running process.  Returns 0 on success, or -1
 * (after printing the missing name to stderr) if any routine is absent. */
int trlog_vpi_fn_load(void);

#ifndef TRLOG_VPI_FN_NO_MACROS
#define vpi_register_cb     (trlog_vpi_fn.register_cb)
#define vpi_register_systf  (trlog_vpi_fn.register_systf)
#define vpi_handle          (trlog_vpi_fn.handle)
#define vpi_iterate         (trlog_vpi_fn.iterate)
#define vpi_scan            (trlog_vpi_fn.scan)
#define vpi_get             (trlog_vpi_fn.get)
#define vpi_get_str         (trlog_vpi_fn.get_str)
#define vpi_get_value       (trlog_vpi_fn.get_value)
#define vpi_get_time        (trlog_vpi_fn.get_time)
#define vpi_printf          (trlog_vpi_fn.printf)
#define vpi_get_vlog_info   (trlog_vpi_fn.get_vlog_info)
#define vpi_control         (trlog_vpi_fn.control)
#endif
