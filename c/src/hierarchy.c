#include "varint_internal.h"

static trl_status_t trl_hierarchy_ensure_var_cap(trl_hierarchy_t *hier, size_t need) {
    if (need <= hier->var_sig_cap) {
        return TRL_OK;
    }
    size_t new_cap = hier->var_sig_cap ? hier->var_sig_cap : 16;
    while (new_cap < need) {
        new_cap *= 2;
    }
    uint32_t *n = (uint32_t *)realloc(hier->var_sig_type_ids, new_cap * sizeof(uint32_t));
    if (!n) {
        return TRL_ERR_OOM;
    }
    memset(n + hier->var_sig_cap, 0, (new_cap - hier->var_sig_cap) * sizeof(uint32_t));
    hier->var_sig_type_ids = n;
    hier->var_sig_cap = new_cap;
    return TRL_OK;
}

void trl_hierarchy_init(trl_hierarchy_t *hier) {
    memset(hier, 0, sizeof(*hier));
    trl_buf_init(&hier->body);
    /* Var and stream IDs are numbered across all hierarchies of a writer,
     * so trl_hierarchy_begin does not reset them. */
    hier->next_var_id = 1;
    hier->next_stream_id = 1;
}

void trl_hierarchy_free(trl_hierarchy_t *hier) {
    trl_buf_free(&hier->body);
    free(hier->var_sig_type_ids);
    memset(hier, 0, sizeof(*hier));
}

trl_status_t trl_hierarchy_begin(trl_hierarchy_t *hier, uint32_t hier_id, uint8_t kind, uint32_t name_id) {
    trl_buf_free(&hier->body);
    trl_buf_init(&hier->body);
    hier->hier_id = hier_id;
    hier->kind = kind;
    hier->name_id = name_id;
    hier->scope_depth = 0;
    return TRL_OK;
}

trl_status_t trl_hierarchy_begin_scope(trl_hierarchy_t *hier, uint8_t scope_type, uint32_t name_id, uint32_t component_id,
                                       uint32_t src_file_str_id, uint32_t src_line) {
    trl_status_t st = trl_buf_append_u8(&hier->body, TRL_H_SCOPE);
    if (st == TRL_OK) st = trl_buf_append_u8(&hier->body, scope_type);
    if (st == TRL_OK) st = trl_buf_append_uvarint(&hier->body, name_id);
    if (st == TRL_OK) st = trl_buf_append_uvarint(&hier->body, component_id);
    if (st == TRL_OK) st = trl_buf_append_uvarint(&hier->body, src_file_str_id);
    if (st == TRL_OK) st = trl_buf_append_uvarint(&hier->body, src_line);
    if (st == TRL_OK) {
        hier->scope_depth++;
    }
    return st;
}

trl_status_t trl_hierarchy_end_scope(trl_hierarchy_t *hier) {
    if (hier->scope_depth <= 0) {
        return TRL_ERR_CORRUPT;
    }
    hier->scope_depth--;
    return trl_buf_append_u8(&hier->body, TRL_H_UPSCOPE);
}

uint32_t trl_hierarchy_add_var(trl_hierarchy_t *hier, uint32_t name_id, uint32_t sig_type_id, uint8_t dir,
                              uint32_t src_file_str_id, uint32_t src_line) {
    uint32_t var_id = hier->next_var_id++;
    if (trl_hierarchy_ensure_var_cap(hier, (size_t)var_id + 1) != TRL_OK) {
        return 0;
    }
    hier->var_sig_type_ids[var_id] = sig_type_id;
    if (trl_buf_append_u8(&hier->body, TRL_H_VAR) != TRL_OK ||
        trl_buf_append_uvarint(&hier->body, var_id) != TRL_OK ||
        trl_buf_append_uvarint(&hier->body, name_id) != TRL_OK ||
        trl_buf_append_uvarint(&hier->body, sig_type_id) != TRL_OK ||
        trl_buf_append_u8(&hier->body, dir) != TRL_OK ||
        trl_buf_append_uvarint(&hier->body, 0) != TRL_OK ||
        trl_buf_append_uvarint(&hier->body, src_file_str_id) != TRL_OK ||
        trl_buf_append_uvarint(&hier->body, src_line) != TRL_OK) {
        return 0;
    }
    return var_id;
}

