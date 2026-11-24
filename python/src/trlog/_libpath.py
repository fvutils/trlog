"""Where trlog's native libraries are found, shared by every ctypes loader.

A wheel ships the libraries next to this file (the trlog package dir); a
source checkout has them in a CMake build dir. TRL_LIBRARY_PATH (a full path
to libtrl) overrides both.
"""

import ctypes
import os
import sys
from pathlib import Path
from typing import List

PKG_DIR = Path(__file__).resolve().parent
_REPO = PKG_DIR.parents[2]


def lib_name(base: str) -> str:
    """Platform file name of native library ``base`` (e.g. "trl" -> libtrl.so)."""
    if sys.platform == "win32":
        return f"{base}.dll"
    if sys.platform == "darwin":
        return f"lib{base}.dylib"
    return f"lib{base}.so"


def candidates(base: str = "trl") -> List[Path]:
    """Paths to try for native library ``base``, in priority order."""
    name = lib_name(base)
    out = []
    env = os.environ.get("TRL_LIBRARY_PATH")
    if env and base == "trl":
        out.append(Path(env))
    out.append(PKG_DIR / name)
    out += [_REPO / "build" / "c" / name, _REPO / "c" / "build" / name, _REPO / "build" / name]
    return out


def open_cdll(path: Path) -> ctypes.CDLL:
    """ctypes.CDLL(path), letting Windows find DLLs that sit beside it."""
    if sys.platform == "win32" and hasattr(os, "add_dll_directory"):
        os.add_dll_directory(str(Path(path).resolve().parent))
    return ctypes.CDLL(str(path))


# Discovery helpers for C, DPI and VPI consumers (simulator command lines).
# In a wheel everything sits in the package dir; in a source checkout the
# libraries are in the CMake build dir and the headers/SV in the tree.

def get_libdirs() -> List[str]:
    """Directories holding the trl, trl_sv and trlog_vpi libraries."""
    if (PKG_DIR / lib_name("trl")).is_file():
        return [str(PKG_DIR)]
    out = []
    for d in (_REPO / "build" / "c", _REPO / "c" / "build", _REPO / "build"):
        if (d / lib_name("trl")).is_file():
            out.append(str(d))
            break
    for d in (_REPO / "build" / "vpi", _REPO / "vpi" / "build"):
        if (d / lib_name("trlog_vpi")).is_file():
            out.append(str(d))
            break
    return out


def get_incdirs() -> List[str]:
    """Include directories: <dir>/trl/trl.h and <dir>/trlog_vpi.h."""
    share = PKG_DIR / "share" / "include"
    if share.is_dir():
        return [str(share)]
    return [str(d) for d in (_REPO / "c" / "include", _REPO / "vpi" / "include")
            if d.is_dir()]


def get_svdirs() -> List[str]:
    """Directories holding the SystemVerilog package (trl_pkg.sv)."""
    for d in (PKG_DIR / "share" / "sv", _REPO / "sv" / "src"):
        if (d / "trl_pkg.sv").is_file():
            return [str(d)]
    return []
