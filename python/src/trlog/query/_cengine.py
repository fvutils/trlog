"""Optional C scan engine for ``trlog.query`` (query CLI design §7.1, Q4).

The pure-Python engine in ``_engine.py`` is the reference and always
works. This module drives ``libtrl``'s ``trl_qscan_*`` API when the
library can be loaded, and must give exactly the same answers:

- Python passes everything that is a semantic choice (block plan, field
  types, field-name slots, value normalisation, stream/type sets), so C
  only decodes and joins.
- The filter compiles to a bytecode whose result is keep, drop or *defer*.
  Python evaluates the full filter on deferred rows, so any expression
  works, and C is fast on the common integer/enum cases.
- When C meets anything it does not decode exactly as Python would, the
  run returns FALLBACK and the caller reruns on the Python engine.

Nothing here is required: :func:`load` returns None when the library or
its ``trl_qscan_*`` symbols are missing, and the caller uses Python.
"""

from __future__ import annotations

import ctypes
import os
import re
import struct
import sys
from array import array
from pathlib import Path
from typing import Callable, Dict, List, Optional, Sequence, Tuple

from .. import _libpath
from ._expr import BUILTINS, QueryError, cmp_values, match_pred
from ._values import MISSING, EnumVal, LogicVal, normalize_field

# ---------------------------------------------------------------------------
# Library loading
# ---------------------------------------------------------------------------

_lib = None
_lib_error: Optional[str] = None
_lib_tried = False


def _candidates() -> List[Path]:
    return _libpath.candidates("trl")


def load():
    """The ctypes library with the qscan API, or None (reason in ``load_error()``)."""
    global _lib, _lib_error, _lib_tried
    if _lib_tried:
        return _lib
    _lib_tried = True
    errors = []
    for p in _candidates():
        try:
            if not p.exists():
                continue
            lib = _libpath.open_cdll(p)
            if not hasattr(lib, "trl_qscan_abi_version"):
                errors.append(f"{p}: no trl_qscan API")
                continue
            abi = lib.trl_qscan_abi_version()
            if abi != QSCAN_ABI:
                errors.append(f"{p}: trl_qscan ABI {abi}, need {QSCAN_ABI}")
                continue
            _bind(lib)
            _lib = lib
            return lib
        except (OSError, AttributeError) as e:
            errors.append(f"{p}: {e}")
    _lib_error = "; ".join(errors) or "libtrl not found"
    return None


def load_error() -> Optional[str]:
    return _lib_error


def _bind(lib) -> None:
    vp, c64, u64, u32, i32, sz = (ctypes.c_void_p, ctypes.c_int64, ctypes.c_uint64,
                                  ctypes.c_uint32, ctypes.c_int32, ctypes.c_size_t)
    P = ctypes.POINTER
    sig = {
        "trl_qscan_new": ([ctypes.c_char_p], vp),
        "trl_qscan_free": ([vp], None),
        "trl_qscan_set_schema": ([vp, u32, u32, P(ctypes.c_uint8)], ctypes.c_int),
        "trl_qscan_reset": ([vp], None),
        "trl_qscan_set_catalog": ([vp, u32, u32, P(i32), P(ctypes.c_uint8)], ctypes.c_int),
        "trl_qscan_set_plan": ([vp, sz, P(c64), P(u64), P(u32)], ctypes.c_int),
        "trl_qscan_set_streams": ([vp, sz, P(u32)], ctypes.c_int),
        "trl_qscan_set_types": ([vp, sz, P(u32)], ctypes.c_int),
        "trl_qscan_set_program": ([vp, sz, P(c64), sz, P(c64), ctypes.c_int], ctypes.c_int),
        "trl_qscan_set_options": ([vp, u32, c64, c64, c64, c64], None),
        "trl_qscan_run": ([vp], ctypes.c_int),
        "trl_qscan_fallback_reason": ([vp], ctypes.c_char_p),
        "trl_qscan_rows": ([vp, P(vp)], sz),
        "trl_qscan_attrs": ([vp, P(vp)], sz),
        "trl_qscan_metas": ([vp, P(vp)], sz),
        "trl_qscan_links": ([vp, P(vp)], sz),
        "trl_qscan_heap": ([vp, P(vp)], sz),
        "trl_qscan_counts": ([vp, P(c64)], None),
        "trl_qscan_trim": ([vp], None),
        "trl_qscan_set_entries": ([vp, c64, sz, P(c64)], ctypes.c_int),
        "trl_qscan_proj": ([vp, P(_Proj)], sz),
    }
    for name, (args, res) in sig.items():
        fn = getattr(lib, name)
        fn.argtypes = args
        fn.restype = res


