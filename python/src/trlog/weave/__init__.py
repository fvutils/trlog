"""trlog's sv-weave plugin (``trlog[weave]``): generates transaction recorders for
SystemVerilog classes. See ``research/sv_gen.md``.

Importing this package needs sv-weave; the rest of trlog does not.
"""
from ._record import RecordWeaver, TrlField, layout_fields, schema_hash

__all__ = ["RecordWeaver", "TrlField", "layout_fields", "schema_hash"]
