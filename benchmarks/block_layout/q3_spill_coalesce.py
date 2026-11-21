"""Q3 follow-up: per-stream + coalesced spill vs per_stream_cap, with an
index that lists the streams in each block (simulated: block->stream set kept
here, offsets captured at write time)."""
import os, sys, time, json
sys.argv = [sys.argv[0], sys.argv[1]]
import importlib.util
spec = importlib.util.spec_from_file_location("q3", os.path.join(os.path.dirname(__file__), "q3_policies.py"))
src = open(spec.origin).read().split("SCEN = {")[0]
ns = {}; exec(compile(src, "q3", "exec"), ns)
gen, BLK, CAP = ns["gen"], ns["BLK"], ns["CAP"]
from trlog._writer import TrlWriter
from trlog._reader import TrlReader
from trlog._types import FieldType as FT, FieldDef, TxnAttr
OUT = sys.argv[1]

def plan_cap(recs):
    out, peak = ns["plan_per_stream"](recs, cap=CAP)
    return [(sid, b) for sid, b in out], peak

def plan_coalesce(recs, small_frac=0.25):
    bufs = {}; out = []; total = 0; peak = 0
    for r in recs:
        b = bufs.setdefault(r["sid"], []); b.append(r); total += 1
        if len(b) >= BLK:
            out.append((r["sid"], b)); total -= len(b); bufs[r["sid"]] = []
        elif total > CAP:
            big = max(bufs, key=lambda k: len(bufs[k]))
            if len(bufs[big]) >= small_frac * BLK:
                out.append((big, bufs[big])); total -= len(bufs[big]); bufs[big] = []
            else:   # everything is small: spill all buffers as coalesced blocks
                pend = [x for k in list(bufs) for x in bufs[k]]
                for i in range(0, len(pend), BLK):
                    out.append(("multi", pend[i:i + BLK]))
                total -= len(pend); bufs = {}
        peak = max(peak, total)
    for sid, b in bufs.items():
        if b: out.append((sid, b))
    return out, peak

def write(path, plan, nsch, layout):
    w = TrlWriter(path, column_layout=layout)
    fields = [FieldDef(w.intern(n), ft) for n, ft in
              [("addr", FT.FT_U64), ("data", FT.FT_U32), ("len", FT.FT_U8), ("kind", FT.FT_U8)]]
    for k in range(nsch): w.add_txn_schema(f"t{k}", fields)
    blocks = []
    for sid, blk in plan:
        blk = sorted(blk, key=lambda r: (r["start"], r["id"]))
        off = w._file.tell()
        with w.begin_txn_block(blk[0]["start"], stream_inst_id=(None if sid == "multi" else sid)) as tb:
            for r in blk:
                tb.write_full(r["sid"], r["typ"], r["id"], r["start"], r["end"], 0,
                              [TxnAttr(i, v) for i, v in enumerate(r["attrs"])])
        blocks.append((off, {r["sid"] for r in blk}))
    w.close()
    return blocks

def read(path, blocks, sid):
    t0 = time.perf_counter(); nb = 0
    with TrlReader(path) as r:
        for off, sids in blocks:
            if sid is not None and sid not in sids: continue
            r._file.seek(off); bt, fl, pl, _ = r._read_one_block()
            recs = []; r._txn_codec.decode_block(None, r._store, pl, fl, recs.append); nb += 1
    return nb, round(time.perf_counter() - t0, 3)

for name, (nstr, z, nsch) in {"S2_64x_zipf": (64, 1.2, 4), "S3_1000x_zipf": (1000, 1.2, 8)}.items():
    recs = gen(nstr, z, nsch)
    cnt = {}
    for r in recs: cnt[r["sid"]] = cnt.get(r["sid"], 0) + 1
    heavy = max(cnt, key=cnt.get); quiet = min(cnt, key=cnt.get)
    med = sorted(cnt, key=cnt.get)[len(cnt) // 2]
    for pname, fn in (("per_stream_cap", plan_cap), ("spill_coalesce", plan_coalesce)):
        plan, peak = fn(recs)
        path = os.path.join(OUT, f"b_{name}_{pname}.trl")
        blocks = write(path, plan, nsch, "adaptive")
        row = dict(scen=name, policy=pname, blocks=len(plan), multi=sum(1 for s, _ in plan if s == "multi"),
                   bytes=os.path.getsize(path), peak_buf=peak)
        for tag, s in (("heavy", heavy), ("median", med), ("quiet", quiet)):
            row[tag] = (cnt[s],) + read(path, blocks, s)
        print(json.dumps(row), flush=True)
