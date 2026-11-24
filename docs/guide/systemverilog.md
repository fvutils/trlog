# SystemVerilog

The `trl_pkg` package records transactions from a SystemVerilog testbench
into a trace file, and reads trace files back during simulation, for
example for replay or checking. It is a thin layer of SV classes over DPI-C
calls into the trlog C library. The full reference is in
{doc}`../api/sv`.

Import the package with a wildcard:

```systemverilog
import trl_pkg::*;
```

Every name it exports starts with `trl_` (classes, functions) or `TRL_`
(enum literals, macros), so the wildcard import does not collide with user
or UVM names.

## Building

The package is three source files plus two shared libraries from the C
build (`cmake --build build`):

| File | Role |
|---|---|
| `sv/src/trl_pkg.sv` | the package; `include`s the per-class `.svh` files, so put `sv/src` on the include path |
| `sv/src/trl_final.sv` | optional module that closes the trace at end of simulation |
| `libtrl_sv.so`, `libtrl.so` | the DPI-C implementation and the core library |

With Verilator:

```bash
verilator --binary --sv --timing -Isv/src \
    -LDFLAGS "-Lbuild/c -Wl,-rpath,build/c -ltrl_sv -ltrl" \
    sv/src/trl_pkg.sv sv/src/trl_final.sv my_tb.sv
```

## Turning recording on

Recording is off unless the simulation is run with a trace file:

```bash
./Vmy_tb +trlog_file=run.trl
```

The `"default"` database is opened lazily, by the first call that needs it,
from these plusargs:

| Plusarg | Meaning | Default |
|---|---|---|
| `+trlog_file=<path>` | turn recording on, writing to `<path>` | off |
| `+trlog_off` | force recording off, even with `+trlog_file` | |
| `+trlog_compress=none\|zlib` | block compression | `zlib` |
| `+trlog_block_kb=<n>` | target block payload size, in KiB | `64` |
| `+trlog_read=<path>` | file that {sv:func}`trl_reader::get` opens as `"default"` | none |
| `+trlog_read_<name>=<path>` | file that `trl_reader::get("<name>")` opens | none |

When recording is off, the API stays callable: streams come back with
`en == 0`, transactions are no-op objects, and nothing crosses into C. Test
code does not need its own on/off switch. {sv:func}`trl_pkg::trl_enabled`
reports whether recording is on.

## Closing the trace

Buffered records are written when the database is closed. Either call
{sv:func}`trl_pkg::trl_shutdown` at the end of the test (in UVM, from
`final_phase`), or instantiate the {sv:mod}`trl_final` module once at the
top level:

```systemverilog
module my_tb;
    trl_final u_trl_final();
    ...
endmodule
```

Doing both is harmless.

## Recording

A {sv:class}`trl_stream` is a named channel of transactions, typically one
per monitor. A {sv:class}`trl_type` is a transaction schema: a named list
of typed fields. Define the type once, then record against it.

```systemverilog
trl_type   t = trl_type::create("axi_rd");
int f_addr   = t.add_field("addr", TRL_FT_U64);
int f_len    = t.add_field("len",  TRL_FT_U8);
int f_data   = t.add_field("data", TRL_FT_U32);
t.seal();

trl_stream s = trl_stream::create("axi_rd", null, t, "axi");
```

`add_field` returns the field's index. Every later call refers to the field
by that index, never by name, so recording does not pass strings to C.
Streams form a hierarchy through the `parent` argument of
{sv:func}`trl_stream::create`.

There are three ways to record, from fastest to most flexible.

### Complete transactions: `record()`

When every field is an integer and the transaction is known in full,
{sv:func}`trl_stream::record` writes it in one call. Element `i` of the value
array is field `i`:

```systemverilog
longint unsigned v[3];

`TRL_IF(s) begin
    v[f_addr] = vif.araddr;
    v[f_len]  = vif.arlen;
    v[f_data] = vif.rdata;
    void'(s.record(t_start, $time, v));
