"""Filter expression language (design: query_cli_design.md §5).

``compile_filter(text, schema)`` parses an expression, resolves and checks its
names against the trace's schema, and returns a :class:`Filter` whose
``match(txn)`` evaluates it against one joined transaction.

Precedence, loosest first::

    || or          (left-assoc)
    && and         (left-assoc)
    ! not          (prefix)
    == != < <= > >= ~ =~ in, not in   (no chaining)
    |   ^   &   << >>   + -   * / %   (Python order: bit ops bind tighter
                                       than comparisons)
    unary - + ~
    postfix  .name  [key]  (args)

Every comparison against a MISSING value is false (§5.3).
"""

from __future__ import annotations

import difflib
import fnmatch
import re
from dataclasses import dataclass, field
from fractions import Fraction
from typing import Callable, Dict, List, Optional, Sequence, Set

from ._values import MISSING, EnumVal, LogicVal, UNIT_EXP, to_ticks


class QueryError(Exception):
    """A user-facing error with a structured payload.

    Raised for bad expressions, unknown names, bad selections, missing refs
    and unreadable files. ``str(e)`` is the message.

    Attributes:
        info: The CLI's JSON error object. It always has ``error`` (a code
            such as ``unknown_field``, ``syntax`` or ``not_found``) and
            ``message``; it may have ``pos``, ``did_you_mean``,
            ``valid_values``, ``hint`` and code-specific keys.
    """

    def __init__(self, code: str, message: str, **extra) -> None:
        super().__init__(message)
        self.info = {"error": code, "message": message}
        self.info.update({k: v for k, v in extra.items() if v is not None})


# ---------------------------------------------------------------------------
# Schema view used for name resolution and static checks
# ---------------------------------------------------------------------------

NUMERIC_KINDS = frozenset({
    "u8", "u16", "u32", "u64", "i8", "i16", "i32", "i64", "f32", "f64",
    "bool", "bitvec", "logicvec", "time"})

BUILTINS = {
    "id": "int", "start": "int", "end": "int", "dur": "int",
    "parent": "int", "open": "bool", "stream": "str", "type": "str",
    "nlinks": "int", "nchildren": "int",
}

# Names the design reserves for later phases (name -> phase).
DEFERRED: Dict[str, str] = {}


@dataclass
class FieldSpec:
    """Kind and enum labels of a field, for name resolution."""

    kind: str
    enum_labels: Optional[Dict[int, str]] = None


@dataclass
class SchemaView:
    """What the expression compiler needs to know about a trace.

    Get one from :meth:`Trace.schema_view`.

    Attributes:
        timescale_exp: Time unit as a power of ten in seconds; time
            literals such as ``10ns`` convert to ticks with it.
        types: ``{type name: {field name: FieldSpec}}``.
        stream_paths: Known stream paths.
        selected_types: When set (from ``-T``), unqualified field names
            resolve against these types only.
    """

    timescale_exp: int = -9
    # type name -> field name -> FieldSpec
    types: Dict[str, Dict[str, FieldSpec]] = field(default_factory=dict)
    stream_paths: Sequence[str] = ()
    # When set (from -T), unqualified field names resolve against these only.
    selected_types: Optional[Set[str]] = None

    def visible_types(self) -> Dict[str, Dict[str, FieldSpec]]:
        """The types that unqualified names resolve against."""
        if not self.selected_types:
            return self.types
        return {t: f for t, f in self.types.items() if t in self.selected_types}

    def field_specs(self, name: str) -> List[FieldSpec]:
        """Specs of ``name`` in each visible type that has it."""
        return [fs[name] for fs in self.visible_types().values() if name in fs]


# ---------------------------------------------------------------------------
# Lexer
# ---------------------------------------------------------------------------

@dataclass
class Tok:
    kind: str      # num float time str regex ident op eof
    value: object
    pos: int
    text: str = ""


_OPS = sorted([
    "&&", "||", "==", "!=", "<=", ">=", "<<", ">>", "=~", "..",
    "!", "<", ">", "+", "-", "*", "/", "%", "&", "|", "^", "~",
    "(", ")", ",", "[", "]", ".", "=",
], key=len, reverse=True)

_KEYWORDS = {"and", "or", "not", "in", "true", "false"}

_SV_LIT = re.compile(r"(\d+)?'([sS])?([hHdDbBoO])([0-9a-fA-FxXzZ_?]+)")
_NUM = re.compile(
    r"0[xX][0-9a-fA-F_]+|0[bB][01_]+|0[oO][0-7_]+"
    r"|\d[\d_]*(?:\.\d+)?(?:[eE][+-]?\d+)?")
_UNIT = re.compile(r"(fs|ps|ns|us|ms|s)(?![A-Za-z0-9_])")
_IDENT = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


