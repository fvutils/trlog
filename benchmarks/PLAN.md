# Trace Format Benchmark Plan: VCD vs FST vs TRLOG

## Goals

Quantitatively compare three waveform/trace formats on two axes:

| Axis | Question |
|------|----------|
| **Space** | How large is the file for the same signal data? |
| **Performance** | How fast is write? read? format conversion? |

Supported formats:
- **VCD** — IEEE-standard Value Change Dump (ASCII text)
- **FST** — Fast Signal Trace (GTKWave binary, natively supported by Verilator `--trace-fst`)
- **TRLOG** — Zuspec Signal Trace (this project; binary, typed)

---

## Toolchain Assumptions

| Tool | Path / Version |
|------|---------------|
| `verilator` | `$PATH` — must support `--trace-vcd` and `--trace-fst` |
| `vcd2fst` | `$PATH` — from GTKWave |
| Python `trlog` package | `python/src/trlog` in this repo |
| pytest-benchmark | installed in the test environment |

---

## Workload Taxonomy

Each benchmark is parameterized along two dimensions:

### Signal Mix

| Mix ID | Description |
|--------|-------------|
| `clk_only` | Single 1-bit signal toggling every timestep |
| `bus_mix` | 1×1-bit clock + 1×8-bit byte bus + 1×32-bit word bus + 1×64-bit wide bus (active every 4 clocks) |
| `dense` | 64×1-bit signals all toggling independently |
| `wide` | 4×128-bit buses toggling every 8 clocks |

### Scale

| Scale ID | Simulation timesteps |
|----------|---------------------|
| `small` | 100 K |
| `medium` | 1 M |
| `large` | 10 M |

---

## Benchmark Categories

### 1. Write Performance

**Goal:** How quickly can each format capture signal activity?

**Method — Verilator native (most realistic):**
- Write a parameterized SV testbench (`sv/bench_dut.sv`) that instantiates the selected signal mix and drives activity for N timesteps.
- Compile twice: once with `--trace-vcd`, once with `--trace-fst`.
- Run the simulation and record wall-clock time for each.
- For TRLOG: replay the same signal activity via the Python `ZstWriter` API, driven by a Python loop of equivalent depth.

**Measurements:**
- Elapsed wall-clock time for the write phase
- Output file size

**Pytest fixture strategy:**
- `conftest.py` provides a `verilator_compile` fixture that returns (vcd_binary, fst_binary) paths.
- Each benchmark test calls `subprocess.run([binary, '+runtime=N'])` timed with `benchmark()`.
- TRLOG write is a pure Python function wrapped in `benchmark()`.

---

### 2. File Size

**Goal:** How many bytes does each format require for the same recorded data?

**Method:**
- After writing, record `os.path.getsize()` for each file.
- Also gzip the VCD and record its compressed size (VCD is compressible plain text; this gives a fairer baseline).
- Express sizes as bytes and as ratio relative to VCD.

**Table structure (output):**
```
workload          VCD      VCD.gz    FST      TRLOG
clk_only/small    X MB     Y MB      Z MB     W MB
bus_mix/medium    ...
dense/large       ...
```

This is collected as a non-benchmark pytest test using `record_property` so results appear in JUnit XML / terminal output.

---

### 3. Read Performance

**Goal:** How quickly can a tool/library parse each format and iterate over signal changes?

**VCD reading strategy:**
- Parse with a minimal hand-written VCD tokenizer (no dependencies; measures raw I/O + parsing).
- Counts total `#timestamp` and value-change tokens seen.

**FST reading strategy:**
- Use `subprocess.run(['vcd2fst', ...])` to convert back to VCD (measures FST decode throughput end-to-end).
- Alternatively: call `fst2vcd` and count lines if available.

**TRLOG reading strategy:**
- Use `ZstReader` from this repo and iterate `read_signal()` over all recorded signals.

**Measurements:**
- Elapsed time to fully iterate all changes
- Derived throughput: changes/s and MB/s

---

### 4. Conversion Benchmarks

**Goal:** Quantify the cost of format conversion pipelines.

| Conversion | Command / Method |
|------------|-----------------|
| VCD → FST | `vcd2fst <in.vcd> <out.fst>` via subprocess |
| VCD → TRLOG | Python: parse VCD, emit via `ZstWriter` |
| FST → VCD | `fst2vcd <in.fst> <out.vcd>` via subprocess (if available) |

**Measurements:**
- Wall-clock time per conversion
- Output file size (already captured in category 2)

---

## File / Directory Layout

```
benchmarks/
  PLAN.md                    ← this file
  sv/
    bench_dut.sv             ← parameterized SV testbench (clk + signal mix)
  tests/
    conftest.py              ← fixtures: compile DUT, generate reference traces
    test_write_perf.py       ← write-performance benchmarks
    test_read_perf.py        ← read-performance benchmarks
    test_file_size.py        ← file-size comparison (non-benchmark)
    test_conversion.py       ← conversion-pipeline benchmarks
    _vcd_tokenizer.py        ← minimal VCD parser used by read bench
```

---

