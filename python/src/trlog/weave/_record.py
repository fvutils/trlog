"""The ``trlog-record`` weaver: a recorder class per recorded SV class.

For each recorded class ``C`` of package ``p``, ``p_woven`` gets ``C_trl``:

- ``localparam int F_<field>``: each field's index, plus ``NF``, ``TYPE_NAME`` and
  ``SCHEMA_HASH``;
- ``type_()``: the trl_type, built and sealed once;
- ``set_fields(x, obj)``: sets every field of the open transaction ``x`` from ``obj``;
- ``record(s, obj, start, end_t, parent)``: records ``obj`` on stream ``s`` and returns
  the transaction ID (0 when ``s`` is off).

``C_trl`` needs no binding and exists in every profile. For a UVM class the weaver
also overrides ``do_record`` in the woven class: with a ``trl_uvm_pkg`` recorder it
records through ``C_trl`` (one typed transaction, no field-by-name calls); with any
other recorder it makes UVM's generic ``record_*`` calls, so the class records
everywhere without field macros.
The weave also writes ``trl_types.json`` (kind ``trlogTypes``), describing every
recorded type for the Python and YAML property front-ends.

Classes are recorded when marked ``(* trl_record *)`` or matched by the ``select``
option, an sv-weave match expression. Attributes on fields: ``trl_skip``,
``trl_name = "..."``. On classes: ``trl_name = "..."`` (the trace type name).
"""
import dataclasses as dc
import hashlib
import json
import re
from typing import Dict, List, Optional, Tuple

from sv_weave import plugin
from sv_weave.frontend.model import Visibility
from sv_weave.frontend.types import ArrayKind, TypeCat
from sv_weave.match import extends_root, has_attr, parse

TYPES_JSON = "trl_types.json"
TYPES_KIND = "trlogTypes"
TYPES_FORMAT = "0.1"

# Packages whose classes' fields are the library's own, not the user's.
DEFAULT_STOP_PACKAGES = ("uvm_pkg",)


@dc.dataclass
class TrlField:
    """One recorded field: its trace name, kind, SV bit width (integral kinds; 0
    otherwise) and the SV expression, relative to the object, that reads it."""
    name: str
    kind: str          # trl_field_type_e literal without the TRL_FT_ prefix, e.g. "U32"
    width: int
    expr: str          # e.g. "addr", "data[2]"
    setter: str        # "set_int", "set_real" or "set_str"
    enum: bool = False


def _int_kind(width: int, signed: bool) -> str:
    if width == 1 and not signed:
        return "BOOL"
    for w in (8, 16, 32, 64):
        if width <= w:
            return ("I%d" if signed else "U%d") % w
    raise ValueError(width)


UVM_ROOT = "uvm_pkg::uvm_object"


def scalar_kind(t) -> Tuple[Optional[str], int, str, str]:
    """(kind, width, setter, why-not) for an sv-weave SvType. kind is None when
    trl_pkg cannot record it yet; why-not then says why."""
    cat = t.cat
    if cat in (TypeCat.INTEGRAL, TypeCat.ENUM) or (cat in (TypeCat.STRUCT, TypeCat.UNION)
                                                   and t.packed):
        if t.width > 64:
            return None, 0, "", "wider than 64 bits (needs set_bits, not in trl_pkg yet)"
        if cat == TypeCat.INTEGRAL and (t.spelling or "") == "time":
            return "TIME", 64, "set_int", ""
        return _int_kind(t.width, t.signed), t.width, "set_int", ""
    if cat == TypeCat.REAL:
        return ("F32" if t.width == 32 else "F64"), 0, "set_real", ""
    if cat == TypeCat.STRING:
        return "STRING", 0, "set_str", ""
    if cat in (TypeCat.STRUCT, TypeCat.UNION):
        return None, 0, "", "an unpacked struct or union (not flattened yet)"
    if cat == TypeCat.CLASS:
        return None, 0, "", "a class handle (not flattened yet)"
    if cat == TypeCat.ARRAY:
        what = {ArrayKind.DYNAMIC: "a dynamic array", ArrayKind.QUEUE: "a queue",
                ArrayKind.ASSOC: "an associative array"}.get(t.array_kind, "an array")
        return None, 0, "", what + " (not recorded by trl_pkg yet)"
    return None, 0, "", "of a type trlog cannot record (%s)" % (t.text or cat.value)


_DIM = re.compile(r"^\[(-?\d+):(-?\d+)\]$")


def _fixed_indices(t) -> Optional[List[int]]:
    """Indices of a 1-D fixed unpacked array, left to right; None otherwise."""
    if t.cat != TypeCat.ARRAY or t.array_kind != ArrayKind.FIXED or t.elem is None:
        return None
    if t.elem.unpacked:
        return None
    m = _DIM.match(t.unpacked or "")
    if m is None:
        return None
    lo, hi = int(m.group(1)), int(m.group(2))
    step = 1 if hi >= lo else -1
    return list(range(lo, hi + step, step))