trl_status_t trl_hierarchy_add_attr(trl_hierarchy_t *hier, uint32_t key_str_id, uint32_t value_str_id) {
    /* H_ATTR attaches to the most recently declared node; an empty body has
     * none (trl_hierarchy_begin clears it). */
    if (hier->body.size == 0) {
        return TRL_ERR_INVALID;
    }
    trl_status_t st = trl_buf_append_u8(&hier->body, TRL_H_ATTR);
    if (st == TRL_OK) st = trl_buf_append_uvarint(&hier->body, key_str_id);
    if (st == TRL_OK) st = trl_buf_append_uvarint(&hier->body, value_str_id);
    return st;
}

uint32_t trl_hierarchy_add_stream(trl_hierarchy_t *hier, uint32_t stream_type_id, uint32_t name_id) {
    uint32_t inst_id = hier->next_stream_id++;
    if (trl_hierarchy_add_stream_explicit(hier, inst_id, stream_type_id, name_id) != TRL_OK) {
        return 0;
    }
    return inst_id;
}

trl_status_t trl_hierarchy_add_stream_explicit(trl_hierarchy_t *hier, uint32_t stream_inst_id,
                                               uint32_t stream_type_id, uint32_t name_id) {
    trl_status_t st;
    if (stream_inst_id >= hier->next_stream_id) hier->next_stream_id = stream_inst_id + 1;
    st = trl_buf_append_u8(&hier->body, TRL_H_STREAM);
    if (st == TRL_OK) st = trl_buf_append_uvarint(&hier->body, stream_inst_id);
    if (st == TRL_OK) st = trl_buf_append_uvarint(&hier->body, stream_type_id);
    if (st == TRL_OK) st = trl_buf_append_uvarint(&hier->body, name_id);
    return st;
}

trl_status_t trl_hierarchy_encode_block(const trl_hierarchy_t *hier, trl_compress_t compress, trl_buf_t *out) {
    trl_status_t st;
    trl_buf_t inner;
    trl_buf_init(&inner);
    st = trl_buf_append_uvarint(&inner, hier->hier_id);
    if (st == TRL_OK) st = trl_buf_append_u8(&inner, hier->kind);
    if (st == TRL_OK) st = trl_buf_append_uvarint(&inner, hier->name_id);
    if (st == TRL_OK) st = trl_buf_append(&inner, hier->body.data, hier->body.size);
    if (st != TRL_OK) {
        trl_buf_free(&inner);
        return st;
    }
    if (compress == TRL_COMPRESS_ZLIB) {
        trl_buf_t comp, payload;
        trl_buf_init(&comp);
        trl_buf_init(&payload);
        st = trl_zlib_compress_buf(inner.data, inner.size, &comp);
        if (st == TRL_OK) st = trl_buf_append_le64(&payload, (uint64_t)inner.size);
        if (st == TRL_OK) st = trl_buf_append(&payload, comp.data, comp.size);
        if (st == TRL_OK) st = trl_make_block(TRL_BLK_HIERARCHY, TRL_FLAG_COMPRESSED, payload.data, payload.size, out);
        trl_buf_free(&comp);
        trl_buf_free(&payload);
        trl_buf_free(&inner);
        return st;
    }
    st = trl_make_block(TRL_BLK_HIERARCHY, 0, inner.data, inner.size, out);
    trl_buf_free(&inner);
    return st;
}

/* H_SCOPE body, as trl_hierarchy_begin_scope writes it: u8 scope_type, then
 * name_id, component_id, src_file_str_id and src_line as uvarints. */
