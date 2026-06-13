"""Hierarchy block encode/decode.

Each BLK_HIERARCHY block describes one named hierarchy tree identified by
``hier_id``.  The payload is always compressed (ZLib by default; Zstd optional).
"""

from __future__ import annotations

import struct
import zlib
from typing import Dict, List, Optional, Tuple

from ._codec import encode_uvarint, decode_uvarint
from ._string_table import _make_block
from ._types import (
    BlockType, FLAG_COMPRESSED, FLAG_COMPRESS_ALG, BLOCK_HEADER_SIZE,
    HierKind, ScopeType, VarDir, HierTag,
    HierarchyHeader, HScope, HVar, HStream, HAttr,
)
from ._exceptions import ZstFormatError


class HierarchyBlock:
    """Builds or holds a single named hierarchy tree."""

    def __init__(
        self,
        hier_id: int = 1,
        kind: HierKind = HierKind.HK_DESIGN,
        name_str_id: int = 0,
    ) -> None:
        self.header = HierarchyHeader(hier_id=hier_id, kind=kind, name_str_id=name_str_id)
        self._scope_stack: List[HScope] = []
        self._root_children: list = []
        self._next_var_id    = 1
        self._next_stream_id = 1
        # flat maps for easy lookup after decode
        self.vars:    Dict[int, HVar]    = {}
        self.streams: Dict[int, HStream] = {}
        # tracks the most recently declared node for H_ATTR attachment
        self._last_node = None

    @property
    def hier_id(self) -> int:
        return self.header.hier_id

    # ------------------------------------------------------------------
    # Builder API
    # ------------------------------------------------------------------

    def begin_scope(
        self,
        scope_type: ScopeType,
        name_str_id: int,
        component_str_id: int = 0,
        src_file_str_id: int = 0,
        src_line: int = 0,
    ) -> HScope:
        scope = HScope(
            scope_type=scope_type,
            name_str_id=name_str_id,
            component_str_id=component_str_id,
            src_file_str_id=src_file_str_id,
            src_line=src_line,
        )
        if self._scope_stack:
            self._scope_stack[-1].children.append(scope)
        else:
            self._root_children.append(scope)
        self._scope_stack.append(scope)
        self._last_node = scope
        return scope

    def end_scope(self) -> None:
        if not self._scope_stack:
            raise ZstFormatError("H_UPSCOPE without matching H_SCOPE")
        self._scope_stack.pop()
        self._last_node = self._scope_stack[-1] if self._scope_stack else None

    def add_var(
        self,
        name_str_id: int,
        sig_type_id: int,
        var_dir: VarDir = VarDir.VD_NONE,
        alias_of_var_id: int = 0,
        src_file_str_id: int = 0,
        src_line: int = 0,
    ) -> int:
        var_id = self._next_var_id
        self._next_var_id += 1
        var = HVar(
            var_id=var_id,
            name_str_id=name_str_id,
            sig_type_id=sig_type_id,
            var_dir=var_dir,
            alias_of_var_id=alias_of_var_id,
            src_file_str_id=src_file_str_id,
            src_line=src_line,
        )
        self.vars[var_id] = var
        if self._scope_stack:
            self._scope_stack[-1].children.append(var)
        else:
            self._root_children.append(var)
        self._last_node = var
        return var_id

    def add_stream(self, stream_type_id: int, name_str_id: int) -> int:
        inst_id = self._next_stream_id
        self._next_stream_id += 1
        stream = HStream(
            stream_inst_id=inst_id,
            stream_type_id=stream_type_id,
            name_str_id=name_str_id,
        )
        self.streams[inst_id] = stream
        if self._scope_stack:
            self._scope_stack[-1].children.append(stream)
        else:
            self._root_children.append(stream)
        self._last_node = stream
        return inst_id

    def add_attr(self, key_str_id: int, value_str_id: int) -> None:
        """Attach an attribute to the most recently declared scope/var/stream."""
        attr = HAttr(key_str_id=key_str_id, value_str_id=value_str_id)
        if self._last_node is None:
            raise ZstFormatError("H_ATTR without any enclosing scope/var/stream")
        self._last_node.attrs.append(attr)

    def add_typed_attr(self, attr) -> None:
        """Attach a typed transparent attr (H_ATTR2, §2.3) to the last node."""
        if self._last_node is None:
            raise ZstFormatError("H_ATTR2 without any enclosing scope/var/stream")
        self._last_node.typed_attrs.append(attr)

    # ------------------------------------------------------------------
    # Block serialisation
    # ------------------------------------------------------------------

    def encode_block(self, compress: bool = True, use_zstd: bool = False) -> bytes:
        """Encode this hierarchy as a BLK_HIERARCHY block."""
        inner = self._encode_inner()
        flags = 0
        if compress:
            uncompressed_len = len(inner)
            if use_zstd:
                try:
                    import zstandard as zstd  # type: ignore
                    compressed = zstd.ZstdCompressor().compress(inner)
                    flags = FLAG_COMPRESSED | FLAG_COMPRESS_ALG
                except ImportError:
                    raise RuntimeError("zstandard is not installed")
            else:
                compressed = zlib.compress(inner)
                flags = FLAG_COMPRESSED
            # Prepend the uncompressed_length field (u64 LE) before the payload
            payload = struct.pack('<Q', uncompressed_len) + compressed
        else:
            payload = inner

        return _make_block(BlockType.BLK_HIERARCHY, flags, payload)

    def read_block(self, payload: bytes | memoryview, flags: int = 0) -> None:
        """Decode a BLK_HIERARCHY payload into this block's data structures."""
        inner = self._decompress(payload, flags)
        self._decode_inner(inner)

    def read_block_v1(self, payload: bytes | memoryview, flags: int = 0) -> None:
        """Decode a v1-format BLK_HIERARCHY payload (no hierarchy header).

        The caller is responsible for supplying the correct ``hier_id`` and
        ``kind`` defaults (typically ``hier_id=1, kind=HK_DESIGN``).
        """
        inner = self._decompress(payload, flags)
        self._decode_tag_sequence(inner, offset=0)

    def _decompress(self, payload: bytes | memoryview, flags: int) -> bytes:
        if flags & FLAG_COMPRESSED:
            compressed = bytes(payload[8:])
            if flags & FLAG_COMPRESS_ALG:
                import zstandard as zstd  # type: ignore
                return zstd.ZstdDecompressor().decompress(compressed)
            else:
                return zlib.decompress(compressed)
        return bytes(payload)

    # ------------------------------------------------------------------
    # Internal encode
    # ------------------------------------------------------------------

    def _encode_inner(self) -> bytes:
        out = bytearray()
        # Hierarchy header
        out += encode_uvarint(self.header.hier_id)
        out.append(int(self.header.kind))
        out += encode_uvarint(self.header.name_str_id)
        # Tag sequence
        for child in self._root_children:
            out += self._encode_node(child)
        return bytes(out)

    def _encode_node(self, node) -> bytes:
        out = bytearray()
        if isinstance(node, HScope):
            out.append(HierTag.H_SCOPE)
            out.append(int(node.scope_type))
            out += encode_uvarint(node.name_str_id)
            out += encode_uvarint(node.component_str_id)
            out += encode_uvarint(node.src_file_str_id)
            out += encode_uvarint(node.src_line)
            for attr in node.attrs:
                out += self._encode_attr(attr)
            out += self._encode_typed_attrs(node)
            for child in node.children:
                out += self._encode_node(child)
            out.append(HierTag.H_UPSCOPE)
        elif isinstance(node, HVar):
            out.append(HierTag.H_VAR)
            out += encode_uvarint(node.var_id)
            out += encode_uvarint(node.name_str_id)
            out += encode_uvarint(node.sig_type_id)
            out.append(int(node.var_dir))
            out += encode_uvarint(node.alias_of_var_id)
            out += encode_uvarint(node.src_file_str_id)
            out += encode_uvarint(node.src_line)
            for attr in node.attrs:
                out += self._encode_attr(attr)
            out += self._encode_typed_attrs(node)
        elif isinstance(node, HStream):
            out.append(HierTag.H_STREAM)
            out += encode_uvarint(node.stream_inst_id)
            out += encode_uvarint(node.stream_type_id)
            out += encode_uvarint(node.name_str_id)
            for attr in node.attrs:
                out += self._encode_attr(attr)
            out += self._encode_typed_attrs(node)
        return bytes(out)

    def _encode_typed_attrs(self, node) -> bytes:
        from ._metadata import encode_typed_attr
        out = bytearray()
        for ta in getattr(node, "typed_attrs", ()):  # H_ATTR2 (§2.3)
            out.append(HierTag.H_ATTR2)
            out += encode_typed_attr(ta)
        return bytes(out)

    def _encode_attr(self, attr: HAttr) -> bytes:
        out = bytearray()
        out.append(HierTag.H_ATTR)
        out += encode_uvarint(attr.key_str_id)
        out += encode_uvarint(attr.value_str_id)
        return bytes(out)

    # ------------------------------------------------------------------
    # Internal decode
    # ------------------------------------------------------------------

    def _decode_inner(self, data: bytes) -> None:
        offset = 0
        # Hierarchy header
        hier_id, offset    = decode_uvarint(data, offset)
        kind               = HierKind(data[offset]); offset += 1
        name_str_id, offset = decode_uvarint(data, offset)
        self.header = HierarchyHeader(hier_id=hier_id, kind=kind, name_str_id=name_str_id)
        self._decode_tag_sequence(data, offset)

    def _decode_tag_sequence(self, data: bytes, offset: int) -> None:
        scope_stack: List[HScope] = []

        def current_children():
            return scope_stack[-1].children if scope_stack else self._root_children

        def last_node():
            children = current_children()
            if children:
                return children[-1]
            if scope_stack:
                return scope_stack[-1]
            return None

        while offset < len(data):
            tag = data[offset]; offset += 1

            if tag == HierTag.H_SCOPE:
                scope_type = ScopeType(data[offset]); offset += 1
                name_sid,      offset = decode_uvarint(data, offset)
                comp_sid,      offset = decode_uvarint(data, offset)
                file_sid,      offset = decode_uvarint(data, offset)
                src_line,      offset = decode_uvarint(data, offset)
                scope = HScope(
                    scope_type=scope_type,
                    name_str_id=name_sid,
                    component_str_id=comp_sid,
                    src_file_str_id=file_sid,
                    src_line=src_line,
                )
                current_children().append(scope)
                scope_stack.append(scope)

            elif tag == HierTag.H_UPSCOPE:
                if not scope_stack:
                    raise ZstFormatError("H_UPSCOPE without matching H_SCOPE")
                scope_stack.pop()

            elif tag == HierTag.H_VAR:
                var_id,         offset = decode_uvarint(data, offset)
                name_sid,       offset = decode_uvarint(data, offset)
                sig_type_id,    offset = decode_uvarint(data, offset)
                var_dir        = VarDir(data[offset]); offset += 1
                alias_id,       offset = decode_uvarint(data, offset)
                file_sid,       offset = decode_uvarint(data, offset)
                src_line,       offset = decode_uvarint(data, offset)
                var = HVar(
                    var_id=var_id,
                    name_str_id=name_sid,
                    sig_type_id=sig_type_id,
                    var_dir=var_dir,
                    alias_of_var_id=alias_id,
                    src_file_str_id=file_sid,
                    src_line=src_line,
                )
                current_children().append(var)
                self.vars[var_id] = var

            elif tag == HierTag.H_STREAM:
                inst_id,        offset = decode_uvarint(data, offset)
                stream_type_id, offset = decode_uvarint(data, offset)
                name_sid,       offset = decode_uvarint(data, offset)
                stream = HStream(
                    stream_inst_id=inst_id,
                    stream_type_id=stream_type_id,
                    name_str_id=name_sid,
                )
                current_children().append(stream)
                self.streams[inst_id] = stream

            elif tag == HierTag.H_ATTR:
                key_sid,   offset = decode_uvarint(data, offset)
                val_sid,   offset = decode_uvarint(data, offset)
                attr = HAttr(key_str_id=key_sid, value_str_id=val_sid)
                # Attach to the last node (scope, var, or stream) in scope or root
                node = last_node()
                if node is None:
                    raise ZstFormatError("H_ATTR without any enclosing node")
                if isinstance(node, HScope) and scope_stack and scope_stack[-1] is node:
                    # Attr directly on scope (before its children)
                    node.attrs.append(attr)
                else:
                    node.attrs.append(attr)

            elif tag == HierTag.H_ATTR2:
                from ._metadata import decode_typed_attr
                ta, offset = decode_typed_attr(data, offset)
                node = last_node()
                if node is None:
                    raise ZstFormatError("H_ATTR2 without any enclosing node")
                node.typed_attrs.append(ta)
            else:
                # Unknown tag — stop (cannot know size)
                break
