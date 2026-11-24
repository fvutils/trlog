"""Time trlog CLI commands on the query benchmark trace, per scan engine.

    PYTHONPATH=python/src python benchmarks/query/bench.py TRACE.trl [OUT.md]

TRACE.trl comes from ``bigtrace.py`` (200k txns: 8 streams, one ``axi``
type with addr/len). Each command runs in a fresh process under
``/usr/bin/time`` with TRLOG_ENGINE=python and =c, so the times include
interpreter start-up. Prints a Markdown table (and writes it to OUT.md).
"""
import os
import subprocess
import sys
import tempfile

COMMANDS = [
    ("info", ["info", "{T}"]),
    ("count (all)", ["count", "{T}"]),
    ("count 'addr > 0x1000 && len < 4'", ["count", "{T}", "addr > 0x1000 && len < 4"]),
    ("find 'len == 3' (first 20)", ["find", "{T}", "len == 3"]),
    ("count --by stream", ["count", "{T}", "--by", "stream"]),
    ("active @1us", ["active", "{T}", "@1us"]),
    ("stats dur --by stream", ["stats", "{T}", "dur", "--by", "stream"]),
    ("hist start --bin 100ns", ["hist", "{T}", "start", "--bin", "100ns"]),
    ("gaps --min 1ns", ["gaps", "{T}", "--min", "1ns"]),
    ("match --a len==0 --b len==1 --on addr",
     ["match", "{T}", "--a", "len==0", "--b", "len==1", "--on", "addr"]),
    ("show t:100000 (full graph)", ["show", "{T}", "t:100000"]),
    ("export sqlite", ["export", "{T}", "sqlite", "-o", "{D}/b.db", "--force"]),
]


def run(argv, engine):
    env = dict(os.environ, TRLOG_ENGINE=engine)
    cmd = ["/usr/bin/time", "-f", "%e %M", sys.executable, "-m", "trlog"] + argv
    p = subprocess.run(cmd, env=env, capture_output=True, text=True)
    secs, kb = p.stderr.strip().splitlines()[-1].split()
    return float(secs), int(kb) // 1024, p.returncode


def main():
    trace = sys.argv[1]
    rows = ["| Command | Python | C | Speed-up |", "|---|---|---|---|"]
    with tempfile.TemporaryDirectory() as d:
        for label, argv in COMMANDS:
            argv = [a.format(T=trace, D=d) for a in argv]
            res = {}
            for eng in ("python", "c"):
                best = min((run(argv, eng) for _ in range(2)), key=lambda r: r[0])
                if best[2] not in (0, 1):
                    raise SystemExit(f"{label} failed on {eng} (exit {best[2]})")
                res[eng] = best
            (tp, mp, _), (tc, mc, _) = res["python"], res["c"]
            rows.append(f"| `{label}` | {tp:.2f} s, {mp} MB | {tc:.2f} s, {mc} MB "
                        f"| {tp / tc:.1f}x |")
            print(rows[-1], flush=True)
    text = "\n".join(rows) + "\n"
    if len(sys.argv) > 2:
        with open(sys.argv[2], "w") as f:
            f.write(text)


if __name__ == "__main__":
    main()