# Must match trl_qscan.h.
QSCAN_ABI = 2
WANT_LINKS, WANT_META, HAS_T_HI, FILTER, HAS_LO, HAS_HI, HAS_MAX, PROJECT = (
    0x01, 0x02, 0x04, 0x08, 0x10, 0x20, 0x40, 0x80)
V_MISS, V_INT, V_ENUM, V_BOOL = 0, 1, 2, 5


class _Proj(ctypes.Structure):
    _fields_ = [(n, ctypes.c_void_p) for n in
                ("id", "start", "end", "parent", "stream", "type", "open",
                 "vtag", "val", "aux")]
QS_OK, QS_FALLBACK = 0, 100
ROW_OPEN, ROW_DEFER = 0x01, 0x02
NORM = {"bitvec": 1, "logicvec": 2, "enum": 3}
ROW = struct.Struct("<qqqqIIIIIIIIII")
ATTR = struct.Struct("<IIQIIII")
META = struct.Struct("<II")
LINK = struct.Struct("<qIBBH")

I64_MIN, I64_MAX = -(1 << 63), (1 << 63) - 1


def _is_i64(v) -> bool:
    return type(v) is int and I64_MIN <= v <= I64_MAX


class Fallback(Exception):
    """C cannot run this query exactly; use the Python engine."""


# ---------------------------------------------------------------------------
# Bytecode (mirrors c/src/qscan.c)
# ---------------------------------------------------------------------------

OP_END, OP_INT, OP_MISS, OP_DEFER, OP_LOAD, OP_HAS, OP_BUILTIN, OP_UN, OP_BIN, \
    OP_CMP, OP_AND, OP_OR, OP_LABEL, OP_IDTEST, OP_MINMAX, OP_BIT, OP_BITS, \
    OP_LOADT, OP_HAST = range(19)
_BUILTIN = {"id": 0, "start": 1, "end": 2, "dur": 3, "parent": 4, "open": 5, "nlinks": 6}
_UN = {"-": 0, "+": 1, "~": 2, "abs": 3, "not": 4, "notmiss": 5}
_BIN = {"+": 0, "-": 1, "*": 2, "/": 3, "%": 4, "&": 5, "|": 6, "^": 7, "<<": 8, ">>": 9}
_CMP = {"==": 0, "!=": 1, "<": 2, "<=": 3, ">": 4, ">=": 5}
_FNAME = re.compile(r"f(0|[1-9][0-9]*)\Z")
_PROBE_LXZ = LogicVal(4, 0, 1)


class Program:
    """A filter compiled for the C engine."""

    def __init__(self) -> None:
        self.code: List[int] = []
        self.sets: List[int] = []
        self.slots: Dict[str, int] = {}
        self.strict = False
        self.unsafe = False      # Python may raise where C cannot tell: defer all
        self.filter_entry = -1   # code offset of the filter, -1 = none
        self.val_entries: List[int] = []   # code offsets of projected values

    def slot(self, name: str) -> int:
        if _FNAME.match(name):
            self.strict = True       # could be an unmapped attr's f<idx> name
        return self.slots.setdefault(name, len(self.slots))


