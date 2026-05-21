#pragma once
/*
 * mock_vpi.h — in-process stub implementation of the VPI API for unit tests.
 *
 * Provides just enough VPI surface to exercise trlog_vpi_state.c and the
 * packing helpers in trlog_vpi_value.c without a simulator.
 */

#include <stddef.h>
#include <stdint.h>
#include <stdlib.h>
#include <string.h>
#include <stdio.h>

/* ---- Minimal VPI type definitions (replaces vpi_user.h in test builds) --- */

typedef int            PLI_INT32;
typedef unsigned int   PLI_UINT32;
typedef char           PLI_BYTE8;
typedef void          *vpiHandle;

#define vpiSimTime        2
#define vpiVectorVal      9
#define vpiObjTypeVal    12
#define vpiStringVal      8
#define vpiRealVal        7
#define vpiBinStrVal      1
#define vpiIntVal         6

#define vpiModule        32
#define vpiNet           36
#define vpiReg           48
#define vpiIntegerVar    25
#define vpiRealVar       47
#define vpiVariables    100
#define vpiRegArray     116
#define vpiNetArray     114
#define vpiMemory        29
#define vpiNamedBegin    33
#define vpiNamedFork     35
#define vpiGenScope     134
#define vpiTask          59
#define vpiFunction      20
#define vpiArgument      89

#define vpiName           2
#define vpiSize           4
#define vpiType           1
#define vpiTimeUnit      11
#define vpiParent        81

#define vpiSysTask        1
#define vpiSysFunc        2
#define vpiSysTfCall     85

#define cbValueChange         1
#define cbEndOfSimulation    12
#define cbStartOfSimulation  11

#define vpiFinish        67

typedef struct t_vpi_time {
    PLI_INT32  type;
    PLI_UINT32 high, low;
    double     real;
} s_vpi_time, *p_vpi_time;

typedef struct t_vpi_vecval {
    PLI_UINT32 aval;
    PLI_UINT32 bval;
} s_vpi_vecval, *p_vpi_vecval;

typedef struct t_vpi_value {
    PLI_INT32 format;
    union {
        PLI_BYTE8    *str;
        PLI_INT32     scalar;
        PLI_INT32     integer;
        double        real;
        p_vpi_time    time;
        p_vpi_vecval  vector;
        PLI_INT32     misc;
    } value;
} s_vpi_value, *p_vpi_value;

typedef struct t_cb_data {
    PLI_INT32   reason;
    PLI_INT32 (*cb_rtn)(struct t_cb_data *);
    vpiHandle   obj;
    p_vpi_time  time;
    p_vpi_value value;
    PLI_INT32   index;
    PLI_BYTE8  *user_data;
} s_cb_data, *p_cb_data;

typedef struct t_vpi_systf_data {
    PLI_INT32   type;
    PLI_INT32   sysfunctype;
    PLI_BYTE8  *tfname;
    PLI_INT32 (*calltf)(PLI_BYTE8 *);
    PLI_INT32 (*compiletf)(PLI_BYTE8 *);
    PLI_INT32 (*sizetf)(PLI_BYTE8 *);
    PLI_BYTE8  *user_data;
} s_vpi_systf_data, *p_vpi_systf_data;

typedef struct t_vpi_vlog_info {
    PLI_INT32   argc;
    PLI_BYTE8 **argv;
    PLI_BYTE8  *product;
    PLI_BYTE8  *version;
} s_vpi_vlog_info, *p_vpi_vlog_info;

/* ---- Stub function declarations ------------------------------------------ */

vpiHandle mock_vpi_register_systf(p_vpi_systf_data data);
vpiHandle mock_vpi_register_cb(p_cb_data cb);
PLI_INT32 mock_vpi_get(PLI_INT32 prop, vpiHandle obj);
PLI_BYTE8 *mock_vpi_get_str(PLI_INT32 prop, vpiHandle obj);
void      mock_vpi_get_value(vpiHandle obj, p_vpi_value val);
void      mock_vpi_get_time(vpiHandle obj, p_vpi_time t);
PLI_INT32 mock_vpi_get_vlog_info(p_vpi_vlog_info info);
void      mock_vpi_printf(const char *fmt, ...);
PLI_INT32 mock_vpi_control(PLI_INT32 op, ...);

/* Redirect VPI calls to mocks. */
#define vpi_register_systf  mock_vpi_register_systf
#define vpi_register_cb     mock_vpi_register_cb
#define vpi_get             mock_vpi_get
#define vpi_get_str         mock_vpi_get_str
#define vpi_get_value       mock_vpi_get_value
#define vpi_get_time        mock_vpi_get_time
#define vpi_get_vlog_info   mock_vpi_get_vlog_info
#define vpi_printf          mock_vpi_printf
#define vpi_control         mock_vpi_control
#define vpi_iterate(t,h)    NULL
#define vpi_scan(it)        NULL
#define vpi_handle(t,h)     NULL
