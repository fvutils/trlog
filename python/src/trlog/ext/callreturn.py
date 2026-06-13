"""org.fvutils.trlog.ext.callreturn — call/return (call-stack) codec.

The canonical *external* codec (design §6.2): domain-specific enough to need its
own schema, interning and a non-value-change domain API, yet built **entirely on
the public storage SPI with no core changes** — the proof that the core/codec
boundary holds. It ships in-tree but is optional (not a guaranteed ``core.*``).

What it uses from the public surface, and nothing else:

* **Signatures** — a shared *opaque-metadata stream* via ``writer.write_aux`` /
  ``reader.read_aux``, keyed by a signature stream-type id and shareable across
  many call streams.
* **Dependencies** — the call stream declares the signature stream as an input
  via ``writer.declare_dependencies`` (Phase 2c catalog); the reader resolves it
  with ``reader.dependencies``.
* **Per-call payload** — CTF-style tagged variant in a ``BLK_EXT`` block
  (``writer.write_ext`` / ``reader.iter_ext_blocks``): a ``func_id`` selects the
  argument layout; integer args are zig-zag delta-encoded against the previous
  call of the same function; a bounded LRU window of recent frames lets a
  repeated identical call collapse to a single back-reference varint
  (Perfetto-style interning); enter/exit are stored as a depth-delta.
* **Raw fallback** (``HAS_RAW_FALLBACK``) — a call that doesn't fit its signature
  is spilled to an ordinary transaction (``core.record``) so a reader *without*
  this codec still gets a usable, generic view.
"""

from __future__ import annotations

import enum
import struct
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

from .._codec import encode_uvarint, decode_uvarint, encode_svarint, decode_svarint
from .._types import FieldType, TxnAttr
from ..codec import TxnCodec, txn_codec, Capability, Store


CALLRETURN_CODEC_ID = "org.fvutils.trlog.ext.callreturn"

# 4-byte BLK_EXT type tag for a call-event block, and the aux key under the
# signature stream where the signature table is stored.
_EXT_TYPE = b"TRCR"
_SIG_KEY = 0


class ArgType(enum.IntEnum):
    NONE = 0
    UINT = 1
    INT  = 2
    REAL = 3
    STR  = 4


# Per-event tag bits (the first byte of each encoded event).
_TAG_ENTER   = 0x01   # else exit
_TAG_BACKREF = 0x02   # enter only: a back-reference to a recent frame
_TAG_HASRET  = 0x04   # exit only: a return value follows


@dataclass
class Signature:
    func_id: int
    name: str
    arg_types: List[ArgType]
    ret_type: ArgType = ArgType.NONE


# ---------------------------------------------------------------------------
# Signature table (the shared opaque-metadata stream payload)
# ---------------------------------------------------------------------------

def _encode_signatures(sigs: List[Signature], intern) -> bytes:
    out = bytearray()
    out += encode_uvarint(len(sigs))
    for s in sigs:
        out += encode_uvarint(s.func_id)
        out += encode_uvarint(intern(s.name))
        out += encode_uvarint(len(s.arg_types))
        for at in s.arg_types:
            out.append(int(at))
        out.append(int(s.ret_type))
    return bytes(out)


def _decode_signatures(payload: bytes, lookup) -> Dict[int, Signature]:
    o = 0
    n, o = decode_uvarint(payload, o)
    sigs: Dict[int, Signature] = {}
    for _ in range(n):
        func_id, o = decode_uvarint(payload, o)
        name_id, o = decode_uvarint(payload, o)
        na, o = decode_uvarint(payload, o)
        arg_types = [ArgType(payload[o + i]) for i in range(na)]
        o += na
        ret_type = ArgType(payload[o]); o += 1
        sigs[func_id] = Signature(func_id, lookup(name_id), arg_types, ret_type)
    return sigs


# ---------------------------------------------------------------------------
# Argument value <-> bytes (per-function delta predictors for integers)
# ---------------------------------------------------------------------------

def _encode_args(out: bytearray, sig: Signature, args, prev, intern) -> None:
    for i, at in enumerate(sig.arg_types):
        v = args[i]
        if at in (ArgType.UINT, ArgType.INT):
            out += encode_svarint(int(v) - int(prev[i]))
            prev[i] = int(v)
        elif at == ArgType.REAL:
            out += struct.pack("<d", float(v))
        elif at == ArgType.STR:
            out += encode_uvarint(intern(v))


def _decode_args(payload: bytes, o: int, sig: Signature, prev, lookup):
    args = []
    for i, at in enumerate(sig.arg_types):
        if at in (ArgType.UINT, ArgType.INT):
            d, o = decode_svarint(payload, o)
            prev[i] = int(prev[i]) + d
            args.append(prev[i])
        elif at == ArgType.REAL:
            args.append(struct.unpack_from("<d", payload, o)[0]); o += 8
        elif at == ArgType.STR:
            sid, o = decode_uvarint(payload, o)
            args.append(lookup(sid))
    return args, o


