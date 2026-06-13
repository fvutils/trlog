#include "varint_internal.h"

static trl_status_t trl_type_registry_ensure_signal_cap(trl_type_registry_t *reg, size_t need) {
    if (need <= reg->signal_cap) {
        return TRL_OK;
    }
    size_t new_cap = reg->signal_cap ? reg->signal_cap : 8;
    while (new_cap < need) {
        new_cap *= 2;
    }
    trl_signal_type_entry_t *n = (trl_signal_type_entry_t *)realloc(reg->signal_types, new_cap * sizeof(trl_signal_type_entry_t));
    if (!n) {
        return TRL_ERR_OOM;
    }
    memset(n + reg->signal_cap, 0, (new_cap - reg->signal_cap) * sizeof(trl_signal_type_entry_t));
    reg->signal_types = n;
    reg->signal_cap = new_cap;
    return TRL_OK;
}

static trl_status_t trl_type_registry_ensure_txn_cap(trl_type_registry_t *reg, size_t need) {
    if (need <= reg->txn_cap) {
        return TRL_OK;
    }
    size_t new_cap = reg->txn_cap ? reg->txn_cap : 8;
    while (new_cap < need) {
        new_cap *= 2;
    }
    trl_txn_schema_entry_t *n = (trl_txn_schema_entry_t *)realloc(reg->txn_schemas, new_cap * sizeof(trl_txn_schema_entry_t));
    if (!n) {
        return TRL_ERR_OOM;
    }
    memset(n + reg->txn_cap, 0, (new_cap - reg->txn_cap) * sizeof(trl_txn_schema_entry_t));
    reg->txn_schemas = n;
    reg->txn_cap = new_cap;
    return TRL_OK;
}

void trl_type_registry_init(trl_type_registry_t *reg) {
    memset(reg, 0, sizeof(*reg));
}

void trl_type_registry_free(trl_type_registry_t *reg) {
    if (reg->txn_schemas) {
        for (size_t i = 1; i <= reg->txn_count; ++i) {
            free(reg->txn_schemas[i].fields);
        }
    }
    free(reg->signal_types);
    free(reg->txn_schemas);
    memset(reg, 0, sizeof(*reg));
}

uint32_t trl_type_registry_add_signal_type(trl_type_registry_t *reg, uint8_t encoding, uint32_t bit_width, uint8_t radix) {
    uint32_t id = (uint32_t)(reg->signal_count + 1);
    if (trl_type_registry_ensure_signal_cap(reg, id + 1) != TRL_OK) {
        return 0;
    }
    reg->signal_types[id].encoding = encoding;
    reg->signal_types[id].bit_width = bit_width;
    reg->signal_types[id].radix = radix;
    reg->signal_count = id;
    return id;
}

uint32_t trl_type_registry_add_txn_schema(trl_type_registry_t *reg, uint32_t name_id, uint32_t field_count, const trl_field_def_t *fields) {
    uint32_t id = (uint32_t)(reg->txn_count + 1);
    if (trl_type_registry_ensure_txn_cap(reg, id + 1) != TRL_OK) {
        return 0;
    }
    reg->txn_schemas[id].name_id = name_id;
    reg->txn_schemas[id].field_count = field_count;
    if (field_count) {
        reg->txn_schemas[id].fields = (trl_field_def_t *)malloc((size_t)field_count * sizeof(trl_field_def_t));
        if (!reg->txn_schemas[id].fields) {
            return 0;
        }
        memcpy(reg->txn_schemas[id].fields, fields, (size_t)field_count * sizeof(trl_field_def_t));
    }
    reg->txn_count = id;
    return id;
}

const trl_signal_type_entry_t *trl_type_registry_get_signal_type(const trl_type_registry_t *reg, uint32_t id) {
    if (!id || id > reg->signal_count || !reg->signal_types) {
        return NULL;
    }
    return &reg->signal_types[id];
}