static trl_status_t trl_hierarchy_skip_scope(const uint8_t *data, size_t len, size_t *offset) {
    uint64_t tmp = 0;
    trl_status_t st = TRL_OK;
    if (*offset >= len) return TRL_ERR_CORRUPT;
    (*offset)++;
    for (int i = 0; st == TRL_OK && i < 4; ++i) st = trl_decode_uvarint(data, len, offset, &tmp);
    return st;
}

/* One typed attr value (python/src/trlog/_metadata.py encode_typed_value). */
static trl_status_t trl_hierarchy_skip_typed_value(const uint8_t *data, size_t len, size_t *offset, uint8_t attr_type) {
    uint64_t tmp = 0;
    trl_status_t st = TRL_OK;
    uint8_t base = attr_type & (uint8_t)~TRL_AT_ARRAY;
    if (attr_type & TRL_AT_ARRAY) {
        uint64_t count = 0;
        if ((st = trl_decode_uvarint(data, len, offset, &count)) != TRL_OK) return st;
        for (uint64_t i = 0; st == TRL_OK && i < count; ++i) {
            st = trl_hierarchy_skip_typed_value(data, len, offset, base);
        }
        return st;
    }
    switch (base) {
    case TRL_AT_BOOL:
        if (*offset >= len) return TRL_ERR_CORRUPT;
        (*offset)++;
        return TRL_OK;
    case TRL_AT_I64:   /* zigzag svarint: same byte layout as a uvarint */
    case TRL_AT_U64:
    case TRL_AT_STR:   /* string-table id */
        return trl_decode_uvarint(data, len, offset, &tmp);
    case TRL_AT_F64:
        if (len - *offset < 8) return TRL_ERR_CORRUPT;
        *offset += 8;
        return TRL_OK;
    default:
        return TRL_ERR_CORRUPT;
    }
}

/* H_ATTR2 body: uvarint key_str_id, u8 attr_type, typed value. */
static trl_status_t trl_hierarchy_skip_attr2(const uint8_t *data, size_t len, size_t *offset) {
    uint64_t tmp = 0;
    trl_status_t st = trl_decode_uvarint(data, len, offset, &tmp);
    if (st != TRL_OK) return st;
    if (*offset >= len) return TRL_ERR_CORRUPT;
    uint8_t attr_type = data[(*offset)++];
    return trl_hierarchy_skip_typed_value(data, len, offset, attr_type);
}

