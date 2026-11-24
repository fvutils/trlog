/*
 * trlog_vpi_fn.c — find the simulator's VPI routines in the running process.
 *
 * POSIX: dlsym(RTLD_DEFAULT), then the main program via dlopen(NULL).
 * Windows: the main program, then every module loaded in the process.
 */

#if !defined(_WIN32) && !defined(_GNU_SOURCE)
#define _GNU_SOURCE /* RTLD_DEFAULT on older glibc (manylinux_2_28) */
#endif
#define TRLOG_VPI_FN_NO_MACROS
#include "trlog_vpi_internal.h"

#ifdef _WIN32
#include <windows.h>
#include <psapi.h>
#else
#include <dlfcn.h>
#endif

trlog_vpi_fn_t trlog_vpi_fn;

static void *find_symbol(const char *name) {
#ifdef _WIN32
    HMODULE  mods[1024];
    DWORD    needed = 0;
    void    *p = (void *)GetProcAddress(GetModuleHandleA(NULL), name);
    if (p)
        return p;
    if (EnumProcessModules(GetCurrentProcess(), mods, sizeof(mods), &needed)) {
        DWORD n = needed / sizeof(HMODULE), i;
        if (n > sizeof(mods) / sizeof(HMODULE))
            n = sizeof(mods) / sizeof(HMODULE);
        for (i = 0; i < n; i++) {
            p = (void *)GetProcAddress(mods[i], name);
            if (p)
                return p;
        }
    }
    return NULL;
#else
    void *p = dlsym(RTLD_DEFAULT, name);
    if (!p) {
        void *self = dlopen(NULL, RTLD_LAZY);
        if (self) {
            p = dlsym(self, name);
            dlclose(self);
        }
    }
    return p;
#endif
}

#define LOAD(field, name)                                               \
    do {                                                                \
        void *p_ = find_symbol(name);                                   \
        if (!p_) {                                                      \
            fprintf(stderr, "trlog_vpi: VPI routine %s not found in "   \
                    "this process; not registering\n", name);           \
            return -1;                                                  \
        }                                                               \
        memcpy(&trlog_vpi_fn.field, &p_, sizeof(p_));                   \
    } while (0)

int trlog_vpi_fn_load(void) {
    if (trlog_vpi_fn.get)
        return 0;
    LOAD(register_cb,    "vpi_register_cb");
    LOAD(register_systf, "vpi_register_systf");
    LOAD(handle,         "vpi_handle");
    LOAD(iterate,        "vpi_iterate");
    LOAD(scan,           "vpi_scan");
    LOAD(get_str,        "vpi_get_str");
    LOAD(get_value,      "vpi_get_value");
    LOAD(get_time,       "vpi_get_time");
    LOAD(printf,         "vpi_printf");
    LOAD(get_vlog_info,  "vpi_get_vlog_info");
    LOAD(control,        "vpi_control");
    LOAD(get,            "vpi_get");    /* last: marks the table complete */
    return 0;
}