def tokenize(text: str) -> List[Tok]:
    toks: List[Tok] = []
    i, n = 0, len(text)
    while i < n:
        c = text[i]
        if c.isspace():
            i += 1
            continue
        # Regex literal, only directly after =~
        if c == "/" and toks and toks[-1].kind == "op" and toks[-1].value == "=~":
            j = i + 1
            buf = []
            while j < n and text[j] != "/":
                if text[j] == "\\" and j + 1 < n and text[j + 1] == "/":
                    buf.append("/")
                    j += 2
                    continue
                buf.append(text[j])
                j += 1
            if j >= n:
                raise QueryError("syntax", "unterminated regex literal", pos=i,
                                 hint="close the regex with '/', e.g. msg =~ /timeout|err/")
            toks.append(Tok("regex", "".join(buf), i, text[i:j + 1]))
            i = j + 1
            continue
        if c == "'":
            # SV-style unsized literal ('h4000) unless it is really a string
            # ('hello', 'h40').
            m = _SV_LIT.match(text, i)
            if m and (m.end() >= n or not (text[m.end()].isalnum()
                                           or text[m.end()] in "_'")):
                toks.append(Tok("num", _sv_literal(m, i), i, m.group(0)))
                i = m.end()
                continue
        if c in "'\"":
            j = i + 1
            buf = []
            while j < n and text[j] != c:
                if text[j] == "\\" and j + 1 < n:
                    buf.append({"n": "\n", "t": "\t"}.get(text[j + 1], text[j + 1]))
                    j += 2
                    continue
                buf.append(text[j])
                j += 1
            if j >= n:
                raise QueryError("syntax", "unterminated string literal", pos=i)
            toks.append(Tok("str", "".join(buf), i, text[i:j + 1]))
            i = j + 1
            continue
        if c.isdigit():
            m = _SV_LIT.match(text, i)
            if m:
                toks.append(Tok("num", _sv_literal(m, i), i, m.group(0)))
                i = m.end()
                continue
            m = _NUM.match(text, i)
            lit = m.group(0)
            j = m.end()
            if lit[:2].lower() in ("0x", "0b", "0o"):
                val: object = int(lit.replace("_", ""), 0)
                kind = "num"
            elif any(ch in lit for ch in ".eE"):
                val = Fraction(lit.replace("_", ""))
                kind = "float"
            else:
                val = int(lit.replace("_", ""))
                kind = "num"
            um = _UNIT.match(text, j)
            if um and lit[:2].lower() not in ("0x", "0b", "0o"):
                toks.append(Tok("time", (Fraction(val), UNIT_EXP[um.group(1)]), i,
                                text[i:um.end()]))
                i = um.end()
                continue
            if j < n and (text[j].isalpha() or text[j] == "_"):
                raise QueryError(
                    "syntax", f"bad number {text[i:j + 1]!r}...", pos=i,
                    hint="time units are fs ps ns us ms s; hex is 0x..., "
                         "SV-style is 32'h...")
            toks.append(Tok(kind, float(val) if kind == "float" else val, i, lit))
            i = j
            continue
        m = _IDENT.match(text, i)
        if m:
            word = m.group(0)
            kind = "kw" if word in _KEYWORDS else "ident"
            toks.append(Tok(kind, word, i, word))
            i = m.end()
            continue
        for op in _OPS:
            if text.startswith(op, i):
                if op == "=":
                    raise QueryError("syntax", "'=' is not an operator", pos=i,
                                     hint="use '==' to compare")
                toks.append(Tok("op", op, i, op))
                i += len(op)
                break
        else:
            raise QueryError("syntax", f"unexpected character {c!r}", pos=i)
    toks.append(Tok("eof", None, n, ""))
    return toks


def _sv_literal(m, pos) -> int:
    base = {"h": 16, "d": 10, "b": 2, "o": 8}[m.group(3).lower()]
    digits = m.group(4).replace("_", "")
    if any(ch in "xXzZ?" for ch in digits):
        raise QueryError("syntax", f"x/z digits are not supported in {m.group(0)!r}",
                         pos=pos, hint="compare against known bits with a mask, "
                                       "e.g. (x & 0xf0) == 0x40")
    try:
        return int(digits, base)
    except ValueError:
        raise QueryError("syntax", f"bad digits in {m.group(0)!r}", pos=pos)


# ---------------------------------------------------------------------------
# Parser -> raw AST (tuples), then resolver -> nodes with closures
# ---------------------------------------------------------------------------
#
# Raw AST node shapes:
#   ("lit", value, kind)          kind in int float str bool regex time
#   ("name", ident)
#   ("attr", obj, ident)
#   ("index", obj, keynode)
#   ("call", fname, [args])
#   ("un", op, x)
#   ("bin", op, a, b)
#   ("cmp", op, a, b)
#   ("in", x, [items], negate)    item: node or ("range", lo, hi)
#   ("not", x) ("and", a, b) ("or", a, b)
# Every node is followed by its source position as the last element.

_CMP_OPS = {"==", "!=", "<", "<=", ">", ">=", "~", "=~"}


