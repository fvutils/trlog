# Automatic transaction recording from Python coroutines

Research note (2026-11-21). Question: can we record transactions automatically
from cocotb-like environments that drive simulation with coroutines, without
instrumenting at the pyhdl-if boundary? And can we recover organization
(thread → threads → call)?

Short answer: yes. `sys.monitoring` (Python 3.12+) gives low-overhead,
scheduler-agnostic start/suspend/resume/complete events for coroutines.
Call nesting within a thread is free; thread→thread parentage needs one
small hook at task creation.

Prototype: [`py_coro_tracing_proto.py`](py_coro_tracing_proto.py) (verified on
Python 3.12.3 with asyncio).

## 1. Start/complete notification — `sys.monitoring` (PEP 669)

Coroutine-relevant events:

| Event | Coroutine meaning |
|---|---|
| `PY_START` | Coroutine body begins (first `send`). Args available via `sys._getframe(1).f_locals`. |
| `PY_YIELD` | Suspended at an `await` that reached the scheduler — i.e. it is consuming sim time. |
| `PY_RESUME` | Scheduler woke it. |
| `PY_RETURN` / `PY_UNWIND` | Completed (return value is passed to the callback) / exited via exception. |

Why it beats `sys.settrace` / `sys.setprofile`:

- **Cheap.** A callback that returns `sys.monitoring.DISABLE` turns that event off
  for that code location permanently, so uninteresting functions cost once, not
  per call.
- **Per-function opt-in.** `sys.monitoring.set_local_events(tool, fn.__code__, ...)`
  enables events only on chosen code objects (e.g. functions tagged with a
  `@transaction` decorator, or a BFM module allowlist).
- **Scheduler-agnostic.** Hooks live in the interpreter, so they work for asyncio,
  cocotb's own scheduler, or anything else that drives coroutines via `send()`.

Coroutine detection in the callback: `code.co_flags & 0x80` (`CO_COROUTINE`).

## 2. Parentage

### Call within a thread (`await child()`)

A directly-awaited coroutine runs on top of its caller's frame, so
`frame.f_back` is the parent call. The prototype showed `write → seq → main`
correctly within each task. Alternatively keep a per-task shadow stack
(push on `PY_START`, pop on `PY_RETURN`/`PY_UNWIND`).

### Thread spawning thread (`cocotb.start_soon` / `asyncio.create_task`)

The gap: a spawned task's root frame has the *scheduler* as `f_back`
(`_run` in the asyncio test), not the spawner. Options:

1. **Hook task creation** — wrap `cocotb.start_soon` / `Task.__init__`, record the
   current task as parent, assign a task id → stream in the trlog hierarchy.
   Most reliable; scheduler-specific (one small adapter per scheduler).
2. **`contextvars`** — asyncio tasks copy the creating context, so a ContextVar
   carrying the current thread/transaction id propagates parent→child
   automatically. **Open question:** does cocotb's `Task` copy context
   (1.x vs 2.0)? Needs checking.
3. **`sys.set_coroutine_origin_tracking_depth(n)`** — every coroutine records its
   creation site in `cr_origin` as `(file, line, func)` tuples. Generic, but gives
   a creation site, not a task identity; useful as a fallback/debug aid.

Python 3.14 adds asyncio call-graph introspection (`asyncio.capture_call_graph`),
but it is asyncio-only and does not help cocotb.

## 3. Proposed mapping onto trlog

- **Thread → stream.** Each scheduler task gets a stream; child streams nest
  under the spawning task's stream.
- **Call → transaction.** Begin at `PY_START` (sim time + args as fields), end at
  `PY_RETURN` (return value as a field); parent transaction comes from the
  within-task call stack.
- **Filter on time consumed.** Only emit a call that saw at least one `PY_YIELD`;
  otherwise every zero-time helper coroutine becomes a transaction. Buffer the
  begin and commit it on first yield.
- **Selection.** Per-code-object opt-in (decorator / module allowlist), or global
  events with `DISABLE` for non-matching code.

## 4. Caveats

- `sys.monitoring` is per-interpreter, with a limited number of tool ids;
  coverage tools and debuggers also claim ids, so conflicts are possible.
- Under pyhdl-if/cocotb, Python runs inside simulator callbacks; timestamps must
  come from the cocotb / pyhdl-if sim-time API, not wall clock.
- Complementary to pyhdl-if boundary capture: pyhdl-if gives exact HDL-level
  transactions; monitoring gives the test-level structure above them
  (sequence → call → nested call) with no test-code changes.

## Next steps (if pursued)

- Check whether cocotb `Task` propagates `contextvars`; find its current-task API.
- Prototype against a real cocotb test with sim-time timestamps.
- Measure overhead with global events + `DISABLE` vs. per-code-object opt-in.
