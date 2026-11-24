"""Value model shared by the expression evaluator, the engine and the renderers.

Decoded field values are normalised into a small set of Python types:

- ``int`` / ``float`` / ``bool`` / ``str`` / ``bytes`` as decoded;
- bitvecs and 2-state logicvecs become plain ``int``;
- enum values become :class:`EnumVal`, an ``int`` that also carries its label;
- logicvecs with x/z bits become :class:`LogicVal`;
- an absent value is the :data:`MISSING` singleton (query_cli_design.md §5.3).
"""

from __future__ import annotations

from decimal import Decimal
from fractions import Fraction
from typing import Optional, Tuple


class _Missing:
    """Absent value. Falsy; every comparison against it is false."""

    __slots__ = ()
    _inst = None

    def __new__(cls):
        if cls._inst is None:
            cls._inst = super().__new__(cls)
        return cls._inst

    def __bool__(self) -> bool:
        return False

    def __repr__(self) -> str:
        return "MISSING"


MISSING = _Missing()
"""The absent value: a field the transaction does not have, an unset field,
or ``dur`` of an open transaction. Falsy; every comparison against it is
false."""


class EnumVal(int):
    """An enum field value: compares as its integer, displays as its label.

    Attributes:
        label: The enum label, or None when the value has no label.
        labels: The field's ``{value: label}`` table, or None. Ordering an
            enum value against a label (``resp >= 'SLVERR'``) looks the
            label up here and compares values.
    """

    def __new__(cls, value: int, label: Optional[str], labels: Optional[dict] = None):
        obj = int.__new__(cls, value)
        obj.label = label
        obj.labels = labels
        return obj

    def value_of(self, label: str) -> Optional[int]:
        """The value of ``label`` in this field's enum, or None."""
        if self.labels:
            for v, l in self.labels.items():
                if l == label:
                    return v
        return None

    def __repr__(self) -> str:
        return f"EnumVal({int(self)}, {self.label!r})"


