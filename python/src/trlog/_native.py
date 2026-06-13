"""ctypes-native binding with safe fallback to pure Python."""

from __future__ import annotations

import ctypes
import os
from pathlib import Path
from typing import Iterable, Optional

from ._types import FieldType, HierKind, Radix, ScopeType, SignalEncoding, TxnAttr, VarDir
from ._writer import TrlWriter as _PyWriter
from ._reader import TrlReader as _PyReader


class _FieldDef(ctypes.Structure):
    _fields_ = [
        ("name_str_id", ctypes.c_uint32),
        ("field_type", ctypes.c_int),
    ]


class _AttrValue(ctypes.Union):
    _fields_ = [
        ("u64", ctypes.c_uint64),
        ("i64", ctypes.c_int64),
        ("f64", ctypes.c_double),
        ("str", ctypes.c_char_p),
    ]


class _Attr(ctypes.Structure):
    _fields_ = [
        ("field_idx", ctypes.c_uint32),
        ("field_type", ctypes.c_int),
        ("value", _AttrValue),
    ]


def _load_library() -> Optional[ctypes.CDLL]:
    candidates = []
    env_path = os.environ.get("TRL_LIBRARY_PATH")
    if env_path:
        candidates.append(Path(env_path))
    repo_root = Path(__file__).resolve().parents[3]
    candidates.extend(
        [
            repo_root / "build" / "libtrl.so",
            repo_root / "c" / "build" / "libtrl.so",
            Path("libtrl.so"),
        ]
    )
    for path in candidates:
        try:
            if path.exists():
                return ctypes.CDLL(str(path))
        except OSError:
            continue
    return None


_LIB = _load_library()
_USE_CTYPES = _LIB is not None and os.environ.get("TRL_USE_CTYPES_NATIVE", "0") == "1"

if _LIB is not None:
    _LIB.trl_writer_open.argtypes = [ctypes.c_char_p, ctypes.c_int, ctypes.c_int]
    _LIB.trl_writer_open.restype = ctypes.c_void_p
    _LIB.trl_intern.argtypes = [ctypes.c_void_p, ctypes.c_char_p]
    _LIB.trl_intern.restype = ctypes.c_uint32
    _LIB.trl_add_signal_type.argtypes = [ctypes.c_void_p, ctypes.c_uint8, ctypes.c_uint32, ctypes.c_uint8]
    _LIB.trl_add_signal_type.restype = ctypes.c_uint32
    _LIB.trl_add_txn_schema.argtypes = [ctypes.c_void_p, ctypes.c_uint32, ctypes.c_uint32, ctypes.POINTER(_FieldDef)]
    _LIB.trl_add_txn_schema.restype = ctypes.c_uint32
    _LIB.trl_begin_hierarchy.argtypes = [ctypes.c_void_p, ctypes.c_uint32, ctypes.c_uint8, ctypes.c_uint32]
    _LIB.trl_begin_hierarchy.restype = ctypes.c_int
    _LIB.trl_hier_begin_scope.argtypes = [ctypes.c_void_p, ctypes.c_uint8, ctypes.c_uint32, ctypes.c_uint32, ctypes.c_uint32, ctypes.c_uint32]
    _LIB.trl_hier_begin_scope.restype = ctypes.c_int
    _LIB.trl_hier_end_scope.argtypes = [ctypes.c_void_p]
    _LIB.trl_hier_end_scope.restype = ctypes.c_int
    _LIB.trl_hier_add_var.argtypes = [ctypes.c_void_p, ctypes.c_uint32, ctypes.c_uint32, ctypes.c_uint8, ctypes.c_uint32, ctypes.c_uint32]
    _LIB.trl_hier_add_var.restype = ctypes.c_uint32
    _LIB.trl_flush_hierarchy.argtypes = [ctypes.c_void_p]
    _LIB.trl_flush_hierarchy.restype = ctypes.c_int
    _LIB.trl_vc_begin.argtypes = [ctypes.c_void_p, ctypes.c_uint64]
    _LIB.trl_vc_begin.restype = ctypes.c_int
    _LIB.trl_vc_change_u64.argtypes = [ctypes.c_void_p, ctypes.c_uint32, ctypes.c_uint64, ctypes.c_uint64]
    _LIB.trl_vc_change_u64.restype = ctypes.c_int
    _LIB.trl_vc_change_bytes.argtypes = [ctypes.c_void_p, ctypes.c_uint32, ctypes.c_uint64, ctypes.c_void_p, ctypes.c_uint32]
    _LIB.trl_vc_change_bytes.restype = ctypes.c_int
    _LIB.trl_vc_change_str.argtypes = [ctypes.c_void_p, ctypes.c_uint32, ctypes.c_uint64, ctypes.c_char_p]
    _LIB.trl_vc_change_str.restype = ctypes.c_int
    _LIB.trl_vc_change_real.argtypes = [ctypes.c_void_p, ctypes.c_uint32, ctypes.c_uint64, ctypes.c_double]
    _LIB.trl_vc_change_real.restype = ctypes.c_int
    _LIB.trl_vc_flush.argtypes = [ctypes.c_void_p]
    _LIB.trl_vc_flush.restype = ctypes.c_int
    _LIB.trl_txn_begin_block.argtypes = [ctypes.c_void_p, ctypes.c_uint64]
    _LIB.trl_txn_begin_block.restype = ctypes.c_int
    _LIB.trl_txn_full.argtypes = [ctypes.c_void_p, ctypes.c_uint32, ctypes.c_uint32, ctypes.c_uint64, ctypes.c_uint64, ctypes.c_uint64, ctypes.c_uint64, ctypes.c_uint32, ctypes.POINTER(_Attr)]
    _LIB.trl_txn_full.restype = ctypes.c_int
    _LIB.trl_txn_end_block.argtypes = [ctypes.c_void_p]
    _LIB.trl_txn_end_block.restype = ctypes.c_int
    _LIB.trl_writer_close.argtypes = [ctypes.c_void_p]
    _LIB.trl_writer_close.restype = ctypes.c_int
    _LIB.trl_reader_open.argtypes = [ctypes.c_char_p]
    _LIB.trl_reader_open.restype = ctypes.c_void_p
    _LIB.trl_reader_close.argtypes = [ctypes.c_void_p]
    _LIB.trl_reader_close.restype = ctypes.c_int


