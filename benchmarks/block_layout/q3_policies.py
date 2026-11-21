"""Q3 experiment: mixed vs per-stream vs pooled txn blocks (Python reference writer)."""
import os, random, sys, time, heapq, json
from trlog._writer import TrlWriter
from trlog._reader import TrlReader
from trlog._types import FieldType as FT, FieldDef, TxnAttr

OUT = sys.argv[1]
N_TXN = int(os.environ.get("N_TXN", "100000"))
BLK = int(os.environ.get("BLK", "2000"))          # records per block target
CAP = int(os.environ.get("CAP", str(8 * BLK)))     # per-stream: total buffered-record cap

def gen(n_streams, zipf, n_schemas, seed=1):
    rnd = random.Random(seed)
    w = [1.0 / (i + 1) ** zipf for i in range(n_streams)]
    tot = sum(w); w = [x / tot for x in w]
    addr = [rnd.randrange(1 << 32) & ~63 for _ in range(n_streams)]
    t = 0; recs = []
    for i in range(N_TXN):
        t += rnd.randint(1, 4) * 10
        s = rnd.choices(range(n_streams), w)[0]
        addr[s] += 64
        start = t; end = t + rnd.randint(1, 8) * 10
        sch = s % n_schemas
        recs.append(dict(sid=s + 1, typ=sch + 1, id=i + 1, start=start, end=end,
                         attrs=[addr[s], rnd.randrange(1 << 32), rnd.randint(1, 16), rnd.randrange(4)]))
    # emission order = end-time order (records are written on close)
    recs.sort(key=lambda r: (r["end"], r["id"]))
    return recs

def plan_mixed(recs):
    return [(None, recs[i:i + BLK]) for i in range(0, len(recs), BLK)]

def plan_per_stream(recs, cap=None):
    bufs = {}; out = []; total = 0; peak = 0
    for r in recs:
        b = bufs.setdefault(r["sid"], []); b.append(r); total += 1
        if len(b) >= BLK:
            out.append((r["sid"], b)); total -= len(b); bufs[r["sid"]] = []
        elif cap and total > cap:          # memory cap: flush the largest buffer
            sid = max(bufs, key=lambda k: len(bufs[k]))
            out.append((sid, bufs[sid])); total -= len(bufs[sid]); bufs[sid] = []
        peak = max(peak, total)
    for sid, b in bufs.items():
        if b: out.append((sid, b))
    return out, peak

def plan_pooled(recs, promote_frac=0.10):
    """Streams start in a shared pool; a stream holding >= promote_frac of a
    flushed pool block gets a dedicated buffer from then on."""
    pool = []; ded = {}; dedicated = set(); out = []; total = 0; peak = 0
    for r in recs:
        sid = r["sid"]
        if sid in dedicated:
            b = ded.setdefault(sid, []); b.append(r); total += 1
            if len(b) >= BLK:
                out.append((sid, b)); total -= len(b); ded[sid] = []
        else:
            pool.append(r); total += 1
            if len(pool) >= BLK:
                cnt = {}
                for x in pool: cnt[x["sid"]] = cnt.get(x["sid"], 0) + 1
                for k, c in cnt.items():
                    if c >= promote_frac * BLK: dedicated.add(k)
                out.append((None, pool)); total -= len(pool); pool = []
        peak = max(peak, total)
    if pool: out.append((None, pool))
    for sid, b in ded.items():
        if b: out.append((sid, b))
    return out, peak

def write(path, plan, n_schemas, layout):
    w = TrlWriter(path, column_layout=layout)
    fields = [FieldDef(w.intern(n), ft) for n, ft in
              [("addr", FT.FT_U64), ("data", FT.FT_U32), ("len", FT.FT_U8), ("kind", FT.FT_U8)]]
    for k in range(n_schemas):
        w.add_txn_schema(f"t{k}", fields)
    t0 = time.perf_counter()
    for sid, blk in plan:
        blk = sorted(blk, key=lambda r: (r["start"], r["id"]))   # sort-in-block (design §7.3)
        with w.begin_txn_block(blk[0]["start"], stream_inst_id=sid) as tb:
            for r in blk:
                tb.write_full(r["sid"], r["typ"], r["id"], r["start"], r["end"], 0,
                              [TxnAttr(i, v) for i, v in enumerate(r["attrs"])])
    w.close()
    return time.perf_counter() - t0

def read_cost(path, sid):
    """Decode every block a stream query must touch (including shared blocks)."""
    SENT = 0xFFFFFFFF
    t0 = time.perf_counter(); n = 0; nb = 0
    with TrlReader(path) as r:
        ents = r._index._txn_entries
        has_sid = any(x[3] != SENT for x in ents)
        if sid is None or not has_sid:
            offs = [o for _, _, o, _ in ents]
        else:
            offs = [o for _, _, o, s in ents if s == sid or s == SENT]
        for off in offs:
            r._file.seek(off)
            bt, flags, payload, _ = r._read_one_block()
            recs = []
            r._txn_codec.decode_block(None, r._store, payload, flags, recs.append)
            nb += 1; n += len(recs)
    return time.perf_counter() - t0, nb, n

SCEN = {
    "S1_8x_uniform":   (8, 0.0, 1),
    "S2_64x_zipf":     (64, 1.2, 4),
    "S3_1000x_zipf":   (1000, 1.2, 8),
}
res = []
for name, (ns, z, nsch) in SCEN.items():
    recs = gen(ns, z, nsch)
    cnt = {}
    for r in recs: cnt[r["sid"]] = cnt.get(r["sid"], 0) + 1
    heavy = max(cnt, key=cnt.get); quiet = min(cnt, key=cnt.get)
    ps, ps_peak = plan_per_stream(recs)
    pc, pc_peak = plan_per_stream(recs, cap=CAP)
    pp, pp_peak = plan_pooled(recs)
    plans = {"mixed": (plan_mixed(recs), BLK), "per_stream": (ps, ps_peak),
             "per_stream_cap": (pc, pc_peak), "pooled": (pp, pp_peak)}
    for pname, (plan, peak) in plans.items():
        for layout in ("row", "adaptive"):
            if pname == "mixed" and layout == "adaptive": pass
            path = os.path.join(OUT, f"{name}_{pname}_{layout}.trl")
            wt = write(path, plan, nsch, layout)
            row = dict(scen=name, policy=pname, layout=layout, blocks=len(plan),
                       bytes=os.path.getsize(path), peak_buf=peak, write_s=round(wt, 2))
            for tag, sid in (("heavy", heavy), ("quiet", quiet)):
                s, nb, n = read_cost(path, sid)
                row[f"rd_{tag}_blocks"] = nb; row[f"rd_{tag}_s"] = round(s, 3)
            s, nb, n = read_cost(path, None); row["rd_all_s"] = round(s, 2)
            row["heavy_n"] = cnt[heavy]; row["quiet_n"] = cnt[quiet]
            res.append(row); print(json.dumps(row), flush=True)