class _Compiler:
    """AST (``_expr._Parser``) to bytecode. Only runs on a filter that the
    Python compiler accepted, so names and static checks are already done.
    Anything it does not translate exactly becomes OP_DEFER."""

    def __init__(self, trace) -> None:
        self.tr = trace
        self.p = Program()

    def emit(self, *words) -> None:
        self.p.code.extend(words)

    def defer(self) -> None:
        self.emit(OP_DEFER)

    def emit_bool(self, b) -> None:
        """A Python bool (not an int): `not not` of the int."""
        self.emit(OP_INT, int(bool(b)), OP_UN, _UN["not"], OP_UN, _UN["not"])

    # -- sets ---------------------------------------------------------------

    def _label_set(self, pred: Callable) -> Optional[int]:
        """Set for OP_LABEL: pred's result for each value class. None when
        pred on a plain int depends on the int (then C cannot decide)."""
        r_int = {bool(pred(v)) for v in (0, 1, -1, 7)}
        if len(r_int) != 1:
            return None
        r_int = r_int.pop()
        r_unknown = bool(pred(EnumVal(0, None)))
        entries = []
        for ti in self.tr.types.values():
            for idx, fi in enumerate(ti.fields):
                if fi.kind != "enum" or not fi.enum_labels:
                    continue
                for raw, label in fi.enum_labels.items():
                    if not _is_i64(raw):
                        continue
                    res = bool(pred(EnumVal(raw, label, fi.enum_labels)))
                    if res != r_unknown:
                        entries += [(ti.id << 32) | idx, raw, int(res)]
        off = len(self.p.sets)
        self.p.sets += [len(entries) // 3, int(r_int), int(bool(pred(_PROBE_LXZ))),
                        int(bool(pred(MISSING))), int(r_unknown)] + entries
        return off

    def _id_set(self, which: str, pred: Callable) -> int:
        off = len(self.p.sets)
        if which == "stream":
            items = [(s.id, s.path) for s in self.tr.streams.values()]
        else:
            items = [(t.id, t.name) for t in self.tr.types.values()]
        self.p.sets.append(len(items))
        for i, name in items:
            self.p.sets += [i, int(bool(pred(name)))]
        return off

    def _type_ids(self, name: str) -> int:
        off = len(self.p.sets)
        ids = [t.id for t in self.tr.types.values() if t.name == name]
        self.p.sets.append(len(ids))
        for i in ids:
            self.p.sets += [i, 1]
        return off

    # -- nodes --------------------------------------------------------------

    def compile(self, node) -> None:
        getattr(self, "_" + node[0])(node)

    def _lit(self, node) -> None:
        _, v, kind, _pos = node
        if kind == "bool":
            self.emit_bool(v)
        elif kind in ("int", "time") and _is_i64(v):
            self.emit(OP_INT, v)
        else:
            self.defer()             # floats, strings, regexes, big ints

    def _name(self, node) -> None:
        name = node[1]
        if name in BUILTINS:
            if name in _BUILTIN:
                self.emit(OP_BUILTIN, _BUILTIN[name])
            else:
                self.defer()         # stream, type (strings), nchildren
            return
        self.emit(OP_LOAD, self.p.slot(name))

    def _attr(self, node) -> None:
        _, obj, name, _pos = node
        if obj[0] != "name" or obj[1] == "parent" or obj[1] not in self.tr._types_by_name:
            self.defer()
            return
        self.emit(OP_LOADT, self.p.slot(name), self._type_ids(obj[1]))

    def _index(self, node) -> None:
        self.defer()                 # meta[...]

    def _call(self, node) -> None:
        _, fname, args, _pos = node
        if fname == "has":
            a = args[0]
            if a[0] == "name" and a[1] in BUILTINS:
                if a[1] in ("stream", "type"):
                    self.emit_bool(True)
                elif a[1] in _BUILTIN:
                    self.emit(OP_BUILTIN, _BUILTIN[a[1]], OP_UN, _UN["notmiss"])
                else:
                    self.defer()
            elif a[0] == "name":
                self.emit(OP_HAS, self.p.slot(a[1]))
            elif (a[0] == "attr" and a[1][0] == "name" and a[1][1] != "parent"
                  and a[1][1] in self.tr._types_by_name):
                self.emit(OP_HAST, self.p.slot(a[2]), self._type_ids(a[1][1]))
            else:
                self.defer()
            return
        if fname in ("abs", "min", "max", "bit", "bits"):
            for a in args:
                self.compile(a)
            if fname == "abs":
                self.emit(OP_UN, _UN["abs"])
            elif fname in ("min", "max"):
                self.emit(OP_MINMAX, int(fname == "max"), len(args))
            elif fname == "bit":
                self.emit(OP_BIT)
            else:
                self.emit(OP_BITS)
            return
        self.defer()                 # len()

    def _un(self, node) -> None:
        self.compile(node[2])
        self.emit(OP_UN, _UN[node[1]])

    def _bin(self, node) -> None:
        self.compile(node[2])
        self.compile(node[3])
        self.emit(OP_BIN, _BIN[node[1]])

    def _not(self, node) -> None:
        self.compile(node[1])
        self.emit(OP_UN, _UN["not"])

    def _and(self, node) -> None:
        self.compile(node[1])
        self.compile(node[2])
        self.emit(OP_AND)

    def _or(self, node) -> None:
        self.compile(node[1])
        self.compile(node[2])
        self.emit(OP_OR)

    @staticmethod
    def _is_plain_lit(node) -> bool:
        """A literal C holds as an int (or a bool)."""
        return node[0] == "lit" and (node[2] == "bool" or
                                     (node[2] in ("int", "time") and _is_i64(node[1])))

    @staticmethod
    def _const_lit(node) -> bool:
        """A literal whose comparison with any int64 has one answer: a
        string, a regex, or an int outside int64 (not a float)."""
        return node[0] == "lit" and (node[2] in ("str", "regex") or (
            node[2] in ("int", "time") and type(node[1]) is int))

    @staticmethod
    def _lit_value(node):
        """The literal's value as the Python closure sees it (_c_lit)."""
        if node[2] == "regex":
            return re.compile(node[1])
        return node[1]

    def _value_test(self, x, pred: Callable) -> None:
        """Push pred(value of x), for x compared with a constant literal."""
        if x[0] == "name" and x[1] in ("stream", "type"):
            self.emit(OP_IDTEST, 0 if x[1] == "stream" else 1, self._id_set(x[1], pred))
            return
        if x[0] == "lit":
            self.emit_bool(pred(self._lit_value(x)))
            return
        off = self._label_set(pred)
        if off is None:
            self.defer()
            return
        self.compile(x)
        self.emit(OP_LABEL, off)

    def _compare(self, op: str, a, b) -> None:
        if op in ("~", "=~"):
            if b[0] != "lit" or b[2] not in ("str", "regex"):
                # `x =~ 3`: Python raises on a string x; leave it all to Python.
                self.p.unsafe = True
                self.defer()
                return
            self._value_test(a, match_pred(op, b[1]))
            return
        if self._const_lit(a) and not self._is_plain_lit(a):
            lit = self._lit_value(a)
            self._value_test(b, lambda v: cmp_values(op, lit, v))
            return
        if self._const_lit(b) and not self._is_plain_lit(b):
            lit = self._lit_value(b)
            self._value_test(a, lambda v: cmp_values(op, v, lit))
            return
        for x in (a, b):
            if x[0] == "name" and x[1] in ("stream", "type"):
                self.defer()         # string builtin against a non-literal
                return
        self.compile(a)
        self.compile(b)
        self.emit(OP_CMP, _CMP[op])

    def _cmp(self, node) -> None:
        _, op, a, b, _pos = node
        self._compare(op, a, b)

    def _in(self, node) -> None:
        _, x, items, negate, _pos = node
        for it in items:
            if it[0] == "range":
                self._compare(">=", x, it[1])
                self._compare("<=", x, it[2])
                self.emit(OP_AND)
            else:
                self._compare("==", x, it)
        for _ in range(len(items) - 1):
            self.emit(OP_OR)
        if negate:
            self.emit(OP_UN, _UN["not"])
        if not (x[0] == "name" and x[1] in ("stream", "type")):
            # `v is MISSING -> False` comes first in the Python closure.
            self.compile(x)
            self.emit(OP_UN, _UN["notmiss"])
            self.emit(OP_AND)