trl_status_t trl_hierarchy_decode_payload(const uint8_t *payload, size_t payload_len, uint8_t flags, uint32_t **var_sig_type_ids, size_t *var_sig_cap) {
    trl_status_t st = TRL_OK;
    const uint8_t *data = payload;
    size_t len = payload_len;
    uint8_t *owned = NULL;
    size_t offset = 0;
    uint64_t tmp = 0;
    int scope_depth = 0;

    if (flags & TRL_FLAG_COMPRESSED) {
        if (payload_len < 8) {
            return TRL_ERR_CORRUPT;
        }
        uint64_t out_len = trl_read_le64(payload);
        owned = (uint8_t *)malloc((size_t)out_len);
        if (!owned) {
            return TRL_ERR_OOM;
        }
        st = trl_zlib_uncompress_buf(payload + 8, payload_len - 8, owned, (size_t)out_len);
        if (st != TRL_OK) {
            free(owned);
            return st;
        }
        data = owned;
        len = (size_t)out_len;
    }

    st = trl_decode_uvarint(data, len, &offset, &tmp);
    if (st == TRL_OK && offset < len) {
        offset++;
    }
    if (st == TRL_OK) {
        st = trl_decode_uvarint(data, len, &offset, &tmp);
    }
    while (st == TRL_OK && offset < len) {
        uint8_t tag = data[offset++];
        if (tag == TRL_H_SCOPE) {
            scope_depth++;
            st = trl_hierarchy_skip_scope(data, len, &offset);
        } else if (tag == TRL_H_UPSCOPE) {
            if (scope_depth <= 0) {
                st = TRL_ERR_CORRUPT;
                break;
            }
            scope_depth--;
        } else if (tag == TRL_H_VAR) {
            uint64_t var_id = 0, sig_type_id = 0;
            if ((st = trl_decode_uvarint(data, len, &offset, &var_id)) != TRL_OK) break;
            if ((st = trl_decode_uvarint(data, len, &offset, &tmp)) != TRL_OK) break;
            if ((st = trl_decode_uvarint(data, len, &offset, &sig_type_id)) != TRL_OK) break;
            if (offset >= len) { st = TRL_ERR_CORRUPT; break; }
            offset++;
            if ((st = trl_decode_uvarint(data, len, &offset, &tmp)) != TRL_OK) break;
            if ((st = trl_decode_uvarint(data, len, &offset, &tmp)) != TRL_OK) break;
            if ((st = trl_decode_uvarint(data, len, &offset, &tmp)) != TRL_OK) break;
            if ((size_t)var_id >= *var_sig_cap) {
                size_t new_cap = *var_sig_cap ? *var_sig_cap : 16;
                while (new_cap <= (size_t)var_id) new_cap *= 2;
                uint32_t *n = (uint32_t *)realloc(*var_sig_type_ids, new_cap * sizeof(uint32_t));
                if (!n) { st = TRL_ERR_OOM; break; }
                memset(n + *var_sig_cap, 0, (new_cap - *var_sig_cap) * sizeof(uint32_t));
                *var_sig_type_ids = n;
                *var_sig_cap = new_cap;
            }
            (*var_sig_type_ids)[var_id] = (uint32_t)sig_type_id;
        } else if (tag == TRL_H_STREAM) {
            if ((st = trl_decode_uvarint(data, len, &offset, &tmp)) != TRL_OK) break;
            if ((st = trl_decode_uvarint(data, len, &offset, &tmp)) != TRL_OK) break;
            if ((st = trl_decode_uvarint(data, len, &offset, &tmp)) != TRL_OK) break;
        } else if (tag == TRL_H_ATTR) {
            if ((st = trl_decode_uvarint(data, len, &offset, &tmp)) != TRL_OK) break;
            if ((st = trl_decode_uvarint(data, len, &offset, &tmp)) != TRL_OK) break;
        } else if (tag == TRL_H_ATTR2) {
            st = trl_hierarchy_skip_attr2(data, len, &offset);
        } else {
            break;
        }
    }
    free(owned);
    return st;
}