const trl_txn_schema_entry_t *trl_type_registry_get_txn_schema(const trl_type_registry_t *reg, uint32_t id) {
    if (!id || id > reg->txn_count || !reg->txn_schemas) {
        return NULL;
    }
    return &reg->txn_schemas[id];
}

trl_status_t trl_type_registry_encode_block(const trl_type_registry_t *reg, trl_compress_t compress, trl_buf_t *out) {
    trl_buf_t payload;
    trl_status_t st;
    trl_buf_init(&payload);
    st = trl_buf_append_uvarint(&payload, reg->signal_count + reg->txn_count);
    if (st != TRL_OK) {
        trl_buf_free(&payload);
        return st;
    }
    for (uint32_t id = 1; id <= reg->signal_count; ++id) {
        const trl_signal_type_entry_t *e = &reg->signal_types[id];
        st = trl_buf_append_u8(&payload, TRL_TYPE_TAG_SIGNAL);
        if (st == TRL_OK) st = trl_buf_append_uvarint(&payload, id);
        if (st == TRL_OK) st = trl_buf_append_u8(&payload, e->encoding);
        if (st == TRL_OK) st = trl_buf_append_uvarint(&payload, e->bit_width);
        if (st == TRL_OK) st = trl_buf_append_u8(&payload, e->radix);
        if (st != TRL_OK) {
            trl_buf_free(&payload);
            return st;
        }
    }
    for (uint32_t id = 1; id <= reg->txn_count; ++id) {
        const trl_txn_schema_entry_t *e = &reg->txn_schemas[id];
        st = trl_buf_append_u8(&payload, TRL_TYPE_TAG_TXN_SCHEMA);
        if (st == TRL_OK) st = trl_buf_append_uvarint(&payload, id);
        if (st == TRL_OK) st = trl_buf_append_uvarint(&payload, e->name_id);
        if (st == TRL_OK) st = trl_buf_append_uvarint(&payload, e->field_count);
        for (uint32_t i = 0; st == TRL_OK && i < e->field_count; ++i) {
            st = trl_buf_append_uvarint(&payload, e->fields[i].name_str_id);
            if (st == TRL_OK) st = trl_buf_append_u8(&payload, (uint8_t)e->fields[i].field_type);
            if (st == TRL_OK) st = trl_buf_append_u8(&payload, 0);
            if (st == TRL_OK) st = trl_buf_append_uvarint(&payload, 0);
        }
        if (st != TRL_OK) {
            trl_buf_free(&payload);
            return st;
        }
    }
    if (compress == TRL_COMPRESS_ZLIB && payload.size) {
        trl_buf_t comp;
        trl_buf_init(&comp);
        st = trl_zlib_compress_buf(payload.data, payload.size, &comp);
        trl_buf_free(&payload);
        if (st != TRL_OK) {
            trl_buf_free(&comp);
            return st;
        }
        st = trl_make_block(TRL_BLK_TYPE_REG, TRL_FLAG_COMPRESSED, comp.data, comp.size, out);
        trl_buf_free(&comp);
        return st;
    }
    st = trl_make_block(TRL_BLK_TYPE_REG, 0, payload.data, payload.size, out);
    trl_buf_free(&payload);
    return st;
}