## SV Testbench Design (`sv/bench_dut.sv`)

```systemverilog
// Parameterized benchmark DUT
// Parameters passed via +define or plusargs
module bench_dut;

  // Defaults; override at compile time via +define+BENCH_STEPS=N etc.
  parameter int STEPS      = 1_000_000;
  parameter int SIGNAL_MIX = 0;   // 0=clk_only, 1=bus_mix, 2=dense, 3=wide

  logic        clk = 0;
  logic [7:0]  byte_bus;
  logic [31:0] word_bus;
  logic [63:0] wide_bus;
  logic [63:0] dense [0:63];   // 64 independent 1-bit-wide signals (use [0] bit)
  logic [127:0] wide128 [0:3];

  // Clock
  always #5 clk = ~clk;

  integer step;
  initial begin
    $dumpfile("trace.vcd");   // overridden per compile
    $dumpvars(0, bench_dut);

    for (step = 0; step < STEPS; step++) begin
      @(posedge clk);

      if (SIGNAL_MIX >= 1) begin
        byte_bus  <= step[7:0];
        word_bus  <= step;
        wide_bus  <= {step, step};
      end
      if (SIGNAL_MIX >= 2) begin
        for (int i = 0; i < 64; i++)
          dense[i][0] <= $urandom_range(0,1);
      end
      if (SIGNAL_MIX >= 3) begin
        for (int i = 0; i < 4; i++)
          wide128[i] <= {$random, $random, $random, $random};
      end
    end
    $finish;
  end
endmodule
```

For `--trace-fst`, Verilator uses `$dumpfile`/`$dumpvars` when the binary is invoked with `+verilator+fst+filename+<path>`.  The compile flag switches the underlying trace library.

---

## conftest.py Fixtures

```python
import os, shutil, subprocess, pytest

VERILATOR = shutil.which("verilator")
VCD2FST   = shutil.which("vcd2fst")

SV_DIR = os.path.join(os.path.dirname(__file__), "../sv")

MIXES  = ["clk_only", "bus_mix", "dense", "wide"]
SCALES = {"small": 100_000, "medium": 1_000_000, "large": 10_000_000}

@pytest.fixture(scope="session", params=[
    pytest.param(("clk_only", "medium"), id="clk_only-medium"),
    pytest.param(("bus_mix",  "medium"), id="bus_mix-medium"),
    pytest.param(("dense",    "medium"), id="dense-medium"),
    pytest.param(("clk_only", "large"),  id="clk_only-large"),
    pytest.param(("bus_mix",  "large"),  id="bus_mix-large"),
])
def compiled_sim(tmp_path_factory, request):
    """Compile the SV DUT for VCD and FST output; return run information."""
    mix_name, scale_name = request.param
    mix_id    = MIXES.index(mix_name)
    steps     = SCALES[scale_name]
    base      = tmp_path_factory.mktemp(f"{mix_name}_{scale_name}")

    def _compile(trace_flag, obj_dir_suffix):
        obj_dir = base / f"obj_{obj_dir_suffix}"
        cmd = [
            VERILATOR, "--binary", "--sv",
            f"+define+STEPS={steps}",
            f"+define+SIGNAL_MIX={mix_id}",
            trace_flag,
            "-o", f"simv_{obj_dir_suffix}",
            "--Mdir", str(obj_dir),
            os.path.join(SV_DIR, "bench_dut.sv"),
        ]
        subprocess.check_call(cmd, cwd=str(base))
        return base / f"simv_{obj_dir_suffix}"

    vcd_bin = _compile("--trace-vcd", "vcd")
    fst_bin = _compile("--trace-fst", "fst")

    return {
        "mix": mix_name, "scale": scale_name, "steps": steps,
        "base": base,
        "vcd_bin": vcd_bin, "fst_bin": fst_bin,
    }
```

---

## Individual Test Modules

### `test_write_perf.py`

```python
import subprocess, os, sys, pytest

sys.path.insert(0, ...)
from trlog import ZstWriter
from trlog._types import SignalEncoding, ScopeType

def test_write_vcd(benchmark, compiled_sim, tmp_path):
    bin = compiled_sim["vcd_bin"]
    out = tmp_path / "trace.vcd"
    def _run():
        subprocess.check_call([str(bin), "+trace", f"+verilator+vcd+filename+{out}"])
    benchmark(_run)

def test_write_fst(benchmark, compiled_sim, tmp_path):
    bin = compiled_sim["fst_bin"]
    out = tmp_path / "trace.fst"
    def _run():
        subprocess.check_call([str(bin), "+trace", f"+verilator+fst+filename+{out}"])
    benchmark(_run)

def test_write_zst(benchmark, compiled_sim, tmp_path):
    steps = compiled_sim["steps"]
    mix   = compiled_sim["mix"]
    out   = tmp_path / "trace.trl"
    def _write():
        with ZstWriter(str(out), compress=False) as w:
            _populate_zst(w, mix, steps)
        return out.stat().st_size
    benchmark(_write)
```

### `test_file_size.py`

