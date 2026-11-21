// trlog_macros.svh — compile-time knobs for trlog_pkg (sv_api_design.md §4.5).
//
// `TRLOG_MAX_BITS` (the wide packed-vector argument width) is not defined
// here: trl_sv.c doesn't export set_bits/set_logic/set_bytes this phase
// (see its header comment), so there is no fixed-width DPI argument that
// would need it yet.

`ifndef TRLOG_MACROS_SVH
`define TRLOG_MACROS_SVH

`ifndef TRLOG_TIMEUNIT
`define TRLOG_TIMEUNIT 1ps
`endif

// TRLOG_IF(stream): the recommended idiom for skipping field computation on
// a disabled stream without a DPI crossing (§3.4). `stream` must be a
// trlog_pkg::stream handle.
`define TRLOG_IF(stream) if ((stream) != null && (stream).en)

`endif // TRLOG_MACROS_SVH
