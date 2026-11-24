# VPI waveform dumper

`libtrlog_vpi.so` is a VPI library that records signal value changes from a
running simulation into a `.trl` file, in the same way `$dumpvars` records a
VCD. It walks the design through VPI, declares each selected signal in a
design hierarchy, and writes value changes with the `trl.h` writer
({doc}`c-api`). It records signals only; record transactions with the
SystemVerilog package ({doc}`systemverilog`).

The library is built with the C library (see {doc}`c-api`) as
`build/vpi/libtrlog_vpi.so`, and links `build/c/libtrl.so`.

## Loading the library

The library exports the standard `vlog_startup_routines` table and a
registration function, `trlog_vpi_register`. Load it the way your simulator
loads VPI applications:

| Simulator | How to load |
|---|---|
| Icarus Verilog | `vvp` loads `<name>.vpi` modules: link or copy the library to `trlog.vpi`, then `iverilog -L<dir> -mtrlog ...` and `vvp -M<dir> -mtrlog sim ...` |
| VCS | `vcs -load /abs/path/libtrlog_vpi.so:trlog_vpi_register ...` |
| Other simulators | use the option that loads a VPI library with `vlog_startup_routines`, or that names a bootstrap function (`trlog_vpi_register`) |

`libtrl.so` must also be found at run time: set an rpath when linking, or
add `build/c` to `LD_LIBRARY_PATH`.

For example, with Icarus Verilog:

```bash
ln -sf $TRLOG/build/vpi/libtrlog_vpi.so trlog.vpi
iverilog -g2012 -L. -mtrlog -o sim top.sv
LD_LIBRARY_PATH=$TRLOG/build/c vvp -M. -mtrlog sim +trlog_file=dump.trl
```

Verilator is not supported at present: the integration tests in
`tests/integration/test_vpi.py` do not build with the installed Verilator,
and Verilator cannot call the `$trlog_*` tasks from the design.

## Tracing without changing the design

Pass `+trlog_file=<path>` on the simulator command line. At the start of
simulation the library opens `<path>`, walks every top-level module to full
depth, and traces every signal that passes the `+trlog_signals` filter.

```bash
vvp -M. -mtrlog sim +trlog_file=dump.trl +trlog_signals='top.dut.*'
```

## System tasks

To control tracing from the design, call the system tasks. They follow the
VCD tasks of the same name:

| Task | Effect |
|---|---|
| `$trlog_dumpfile("file.trl")` | open the output file; the other tasks do nothing until a file is open |
| `$trlog_dumpvars(depth, scope)` | declare and trace the signals in `scope` (default: the first top-level module) down to `depth` levels; 0 means all levels, 1 means `scope` only |
| `$trlog_dumpoff` | write buffered changes and stop recording |
| `$trlog_dumpon` | resume recording, and record the current value of every traced signal |
| `$trlog_dumpall` | record the current value of every traced signal |
| `$trlog_dumpflush` | write buffered changes to the file |
| `$trlog_dumplimit(bytes)` | stop recording when the estimated output size reaches `bytes` |

```systemverilog
initial begin
    $trlog_dumpfile("dump.trl");
    $trlog_dumpvars(0, top);
end
```

The file is closed at the end of simulation.

## Plusargs

| Plusarg | Effect |
|---|---|
| `+trlog_file=<path>` | trace the whole design into `<path>` from the start of simulation (see above) |
| `+trlog_signals=<glob>[,<glob>...]` | trace only signals whose dotted path (for example `top.dut.clk`) matches one of the globs (`fnmatch` syntax; `*` also matches `.`); up to 64 patterns |
| `+trlog_compress=none` | do not compress blocks; any other value, and the default, is zlib |
| `+trlog_2state` | record every signal as 2-state, without X/Z |
| `+trlog_flush_interval=<n>` | write a value-change block after `n` changes (default 100000) |
| `+trlog_scope_grouped` | buffer changes and write one value-change block per scope instead of one shared block |

## What is recorded

- Scopes: modules, generate blocks, tasks, functions, named begin and fork
  blocks.
- Signals: nets, regs, integer and real variables, SystemVerilog
  variables, and the elements of reg and net arrays (named `mem[0]`,
  `mem[1]`, ...).
- Real variables are recorded as reals; everything else as 4-state, unless
  `+trlog_2state` is given.
- Changes within one time step are merged: the library waits until the
  time step's delta cycles have settled and records the final value.
- Times are simulation time in the simulator's time precision; the file's
  timescale is the value of `vpi_get(vpiTimeUnit, NULL)`.

Read the file with the Python package or with `trl_iter_vc`
({doc}`c-api`).

## Limitations

- `$trlog_dumplimit` estimates size as 16 bytes per change recorded since
  the file was opened, not from the bytes actually written, so the file can
  end up smaller or larger than the limit.