class _Parser:
    def __init__(self, text: str, timescale_exp: int) -> None:
        self.text = text
        self.toks = tokenize(text)
        self.i = 0
        self.ts = timescale_exp

    @property
    def tok(self) -> Tok:
        return self.toks[self.i]

    def _is(self, kind: str, *vals) -> bool:
        t = self.tok
        return t.kind == kind and (not vals or t.value in vals)

    def _take(self) -> Tok:
        t = self.tok
        self.i += 1
        return t

    def _expect_op(self, op: str) -> Tok:
        if not self._is("op", op):
            self._unexpected(f"expected '{op}'")
        return self._take()

    def _unexpected(self, what: str):
        t = self.tok
        got = "end of expression" if t.kind == "eof" else repr(t.text)
        raise QueryError("syntax", f"{what}, got {got}", pos=t.pos)

    def parse(self):
        if self._is("eof"):
            raise QueryError("syntax", "empty expression", pos=0)
        node = self._or()
        if not self._is("eof"):
            self._unexpected("expected an operator or end of expression")
        return node

    def _or(self):
        a = self._and()
        while self._is("op", "||") or self._is("kw", "or"):
            pos = self._take().pos
            a = ("or", a, self._and(), pos)
        return a

    def _and(self):
        a = self._not()
        while self._is("op", "&&") or self._is("kw", "and"):
            pos = self._take().pos
            a = ("and", a, self._not(), pos)
        return a

    def _not(self):
        if self._is("op", "!") or self._is("kw", "not"):
            pos = self._take().pos
            return ("not", self._not(), pos)
        return self._cmp()

    def _cmp(self):
        a = self._bitor()
        t = self.tok
        node = None
        if t.kind == "op" and t.value in _CMP_OPS:
            self._take()
            node = ("cmp", t.value, a, self._bitor(), t.pos)
        elif self._is("kw", "in"):
            self._take()
            node = ("in", a, self._in_rhs(), False, t.pos)
        elif self._is("kw", "not") and self.toks[self.i + 1].kind == "kw" \
                and self.toks[self.i + 1].value == "in":
            self._take()
            self._take()
            node = ("in", a, self._in_rhs(), True, t.pos)
        if node is None:
            return a
        t2 = self.tok
        if (t2.kind == "op" and t2.value in _CMP_OPS) or self._is("kw", "in"):
            raise QueryError("syntax", "comparisons cannot be chained", pos=t2.pos,
                             hint="combine them with &&, e.g. a < x && x < b")
        return node

    def _in_item(self):
        lo = self._bitor()
        if self._is("op", ".."):
            pos = self._take().pos
            return ("range", lo, self._bitor(), pos)
        return lo

    def _in_rhs(self):
        if self._is("op", "("):
            self._take()
            items = [self._in_item()]
            while self._is("op", ","):
                self._take()
                if self._is("op", ")"):
                    break
                items.append(self._in_item())
            self._expect_op(")")
            return items
        return [self._in_item()]

    def _binary(self, sub, ops):
        a = sub()
        while self.tok.kind == "op" and self.tok.value in ops:
            t = self._take()
            a = ("bin", t.value, a, sub(), t.pos)
        return a

    def _bitor(self):
        return self._binary(self._bitxor, ("|",))

    def _bitxor(self):
        return self._binary(self._bitand, ("^",))

    def _bitand(self):
        return self._binary(self._shift, ("&",))

    def _shift(self):
        return self._binary(self._add, ("<<", ">>"))

    def _add(self):
        return self._binary(self._mul, ("+", "-"))

    def _mul(self):
        return self._binary(self._unary, ("*", "/", "%"))

    def _unary(self):
        if self.tok.kind == "op" and self.tok.value in ("-", "+", "~"):
            t = self._take()
            return ("un", t.value, self._unary(), t.pos)
        if self._is("op", "!"):
            raise QueryError("syntax", "'!' cannot be used inside an arithmetic "
                             "or comparison operand", pos=self.tok.pos,
                             hint="'!' negates a whole condition: !(x == 1) or !has(x)")
        return self._postfix()

    def _postfix(self):
        node = self._primary()
        while True:
            if self._is("op", "."):
                pos = self._take().pos
                if not self._is("ident") and not self._is("kw"):
                    self._unexpected("expected a field name after '.'")
                node = ("attr", node, self._take().value, pos)
            elif self._is("op", "["):
                pos = self._take().pos
                key = self._or()
                self._expect_op("]")
                node = ("index", node, key, pos)
            elif self._is("op", "("):
                pos = self._take().pos
                if node[0] != "name":
                    raise QueryError("syntax", "only named functions can be called", pos=pos)
                args = []
                if not self._is("op", ")"):
                    args.append(self._or())
                    while self._is("op", ","):
                        self._take()
                        args.append(self._or())
                self._expect_op(")")
                node = ("call", node[1], args, node[2])
            else:
                return node

    def _primary(self):
        t = self.tok
        if t.kind == "num":
            self._take()
            return ("lit", t.value, "int", t.pos)
        if t.kind == "float":
            self._take()
            return ("lit", t.value, "float", t.pos)
        if t.kind == "time":
            self._take()
            v, unit_exp = t.value
            return ("lit", to_ticks(v, unit_exp, self.ts), "time", t.pos)
        if t.kind == "str":
            self._take()
            return ("lit", t.value, "str", t.pos)
        if t.kind == "regex":
            self._take()
            return ("lit", t.value, "regex", t.pos)
        if t.kind == "kw" and t.value in ("true", "false"):
            self._take()
            return ("lit", t.value == "true", "bool", t.pos)
        if t.kind == "ident":
            self._take()
            return ("name", t.value, t.pos)
        if self._is("op", "("):
            self._take()
            node = self._or()
            self._expect_op(")")
            return node
        self._unexpected("expected a value, name or '('")