end
```

The `TRL_IF` macro skips the whole block, including computing the field
values, when the stream is disabled. It is equivalent to
`if (s != null && s.en)`.

### Open, set, close: `open_txn()`

{sv:func}`trl_stream::open_txn` returns a {sv:class}`trl_txn`. Set fields as
their values become known, then close it. The start time defaults to the
current time, and so does the end time passed to `close`:

```systemverilog
trl_txn x = s.open_txn(t);
x.set_int(f_addr, vif.araddr);
x.set_int(f_len,  vif.arlen);
@(posedge vif.rlast);
x.set_int(f_data, vif.rdata);
x.close();
```

Leaving out the type uses the stream's default type, the `dflt` argument of
{sv:func}`trl_stream::create`. If the stream has none, `open_txn()` and
`record()` print a `$warning`, record nothing, and return a no-op transaction
(ID 0).

The `set_*` functions cover integer, real and string fields:
{sv:func}`trl_txn::set_int`, {sv:func}`trl_txn::set_real`,
{sv:func}`trl_txn::set_str` and {sv:func}`trl_txn::set_str_id`. For a string
value that repeats, intern it once with {sv:func}`trl_db::intern` and set it
by ID with `set_str_id`.

To avoid allocating an object per transaction, keep one `trl_txn` and pass
it as `into`:

```systemverilog
trl_txn x = new;
...
void'(s.open_txn(t, .into(x)));
```

### Fields by name: `field()`

For debug-style streams with no schema set up in advance,
{sv:func}`trl_stream::field` creates fields by name on first use, on the
stream's ad-hoc type. Later lookups of the same name are an SV
associative-array hit:

```systemverilog
trl_stream dbg = trl_stream::create("dbg");

`TRL_IF(dbg) begin
    trl_txn x = dbg.open_txn(dbg.adhoc_type());
    x.set_str(dbg.field("msg", TRL_FT_STRING), msg);
    x.set_int(dbg.field("lvl", TRL_FT_U8), lvl);
    x.close();
end
```

Only scalar field kinds can be created this way.

### Relationships between transactions

A transaction can have a parent (pass the parent's ID as `parent`, or use
{sv:func}`trl_stream::open_child`), links to other transactions, and
key/value metadata:

```systemverilog
trl_db  db  = trl_db::get();
trl_txn req = s.open_txn(t);
trl_txn rsp = s.open_child(req, t_rsp);
rsp.link(other.id(), TRL_LINK_CAUSE);
rsp.meta(db.intern("origin"), db.intern("scoreboard"));
```

Link kinds are listed in `trl_link_e`.

### More than one file

Every `create` and `find` function takes an optional database. The default
is the `"default"` database opened from `+trlog_file`. To write a second
file, open it with {sv:func}`trl_db::open` and pass it explicitly:

```systemverilog
trl_db perf = trl_db::open("perf", "perf.trl");
trl_stream ps = trl_stream::create("lat", null, t_lat, "", perf);
```

## Reading

A {sv:class}`trl_reader` opens a trace file. A {sv:class}`trl_cursor` walks
its transactions in start-time order, either over every stream or over
selected streams ({sv:class}`trl_rstream`). On the reading side, a type is
a {sv:class}`trl_schema`. Use it to look up field indices by name:

```systemverilog
trl_reader  r  = trl_reader::get();              // +trlog_read=golden.trl
trl_rstream rs = r.find_stream("axi_rd");
trl_schema  sc = r.find_schema("axi_rd");
int f_addr = sc.index("addr");
int f_data = sc.index("data");

trl_cursor c = trl_cursor::create(r, rs);
while (c.next()) begin
    c.wait_start();                              // replay in simulation time
    drive_read(c.get_int(f_addr), c.get_int(f_data));
end
c.free();
```

After each successful `next()`, the cursor's properties (`id`, `start`,
`end_t`, `parent`, `attr_count`, ...) describe the current transaction.
Read field values with `get_int`, `get_real` and `get_str`, and links and
metadata with {sv:func}`trl_cursor::link` and {sv:func}`trl_cursor::meta_at`.

## Current limitations

- Field values can be integers (up to 64 bits), reals and strings. There
  are no set/get functions yet for wide bit vectors, 4-state values, byte
  arrays or enums, and no nested (struct or array) fields.
- `+trlog_streams`, `+trlog_buf_mb`, `+trlog_max_age`, `+trlog_check` and
  `+trlog_verbose` from the design are not implemented yet.
- Trace files are written with a 1 ns timescale, and times are taken from
  `$time` in `trl_pkg`'s time unit. The `TRL_TIMEUNIT` macro is defined
  but not applied yet.
- With Verilator, `record()` makes one DPI call per field instead of a
  single call, because Verilator cannot pass a dynamic array through a DPI
  open-array argument.