def layout_fields(fields, warn=None) -> List[TrlField]:
    """The recorded fields of a class, from sv-weave ``SvField``s (root-most first).
    ``warn(msg, loc)`` reports a field that is left out."""
    def skip(f, why):
        if warn is not None:
            warn("%s.%s is %s; not recorded (mark it (* trl_skip *) to silence this)"
                 % (f.owner.qname, f.name, why), f.loc)

    out: List[TrlField] = []
    for f in fields:
        if f.static or "trl_skip" in f.attrs:
            continue
        if f.visibility != Visibility.PUBLIC:
            skip(f, "%s, so the recorder cannot read it" % f.visibility.value)
            continue
        name = f.attrs.get("trl_name")
        name = name if isinstance(name, str) and name else f.name
        if f.attrs.get("trl_kind") is not None:
            skip(f, "marked trl_kind='%s', which trl_pkg cannot record yet"
                 % f.attrs.get("trl_kind"))
            continue
        idx = _fixed_indices(f.type)
        if idx is not None:
            kind, width, setter, why = scalar_kind(f.type.elem)
            if kind is None:
                skip(f, "an array whose elements are " + why)
                continue
            out.extend(TrlField("%s[%d]" % (name, i), kind, width, "%s[%d]" % (f.name, i), setter,
                                f.type.elem.cat == TypeCat.ENUM) for i in idx)
            continue
        kind, width, setter, why = scalar_kind(f.type)
        if kind is None:
            skip(f, why)
            continue
        out.append(TrlField(name, kind, width, f.name, setter, f.type.cat == TypeCat.ENUM))
    return out


def schema_hash(type_name: str, fields: List[TrlField]) -> str:
    """64-bit hash (16 hex digits) over the type name and each field's name, kind and
    width. Property sets built against one generation check it at bind."""
    h = hashlib.sha256(type_name.encode())
    for f in fields:
        h.update(b"\0%s\0%s\0%d" % (f.name.encode(), f.kind.encode(), f.width))
    return h.hexdigest()[:16]


def _ident(text: str) -> str:
    return re.sub(r"[^A-Za-z0-9_]", "_", text)


def _param_names(fields: List[TrlField]) -> List[str]:
    """``F_<name>`` per field, made unique."""
    seen: Dict[str, int] = {}
    out = []
    for f in fields:
        base = "F_" + _ident(f.name).rstrip("_")
        n = seen.get(base, 0)
        seen[base] = n + 1
        out.append(base if n == 0 else "%s_%d" % (base, n))
    return out


def _q(s: str) -> str:
    return '"%s"' % s.replace("\\", "\\\\").replace('"', '\\"')


def recorder_name(cls) -> str:
    """``C_trl``, or ``G_<hash>_trl`` for a specialization of ``G``."""
    if cls.is_specialization:
        return "%s_%s_trl" % (cls.generic.name, hashlib.sha1(cls.qname.encode()).hexdigest()[:5])
    return cls.name + "_trl"


def type_name(cls) -> str:
    n = cls.attrs.get("trl_name")
    return n if isinstance(n, str) and n else cls.name


def render_recorder(cls, tname: str, fields: List[TrlField], digest: str) -> str:
    rec = recorder_name(cls)
    params = _param_names(fields)
    obj_t = cls.type_expr or cls.qname
    L = ["class %s;" % rec,
         "  // Recorder for %s, generated by trlog-record." % cls.qname,
         "  localparam string TYPE_NAME = %s;" % _q(tname),
         "  localparam longint unsigned SCHEMA_HASH = 64'h%s;" % digest,
         "  localparam int NF = %d;" % len(fields)]
    for i, (p, f) in enumerate(zip(params, fields)):
        L.append("  localparam int %s = %d;  // %s : %s" % (p, i, f.name, f.kind))
    L += ["",
          "  local static trl_pkg::trl_type m_type;",
          "  local static trl_pkg::trl_txn  m_txn;",
          "",
          "  // The trace type, built on first use; null when recording is off.",
          "  static function trl_pkg::trl_type type_();",
          "    if (m_type == null) begin",
          "      m_type = trl_pkg::trl_type::create(TYPE_NAME);",
          "      if (m_type == null) return null;"]
    for f in fields:
        L.append("      void'(m_type.add_field(%s, trl_pkg::TRL_FT_%s));" % (_q(f.name), f.kind))
    L += ["      m_type.seal();",
          "    end",
          "    return m_type;",
          "  endfunction",
          "",
          "  // Sets every field of the open transaction x from obj.",
          "  static function void set_fields(trl_pkg::trl_txn x, %s obj);" % obj_t]
    for p, f in zip(params, fields):
        L.append("    x.%s(%s, obj.%s);" % (f.setter, p, f.expr))
    L += ["  endfunction",
          "",
          "  // Records obj on s; returns the transaction ID, or 0 when s is off.",
          "  static function longint unsigned record(trl_pkg::trl_stream s, %s obj," % obj_t,
          "      longint unsigned start = trl_pkg::trl_now(),",
          "      longint unsigned end_t = trl_pkg::trl_now(),",
          "      longint unsigned parent = 0);",
          "    trl_pkg::trl_txn x;",
          "    if (s == null || !s.en || obj == null) return 0;",
          "    if (m_txn == null) m_txn = new;",
          "    x = s.open_txn(type_(), parent, start, m_txn);"]
    L += ["    set_fields(x, obj);",
          "    record = x.id();",
          "    x.close(end_t);",
          "  endfunction",
          "endclass : %s" % rec]
    return "\n".join(L)