# ---------------------------------------------------------------------------
# Writer — the call/return domain API
# ---------------------------------------------------------------------------

@dataclass
class _Event:
    enter: bool
    time: int
    func_id: int = 0
    args: tuple = ()
    ret: object = None
    has_ret: bool = False


class CallReturnWriter:
    """Domain API: ``enter(func_id, args)`` / ``exit(ret)`` over a trl writer.

    ``call_stream_id`` and ``sig_stream_id`` are stream-type ids from
    ``writer.add_stream_type`` (the dependency between them is declared on
    ``close``). Multiple call streams may share one signature stream.
    """

    def __init__(self, writer, call_stream_id: int, sig_stream_id: int,
                 signatures: List[Signature], window: int = 64) -> None:
        self._w = writer
        self._call_stream_id = call_stream_id
        self._sig_stream_id = sig_stream_id
        self._sigs = {s.func_id: s for s in signatures}
        self._signatures = list(signatures)
        self._window = window
        self._events: List[_Event] = []
        self._raw_calls: List[Tuple[int, int, int, list]] = []  # (func,start,end,args)
        self._open_raw: Dict[int, Tuple[int, int, list]] = {}
        self._closed = False
        self._depth = 0

    # -- domain API ----------------------------------------------------------

    def enter(self, func_id: int, args=(), time: int = 0) -> None:
        sig = self._sigs.get(func_id)
        if sig is None or len(args) != len(sig.arg_types):
            raise ValueError(f"call to {func_id} does not match a signature; "
                             f"use enter_raw() for the fallback path")
        self._events.append(_Event(enter=True, time=time, func_id=func_id,
                                   args=tuple(args)))
        self._depth += 1

    def exit(self, ret=None, time: int = 0) -> None:
        self._events.append(_Event(enter=False, time=time, ret=ret,
                                   has_ret=ret is not None))
        if self._depth > 0:
            self._depth -= 1

    def enter_raw(self, func_id: int, args, start: int, end: int) -> None:
        """Raw-fallback path (``HAS_RAW_FALLBACK``): spill a call that doesn't fit
        a signature to an ordinary transaction, readable without this codec."""
        self._raw_calls.append((func_id, start, end, list(args)))

    # -- finalize ------------------------------------------------------------

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True

        # (1) signatures -> shared aux stream
        self._w.write_aux(
            self._sig_stream_id, _SIG_KEY,
            _encode_signatures(self._signatures, self._w.intern),
            compress=False)

        # (2) declare the call stream's dependency on the signature stream
        self._w.declare_dependencies(self._call_stream_id, [self._sig_stream_id])

        # (3) encode the call-event block -> BLK_EXT
        if self._events:
            self._w.write_ext(_EXT_TYPE, ext_version=1,
                              payload=self._encode_events())

        # (4) raw-fallback calls -> a plain transaction block (core.record)
        if self._raw_calls:
            with self._w.begin_txn_block(0) as txn:
                for i, (func_id, start, end, args) in enumerate(self._raw_calls):
                    attrs = [TxnAttr(field_idx=j, value=v) for j, v in enumerate(args)]
                    for a in attrs:
                        a._field_type = FieldType.FT_I64
                    txn.write_full(stream_inst_id=self._call_stream_id,
                                   txn_type_id=func_id, txn_id=i,
                                   start=start, end=end, parent=0, attrs=attrs)

    def _encode_events(self) -> bytes:
        out = bytearray()
        out += encode_uvarint(self._call_stream_id)
        out += encode_uvarint(self._sig_stream_id)
        out += encode_uvarint(self._window)
        out += encode_uvarint(len(self._events))
        prev_time = 0
        prev_args: Dict[int, list] = {}
        window: List[Tuple[int, tuple]] = []
        for ev in self._events:
            dt = ev.time - prev_time
            prev_time = ev.time
            if ev.enter:
                frame = (ev.func_id, ev.args)
                hit = _window_find(window, frame)
                if hit is not None:
                    out.append(_TAG_ENTER | _TAG_BACKREF)
                    out += encode_uvarint(hit)              # distance from end
                    out += encode_svarint(dt)
                    # keep predictors consistent with the referenced frame
                    prev_args[ev.func_id] = list(ev.args)
                else:
                    out.append(_TAG_ENTER)
                    out += encode_uvarint(ev.func_id)
                    sig = self._sigs[ev.func_id]
                    prev = prev_args.setdefault(ev.func_id, [0] * len(sig.arg_types))
                    _encode_args(out, sig, ev.args, prev, self._w.intern)
                    out += encode_svarint(dt)
                    _window_push(window, frame, self._window)
            else:
                tag = 0
                if ev.has_ret:
                    tag |= _TAG_HASRET
                out.append(tag)
                out += encode_svarint(dt)
                if ev.has_ret:
                    out += encode_svarint(int(ev.ret))
        return bytes(out)


def _window_find(window, frame) -> Optional[int]:
    for p, f in enumerate(window):
        if f == frame:
            return len(window) - 1 - p          # distance from the end
    return None


