#pragma once
/*
 * trlog_vpi.h — public VPI entry points for libtrlog_vpi.so
 *
 * Simulators that use the vlog_startup_routines mechanism call
 * trlog_vpi_register() automatically at elaboration time.  Simulators
 * that use a named entry-point mechanism (e.g. -loadvpi with a symbol
 * argument) should name "trlog_vpi_register" explicitly.
 */

#ifdef __cplusplus
extern "C" {
#endif

/* Called by the simulator at elaboration time. */
void trlog_vpi_register(void);

/* Standard startup routine table used by simulators that scan for
 * vlog_startup_routines at library load time. */
extern void (*vlog_startup_routines[])(void);

#ifdef __cplusplus
}
#endif
