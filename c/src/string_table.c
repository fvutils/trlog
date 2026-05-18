#include "varint_internal.h"

static trl_status_t trl_string_table_rehash(trl_string_table_t *tab, size_t new_cap) {
    trl_str_entry_t *old_map = tab->map;
    size_t old_cap = tab->map_cap;
    trl_str_entry_t *new_map = (trl_str_entry_t *)calloc(new_cap, sizeof(trl_str_entry_t));
    if (!new_map) {
        return TRL_ERR_OOM;
    }
    tab->map = new_map;
    tab->map_cap = new_cap;
    tab->map_count = 0;
    for (size_t i = 0; i < old_cap; ++i) {
        if (!old_map || !old_map[i].key) {
            continue;
        }
        uint64_t h = trl_hash_bytes(old_map[i].key, strlen(old_map[i].key));
        size_t idx = (size_t)(h & (new_cap - 1));
        while (new_map[idx].key) {
            idx = (idx + 1) & (new_cap - 1);
        }
        new_map[idx] = old_map[i];
        tab->map_count++;
    }
    free(old_map);
    return TRL_OK;
}

static trl_status_t trl_string_table_ensure_id_cap(trl_string_table_t *tab, uint32_t need) {
    if (need <= tab->cap) {
        return TRL_OK;
    }
    uint32_t new_cap = tab->cap ? tab->cap : 16;
    while (new_cap < need) {
        new_cap *= 2;
    }
    char **n = (char **)realloc(tab->id_to_str, (size_t)new_cap * sizeof(char *));
    if (!n) {
        return TRL_ERR_OOM;
    }
    for (uint32_t i = tab->cap; i < new_cap; ++i) {
        n[i] = NULL;
    }
    tab->id_to_str = n;
    tab->cap = new_cap;
    return TRL_OK;
}

void trl_string_table_init(trl_string_table_t *tab) {
    memset(tab, 0, sizeof(*tab));
}

void trl_string_table_free(trl_string_table_t *tab) {
    if (tab->id_to_str) {
        for (uint32_t i = 1; i <= tab->count; ++i) {
            free(tab->id_to_str[i]);
        }
    }
    free(tab->id_to_str);
    free(tab->map);
    memset(tab, 0, sizeof(*tab));
}

uint32_t trl_string_table_intern(trl_string_table_t *tab, const char *s) {
    if (!s) {
        return 0;
    }
    if (!tab->map_cap) {
        if (trl_string_table_rehash(tab, 32) != TRL_OK) {
            return 0;
        }
    } else if ((tab->map_count + 1) * 10 >= tab->map_cap * 7) {
        if (trl_string_table_rehash(tab, tab->map_cap * 2) != TRL_OK) {
            return 0;
        }
    }
    size_t len = strlen(s);
    uint64_t h = trl_hash_bytes(s, len);
    size_t idx = (size_t)(h & (tab->map_cap - 1));
    while (tab->map[idx].key) {
        if (strcmp(tab->map[idx].key, s) == 0) {
            return tab->map[idx].id;
        }
        idx = (idx + 1) & (tab->map_cap - 1);
    }
    uint32_t id = tab->count + 1;
    if (trl_string_table_ensure_id_cap(tab, id + 1) != TRL_OK) {
        return 0;
    }
    char *dup = trl_strdup_len(s, len);
    if (!dup) {
        return 0;
    }
    tab->id_to_str[id] = dup;
    tab->count = id;
    tab->map[idx].key = dup;
    tab->map[idx].id = id;
    tab->map_count++;
    return id;
}

const char *trl_string_table_lookup(const trl_string_table_t *tab, uint32_t id) {
    if (!id || id > tab->count || !tab->id_to_str) {
        return NULL;
    }
    return tab->id_to_str[id];
}

trl_status_t trl_string_table_encode_block(const trl_string_table_t *tab, trl_compress_t compress, trl_buf_t *out) {
    trl_status_t st;
    trl_buf_t payload;
    trl_buf_init(&payload);
    st = trl_buf_append_uvarint(&payload, tab->count);
    if (st != TRL_OK) {
        trl_buf_free(&payload);
        return st;
    }
    for (uint32_t i = 1; i <= tab->count; ++i) {
        const char *s = tab->id_to_str[i] ? tab->id_to_str[i] : "";
        size_t len = strlen(s);
        st = trl_buf_append_uvarint(&payload, (uint64_t)len);
        if (st == TRL_OK) {
            st = trl_buf_append(&payload, s, len);
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
        st = trl_make_block(TRL_BLK_STR_TABLE, TRL_FLAG_COMPRESSED, comp.data, comp.size, out);
        trl_buf_free(&comp);
        return st;
    }
    st = trl_make_block(TRL_BLK_STR_TABLE, 0, payload.data, payload.size, out);
    trl_buf_free(&payload);
    return st;
}

trl_status_t trl_string_table_decode_payload(trl_string_table_t *tab, const uint8_t *payload, size_t payload_len, uint8_t flags) {
    trl_status_t st = TRL_OK;
    const uint8_t *data = payload;
    size_t len = payload_len;
    uint8_t *owned = NULL;
    size_t offset = 0;
    uint64_t count = 0;

    if (flags & TRL_FLAG_COMPRESSED) {
        uLongf out_len = payload_len ? (uLongf)(payload_len * 8 + 64) : 64;
        owned = NULL;
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
    for (uint64_t i = 0; i < count; ++i) {
        uint64_t slen = 0;
        st = trl_decode_uvarint(data, len, &offset, &slen);
        if (st != TRL_OK || offset + slen > len) {
            free(owned);
            return TRL_ERR_CORRUPT;
        }
        char *tmp = trl_strdup_len((const char *)(data + offset), (size_t)slen);
        if (!tmp) {
            free(owned);
            return TRL_ERR_OOM;
        }
        uint32_t id = trl_string_table_intern(tab, tmp);
        free(tmp);
        if (!id) {
            free(owned);
            return TRL_ERR_OOM;
        }
        offset += (size_t)slen;
    }
    free(owned);
    return TRL_OK;
}
