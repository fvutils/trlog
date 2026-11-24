"""Native (ctypes) backend selection (issue #23).

The library is found where CMake builds it (build/c/libtrl.so), and
TRL_USE_CTYPES_NATIVE=1 swaps only the writer: the reader stays the
full pure-Python TrlReader, which reads C-written files.
"""
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
LIB = ROOT / "build" / "c" / "libtrl.so"

pytestmark = pytest.mark.skipif(not LIB.exists(), reason="build/c/libtrl.so not built")


def _run(code, **env_over):
    env = dict(os.environ)
    env.pop("TRL_LIBRARY_PATH", None)
    env.pop("TRL_USE_CTYPES_NATIVE", None)
    env.update(env_over)
    env["PYTHONPATH"] = str(ROOT / "python" / "src")
    r = subprocess.run([sys.executable, "-c", code], env=env, cwd="/",
                       capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    return r.stdout.strip()


def test_finds_cmake_build_dir_library():
    out = _run("from trlog import _native as n; print(n._LIB is not None and n._LIB._name)")
    assert out == str(LIB)


def test_ctypes_native_keeps_full_reader(tmp_path):
    p = tmp_path / "n.trl"
    code = f"""
from trlog import TrlWriter, TrlReader, SignalEncoding, ScopeType
from trlog._reader import TrlReader as PyReader
from trlog._native import _CtypesWriter
assert TrlWriter is _CtypesWriter
assert TrlReader is PyReader
with TrlWriter({str(p)!r}) as w:
    st = w.add_signal_type(SignalEncoding.SE_2STATE, 4)
    with w.begin_hierarchy() as h:
        h.begin_scope(ScopeType.ST_MODULE, "top")
        v = h.add_var("d", st)
        h.end_scope()
    with w.begin_vc_block(0) as vc:
        vc.add_change(v, 3, 5)
with TrlReader({str(p)!r}) as r:
    print(list(r.read_signal(v)))
"""
    out = _run(code, TRL_USE_CTYPES_NATIVE="1")
    assert "5" in out, out