```python
import os, gzip, shutil, subprocess

def test_sizes(compiled_sim, tmp_path, record_property):
    vcd_path = tmp_path / "trace.vcd"
    fst_path = tmp_path / "trace.fst"
    trl_path = tmp_path / "trace.trl"
    # run simulations and write TRLOG ...
    sizes = {
        "vcd": vcd_path.stat().st_size,
        "fst": fst_path.stat().st_size,
        "trlog": trl_path.stat().st_size,
    }
    # gzip VCD for fair comparison
    gz_path = tmp_path / "trace.vcd.gz"
    with open(vcd_path, 'rb') as f_in, gzip.open(gz_path, 'wb', compresslevel=6) as f_out:
        shutil.copyfileobj(f_in, f_out)
    sizes["vcd_gz"] = gz_path.stat().st_size

    for k, v in sizes.items():
        record_property(k, v)
        print(f"  {k:10s}: {v:>12,} bytes  ({v/sizes['vcd']*100:5.1f}% of VCD)")
```

### `test_read_perf.py`

```python
def test_read_vcd(benchmark, vcd_trace):
    def _read():
        return _count_vcd_changes(vcd_trace)
    benchmark(_read)

def test_read_fst(benchmark, fst_trace):
    def _read():
        # decode via fst2vcd subprocess or direct reader
        return _count_fst_changes(fst_trace)
    benchmark(_read)

def test_read_zst(benchmark, trl_trace):
    from trlog import ZstReader
    def _read():
        with ZstReader(str(trl_trace)) as r:
            total = sum(len(r.read_signal(v)) for v in r.signals)
        return total
    benchmark(_read)
```

### `test_conversion.py`

```python
def test_vcd_to_fst(benchmark, vcd_trace, tmp_path):
    out = tmp_path / "converted.fst"
    def _convert():
        subprocess.check_call([VCD2FST, str(vcd_trace), str(out)])
    benchmark(_convert)

def test_vcd_to_zst(benchmark, vcd_trace, tmp_path):
    out = tmp_path / "converted.trl"
    def _convert():
        _parse_vcd_write_zst(vcd_trace, out)
    benchmark(_convert)
```

---

## Running the Benchmarks

```bash
# Install benchmark dependency
pip install pytest-benchmark

# Set PYTHONPATH for the trlog package
export PYTHONPATH=$PWD/python/src

# Run all benchmarks (generates .benchmarks/ JSON artifacts)
pytest benchmarks/tests/ -v --benchmark-autosave

# Run only a specific category
pytest benchmarks/tests/test_file_size.py -v
pytest benchmarks/tests/test_write_perf.py -v --benchmark-sort=mean

# Compare saved runs
pytest-benchmark compare .benchmarks/*.json --sort=name
```

---

## Expected Output Shape

After a full run the terminal should show a table like:

```
Name (time in ms)                   Min      Max     Mean   StdDev   Rounds
----------------------------------------------------------------------------
test_write_vcd[clk_only-medium]    43.2    45.1    44.0     0.8        5
test_write_fst[clk_only-medium]    12.1    12.9    12.4     0.3        5
test_write_zst[clk_only-medium]   112.3   118.0   115.0     2.1        5
...
```

And file-size test output:

```
vcd       :  18,432,100 bytes  (100.0% of VCD)
vcd_gz    :   1,204,800 bytes  (  6.5% of VCD)
fst       :     821,440 bytes  (  4.5% of VCD)
trlog       :     614,400 bytes  (  3.3% of VCD)
```

---

## Open Questions / Decisions Needed Before Coding

1. **TRLOG read path** — Does `ZstReader.signals` expose a list of variable handles? The current API (from `bench_read_vc.py`) calls `r.read_signal(v)` with a handle returned at write time. For the read benchmark we need to enumerate all signals from an existing file; confirm the reader API supports this.

2. **FST read** — No Python FST reader is available in this environment. Options:
   - `subprocess` call to `fst2vcd` (if installed alongside `vcd2fst`)
   - Measure FST read by asking Verilator to reload the FST during a second sim pass (complex)
   - **Recommended:** use `vcd2fst` / `fst2vcd` from GTKWave; confirm `fst2vcd` is available.

3. **VCD write via TRLOG pipeline** — The "VCD → TRLOG" conversion benchmark requires a VCD parser. Should we write a minimal one or use a dependency?
   - Option A: write a minimal line-oriented VCD tokenizer (no deps, ~100 lines)
   - Option B: accept `pyvcd` or `vcd` as a test dependency

4. **Compression variants for TRLOG** — TRLOG supports `compress=True` (zlib per block). Should we include a `trl_compressed` column alongside `trl_raw`?
MSB: Yes

5. **Parametric scope** — `large` scale (10M steps) may take >30 s per run in Python (TRLOG write). Consider capping `test_write_zst` at `medium` and using `large` only for the Verilator native benchmarks.

6. **Signal hierarchy depth** — For a fair comparison, the SV testbench should use the same hierarchy depth that a real design would have (≥2 levels). Currently the plan uses a flat `bench_dut` module; add one level of sub-module nesting.
MSB: Yes, we'll need deeper hierarchy
