#pragma once
/*
 * trlog_vpi.h — public VPI entry points for libtrlog_vpi.so
 *
 * Simulators that use the vlog_startup_routines mechanism call
 * trlog_vpi_register() automatically at elaboration time.  Simulators
 * that use a named entry-point mechanism (e.g. -loadvpi with a symbol
 * argument) should name "trlog_vpi_register" explicitly.
 */

/* The simulator looks both symbols up by name, so the library exports them
 * (it builds with hidden visibility; on Windows only marked symbols export). */
#if defined(_WIN32) || defined(__CYGWIN__)
#  define TRLOG_VPI_API __declspec(dllexport)
#elif defined(__GNUC__) || defined(__clang__)
#  define TRLOG_VPI_API __attribute__((visibility("default")))
#else
#  define TRLOG_VPI_API
#endif

#ifdef __cplusplus
extern "C" {
#endif

/* Called by the simulator at elaboration time. */
TRLOG_VPI_API void trlog_vpi_register(void);

/* Standard startup routine table used by simulators that scan for
 * vlog_startup_routines at library load time. */
extern TRLOG_VPI_API void (*vlog_startup_routines[])(void);

#ifdef __cplusplus
}
#endif
