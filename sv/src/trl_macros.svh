// trl_macros.svh — compile-time knobs for trl_pkg; included by trl_pkg.sv (sv_api_design.md §4.5).
//
// `TRL_MAX_BITS` (the wide packed-vector argument width) is not defined
// here: trl_sv.c doesn't export set_bits/set_logic/set_bytes this phase
// (see its header comment), so there is no fixed-width DPI argument that
// would need it yet.

`ifndef TRL_MACROS_SVH
`define TRL_MACROS_SVH

`ifndef TRL_TIMEUNIT
// Reserved time unit for the package. Not yet applied: times are taken
// from ``$time`` in ``trl_pkg``'s time unit, and files are written with a
// 1 ns timescale.
`define TRL_TIMEUNIT 1ps
`endif

// Guard recording code so it is skipped when ``stream`` is disabled.
//
// Skips computing field values as well as the DPI calls::
//
//     `TRL_IF(mon_s) begin
//         trl_txn x = mon_s.open_txn();
//         x.set_int(f_addr, compute_addr());
//         x.close();
//     end
//
// :param stream: a :sv:class:`trl_stream` handle (may be ``null``)
`define TRL_IF(stream) if ((stream) != null && (stream).en)

`endif // TRL_MACROS_SVH
