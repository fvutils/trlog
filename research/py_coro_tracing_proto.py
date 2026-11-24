import sys, asyncio, contextvars
M = sys.monitoring; TOOL = M.PROFILER_ID
M.use_tool_id(TOOL, "trl")
CO_COROUTINE = 0x80
cur = contextvars.ContextVar("cur", default=None)
def mine(code): return code.co_flags & CO_COROUTINE and code.co_filename == __file__
def ev(name):
    def cb(code, off, *a):
        if not mine(code): return M.DISABLE
        f = sys._getframe(1)
        parent = f.f_back.f_code.co_name if f.f_back else None
        t = asyncio.current_task(); 
        print(f"{name:7} {code.co_name:8} parent_frame={parent:14} task={t.get_name()} ctx={cur.get()} args={dict(f.f_locals) if name=='START' else ''}")
    return cb
for n,e in [("START",M.events.PY_START),("YIELD",M.events.PY_YIELD),("RESUME",M.events.PY_RESUME),("RETURN",M.events.PY_RETURN)]:
    M.register_callback(TOOL, e, ev(n))
M.set_events(TOOL, M.events.PY_START|M.events.PY_YIELD|M.events.PY_RESUME|M.events.PY_RETURN)

async def write(addr, data):
    await asyncio.sleep(0.01); return "ok"
async def seq(n):
    cur.set(f"seq{n}")
    await write(0x10, n)
async def main():
    t = asyncio.create_task(seq(1), name="T1")
    await seq(2); await t
asyncio.run(main())