class _HierarchyContext:
    def __init__(self, writer: "_CtypesWriter", hier_id: int, kind: int, name: str):
        self._writer = writer
        self._flushed = False
        name_id = writer.intern(name)
        writer._check(_LIB.trl_begin_hierarchy(writer._handle, hier_id, int(kind), name_id))

    def begin_scope(self, scope_type: ScopeType, name: str, component: str = "", src_file: str = "", src_line: int = 0) -> None:
        self._writer._check(
            _LIB.trl_hier_begin_scope(
                self._writer._handle,
                int(scope_type),
                self._writer.intern(name),
                # The pure-Python reference interns the component string
                # unconditionally (even when empty → a real id), unlike
                # src_file. Mirror that so the string-table ids — and every
                # block that references them — match byte-for-byte.
                self._writer.intern(component),
                self._writer.intern(src_file) if src_file else 0,
                src_line,
            )
        )

    def end_scope(self) -> None:
        self._writer._check(_LIB.trl_hier_end_scope(self._writer._handle))

    def add_var(self, name: str, sig_type_id: int, direction: VarDir = VarDir.VD_IN, src_file: str = "", src_line: int = 0,
                driver_file: str = "", driver_line: int = 0) -> int:
        var_id = int(
            _LIB.trl_hier_add_var(
                self._writer._handle,
                self._writer.intern(name),
                sig_type_id,
                int(direction),
                self._writer.intern(src_file) if src_file else 0,
                src_line,
            )
        )
        enc = self._writer._sig_types.get(sig_type_id)
        if enc is not None:
            self._writer._var_enc[var_id] = enc
        if driver_file:
            self.add_attr("trlog.driver.file", driver_file)
            self.add_attr("trlog.driver.line", str(driver_line))
        return var_id

    def flush(self) -> None:
        if not self._flushed:
            self._writer._check(_LIB.trl_flush_hierarchy(self._writer._handle))
            self._flushed = True

    def __enter__(self):
        return self

    def __exit__(self, *args) -> None:
        self.flush()