def compile_program(trace, flt, values: Sequence = ()) -> Program:
    """The filter (or None) and the value expressions to project, in one
    code array. ``values`` are ValueExprs (with ``.ast``) or FieldKeys."""
    from ._rows import FieldKey
    c = _Compiler(trace)
    p = c.p

    def part(build) -> int:
        start = len(p.code)
        p.unsafe = False
        build()
        c.emit(OP_END)
        if p.unsafe:                 # Python may raise here: always defer
            del p.code[start:]
            c.emit(OP_DEFER, OP_END)
        return start

    if flt is not None:
        p.filter_entry = part(lambda: c.compile(flt.ast))
    for v in values:
        if isinstance(v, FieldKey):
            p.val_entries.append(part(lambda v=v: c.emit(OP_LOAD, p.slot(v.name))))
        else:
            p.val_entries.append(part(lambda v=v: c.compile(v.ast)))
    return p


# ---------------------------------------------------------------------------
# Running a scan
# ---------------------------------------------------------------------------

def _carray(ctype, values):
    arr = (ctype * max(1, len(values)))()
    arr[:len(values)] = values
    return arr


class CScan:
    """One ``trl_qscan`` handle per open Trace."""

    def __init__(self, lib, trace) -> None:
        self.lib = lib
        self.tr = trace
        self.h = lib.trl_qscan_new(trace.path.encode())
        if not self.h:
            raise OSError(f"trl_qscan_new failed for {trace.path}")
        for tid, fts in trace._r._build_txn_schemas().items():
            vals = [int(f) for f in fts]
            lib.trl_qscan_set_schema(self.h, tid, len(vals), _carray(ctypes.c_uint8, vals))
        self.norms = {tid: [NORM.get(f.kind, 0) for f in ti.fields]
                      for tid, ti in trace.types.items()}
        self.last_fallback: Optional[str] = None

    def close(self) -> None:
        if self.h:
            self.lib.trl_qscan_free(self.h)
            self.h = None

    def run(self, plan, stream_ids, type_ids, *, want_links: bool, want_meta: bool,
            t_hi, max_txns=None, window=None, program: Optional[Program] = None,
            project: bool = False):
        """Returns (rows, counts, proj): rows are (Txn, deferred) pairs, proj
        the projection columns (or None).

        ``window`` given means filter mode (``_matches``); otherwise every
        built txn comes back (``scan``). With ``project``, rows C decided in
        full (filter true, every value exact) come back as columns instead
        of Txns. Raises Fallback.
        """
        lib, h = self.lib, self.h
        lib.trl_qscan_reset(h)
        starts = [e[0] for e in plan]
        if not all(_is_i64(s) for s in starts) or (t_hi is not None and not _is_i64(t_hi)):
            raise Fallback("time outside int64")
        n = len(plan)
        lib.trl_qscan_set_plan(h, n, _carray(ctypes.c_int64, starts),
                               _carray(ctypes.c_uint64, [e[2] for e in plan]),
                               _carray(ctypes.c_uint32, [e[3] for e in plan]))
        if stream_ids is not None:
            ids = sorted(stream_ids)
            lib.trl_qscan_set_streams(h, len(ids), _carray(ctypes.c_uint32, ids))
        if type_ids is not None:
            ids = sorted(type_ids)
            lib.trl_qscan_set_types(h, len(ids), _carray(ctypes.c_uint32, ids))
        slots = program.slots if program is not None else {}
        for tid, ti in self.tr.types.items():
            sl = [slots.get(f.name, -1) for f in ti.fields]
            lib.trl_qscan_set_catalog(h, tid, len(sl), _carray(ctypes.c_int32, sl),
                                      _carray(ctypes.c_uint8, self.norms[tid]))
        flags = (WANT_LINKS if want_links else 0) | (WANT_META if want_meta else 0)
        if t_hi is not None:
            flags |= HAS_T_HI
        if max_txns is not None:
            flags |= HAS_MAX
        lo = hi = 0
        if window is not None:
            flags |= FILTER
            lo, hi = window
            if lo is not None:
                if not _is_i64(lo):
                    raise Fallback("window bound is not an int64")
                flags |= HAS_LO
            if hi is not None:
                if not _is_i64(hi):
                    raise Fallback("window bound is not an int64")
                flags |= HAS_HI
        if program is not None:
            lib.trl_qscan_set_program(h, len(program.code),
                                      _carray(ctypes.c_int64, program.code),
                                      len(program.sets),
                                      _carray(ctypes.c_int64, program.sets),
                                      int(program.strict))
            ve = program.val_entries if project else []
            if lib.trl_qscan_set_entries(h, program.filter_entry if window is not None
                                         else -1, len(ve), _carray(ctypes.c_int64, ve)):
                raise Fallback("bad program entries")
        if project:
            flags |= PROJECT
        lib.trl_qscan_set_options(h, flags, t_hi or 0, lo or 0, hi or 0,
                                  max_txns or 0)
        rc = lib.trl_qscan_run(h)
        if rc == QS_FALLBACK:
            reason = (lib.trl_qscan_fallback_reason(h) or b"").decode(errors="replace")
            self.last_fallback = reason
            raise Fallback(reason)
        if rc != QS_OK:
            if rc == 6:     # TRL_ERR_OOM
                raise MemoryError("C scan engine ran out of memory")
            raise Fallback(f"trl_qscan_run returned {rc}")
        counts = (ctypes.c_int64 * 4)()
        lib.trl_qscan_counts(h, counts)
        try:
            nvals = len(program.val_entries) if (project and program) else 0
            proj = self._unpack_proj(nvals) if project else None
            return self._unpack(), list(counts), proj
        finally:
            lib.trl_qscan_trim(h)       # the Txns are built; give C's memory back

    def _buf(self, fn, rec):
        """The C array in place (valid until trl_qscan_trim/reset)."""
        p = ctypes.c_void_p()
        n = fn(self.h, ctypes.byref(p))
        if not n:
            return b""
        return memoryview((ctypes.c_char * (n * rec)).from_address(p.value)).cast("B")

    def _unpack_proj(self, nvals: int) -> dict:
        """The projection columns as Python lists, values as query values."""
        P = _Proj()
        n = self.lib.trl_qscan_proj(self.h, ctypes.byref(P))

        def col(addr, fmt, count=n):
            if not count:
                return []
            size = struct.calcsize(fmt) * count
            mv = memoryview((ctypes.c_char * size).from_address(addr)).cast("B")
            return mv.cast(fmt).tolist()

        out = {"n": n,
               "ids": col(P.id, "q"), "starts": col(P.start, "q"),
               "stream_ids": col(P.stream, "I"), "type_ids": col(P.type, "I")}
        opn = col(P.open, "B")
        out["ends"] = [None if o else e for e, o in zip(col(P.end, "q"), opn)]
        out["parents"] = [p or None for p in col(P.parent, "q")]
        vals = []
        if nvals:
            tags = col(P.vtag, "B", n * nvals)
            raw = col(P.val, "q", n * nvals)
            aux = col(P.aux, "Q", n * nvals) if V_ENUM in tags else None
            for k in range(nvals):
                tk, vk = tags[k::nvals], raw[k::nvals]
                if tk.count(V_INT) != n:
                    ak = aux[k::nvals] if aux is not None else None
                    vk = [self._value(t, v, ak[i] if ak else 0)
                          for i, (t, v) in enumerate(zip(tk, vk))]
                vals.append(vk)
        out["vals"] = vals
        return out

    def _value(self, tag, v, aux):
        if tag == V_INT:
            return v
        if tag == V_BOOL:
            return bool(v)
        if tag == V_MISS:
            return MISSING
        ti = self.tr.types.get(aux >> 32)
        idx = aux & 0xFFFFFFFF
        fi = ti.fields[idx] if ti is not None and idx < len(ti.fields) else None
        labels = fi.enum_labels if fi is not None else None
        return EnumVal(v, labels.get(v) if labels else None, labels)

    def _unpack(self) -> List[tuple]:
        """[(Txn, deferred)] in the engine's dict order."""
        from ._engine import LINK_KIND_NAMES, Link, Txn
        lib, tr = self.lib, self.tr
        rows = self._buf(lib.trl_qscan_rows, ROW.size)
        # attrs/metas/links are laid out in row order, so one iterator each
        # walks them in step with the rows (no big intermediate lists).
        attrs = ATTR.iter_unpack(self._buf(lib.trl_qscan_attrs, ATTR.size))
        metas = META.iter_unpack(self._buf(lib.trl_qscan_metas, META.size))
        links = LINK.iter_unpack(self._buf(lib.trl_qscan_links, LINK.size))
        heap = bytes(self._buf(lib.trl_qscan_heap, 1))
        types, sstr = tr.types, tr._str
        spath = {i: s.path for i, s in tr.streams.items()}
        tname = {i: t.name for i, t in types.items()}
        conv = _converters(types)
        ints = _FT_INTS
        out = []
        append = out.append
        for (tid_, start, end, parent, sid, tyid, af, an, mf, mn, lf, ln,
             flags, _pad) in ROW.iter_unpack(rows):
            path = spath.get(sid)
            if path is None:
                path = f"<stream#{sid}>"
            name = tname.get(tyid)
            if name is None:
                name = f"<type#{tyid}>"
            t = Txn(tid_, sid, path, tyid, name, start,
                    None if flags & ROW_OPEN else end, parent or None)
            if an:
                fields = t.fields
                cv = conv.get(tyid, ())
                ncv = len(cv)
                for _ in range(an):
                    idx, ft, v, off, alen, width, _p = next(attrs)
                    # Unsigned ints and TIME/ENUM are the raw value already.
                    raw = v if ft in ints else _raw(ft, v, heap, off, alen, width)
                    if idx < ncv:
                        fname, norm = cv[idx]
                        fields[fname] = raw if norm is None else norm(raw)
                    else:
                        fields[f"f{idx}"] = raw
            if mn:
                meta = t.meta
                for _ in range(mn):
                    k, v = next(metas)
                    meta[sstr(k)] = sstr(v)
            if ln:
                tl = t.links
                for _ in range(ln):
                    other, label, kind, d, _p = next(links)
                    tl.append(Link(LINK_KIND_NAMES.get(kind, "custom"),
                                   "in" if d else "out", other, sstr(label) or None))
            append((t, bool(flags & ROW_DEFER)))
        return out


