"""Derived (virtual) signals — expression-over-streams codec (design §5.2).

A derived signal stores **the expression, not the data**: a compact post-order
bytecode over a set of input signals, evaluated at read time at the union of the
inputs' change times (the ClickHouse ALIAS / virtual-column model). The identity
case ``Input(0)`` is FST-style signal aliasing — one opcode, zero value data.

This module is pure: an expression AST + bytecode compiler/evaluator and a
``DerivedDef`` (de)serializer. The writer stores a ``DerivedDef`` per derived
variable; the reader evaluates lazily and memoizes, reusing the Phase 2c
dependency graph for ordering and cycle defense (`_deps.py`).

Values are unsigned bit-vectors (Python ints); inputs are integer-valued
signals. Reductions/slice/concat carry explicit widths so the evaluator needs no
type inference.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Tuple

from ._codec import encode_uvarint, decode_uvarint


# Reserved aux owner id under which derived-signal definitions are stored
# (keyed by derived var id), and the high base for derived var ids so they
# never collide with hierarchy var ids.
DERIVED_AUX_OWNER = 0xFFFFFFFE
DERIVED_VAR_BASE  = 0x40000000


# ---------------------------------------------------------------------------
# Opcodes
# ---------------------------------------------------------------------------

OP_PUSH_INPUT = 0x01   # + uvarint idx
OP_PUSH_CONST = 0x02   # + uvarint value
OP_NOT        = 0x03   # bitwise ~ (masked to output width on finalize)
OP_AND        = 0x04
OP_OR         = 0x05
OP_XOR        = 0x06
OP_LNOT       = 0x07   # logical ! -> 0/1
OP_EQ         = 0x08   # -> 0/1
OP_NE         = 0x09   # -> 0/1
OP_SHL        = 0x0A   # + uvarint k
OP_SHR        = 0x0B   # + uvarint k
OP_RED_AND    = 0x0C   # + uvarint width -> 0/1 (AND of low `width` bits)
OP_RED_OR     = 0x0D   # + uvarint width -> 0/1 (OR  of low `width` bits)
OP_SLICE      = 0x0E   # + uvarint lo + uvarint width -> (v >> lo) & mask(width)
OP_CONCAT     = 0x0F   # + uvarint width_b -> (a << width_b) | (b & mask(width_b))


def _mask(width: int) -> int:
    return (1 << width) - 1 if width > 0 else 0


# ---------------------------------------------------------------------------
# Expression AST (writer-facing builder)
# ---------------------------------------------------------------------------

class Expr:
    """Base for the small derived-expression DSL. Operators build an AST that
    :func:`compile_expr` lowers to post-order bytecode."""

    def __and__(self, o): return BinOp(OP_AND, self, _wrap(o))
    def __or__(self, o):  return BinOp(OP_OR, self, _wrap(o))
    def __xor__(self, o): return BinOp(OP_XOR, self, _wrap(o))
    def __invert__(self): return UnOp(OP_NOT, self)

    def lnot(self):        return UnOp(OP_LNOT, self)
    def eq(self, o):       return BinOp(OP_EQ, self, _wrap(o))
    def ne(self, o):       return BinOp(OP_NE, self, _wrap(o))
    def shl(self, k):      return ShiftOp(OP_SHL, self, k)
    def shr(self, k):      return ShiftOp(OP_SHR, self, k)
    def reduce_and(self, width): return ReduceOp(OP_RED_AND, self, width)
    def reduce_or(self, width):  return ReduceOp(OP_RED_OR, self, width)
    def slice_(self, lo, width): return SliceOp(self, lo, width)
    def concat(self, other, width_b): return ConcatOp(self, _wrap(other), width_b)


def _wrap(o):
    return o if isinstance(o, Expr) else Const(int(o))


@dataclass
class Input(Expr):
    idx: int

@dataclass
class Const(Expr):
    value: int

@dataclass
class UnOp(Expr):
    op: int
    a: Expr

@dataclass
class BinOp(Expr):
    op: int
    a: Expr
    b: Expr

@dataclass
class ShiftOp(Expr):
    op: int
    a: Expr
    k: int

@dataclass
class ReduceOp(Expr):
    op: int
    a: Expr
    width: int

@dataclass
class SliceOp(Expr):
    a: Expr
    lo: int
    width: int

@dataclass
class ConcatOp(Expr):
    a: Expr
    b: Expr
    width_b: int


def compile_expr(expr: Expr) -> bytes:
    """Lower an expression AST to post-order bytecode."""
    out = bytearray()

    def emit(node: Expr) -> None:
        if isinstance(node, Input):
            out.append(OP_PUSH_INPUT); out.extend(encode_uvarint(node.idx))
        elif isinstance(node, Const):
            out.append(OP_PUSH_CONST); out.extend(encode_uvarint(node.value))
        elif isinstance(node, UnOp):
            emit(node.a); out.append(node.op)
        elif isinstance(node, BinOp):
            emit(node.a); emit(node.b); out.append(node.op)
        elif isinstance(node, ShiftOp):
            emit(node.a); out.append(node.op); out.extend(encode_uvarint(node.k))
        elif isinstance(node, ReduceOp):
            emit(node.a); out.append(node.op); out.extend(encode_uvarint(node.width))
        elif isinstance(node, SliceOp):
            emit(node.a); out.append(OP_SLICE)
            out.extend(encode_uvarint(node.lo)); out.extend(encode_uvarint(node.width))
        elif isinstance(node, ConcatOp):
            emit(node.a); emit(node.b)
            out.append(OP_CONCAT); out.extend(encode_uvarint(node.width_b))
        else:
            raise TypeError(f"not a derived Expr node: {node!r}")

    emit(expr)
    return bytes(out)


# ---------------------------------------------------------------------------
# Bytecode evaluator
# ---------------------------------------------------------------------------

def eval_bytecode(bytecode: bytes, inputs: List[int], out_width: int) -> int:
    """Evaluate post-order bytecode given the current input values. The result is
    masked to ``out_width`` bits (0 = no mask)."""
    stack: List[int] = []
    o = 0
    n = len(bytecode)
    while o < n:
        op = bytecode[o]; o += 1
        if op == OP_PUSH_INPUT:
            idx, o = decode_uvarint(bytecode, o)
            stack.append(int(inputs[idx]))
        elif op == OP_PUSH_CONST:
            v, o = decode_uvarint(bytecode, o)
            stack.append(v)
        elif op == OP_NOT:
            a = stack.pop(); stack.append(~a)
        elif op == OP_AND:
            b = stack.pop(); a = stack.pop(); stack.append(a & b)
        elif op == OP_OR:
            b = stack.pop(); a = stack.pop(); stack.append(a | b)
        elif op == OP_XOR:
            b = stack.pop(); a = stack.pop(); stack.append(a ^ b)
        elif op == OP_LNOT:
            a = stack.pop(); stack.append(0 if a else 1)
        elif op == OP_EQ:
            b = stack.pop(); a = stack.pop(); stack.append(1 if a == b else 0)
        elif op == OP_NE:
            b = stack.pop(); a = stack.pop(); stack.append(1 if a != b else 0)
        elif op == OP_SHL:
            k, o = decode_uvarint(bytecode, o); a = stack.pop(); stack.append(a << k)
        elif op == OP_SHR:
            k, o = decode_uvarint(bytecode, o); a = stack.pop(); stack.append(a >> k)
        elif op == OP_RED_AND:
            w, o = decode_uvarint(bytecode, o); a = stack.pop()
            stack.append(1 if (a & _mask(w)) == _mask(w) else 0)
        elif op == OP_RED_OR:
            w, o = decode_uvarint(bytecode, o); a = stack.pop()
            stack.append(1 if (a & _mask(w)) != 0 else 0)
        elif op == OP_SLICE:
            lo, o = decode_uvarint(bytecode, o); w, o = decode_uvarint(bytecode, o)
            a = stack.pop(); stack.append((a >> lo) & _mask(w))
        elif op == OP_CONCAT:
            wb, o = decode_uvarint(bytecode, o)
            b = stack.pop(); a = stack.pop(); stack.append((a << wb) | (b & _mask(wb)))
        else:
            raise ValueError(f"bad derived opcode 0x{op:02x}")
    if len(stack) != 1:
        raise ValueError(f"derived expression left {len(stack)} values on the stack")
    result = stack[0]
    if out_width > 0:
        result &= _mask(out_width)
    return result & ((1 << 64) - 1) if out_width == 0 else result


# ---------------------------------------------------------------------------
# Derived definition (stored in an aux block, keyed by derived var id)
# ---------------------------------------------------------------------------

FLAG_MATERIALIZED = 0x01

@dataclass
class DerivedDef:
    bit_width: int
    input_var_ids: List[int]
    bytecode: bytes
    flags: int = 0

    @property
    def materialized(self) -> bool:
        return bool(self.flags & FLAG_MATERIALIZED)

    def encode(self) -> bytes:
        out = bytearray()
        out += encode_uvarint(self.flags)
        out += encode_uvarint(self.bit_width)
        out += encode_uvarint(len(self.input_var_ids))
        for v in self.input_var_ids:
            out += encode_uvarint(v)
        out += encode_uvarint(len(self.bytecode))
        out += self.bytecode
        return bytes(out)

    @staticmethod
    def decode(data: bytes) -> "DerivedDef":
        o = 0
        flags, o = decode_uvarint(data, o)
        bit_width, o = decode_uvarint(data, o)
        n, o = decode_uvarint(data, o)
        inputs = []
        for _ in range(n):
            v, o = decode_uvarint(data, o)
            inputs.append(v)
        blen, o = decode_uvarint(data, o)
        bytecode = bytes(data[o:o + blen])
        return DerivedDef(bit_width=bit_width, input_var_ids=inputs,
                          bytecode=bytecode, flags=flags)


# ---------------------------------------------------------------------------
# Evaluation at the union of input change times
# ---------------------------------------------------------------------------

def evaluate_changes(defn: DerivedDef,
                     input_changes: List[List[Tuple[int, int]]]) -> List[Tuple[int, int]]:
    """Compute the derived signal's ``(time, value)`` changes.

    ``input_changes[i]`` is the sorted change list of the i-th input. Evaluation
    is event-driven over the union of all input change times; a derived change is
    emitted only when the computed value differs from the previous one. Inputs
    default to 0 before their first change.
    """
    # Merge-sorted union of times.
    times = sorted({t for changes in input_changes for t, _ in changes})
    cursors = [0] * len(input_changes)
    cur_vals = [0] * len(input_changes)
    out: List[Tuple[int, int]] = []
    last = None
    for t in times:
        for i, changes in enumerate(input_changes):
            c = cursors[i]
            while c < len(changes) and changes[c][0] <= t:
                cur_vals[i] = int(changes[c][1])
                c += 1
            cursors[i] = c
        val = eval_bytecode(defn.bytecode, cur_vals, defn.bit_width)
        if val != last:
            out.append((t, val))
            last = val
    return out