trl_status_t trl_hierarchy_decode_streams(const uint8_t *payload, size_t payload_len, uint8_t flags,
                                          trl_hier_stream_entry_t **out, size_t *out_count, size_t *out_cap) {
    trl_status_t st = TRL_OK;
    const uint8_t *data = payload;
    size_t len = payload_len;
    uint8_t *owned = NULL;
    size_t offset = 0;
    uint64_t tmp = 0;
    int scope_depth = 0;

    if (flags & TRL_FLAG_COMPRESSED) {
        if (payload_len < 8) {
            return TRL_ERR_CORRUPT;
        }
        uint64_t out_len = trl_read_le64(payload);
        owned = (uint8_t *)malloc((size_t)out_len);
        if (!owned) {
            return TRL_ERR_OOM;
        }
        st = trl_zlib_uncompress_buf(payload + 8, payload_len - 8, owned, (size_t)out_len);
        if (st != TRL_OK) {
            free(owned);
            return st;
        }
        data = owned;
        len = (size_t)out_len;
    }

    /* hier_id, kind, name_id — not needed by this caller (the qualified path
     * is already baked into each stream's own name_id). */
    st = trl_decode_uvarint(data, len, &offset, &tmp);
    if (st == TRL_OK && offset < len) {
        offset++;
    }
    if (st == TRL_OK) {
        st = trl_decode_uvarint(data, len, &offset, &tmp);
    }
    while (st == TRL_OK && offset < len) {
        uint8_t tag = data[offset++];
        if (tag == TRL_H_SCOPE) {
            scope_depth++;
            st = trl_hierarchy_skip_scope(data, len, &offset);
        } else if (tag == TRL_H_UPSCOPE) {
            if (scope_depth <= 0) { st = TRL_ERR_CORRUPT; break; }
            scope_depth--;
        } else if (tag == TRL_H_VAR) {
            /* var_id, name_id, sig_type_id, u8 dir, alias_of, src_file, src_line */
            for (int i = 0; st == TRL_OK && i < 3; ++i) st = trl_decode_uvarint(data, len, &offset, &tmp);
            if (st == TRL_OK) {
                if (offset >= len) { st = TRL_ERR_CORRUPT; break; }
                offset++;
            }
            for (int i = 0; st == TRL_OK && i < 3; ++i) st = trl_decode_uvarint(data, len, &offset, &tmp);
        } else if (tag == TRL_H_STREAM) {
            uint64_t stream_inst_id = 0, stream_type_id = 0, name_id = 0;
            if ((st = trl_decode_uvarint(data, len, &offset, &stream_inst_id)) != TRL_OK) break;
            if ((st = trl_decode_uvarint(data, len, &offset, &stream_type_id)) != TRL_OK) break;
            if ((st = trl_decode_uvarint(data, len, &offset, &name_id)) != TRL_OK) break;
            if (*out_count >= *out_cap) {
                size_t new_cap = *out_cap ? *out_cap * 2 : 8;
                trl_hier_stream_entry_t *n = (trl_hier_stream_entry_t *)realloc(*out, new_cap * sizeof(**out));
                if (!n) { st = TRL_ERR_OOM; break; }
                *out = n;
                *out_cap = new_cap;
            }
            (*out)[(*out_count)++] = (trl_hier_stream_entry_t){
                (uint32_t)stream_inst_id, (uint32_t)stream_type_id, (uint32_t)name_id
            };
        } else if (tag == TRL_H_ATTR) {
            for (int i = 0; st == TRL_OK && i < 2; ++i) st = trl_decode_uvarint(data, len, &offset, &tmp);
        } else if (tag == TRL_H_ATTR2) {
            st = trl_hierarchy_skip_attr2(data, len, &offset);
        } else {
            break;
        }
    }
    free(owned);
    return st;
}

static const char *trl_hier_name(const trl_string_table_t *strtab, uint64_t sid) {
    const char *s = trl_string_table_lookup(strtab, (uint32_t)sid);
    return s ? s : "";
}