def _window_push(window, frame, cap) -> None:
    window.append(frame)
    if len(window) > cap:
        window.pop(0)


# ---------------------------------------------------------------------------
# Reader — decode events, expose iterator + tree
# ---------------------------------------------------------------------------

@dataclass
class Call:
    func_id: int
    name: str
    args: list
    start: int
    end: Optional[int] = None
    ret: object = None
    children: list = field(default_factory=list)


class CallReturnReader:
    def __init__(self, reader, call_stream_id: int) -> None:
        self._r = reader
        self._call_stream_id = call_stream_id
        # Resolve the signature stream via the dependency catalog (Phase 2c).
        deps = reader.dependencies(call_stream_id)
        self._sig_stream_id = deps[0] if deps else None
        self._sigs: Dict[int, Signature] = {}
        if self._sig_stream_id is not None:
            payloads = reader.read_aux(self._sig_stream_id, _SIG_KEY)
            if payloads:
                self._sigs = _decode_signatures(payloads[0], reader.string_table.lookup)
        self._events = self._load_events()

    def signatures(self) -> Dict[int, Signature]:
        return dict(self._sigs)

    def _load_events(self) -> List[dict]:
        for ext in self._r.iter_ext_blocks(ext_type=_EXT_TYPE):
            cs, ss, window, evs = self._decode_block(ext.payload)
            if cs == self._call_stream_id:
                return evs
        return []

    def _decode_block(self, payload: bytes):
        o = 0
        cs, o = decode_uvarint(payload, o)
        ss, o = decode_uvarint(payload, o)
        window_cap, o = decode_uvarint(payload, o)
        n, o = decode_uvarint(payload, o)
        events: List[dict] = []
        prev_time = 0
        prev_args: Dict[int, list] = {}
        window: List[Tuple[int, tuple]] = []
        lookup = self._r.string_table.lookup
        for _ in range(n):
            tag = payload[o]; o += 1
            if tag & _TAG_ENTER:
                if tag & _TAG_BACKREF:
                    idx, o = decode_uvarint(payload, o)
                    func_id, args = window[len(window) - 1 - idx]
                    prev_args[func_id] = list(args)
                    dt, o = decode_svarint(payload, o)
                else:
                    func_id, o = decode_uvarint(payload, o)
                    sig = self._sigs[func_id]
                    prev = prev_args.setdefault(func_id, [0] * len(sig.arg_types))
                    args, o = _decode_args(payload, o, sig, prev, lookup)
                    args = tuple(args)
                    dt, o = decode_svarint(payload, o)
                    _window_push(window, (func_id, args), window_cap)
                prev_time += dt
                name = self._sigs[func_id].name if func_id in self._sigs else ""
                events.append({"kind": "enter", "time": prev_time,
                               "func_id": func_id, "name": name,
                               "args": list(args)})
            else:
                dt, o = decode_svarint(payload, o)
                prev_time += dt
                ret = None
                if tag & _TAG_HASRET:
                    ret, o = decode_svarint(payload, o)
                events.append({"kind": "exit", "time": prev_time, "ret": ret})
        return cs, ss, window_cap, events

    def events(self) -> List[dict]:
        """Flat enter/exit event list, in order, with depth annotated."""
        depth = 0
        out = []
        for ev in self._events:
            e = dict(ev)
            if ev["kind"] == "enter":
                e["depth"] = depth
                depth += 1
            else:
                depth = max(0, depth - 1)
                e["depth"] = depth
            out.append(e)
        return out

    def call_tree(self) -> List[Call]:
        """Reconstruct the nested call tree by pairing enter/exit on depth."""
        roots: List[Call] = []
        stack: List[Call] = []
        for ev in self._events:
            if ev["kind"] == "enter":
                c = Call(func_id=ev["func_id"], name=ev["name"],
                         args=list(ev["args"]), start=ev["time"])
                (stack[-1].children if stack else roots).append(c)
                stack.append(c)
            else:
                if stack:
                    c = stack.pop()
                    c.end = ev["time"]
                    c.ret = ev["ret"]
        return roots


# ---------------------------------------------------------------------------
# Registered codec (governance / capability discovery only)
# ---------------------------------------------------------------------------

@txn_codec(CALLRETURN_CODEC_ID, version=1,
           caps=Capability.NEEDS_INPUT_STREAMS | Capability.HAS_RAW_FALLBACK
           | Capability.LOSSLESS)
class CallReturnCodec(TxnCodec):
    """Registration for the call/return codec. The on-disk work is done by the
    domain API (``CallReturnWriter`` / ``CallReturnReader``) over public SPI
    calls; the block-encode/decode slots are unused (raise)."""

    def open_writer(self, store: Store, stream_id: int, params: bytes):
        return None

    def open_reader(self, store: Store, stream_id: int, params: bytes):
        return None

    def encode_block(self, state, store: Store):
        raise NotImplementedError("use CallReturnWriter")

    def decode_block(self, state, store: Store, payload, flags, emit):
        raise NotImplementedError("use CallReturnReader")