# ---------------------------------------------------------------------------
# Resolution, static checks and compilation to closures
# ---------------------------------------------------------------------------

@dataclass
class _C:
    """A compiled node: evaluator plus the static facts the checker needs."""

    fn: Callable
    kinds: frozenset            # possible value kinds; empty = unknown
    pos: int = 0
    lit: object = MISSING       # literal value, when the node is a literal
    name: Optional[str] = None  # field/builtin name, when it is one
    enum_labels: Optional[Set[str]] = None
    is_field: bool = False
    pushable: Optional[tuple] = None   # ("stream"|"type", ...) for pushdown


def _kind_class(kind: str) -> str:
    if kind in ("str",):
        return "str"
    if kind == "enum":
        return "enum"
    if kind in ("bytes",):
        return "bytes"
    if kind in ("regex",):
        return "regex"
    return "num"


def _num(v):
    """Coerce to a number for arithmetic, or MISSING."""
    if v is MISSING or isinstance(v, (str, bytes, LogicVal)) or v is None:
        return MISSING
    return v


def _cmp_eq(a, b) -> bool:
    if a is MISSING or b is MISSING:
        return False
    if isinstance(a, EnumVal) and isinstance(b, str):
        return a.label == b
    if isinstance(b, EnumVal) and isinstance(a, str):
        return b.label == a
    if isinstance(a, str) != isinstance(b, str):
        return False
    return a == b


def _cmp_ord(op: str, a, b) -> bool:
    if a is MISSING or b is MISSING or isinstance(a, LogicVal) or isinstance(b, LogicVal):
        return False
    # An enum against a label orders by enum value (DECERR=3 > SLVERR=2),
    # not by label text; a label the field does not have matches nothing.
    if isinstance(a, EnumVal) and isinstance(b, str):
        b = a.value_of(b)
        if b is None:
            return False
    elif isinstance(b, EnumVal) and isinstance(a, str):
        a = b.value_of(a)
        if a is None:
            return False
    try:
        if op == "<":
            return a < b
        if op == "<=":
            return a <= b
        if op == ">":
            return a > b
        return a >= b
    except TypeError:
        return False


# A left shift (or a bits() width) beyond this many bits is a missing value
# rather than an attempt to build a huge integer.
MAX_SHIFT = 1 << 16


def _shl(a: int, b: int) -> int:
    if b > MAX_SHIFT and a:
        raise OverflowError("shift too large")
    return a << b


def _total(fn: Callable) -> Callable:
    """Evaluation never raises: a value an operator cannot take (a negative
    bit index, inf in bit(), a raw tuple value) gives MISSING, like the
    binary operators."""
    def g(t):
        try:
            return fn(t)
        except (ValueError, TypeError, OverflowError, MemoryError):
            return MISSING
    return g


def _cmp_ne(a, b) -> bool:
    if a is MISSING or b is MISSING:
        return False
    return not _cmp_eq(a, b)


def cmp_values(op: str, a, b) -> bool:
    """``a OP b`` for ==, !=, <, <=, >, >= (the C engine's label and id sets
    are computed with this, so it must stay what the closures do)."""
    if op == "==":
        return _cmp_eq(a, b)
    if op == "!=":
        return _cmp_ne(a, b)
    return _cmp_ord(op, a, b)


def match_pred(op: str, lit, pos: int = 0) -> Callable:
    """The value test of ``X ~ 'glob'`` or ``X =~ /re/``."""
    if op == "~":
        rx = re.compile(fnmatch.translate(lit))
    else:
        rx = lit
        if isinstance(rx, str):
            try:
                rx = re.compile(rx)
            except re.error as e:
                raise QueryError("syntax", f"bad regex: {e}", pos=pos)
    use_search = op == "=~"

    def m(v):
        if isinstance(v, EnumVal):
            v = v.label
        if not isinstance(v, str):
            return False
        if use_search:
            return rx.search(v) is not None
        return rx.fullmatch(v) is not None
    return m