class _VcContext:
    def __init__(self, writer: "_CtypesWriter", start_time: int):
        self._writer = writer
        writer._check(_LIB.trl_vc_begin(writer._handle, start_time))

    def add_change(self, var_id: int, time: int, value) -> None:
        # 4-state signals carry symbolic values ("X"/"Z"); the pure reference
        # stores the wire code (0..3). For 1-bit vars that code goes through the
        # u64 path, so map it here to match the reference byte-for-byte.
        enc = self._writer._var_enc.get(var_id)
        if enc is not None and enc[0] == int(SignalEncoding.SE_4STATE) and enc[1] == 1:
            from ._vc_data import _4state_code
            self._writer._check(_LIB.trl_vc_change_u64(
                self._writer._handle, var_id, time, _4state_code(value)))
            return
        if enc is not None and enc[0] == int(SignalEncoding.SE_9STATE):
            # 9-state encodes binary integers raw (is_binary=1) and symbolic
            # values ("X"/"Z"/…) as 4-bit nibble codes (is_binary=0) — see
            # _vc_data._pack_9state. Binary ints take the u64 path (the C encoder
            # marks any non-BYTES change is_binary). Symbols go through BYTES
            # carrying the packed nibbles; the C encoder treats a BYTES change as
            # non-binary only when its length exceeds the raw width, so pad the
            # packed bytes past that width for narrow signals (e.g. 1-bit).
            bw = enc[1]
            if isinstance(value, int) and value >= 0:
                self._writer._check(_LIB.trl_vc_change_u64(
                    self._writer._handle, var_id, time, value))
            else:
                from ._vc_data import _pack_9state
                packed = _pack_9state(value, bw)
                binary_bytes = max(1, (bw + 7) // 8)
                if len(packed) <= binary_bytes:
                    packed = b"\x00" * (binary_bytes + 1 - len(packed)) + packed
                self._writer._check(_LIB.trl_vc_change_bytes(
                    self._writer._handle, var_id, time, packed, len(packed)))
            return
        if isinstance(value, float):
            self._writer._check(_LIB.trl_vc_change_real(self._writer._handle, var_id, time, value))
        elif isinstance(value, str):
            self._writer._check(_LIB.trl_vc_change_str(self._writer._handle, var_id, time, value.encode("utf-8")))
        elif isinstance(value, (bytes, bytearray)):
            raw = bytes(value)
            self._writer._check(_LIB.trl_vc_change_bytes(self._writer._handle, var_id, time, raw, len(raw)))
        else:
            self._writer._check(_LIB.trl_vc_change_u64(self._writer._handle, var_id, time, int(value)))

    def set_initial(self, var_id: int, value) -> None:
        self.add_change(var_id, self._writer._last_vc_start, value)

    def flush(self) -> None:
        self._writer._check(_LIB.trl_vc_flush(self._writer._handle))

    def __enter__(self):
        return self

    def __exit__(self, *args) -> None:
        self.flush()


class _TxnContext:
    def __init__(self, writer: "_CtypesWriter", start_time: int):
        self._writer = writer
        writer._check(_LIB.trl_txn_begin_block(writer._handle, start_time))

    def write_full(self, stream_inst_id, txn_type_id, txn_id, start, end, parent=0, attrs=None):
        attrs = list(attrs or [])
        arr = (_Attr * len(attrs))()
        backing = []
        schema = self._writer._txn_schemas.get(txn_type_id, [])
        for i, attr in enumerate(attrs):
            arr[i].field_idx = int(attr.field_idx)
            # Encode the value with the schema's field type (matching the
            # reference), falling back to FT_U64 when no schema covers the index.
            if attr.field_idx < len(schema):
                ft = schema[attr.field_idx]
            else:
                ft = int(getattr(attr, "_field_type", None) or getattr(attr, "field_type", None) or self._infer_field_type(attr.value))
            arr[i].field_type = ft
            if ft == int(FieldType.FT_STRING):
                raw = str(attr.value).encode("utf-8")
                backing.append(raw)
                arr[i].value.str = raw
            elif ft in (int(FieldType.FT_I8), int(FieldType.FT_I16), int(FieldType.FT_I32), int(FieldType.FT_I64)):
                arr[i].value.i64 = int(attr.value)
            elif ft in (int(FieldType.FT_F32), int(FieldType.FT_F64)):
                arr[i].value.f64 = float(attr.value)
            else:
                arr[i].value.u64 = int(attr.value)
        self._writer._check(_LIB.trl_txn_full(self._writer._handle, stream_inst_id, txn_type_id, txn_id, start, end, parent, len(attrs), arr if attrs else None))

    @staticmethod
    def _infer_field_type(value) -> FieldType:
        if isinstance(value, str):
            return FieldType.FT_STRING
        if isinstance(value, float):
            return FieldType.FT_F64
        return FieldType.FT_U64

    def flush(self) -> None:
        self._writer._check(_LIB.trl_txn_end_block(self._writer._handle))

    def __enter__(self):
        return self

    def __exit__(self, *args) -> None:
        self.flush()


class _CtypesWriter:
    def __init__(self, path_or_file, timescale_exp: int = -9, timezero: int = 0, compress: bool = True, compressor: str = "zlib") -> None:
        if not isinstance(path_or_file, (str, bytes, os.PathLike)):
            raise TypeError("ctypes TrlWriter requires a filesystem path")
        if timezero != 0:
            raise ValueError("ctypes TrlWriter currently supports timezero=0 only")
        if compressor != "zlib":
            raise ValueError("ctypes TrlWriter currently supports only zlib compression")
        self._path = os.fspath(path_or_file)
        self._handle = _LIB.trl_writer_open(os.fsencode(self._path), timescale_exp, 1 if compress else 0)
        if not self._handle:
            raise OSError(f"Failed to open native writer for {self._path}")
        self._closed = False
        self._last_vc_start = 0
        # Track signal-type encoding/width so the VC writer can reproduce the
        # pure reference's symbolic-value mapping (e.g. 4-state "X"/"Z").
        self._sig_types: dict[int, tuple[int, int]] = {}   # sig_type_id -> (encoding, bit_width)
        self._var_enc: dict[int, tuple[int, int]] = {}     # var_id -> (encoding, bit_width)
        self._txn_schemas: dict[int, list[int]] = {}       # txn_type_id -> [field_type...]

    @staticmethod
    def _check(status: int) -> None:
        if status != 0:
            raise RuntimeError(f"libtrl error {status}")

    def intern(self, s: str) -> int:
        return int(_LIB.trl_intern(self._handle, s.encode("utf-8")))

    def add_signal_type(self, encoding, bit_width: int, radix: Radix = Radix.RX_HEX) -> int:
        sid = int(_LIB.trl_add_signal_type(self._handle, int(encoding), bit_width, int(radix)))
        self._sig_types[sid] = (int(encoding), int(bit_width))
        return sid

    def add_txn_schema(self, name, fields: Iterable) -> int:
        name_id = name if isinstance(name, int) else self.intern(str(name))
        fields = list(fields)
        arr = (_FieldDef * len(fields))()
        for i, field in enumerate(fields):
            arr[i].name_str_id = int(getattr(field, "name_str_id", 0))
            arr[i].field_type = int(field.field_type)
        tid = int(_LIB.trl_add_txn_schema(self._handle, name_id, len(fields), arr if fields else None))
        # Remember the per-field types so write_full can encode attr values with
        # the schema's type (matching the reference), not an inferred one.
        self._txn_schemas[tid] = [int(f.field_type) for f in fields]
        return tid

    def begin_hierarchy(self, hier_id: int = 1, kind: HierKind = HierKind.HK_DESIGN, name: str = "design"):
        return _HierarchyContext(self, hier_id, int(kind), name)

    def begin_vc_block(self, start_time: int):
        self._last_vc_start = start_time
        return _VcContext(self, start_time)

    def begin_txn_block(self, start_time: int):
        return _TxnContext(self, start_time)

    def close(self) -> None:
        if not self._closed:
            self._check(_LIB.trl_writer_close(self._handle))
            self._closed = True

    def __enter__(self):
        return self

    def __exit__(self, *args) -> None:
        self.close()


class _CtypesReader:
    def __init__(self, path_or_file) -> None:
        if not isinstance(path_or_file, (str, bytes, os.PathLike)):
            raise TypeError("ctypes TrlReader requires a filesystem path")
        self._path = os.fspath(path_or_file)
        self._handle = _LIB.trl_reader_open(os.fsencode(self._path))
        if not self._handle:
            raise OSError(f"Failed to open native reader for {self._path}")
        self._closed = False

    def close(self) -> None:
        if not self._closed:
            _LIB.trl_reader_close(self._handle)
            self._closed = True

    def __enter__(self):
        return self

    def __exit__(self, *args) -> None:
        self.close()


TrlWriter = _CtypesWriter if _USE_CTYPES else _PyWriter
TrlReader = _CtypesReader if _USE_CTYPES else _PyReader

__all__ = ["TrlWriter", "TrlReader"]