def _converters(types) -> Dict[int, list]:
    """Per type: [(field name, normaliser or None)] by field index."""
    out = {}
    for tid, ti in types.items():
        cv = []
        for fi in ti.fields:
            if fi.kind in ("bitvec", "logicvec", "enum"):
                cv.append((fi.name, (lambda raw, k=fi.kind, l=fi.enum_labels:
                                     normalize_field(k, raw, l))))
            else:
                cv.append((fi.name, None))
        out[tid] = cv
    return out


_U64 = 1 << 64
_FT_INTS = frozenset({0, 1, 2, 3, 14, 15})      # U8..U64, TIME, ENUM
_FT_SINTS = frozenset({4, 5, 6, 7})
_D = struct.Struct("<d")
_Q = struct.Struct("<Q")


def _raw(ft, v, heap, off, alen, width):
    """The value _codec.decode_field returns for this attr."""
    if ft in _FT_INTS:
        return v
    if ft in _FT_SINTS:
        return v - _U64 if v >> 63 else v
    if ft == 10:                                   # BOOL
        return bool(v)
    if ft in (8, 9):                               # F32, F64 (C widened F32)
        return _D.unpack(_Q.pack(v))[0]
    data = heap[off:off + alen]
    if ft == 11:                                   # STRING
        return data.decode("utf-8")
    if ft == 13:                                   # BYTES
        return data
    if ft == 12:                                   # BITVEC
        return (width, int.from_bytes(data, "big"))
    h = alen // 2                                  # LOGICVEC
    return (width, int.from_bytes(data[:h], "big"), int.from_bytes(data[h:], "big"))