trl_status_t trl_type_registry_decode_payload(trl_type_registry_t *reg, const uint8_t *payload, size_t payload_len, uint8_t flags) {
    trl_status_t st = TRL_OK;
    const uint8_t *data = payload;
    size_t len = payload_len;
    uint8_t *owned = NULL;
    size_t offset = 0;
    uint64_t count = 0;

    if (flags & TRL_FLAG_COMPRESSED) {
        uLongf out_len = payload_len ? (uLongf)(payload_len * 8 + 64) : 64;
        while (1) {
            uint8_t *tmp = (uint8_t *)realloc(owned, out_len);
            if (!tmp) {
                free(owned);
                return TRL_ERR_OOM;
            }
            owned = tmp;
            uLongf actual = out_len;
            int rc = uncompress(owned, &actual, payload, (uLong)payload_len);
            if (rc == Z_OK) {
                data = owned;
                len = (size_t)actual;
                break;
            }
            if (rc != Z_BUF_ERROR) {
                free(owned);
                return TRL_ERR_COMPRESS;
            }
            out_len *= 2;
        }
    }

    st = trl_decode_uvarint(data, len, &offset, &count);
    if (st != TRL_OK) {
        free(owned);
        return st;
    }
    for (uint64_t i = 0; i < count && offset < len; ++i) {
        uint8_t tag = data[offset++];
        if (tag == TRL_TYPE_TAG_SIGNAL) {
            uint64_t id = 0, bit_width = 0;
            if ((st = trl_decode_uvarint(data, len, &offset, &id)) != TRL_OK) break;
            if (offset + 2 > len) { st = TRL_ERR_CORRUPT; break; }
            uint8_t enc = data[offset++];
            if ((st = trl_decode_uvarint(data, len, &offset, &bit_width)) != TRL_OK) break;
            if (offset >= len) { st = TRL_ERR_CORRUPT; break; }
            uint8_t radix = data[offset++];
            if ((st = trl_type_registry_ensure_signal_cap(reg, (size_t)id + 1)) != TRL_OK) break;
            reg->signal_types[id].encoding = enc;
            reg->signal_types[id].bit_width = (uint32_t)bit_width;
            reg->signal_types[id].radix = radix;
            if (id > reg->signal_count) reg->signal_count = (size_t)id;
        } else if (tag == TRL_TYPE_TAG_TXN_SCHEMA) {
            uint64_t id = 0, name_id = 0, field_count = 0;
            if ((st = trl_decode_uvarint(data, len, &offset, &id)) != TRL_OK) break;
            if ((st = trl_decode_uvarint(data, len, &offset, &name_id)) != TRL_OK) break;
            if ((st = trl_decode_uvarint(data, len, &offset, &field_count)) != TRL_OK) break;
            if ((st = trl_type_registry_ensure_txn_cap(reg, (size_t)id + 1)) != TRL_OK) break;
            if (reg->txn_schemas[id].fields) {
                st = TRL_ERR_CORRUPT;
                break;
            }
            reg->txn_schemas[id].name_id = (uint32_t)name_id;
            reg->txn_schemas[id].field_count = (uint32_t)field_count;
            if (field_count) {
                reg->txn_schemas[id].fields = (trl_field_def_t *)calloc((size_t)field_count, sizeof(trl_field_def_t));
                if (!reg->txn_schemas[id].fields) { st = TRL_ERR_OOM; break; }
            }
            for (uint64_t fi = 0; fi < field_count; ++fi) {
                uint64_t field_name = 0, enum_type = 0;
                if ((st = trl_decode_uvarint(data, len, &offset, &field_name)) != TRL_OK) break;
                if (offset + 2 > len) { st = TRL_ERR_CORRUPT; break; }
                reg->txn_schemas[id].fields[fi].name_str_id = (uint32_t)field_name;
                reg->txn_schemas[id].fields[fi].field_type = (trl_field_type_t)data[offset++];
                offset++;
                if ((st = trl_decode_uvarint(data, len, &offset, &enum_type)) != TRL_OK) break;
            }
            if (st != TRL_OK) break;
            if (id > reg->txn_count) reg->txn_count = (size_t)id;
        } else if (tag >= TRL_TYPE_TAG_STREAM_V2) {
            /* Forward-compat (impl-plan §2.1, decision 6): every tag >= 0x05 is
             * length-prefixed, so an unrecognised entry — including codec-aware
             * stream decls this minimal C reader does not model — is skipped by
             * its declared length, and parsing continues. */
            uint64_t entry_len = 0;
            if ((st = trl_decode_uvarint(data, len, &offset, &entry_len)) != TRL_OK) break;
            if (offset + entry_len > len) { st = TRL_ERR_CORRUPT; break; }
            offset += (size_t)entry_len;
        } else {
            break;
        }
    }
    free(owned);
    return st;
}