class _Compiler:
    def __init__(self, schema: SchemaView, text: str) -> None:
        self.schema = schema
        self.text = text
        self.uses_parent = False
        self.uses_meta = False
        self.uses_links = False         # nlinks: needs link records
        self.uses_children = False      # nchildren: needs the reverse parent map
        self.uses_links = False
        self.parent_lookup: Callable[[int], object] = lambda _id: None

    # -- errors -----------------------------------------------------------

    def _unknown_field(self, name: str, pos: int, type_name: Optional[str] = None):
        types = self.schema.types if type_name else self.schema.visible_types()
        if type_name:
            types = {type_name: types[type_name]}
        candidates = set(BUILTINS) | {f for fs in types.values() for f in fs}
        if not type_name:
            candidates |= set(self.schema.types)    # type names, for T.field
        dym = difflib.get_close_matches(name, sorted(candidates), n=3, cutoff=0.6)
        known = {t: list(fs) for t, fs in types.items()}
        where = f" in type '{type_name}'" if type_name else ""
        hint = "run `trlog schema TRACE` to list types and fields"
        if self.schema.selected_types and not type_name and any(
                name in fs for fs in self.schema.types.values()):
            hint = (f"'{name}' exists in a type not selected by -T; "
                    "widen -T or use TYPE.field")
        raise QueryError("unknown_field", f"unknown field '{name}'{where}", name=name,
                         pos=pos, did_you_mean=dym or None, known_fields=known, hint=hint)

    # -- resolution -------------------------------------------------------

    def compile(self, node) -> _C:
        return getattr(self, "_c_" + node[0])(node)

    def _c_lit(self, node) -> _C:
        _, v, kind, pos = node
        if kind == "regex":
            try:
                rx = re.compile(v)
            except re.error as e:
                raise QueryError("syntax", f"bad regex: {e}", pos=pos)
            return _C(lambda t, rx=rx: rx, frozenset({"regex"}), pos, lit=rx)
        k = {"int": "num", "float": "num", "time": "num", "bool": "num",
             "str": "str"}[kind]
        return _C(lambda t, v=v: v, frozenset({k}), pos, lit=v)

    def _builtin(self, name: str, pos: int) -> _C:
        getters = {
            "id": lambda t: t.id,
            "start": lambda t: t.start,
            "end": lambda t: MISSING if t.end is None else t.end,
            "dur": lambda t: MISSING if t.end is None else t.end - t.start,
            "parent": lambda t: MISSING if t.parent is None else t.parent,
            "open": lambda t: t.end is None,
            "stream": lambda t: t.stream,
            "type": lambda t: t.type,
            "nlinks": lambda t: len(t.links),
            "nchildren": lambda t: MISSING if t.children is None else len(t.children),
        }
        if name == "nlinks":
            self.uses_links = True
        elif name == "nchildren":
            self.uses_children = True
        kind = "str" if BUILTINS[name] == "str" else "num"
        return _C(getters[name], frozenset({kind}), pos, name=name, is_field=True)

    def _field(self, name: str, pos: int, type_name: Optional[str] = None) -> _C:
        if type_name is None:
            specs = self.schema.field_specs(name)
        else:
            fs = self.schema.types[type_name]
            specs = [fs[name]] if name in fs else []
        if not specs:
            self._unknown_field(name, pos, type_name)
        kinds = frozenset(_kind_class(s.kind) for s in specs)
        labels = None
        if any(s.kind == "enum" for s in specs):
            labels = set()
            for s in specs:
                if s.enum_labels:
                    labels.update(l for l in s.enum_labels.values() if l is not None)
        if type_name is None:
            fn = lambda t, n=name: t.fields.get(n, MISSING)
        else:
            fn = (lambda t, n=name, tn=type_name:
                  t.fields.get(n, MISSING) if t.type == tn else MISSING)
        return _C(fn, kinds, pos, name=name, enum_labels=labels, is_field=True)

    def _c_name(self, node) -> _C:
        _, name, pos = node
        if name in BUILTINS:
            return self._builtin(name, pos)
        if name in DEFERRED:
            raise QueryError("not_supported", f"'{name}' is not available yet "
                             f"(planned for phase {DEFERRED[name]})", name=name, pos=pos)
        if name == "meta":
            raise QueryError("syntax", "meta needs a key, e.g. meta[\"trlog.src.file\"]",
                             pos=pos)
        if name in self.schema.types and not self.schema.field_specs(name):
            raise QueryError("syntax", f"'{name}' is a type name, not a field",
                             pos=pos, hint=f"filter by type with type == '{name}' "
                             f"or -T {name}; reach its fields as {name}.FIELD")
        return self._field(name, pos)

    def _c_attr(self, node) -> _C:
        _, obj, name, pos = node
        if obj[0] != "name":
            raise QueryError("syntax", "'.' must follow a type name or 'parent'", pos=pos)
        base = obj[1]
        if base == "parent":
            self.uses_parent = True
            if name in BUILTINS:
                inner = self._builtin(name, pos)
            else:
                # parent's type is unknown statically: check across all types
                saved = self.schema.selected_types
                self.schema.selected_types = None
                try:
                    inner = self._field(name, pos)
                finally:
                    self.schema.selected_types = saved
            lookup = self

            def fn(t, f=inner.fn):
                if t.parent is None:
                    return MISSING
                p = lookup.parent_lookup(t.parent)
                return MISSING if p is None else f(p)
            return _C(fn, inner.kinds, pos, name="parent." + name,
                      enum_labels=inner.enum_labels, is_field=True)
        if base in self.schema.types:
            return self._field(name, pos, type_name=base)
        dym = difflib.get_close_matches(base, sorted(self.schema.types), n=3, cutoff=0.6)
        raise QueryError("unknown_type", f"unknown type '{base}' in '{base}.{name}'",
                         name=base, pos=obj[2], did_you_mean=dym or None,
                         known_types=sorted(self.schema.types))

    def _c_index(self, node) -> _C:
        _, obj, key, pos = node
        if obj[0] != "name" or obj[1] != "meta":
            raise QueryError("syntax", "only meta[...] can be indexed", pos=pos)
        if key[0] != "lit" or key[2] != "str":
            raise QueryError("syntax", "meta key must be a string literal", pos=pos,
                             hint='e.g. meta["trlog.src.file"]')
        self.uses_meta = True
        k = key[1]
        return _C(lambda t, k=k: t.meta.get(k, MISSING) if t.meta else MISSING,
                  frozenset({"str"}), pos, name=f"meta[{k!r}]", is_field=True)

    def _c_call(self, node) -> _C:
        _, fname, args, pos = node
        if fname == "has":
            if len(args) != 1:
                raise QueryError("syntax", "has() takes one field", pos=pos)
            inner = self.compile(args[0])
            if not inner.is_field:
                raise QueryError("syntax", "has() takes a field name, e.g. has(addr)",
                                 pos=pos)
            return _C(lambda t, f=inner.fn: f(t) is not MISSING, frozenset({"num"}), pos)
        nargs = {"abs": (1, 1), "min": (1, 99), "max": (1, 99), "bit": (2, 2),
                 "bits": (3, 3), "len": (1, 1)}
        if fname not in nargs:
            dym = difflib.get_close_matches(fname, sorted(list(nargs) + ["has"]), n=3)
            raise QueryError("unknown_function", f"unknown function '{fname}'",
                             name=fname, pos=pos, did_you_mean=dym or None,
                             known_functions=sorted(list(nargs) + ["has"]))
        lo, hi = nargs[fname]
        if not lo <= len(args) <= hi:
            raise QueryError("syntax", f"{fname}() takes {lo}"
                             + (f" to {hi}" if hi != lo and hi < 99 else
                                " or more" if hi == 99 else "")
                             + f" argument(s), got {len(args)}", pos=pos)
        cs = [self.compile(a) for a in args]
        fns = [c.fn for c in cs]
        if fname == "len":
            def fn(t, f=fns[0]):
                v = f(t)
                return len(v) if isinstance(v, (str, bytes)) else MISSING
        elif fname == "abs":
            def fn(t, f=fns[0]):
                v = _num(f(t))
                return MISSING if v is MISSING else abs(v)
        elif fname in ("min", "max"):
            agg = min if fname == "min" else max

            def fn(t, fs=fns, agg=agg):
                vs = [_num(f(t)) for f in fs]
                if any(v is MISSING for v in vs):
                    return MISSING
                return agg(vs)
        elif fname == "bit":
            def fn(t, fx=fns[0], fn_=fns[1]):
                x, n = _num(fx(t)), _num(fn_(t))
                if x is MISSING or n is MISSING:
                    return MISSING
                return (int(x) >> int(n)) & 1
        else:  # bits
            def fn(t, fx=fns[0], fh=fns[1], fl=fns[2]):
                x, h, l = _num(fx(t)), _num(fh(t)), _num(fl(t))
                if MISSING in (x, h, l) or h < l:
                    return MISSING
                w = int(h) - int(l) + 1
                if w > MAX_SHIFT:
                    raise OverflowError("bits() width too large")
                return (int(x) >> int(l)) & ((1 << w) - 1)
        return _C(_total(fn), frozenset({"num"}), pos)

    def _c_un(self, node) -> _C:  # noqa: C901
        _, op, x, pos = node
        c = self.compile(x)
        f = c.fn
        if op == "-":
            def fn(t):
                v = _num(f(t))
                return MISSING if v is MISSING else -v
        elif op == "+":
            def fn(t):
                return _num(f(t))
        else:
            def fn(t):
                v = _num(f(t))
                return MISSING if v is MISSING or isinstance(v, float) else ~int(v)
        fn = _total(fn)
        lit = MISSING if c.lit is MISSING else fn(None)
        return _C(fn, frozenset({"num"}), pos, lit=lit)

    _ARITH = {
        "+": lambda a, b: a + b, "-": lambda a, b: a - b, "*": lambda a, b: a * b,
        "/": lambda a, b: (a // b if isinstance(a, int) and isinstance(b, int)
                           else a / b),
        "%": lambda a, b: a % b,
        "&": lambda a, b: int(a) & int(b), "|": lambda a, b: int(a) | int(b),
        "^": lambda a, b: int(a) ^ int(b),
        "<<": lambda a, b: _shl(int(a), int(b)), ">>": lambda a, b: int(a) >> int(b),
    }

    def _c_bin(self, node) -> _C:
        _, op, a, b, pos = node
        ca, cb = self.compile(a), self.compile(b)
        for c in (ca, cb):
            if c.kinds and c.kinds <= {"str", "regex"}:
                raise QueryError("type_error", f"operator '{op}' needs numbers",
                                 pos=c.pos, name=c.name)
        f, fa, fb = self._ARITH[op], ca.fn, cb.fn

        def fn(t):
            x, y = _num(fa(t)), _num(fb(t))
            if x is MISSING or y is MISSING:
                return MISSING
            try:
                return f(x, y)
            except (ZeroDivisionError, ValueError, TypeError, OverflowError,
                    MemoryError):
                return MISSING
        lit = MISSING
        if ca.lit is not MISSING and cb.lit is not MISSING:
            lit = fn(None)
        return _C(fn, frozenset({"num"}), pos, lit=lit)

    def _check_operand_pair(self, op: str, field: _C, lit: _C) -> None:
        """Static checks for FIELD <op> LITERAL (§5.4)."""
        if lit.lit is MISSING or field.name is None:
            return
        v = lit.lit
        if field.name == "type" and isinstance(v, str) and op in ("==", "!="):
            if v not in self.schema.types:
                dym = difflib.get_close_matches(v, sorted(self.schema.types), n=3,
                                                cutoff=0.5)
                raise QueryError("unknown_type", f"no transaction type named '{v}'",
                                 name=v, pos=lit.pos, did_you_mean=dym or None,
                                 known_types=sorted(self.schema.types))
            return
        if field.name == "stream" and isinstance(v, str) and op in ("==", "!="):
            if v not in self.schema.stream_paths:
                hint = None
                if any(ch in v for ch in "*?["):
                    hint = f"use '~' for a glob: stream ~ '{v}'"
                dym = difflib.get_close_matches(v, list(self.schema.stream_paths), n=3,
                                                cutoff=0.5)
                raise QueryError("unknown_stream", f"no stream with path '{v}'",
                                 name=v, pos=lit.pos, did_you_mean=dym or None,
                                 hint=hint or "run `trlog streams TRACE` to list paths")
            return
        if not field.kinds or op in ("~", "=~"):
            return
        if isinstance(v, str):
            if "str" in field.kinds:
                return
            if "enum" in field.kinds:
                if field.enum_labels is not None and v not in field.enum_labels:
                    dym = difflib.get_close_matches(v, sorted(field.enum_labels), n=3,
                                                    cutoff=0.5)
                    raise QueryError("unknown_enum",
                                     f"'{v}' is not a value of enum field '{field.name}'",
                                     name=v, field=field.name, pos=lit.pos,
                                     did_you_mean=dym or None,
                                     valid_values=sorted(field.enum_labels))
                return
            raise QueryError("type_error", f"'{field.name}' is numeric but is compared "
                             f"with the string {v!r}", name=field.name, pos=lit.pos,
                             hint="drop the quotes to compare a number")
        if isinstance(v, (int, float)) and field.kinds == {"str"}:
            raise QueryError("type_error", f"'{field.name}' is a string but is compared "
                             f"with the number {v!r}", name=field.name, pos=lit.pos,
                             hint=f"quote the value: {field.name} == '{v}'")

    def _c_cmp(self, node) -> _C:
        _, op, a, b, pos = node
        ca, cb = self.compile(a), self.compile(b)
        self._check_operand_pair(op, ca, cb)
        self._check_operand_pair(op, cb, ca)
        fa, fb = ca.fn, cb.fn
        push = None
        if op in ("~", "=~"):
            if op == "~":
                if cb.lit is MISSING or not isinstance(cb.lit, str):
                    raise QueryError("syntax", "'~' needs a glob string on the right",
                                     pos=pos, hint="e.g. stream ~ 'top.*.mon'")
                if ca.name == "stream":
                    push = ("stream_glob", cb.lit)
            elif cb.lit is MISSING:
                raise QueryError("syntax", "'=~' needs a regex on the right",
                                 pos=pos, hint="e.g. msg =~ /timeout|err/")
            m = match_pred(op, cb.lit, cb.pos)
            return _C(lambda t: m(fa(t)), frozenset({"num"}), pos, pushable=push)
        if op == "==":
            fn = lambda t: _cmp_eq(fa(t), fb(t))
            if ca.name in ("stream", "type") and isinstance(cb.lit, str):
                push = (ca.name + "_eq", cb.lit)
        elif op == "!=":
            fn = lambda t: _cmp_ne(fa(t), fb(t))
        else:
            fn = lambda t: _cmp_ord(op, fa(t), fb(t))
        return _C(fn, frozenset({"num"}), pos, pushable=push)

    def _c_in(self, node) -> _C:
        _, x, items, negate, pos = node
        cx = self.compile(x)
        tests = []
        lits = []
        for it in items:
            if it[0] == "range":
                lo, hi = self.compile(it[1]), self.compile(it[2])
                for c in (lo, hi):
                    self._check_operand_pair("<=", cx, c)
                flo, fhi = lo.fn, hi.fn
                tests.append(lambda t, v, flo=flo, fhi=fhi:
                             _cmp_ord(">=", v, flo(t)) and _cmp_ord("<=", v, fhi(t)))
                lits.append(None)
            else:
                c = self.compile(it)
                self._check_operand_pair("==", cx, c)
                fi = c.fn
                tests.append(lambda t, v, fi=fi: _cmp_eq(v, fi(t)))
                lits.append(c.lit)
        fx = cx.fn

        def fn(t):
            v = fx(t)
            if v is MISSING:
                return False
            hit = any(test(t, v) for test in tests)
            return not hit if negate else hit
        push = None
        if not negate and cx.name in ("stream", "type") and all(
                isinstance(l, str) for l in lits):
            push = (cx.name + "_in", tuple(lits))
        return _C(fn, frozenset({"num"}), pos, pushable=push)

    def _c_not(self, node) -> _C:
        c = self.compile(node[1])
        f = c.fn
        return _C(lambda t: not f(t), frozenset({"num"}), node[2])

    def _c_and(self, node) -> _C:
        ca, cb = self.compile(node[1]), self.compile(node[2])
        fa, fb = ca.fn, cb.fn
        return _C(lambda t: bool(fa(t)) and bool(fb(t)), frozenset({"num"}), node[3])

    def _c_or(self, node) -> _C:
        ca, cb = self.compile(node[1]), self.compile(node[2])
        fa, fb = ca.fn, cb.fn
        return _C(lambda t: bool(fa(t)) or bool(fb(t)), frozenset({"num"}), node[3])


