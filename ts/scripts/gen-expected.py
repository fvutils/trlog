"""Write the reference results the vitest suite compares against.

Reads every tests/golden/fixtures/*.trl with the pure-Python reader and
records, per file: timescale, signals (path, id, encoding, width), the value
changes of each 2-state (<= 64 bits), real and string signal, and the ids
and times of every transaction. Run from the repo root (npm run gen:expected).
"""
import argparse
import json
import os
import sys

from trlog import TrlReader
from trlog._types import SignalEncoding, TxnBegin, TxnFull

FIXTURES = os.path.join("tests", "golden", "fixtures")
SIMPLE = (SignalEncoding.SE_2STATE, SignalEncoding.SE_REAL, SignalEncoding.SE_STRING)


def describe(path):
    r = TrlReader(path)
    try:
        types = r.type_registry
        signals, changes = [], {}
        for scope in r.scopes():
            for v in scope.vars:
                st = types.get_signal_type(v.sig_type_id)
                name = r.string_table.lookup(v.name_str_id)
                signals.append({"path": f"{scope.path}.{name}", "id": v.var_id,
                                "encoding": int(st.encoding), "width": st.bit_width})
                if st.encoding in SIMPLE and not (st.encoding == SignalEncoding.SE_2STATE and st.bit_width > 64):
                    vcs = r.read_signal(v.var_id)
                    changes[str(v.var_id)] = {
                        "times": [str(c.time) for c in vcs],
                        "values": [str(c.value) if isinstance(c.value, int) else c.value for c in vcs],
                    }
        txns = []
        for blk in r.iter_txn_blocks():
            for rec in blk:
                if isinstance(rec, (TxnFull, TxnBegin)):
                    txns.append({"id": str(rec.txn_id), "type": rec.txn_type_id,
                                 "start": str(rec.start_time)})
        txns.sort(key=lambda t: int(t["id"]))
        return {"timescaleExp": r.timescale_exp, "signals": signals,
                "changes": changes, "txns": txns}
    finally:
        r.close()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("-o", "--output", required=True)
    args = ap.parse_args()
    out = {f: describe(os.path.join(FIXTURES, f))
           for f in sorted(os.listdir(FIXTURES)) if f.endswith(".trl")}
    os.makedirs(os.path.dirname(args.output), exist_ok=True)
    with open(args.output, "w") as fp:
        json.dump(out, fp, indent=1)
        fp.write("\n")
    print(f"wrote {args.output}: {len(out)} fixtures", file=sys.stderr)


if __name__ == "__main__":
    main()