class LogicVal:
    """A 4-state vector that has at least one x or z bit.

    Encoding follows the VPI aval/bval convention: (a=0,b=0) is 0, (1,0) is 1,
    (0,1) is z and (1,1) is x. In filter expressions it never compares equal
    to anything, and arithmetic and ordering on it yield MISSING.

    Attributes:
        width: Width in bits.
        aval: The a bits.
        bval: The b bits; a 1 marks an x or z bit.
    """

    __slots__ = ("width", "aval", "bval")

    def __init__(self, width: int, aval: int, bval: int) -> None:
        self.width = width
        self.aval = aval
        self.bval = bval

    def __eq__(self, other) -> bool:
        return (isinstance(other, LogicVal) and self.width == other.width
                and self.aval == other.aval and self.bval == other.bval)

    def __hash__(self) -> int:
        return hash((self.width, self.aval, self.bval))

    def hex(self) -> str:
        """Hex digits, with ``x``/``z`` for nibbles that are all x or all z,
        and ``X`` for a nibble that mixes known and unknown bits."""
        ndig = max(1, (self.width + 3) // 4)
        out = []
        for i in reversed(range(ndig)):
            a = (self.aval >> (4 * i)) & 0xF
            b = (self.bval >> (4 * i)) & 0xF
            if b == 0:
                out.append("%x" % a)
            elif b == 0xF and a == 0xF:
                out.append("x")
            elif b == 0xF and a == 0:
                out.append("z")
            else:
                out.append("X")
        return "0x" + "".join(out)

    def __repr__(self) -> str:
        return f"LogicVal({self.hex()})"


def normalize_field(kind: str, raw, enum_labels: Optional[dict] = None):
    """Turn a decoded field value into the query value model."""
    if kind == "bitvec":
        return raw[1] if isinstance(raw, tuple) else raw
    if kind == "logicvec":
        if isinstance(raw, tuple):
            width, aval, bval = raw
            if bval == 0:
                return aval
            return LogicVal(width, aval, bval)
        return raw
    if kind == "enum":
        label = enum_labels.get(raw) if enum_labels else None
        return EnumVal(raw, label, enum_labels)
    return raw


# ---------------------------------------------------------------------------
# Time units
# ---------------------------------------------------------------------------

UNIT_EXP = {"fs": -15, "ps": -12, "ns": -9, "us": -6, "ms": -3, "s": 0}
_EXP_UNIT = {v: k for k, v in UNIT_EXP.items()}


def time_unit_name(timescale_exp: int) -> str:
    """``-9`` -> ``"ns"``; ``-10`` -> ``"100ps"``."""
    base = (timescale_exp // 3) * 3
    if base < -15:
        base = -15
    if base > 0:
        base = 0
    mult = 10 ** (timescale_exp - base)
    unit = _EXP_UNIT[base]
    return unit if mult == 1 else f"{mult}{unit}"


def to_ticks(value: Fraction, unit_exp: int, timescale_exp: int):
    """Convert ``value`` in unit 10^unit_exp to trace ticks.

    Returns an int when exact, otherwise a float.
    """
    ticks = Fraction(value) * Fraction(10) ** (unit_exp - timescale_exp)
    if ticks.denominator == 1:
        return int(ticks)
    return float(ticks)


def format_time(ticks: int, timescale_exp: int, decimals: int = 3,
                strip: bool = False) -> str:
    """Format trace ticks in a human unit, e.g. ``15.200us``.

    The unit is the largest one in which the value is at least 1.

    Args:
        ticks: Time in ticks.
        timescale_exp: The trace's time unit as a power of ten in seconds
            (``Trace.timescale_exp``).
        decimals: Digits after the point.
        strip: Remove trailing zeros (and a trailing point).

    Returns:
        The formatted time, e.g. ``format_time(45500, -12)`` is
        ``"45.500ns"``.
    """
    if ticks == 0:
        return "0" + _EXP_UNIT[max(-15, min(0, (timescale_exp // 3) * 3))]
    neg = ticks < 0
    mag = Decimal(abs(ticks)).scaleb(timescale_exp)   # seconds
    unit_exp = -15
    for e in (0, -3, -6, -9, -12, -15):
        if mag >= Decimal(1).scaleb(e):
            unit_exp = e
            break
    v = mag.scaleb(-unit_exp)
    s = f"{v:.{decimals}f}"
    if strip and "." in s:
        s = s.rstrip("0").rstrip(".")
    return ("-" if neg else "") + s + _EXP_UNIT[unit_exp]


def parse_time(text: str, timescale_exp: int):
    """Parse a time into ticks.

    Args:
        text: A number with a unit (``10us``, ``1.5ns``; units ``fs`` to
            ``s``), or an integer tick count (``15200000``, ``0x100``).
        timescale_exp: The trace's time unit as a power of ten in seconds.

    Returns:
        Ticks, as an int when exact, else as a float.

    Raises:
        ValueError: The text is not a time.
    """
    t = text.strip()
    for unit in ("fs", "ps", "ns", "us", "ms", "s"):
        if t.endswith(unit):
            num = t[: -len(unit)].strip()
            try:
                return to_ticks(Fraction(num), UNIT_EXP[unit], timescale_exp)
            except (ValueError, ZeroDivisionError):
                raise ValueError(f"bad time value {text!r}")
    try:
        return int(t, 0)
    except ValueError:
        raise ValueError(
            f"bad time value {text!r}: use an integer tick count or a number "
            "with a unit (fs ps ns us ms s)")


def parse_window(text: str, timescale_exp: int) -> Tuple[Optional[int], Optional[int]]:
    """Parse a ``-t`` window: ``A..B``, ``..B``, ``A..``, ``@T`` or ``@T±W``.

    ``@T+-W`` is accepted for ``@T±W``. Times are as for :func:`parse_time`.
    Each bound must be a whole number of ticks of the trace's time unit:
    ``1.5ps`` in a ps trace is an error rather than being truncated or
    rounded, since either would silently move the window.

    Args:
        text: The window.
        timescale_exp: The trace's time unit as a power of ten in seconds.

    Returns:
        ``(lo, hi)`` in ticks, inclusive; None for an open side.

    Raises:
        ValueError: The text is not a window, or a bound is not a whole
            number of ticks.
    """
    def ticks(v) -> int:
        if v != int(v):
            raise ValueError(
                f"bad time window {text!r}: {v:g} ticks is not a whole number of "
                f"ticks (the trace's time unit is 1e{timescale_exp} s)")
        return int(v)

    t = text.strip()
    if t.startswith("@"):
        body = t[1:]
        for sep in ("±", "+-"):
            if sep in body:
                center, width = body.split(sep, 1)
                c = parse_time(center, timescale_exp)
                w = parse_time(width, timescale_exp)
                c, w = ticks(c), ticks(w)
                return (max(0, c - w), c + w)
        c = ticks(parse_time(body, timescale_exp))
        return (c, c)
    if ".." not in t:
        raise ValueError(
            f"bad time window {text!r}: use A..B, ..B, A.., @T or @T±W "
            "(for example 10us..20us)")
    lo, hi = t.split("..", 1)
    a = ticks(parse_time(lo, timescale_exp)) if lo.strip() else None
    b = ticks(parse_time(hi, timescale_exp)) if hi.strip() else None
    return (a, b)
