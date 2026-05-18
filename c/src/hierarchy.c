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
    hier->next_var_id = 1;
    hier->next_stream_id = 1;
    hier->scope_depth = 0;
    memset(hier->var_sig_type_ids, 0, hier->var_sig_cap * sizeof(uint32_t));
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
            st = trl_decode_uvarint(data, len, &offset, &tmp);
            if (st == TRL_OK) st = trl_decode_uvarint(data, len, &offset, &tmp);
            if (st == TRL_OK) st = trl_decode_uvarint(data, len, &offset, &tmp);
            if (st == TRL_OK) st = trl_decode_uvarint(data, len, &offset, &tmp);
            if (st == TRL_OK) st = trl_decode_uvarint(data, len, &offset, &tmp);
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
        } else {
            break;
        }
    }
    free(owned);
    return st;
}
