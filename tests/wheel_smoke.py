"""Smoke test run by cibuildwheel against the installed wheel.

Confirms the bundled native libraries load from the package directory (the
binary-wheel layout) with no source tree or TRL_LIBRARY_PATH, that the
discovery helpers point into the package, and that a trace written by the
native writer (zlib-compressed, so the vendored zlib is exercised) reads back.

Run it from outside the source tree, or the checkout's python/src may shadow
the installed package.
"""
import ctypes
import os
import subprocess
import sys
import tempfile

import trlog
from trlog import _libpath, _native
from trlog.query import _cengine


def fail(msg):
    print("FAIL:", msg)
    return 1


def main():
    pkg_dir = os.path.dirname(os.path.abspath(trlog.__file__))
    print("trlog", trlog.__version__, "from", pkg_dir)
    if "site-packages" not in pkg_dir and "dist-packages" not in pkg_dir:
        print("warning: trlog is not imported from an installed location")

    for base in ("trl", "trl_sv"):
        p = os.path.join(pkg_dir, _libpath.lib_name(base))
        if not os.path.isfile(p):
            return fail("%s missing from the wheel" % p)
    vpi = os.path.join(pkg_dir, _libpath.lib_name("trlog_vpi"))
    if not os.path.isfile(vpi):
        return fail("%s missing from the wheel" % vpi)
    # Simulators dlopen it with no library path: it must find libtrl beside
    # itself and need no VPI symbols at load time (they are looked up later).
    try:
        ctypes.CDLL(vpi)
    except OSError as e:
        return fail("cannot load %s: %s" % (vpi, e))
    print("trlog_vpi:", vpi)

    if _native._LIB is None:
        return fail("_native did not load libtrl")
    if os.path.dirname(os.path.abspath(_native._LIB._name)) != pkg_dir:
        return fail("libtrl loaded from %s, not the package" % _native._LIB._name)
    print("libtrl:", _native._LIB._name)
    if _cengine.load() is None:
        return fail("query engine did not load: %s" % _cengine.load_error())

    # The SV DPI library links against libtrl; it must load beside it.
    _libpath.open_cdll(os.path.join(pkg_dir, _libpath.lib_name("trl_sv")))

    if trlog.get_libdirs() != [pkg_dir]:
        return fail("get_libdirs() = %r" % trlog.get_libdirs())
    inc = trlog.get_incdirs()
    if not any(os.path.isfile(os.path.join(d, "trl", "trl.h")) for d in inc):
        return fail("no include dir holds trl/trl.h: %r" % inc)
    sv = trlog.get_svdirs()
    if not any(os.path.isfile(os.path.join(d, "trl_pkg.sv")) for d in sv):
        return fail("no SV dir holds trl_pkg.sv: %r" % sv)
    print("libdirs:", trlog.get_libdirs(), "incdirs:", inc, "svdirs:", sv)

    out = subprocess.run([sys.executable, "-m", "trlog", "config", "--libdir"],
                         capture_output=True, text=True)
    if out.returncode != 0 or out.stdout.strip() != pkg_dir:
        return fail("trlog config --libdir: rc=%d %r" % (out.returncode, out.stdout))

    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "smoke.trl")
        with _native._CtypesWriter(path, compress=True, compressor="zlib") as w:
            with w.begin_txn_block(0) as txn:
                for i in range(100):
                    txn.write_full(stream_inst_id=1, txn_type_id=1, txn_id=i + 1,
                                   start=i * 10, end=i * 10 + 5)
        with trlog.TrlReader(path) as r:
            n = sum(len(blk) for blk in r.iter_txn_blocks())
        if n != 100:
            return fail("read back %d transactions, wrote 100" % n)
    print("native write + read round trip OK")
    print("PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