def generic_record_stmt(f: TrlField, r: str) -> str:
    """The UVM ``record_*`` call that records ``f`` (read from ``this``) on ``r``."""
    if f.setter == "set_str":
        return "%s.record_string(%s, %s);" % (r, _q(f.name), f.expr)
    if f.setter == "set_real":
        return "%s.record_field_real(%s, %s);" % (r, _q(f.name), f.expr)
    if f.enum:
        return "%s.record_string(%s, %s.name());" % (r, _q(f.name), f.expr)
    if f.kind == "TIME":
        return "%s.record_time(%s, %s);" % (r, _q(f.name), f.expr)
    radix = "UVM_DEC" if f.kind.startswith("I") else "UVM_NORADIX"
    return "%s.record_field_int(%s, %s, %d, uvm_pkg::%s);" % (r, _q(f.name), f.expr, f.width,
                                                              radix)


def render_do_record(cls, fields: List[TrlField], r: str) -> str:
    """The body of the woven ``do_record(uvm_recorder <r>)``: the typed path for a
    ``trl_uvm_recorder``, else ``super`` plus one generic call per field."""
    rec = recorder_name(cls)
    L = ["begin",
         "  trl_uvm_pkg::trl_uvm_recorder __trl_r;",
         "  trl_pkg::trl_txn __trl_x;",
         "  if ($cast(__trl_r, %s)) __trl_x = __trl_r.open_typed(%s::type_());" % (r, rec),
         "  if (__trl_x != null) begin",
         "    %s::set_fields(__trl_x, this);" % rec,
         "    return;",
         "  end",
         "end",
         "super.do_record(%s);" % r]
    L += [generic_record_stmt(f, r) for f in fields]
    return "\n".join(L)


def field_source(cls, roots, stop_packages):
    """The SvFields a class records: up to the first ancestor in a stop package or
    that is a binding root (exclusive)."""
    for a in cls.ancestors():
        if a.package in stop_packages or a.qname in roots:
            return cls.all_fields(upto=a)
    return cls.all_fields()


class RecordWeaver(plugin.Weaver):
    """Options: ``select`` (match expression: classes to record besides those marked
    ``(* trl_record *)``), ``stop_packages`` (packages whose fields are not recorded;
    default ``[uvm_pkg]``)."""
    name = "trlog-record"
    version = "0.1"

    def subscribe(self, bus):
        bus.on_design(self.design)
        bus.on(extends_root() & self._selector(), self.weave, needs_binding=False)

    def _selector(self):
        pred = has_attr("trl_record")
        sel = self.options.get("select")
        if sel:
            pred = pred | parse(str(sel), "trlog-record select")
        return pred

    def _stop(self):
        return set(self.options.get("stop_packages", DEFAULT_STOP_PACKAGES))

    def _fields(self, d, cls):
        b = d.binding_of(cls)
        return field_source(cls, set(b.roots) if b is not None else set(), self._stop())

    def weave(self, w):
        """Per woven class: the UVM ``do_record`` override."""
        cls = w.cls
        if cls.is_interface or not cls.is_a(UVM_ROOT) or cls.find_method("do_record") is None:
            return
        fields = layout_fields(field_source(cls, set(w.binding.roots), self._stop()))
        slot = w.override("do_record")
        slot.replace_super()
        slot.body.add(render_do_record(cls, fields, slot.sig.arg_names[0]))

    def design(self, d):
        pred = self._selector()
        classes = [c for c in d.model.classes(pred) if not c.is_interface]
        types = []
        names: Dict[str, str] = {}
        for cls in classes:
            fields = layout_fields(self._fields(d, cls), d.warning)
            tname = type_name(cls)
            if tname in names:
                d.error("%s and %s are both recorded as type '%s'; give one a "
                        "(* trl_name = \"...\" *)" % (names[tname], cls.qname, tname))
                continue
            names[tname] = cls.qname
            digest = schema_hash(tname, fields)
            d.add_class(cls.package, render_recorder(cls, tname, fields, digest),
                        imports=["trl_pkg"])
            types.append({"name": tname, "sv_class": cls.qname,
                          "recorder": "%s_woven::%s" % (cls.package, recorder_name(cls)),
                          "schema_hash": digest,
                          "fields": [{"name": f.name, "kind": f.kind, "width": f.width}
                                     for f in fields]})
        if types:
            d.add_artifact(TYPES_JSON, json.dumps({"trlog_types": TYPES_FORMAT,
                                                   "types": types}, indent=2) + "\n",
                           TYPES_KIND)