def _conjuncts(node) -> List[tuple]:
    if node[0] == "and":
        return _conjuncts(node[1]) + _conjuncts(node[2])
    return [node]


class Filter:
    """A compiled filter expression; built by :func:`compile_filter`.

    Attributes:
        text: The expression text.
        pushdown: Top-level conjuncts the engine can serve from the block
            index without evaluating the filter.
    """

    def __init__(self, text: str, fn: Callable, compiler: _Compiler,
                 pushdown: List[tuple]) -> None:
        self.text = text
        self._fn = fn
        self._compiler = compiler
        #: Conjuncts that the engine can serve from the block index:
        #: ("stream_eq", path) ("stream_in", paths) ("stream_glob", glob)
        #: ("type_eq", name) ("type_in", names)
        self.pushdown = pushdown

    @property
    def uses_parent(self) -> bool:
        return self._compiler.uses_parent

    @property
    def uses_meta(self) -> bool:
        return self._compiler.uses_meta

    @property
    def uses_links(self) -> bool:
        return self._compiler.uses_links

    @property
    def uses_children(self) -> bool:
        return self._compiler.uses_children

    def set_parent_lookup(self, lookup: Callable[[int], object]) -> None:
        """Set the function that maps a parent id to its transaction."""
        self._compiler.parent_lookup = lookup

    def match(self, txn) -> bool:
        """Evaluate the filter on one joined transaction.

        Filters that use ``parent.X`` need :meth:`set_parent_lookup` first.
        """
        return bool(self._fn(txn))