trl_status_t trl_hierarchy_walk_payload(const uint8_t *payload, size_t payload_len, uint8_t flags,
                                        const trl_string_table_t *strtab, const trl_type_registry_t *typereg,
                                        trl_hier_cb cb, void *user) {
    trl_status_t st = TRL_OK;
    const uint8_t *data = payload;
    size_t len = payload_len;
    uint8_t *owned = NULL;
    size_t offset = 0;
    uint64_t hier_id = 0, name_id = 0, tmp = 0;
    int scope_depth = 0;
    trl_hier_event_t ev;

    if (flags & TRL_FLAG_COMPRESSED) {
        if (payload_len < 8) return TRL_ERR_CORRUPT;
        uint64_t out_len = trl_read_le64(payload);
        owned = (uint8_t *)malloc((size_t)out_len ? (size_t)out_len : 1);
        if (!owned) return TRL_ERR_OOM;
        st = trl_zlib_uncompress_buf(payload + 8, payload_len - 8, owned, (size_t)out_len);
        if (st != TRL_OK) { free(owned); return st; }
        data = owned;
        len = (size_t)out_len;
    }

    /* Block header: hier_id, kind, name. */
    memset(&ev, 0, sizeof(ev));
    if ((st = trl_decode_uvarint(data, len, &offset, &hier_id)) != TRL_OK || offset >= len) {
        free(owned);
        return st != TRL_OK ? st : TRL_ERR_CORRUPT;
    }
    ev.scope_type = data[offset++];
    if ((st = trl_decode_uvarint(data, len, &offset, &name_id)) != TRL_OK) { free(owned); return st; }
    ev.kind = TRL_HE_HIER;
    ev.hier_id = (uint32_t)hier_id;
    ev.name = trl_hier_name(strtab, name_id);
    cb(&ev, user);

    while (st == TRL_OK && offset < len) {
        uint8_t tag = data[offset++];
        memset(&ev, 0, sizeof(ev));
        ev.hier_id = (uint32_t)hier_id;
        ev.name = "";
        if (tag == TRL_H_SCOPE) {
            if (offset >= len) { st = TRL_ERR_CORRUPT; break; }
            ev.kind = TRL_HE_SCOPE;
            ev.scope_type = data[offset++];
            if ((st = trl_decode_uvarint(data, len, &offset, &name_id)) != TRL_OK) break;
            for (int i = 0; st == TRL_OK && i < 3; ++i) st = trl_decode_uvarint(data, len, &offset, &tmp);
            if (st != TRL_OK) break;
            ev.name = trl_hier_name(strtab, name_id);
            scope_depth++;
            cb(&ev, user);
        } else if (tag == TRL_H_UPSCOPE) {
            if (scope_depth <= 0) { st = TRL_ERR_CORRUPT; break; }
            scope_depth--;
            ev.kind = TRL_HE_UPSCOPE;
            cb(&ev, user);
        } else if (tag == TRL_H_VAR) {
            uint64_t var_id = 0, sig_type_id = 0, alias = 0;
            if ((st = trl_decode_uvarint(data, len, &offset, &var_id)) != TRL_OK) break;
            if ((st = trl_decode_uvarint(data, len, &offset, &name_id)) != TRL_OK) break;
            if ((st = trl_decode_uvarint(data, len, &offset, &sig_type_id)) != TRL_OK) break;
            if (offset >= len) { st = TRL_ERR_CORRUPT; break; }
            ev.dir = data[offset++];
            if ((st = trl_decode_uvarint(data, len, &offset, &alias)) != TRL_OK) break;
            if ((st = trl_decode_uvarint(data, len, &offset, &tmp)) != TRL_OK) break;
            if ((st = trl_decode_uvarint(data, len, &offset, &tmp)) != TRL_OK) break;
            ev.kind = TRL_HE_VAR;
            ev.id = (uint32_t)var_id;
            ev.type_id = (uint32_t)sig_type_id;
            ev.alias_of = (uint32_t)alias;
            ev.name = trl_hier_name(strtab, name_id);
            if (sig_type_id && sig_type_id <= typereg->signal_count) {
                ev.encoding = typereg->signal_types[sig_type_id].encoding;
                ev.bit_width = typereg->signal_types[sig_type_id].bit_width;
            }
            cb(&ev, user);
        } else if (tag == TRL_H_STREAM) {
            uint64_t inst = 0, stype = 0;
            if ((st = trl_decode_uvarint(data, len, &offset, &inst)) != TRL_OK) break;
            if ((st = trl_decode_uvarint(data, len, &offset, &stype)) != TRL_OK) break;
            if ((st = trl_decode_uvarint(data, len, &offset, &name_id)) != TRL_OK) break;
            ev.kind = TRL_HE_STREAM;
            ev.id = (uint32_t)inst;
            ev.type_id = (uint32_t)stype;
            ev.name = trl_hier_name(strtab, name_id);
            cb(&ev, user);
        } else if (tag == TRL_H_ATTR) {
            if ((st = trl_decode_uvarint(data, len, &offset, &tmp)) != TRL_OK) break;
            st = trl_decode_uvarint(data, len, &offset, &tmp);
        } else if (tag == TRL_H_ATTR2) {
            st = trl_hierarchy_skip_attr2(data, len, &offset);
        } else {
            break;
        }
    }
    free(owned);
    return st;
}