def compile_filter(text: str, schema: SchemaView) -> Filter:
    """Parse, check and compile a filter expression.

    Args:
        text: The expression, e.g. ``"resp != 'OKAY' && len > 1"``.
        schema: Names to resolve against, from :meth:`Trace.schema_view`.

    Returns:
        The compiled :class:`Filter`.

    Raises:
        QueryError: A syntax error, an unknown name or enum label, or a
            type mismatch, with the position in ``info["pos"]``.
    """
    ast = _Parser(text, schema.timescale_exp).parse()
    comp = _Compiler(schema, text)
    top = comp.compile(ast)
    pushdown = []
    for conj in _conjuncts(ast):
        c = comp.compile(conj)
        if c.pushable:
            pushdown.append(c.pushable)
    f = Filter(text, top.fn, comp, pushdown)
    f.ast = ast
    return f


class ValueExpr:
    """A compiled value expression (``stats``/``hist`` targets, ``match`` keys)."""

    def __init__(self, text: str, node: _C, compiler: _Compiler) -> None:
        self.text = text
        self._fn = node.fn
        self._compiler = compiler
        #: possible value kinds ("num", "str", "enum", "bytes"); empty = unknown
        self.kinds = node.kinds
        #: the builtin or field name when the expression is just a name
        self.name = node.name

    @property
    def uses_parent(self) -> bool:
        return self._compiler.uses_parent

    @property
    def uses_links(self) -> bool:
        return self._compiler.uses_links

    @property
    def uses_children(self) -> bool:
        return self._compiler.uses_children

    def set_parent_lookup(self, lookup: Callable[[int], object]) -> None:
        self._compiler.parent_lookup = lookup

    def __call__(self, txn):
        return self._fn(txn)


def compile_value(text: str, schema: SchemaView) -> ValueExpr:
    """Compile ``text`` as a value rather than a condition."""
    ast = _Parser(text, schema.timescale_exp).parse()
    comp = _Compiler(schema, text)
    vx = ValueExpr(text, comp.compile(ast), comp)
    vx.ast = ast
    return vx
