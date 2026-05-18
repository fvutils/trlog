#include "varint_internal.h"

#include <math.h>

static int cmp_u64(const void *a, const void *b) {
    uint64_t av = *(const uint64_t *)a;
    uint64_t bv = *(const uint64_t *)b;
    return (av > bv) - (av < bv);
}

static int cmp_var(const void *a, const void *b) {
    const trl_vc_var_changes_t *av = *(const trl_vc_var_changes_t * const *)a;
    const trl_vc_var_changes_t *bv = *(const trl_vc_var_changes_t * const *)b;
    return (av->var_id > bv->var_id) - (av->var_id < bv->var_id);
}

static int cmp_change(const void *a, const void *b) {
    const trl_vc_change_t *av = (const trl_vc_change_t *)a;
    const trl_vc_change_t *bv = (const trl_vc_change_t *)b;
    return (av->time > bv->time) - (av->time < bv->time);
}

static trl_status_t trl_vc_block_ensure_vars(trl_vc_block_t *blk, size_t need) {
    if (need <= blk->var_cap) return TRL_OK;
    size_t new_cap = blk->var_cap ? blk->var_cap : 8;
    while (new_cap < need) new_cap *= 2;
    trl_vc_var_changes_t *n = (trl_vc_var_changes_t *)realloc(blk->vars, new_cap * sizeof(trl_vc_var_changes_t));
    if (!n) return TRL_ERR_OOM;
    memset(n + blk->var_cap, 0, (new_cap - blk->var_cap) * sizeof(trl_vc_var_changes_t));
    blk->vars = n;
    blk->var_cap = new_cap;
    return TRL_OK;
}

static trl_status_t trl_vc_var_ensure_items(trl_vc_var_changes_t *var, size_t need) {
    if (need <= var->cap) return TRL_OK;
    size_t new_cap = var->cap ? var->cap : 8;
    while (new_cap < need) new_cap *= 2;
    trl_vc_change_t *n = (trl_vc_change_t *)realloc(var->items, new_cap * sizeof(trl_vc_change_t));
    if (!n) return TRL_ERR_OOM;
    var->items = n;
    var->cap = new_cap;
    return TRL_OK;
}

static trl_vc_var_changes_t *trl_vc_get_var(trl_vc_block_t *blk, uint32_t var_id) {
    for (size_t i = 0; i < blk->var_count; ++i) {
        if (blk->vars[i].var_id == var_id) return &blk->vars[i];
    }
    if (trl_vc_block_ensure_vars(blk, blk->var_count + 1) != TRL_OK) return NULL;
    trl_vc_var_changes_t *var = &blk->vars[blk->var_count++];
    memset(var, 0, sizeof(*var));
    var->var_id = var_id;
    return var;
}

static void trl_vc_free_change(trl_vc_change_t *chg) {
    if (chg->kind == TRL_VC_VALUE_BYTES) {
        free(chg->value.bytes.data);
    } else if (chg->kind == TRL_VC_VALUE_STRING) {
        free(chg->value.str.str);
    }
    memset(chg, 0, sizeof(*chg));
}

static const trl_signal_type_entry_t *trl_vc_lookup_signal(const uint32_t *var_sig_type_ids, size_t var_sig_cap, const trl_type_registry_t *reg, uint32_t var_id) {
    uint32_t sig_type_id = 0;
    if ((size_t)var_id < var_sig_cap) sig_type_id = var_sig_type_ids[var_id];
    if (!sig_type_id) sig_type_id = var_id;
    return trl_type_registry_get_signal_type(reg, sig_type_id);
}

static trl_status_t trl_append_exact_bytes(trl_buf_t *buf, const uint8_t *src, size_t src_len, size_t want) {
    if (src_len >= want) {
        return trl_buf_append(buf, src + (src_len - want), want);
    }
    trl_status_t st;
    for (size_t i = 0; i < want - src_len; ++i) {
        st = trl_buf_append_u8(buf, 0);
        if (st != TRL_OK) return st;
    }
    return trl_buf_append(buf, src, src_len);
}

static size_t trl_time_index(const uint64_t *times, size_t count, uint64_t value) {
    size_t lo = 0, hi = count;
    while (lo < hi) {
        size_t mid = lo + (hi - lo) / 2;
        if (times[mid] < value) lo = mid + 1;
        else hi = mid;
    }
    return lo;
}

void trl_vc_block_init(trl_vc_block_t *blk) {
    memset(blk, 0, sizeof(*blk));
}

void trl_vc_block_free(trl_vc_block_t *blk) {
    if (blk->vars) {
        for (size_t i = 0; i < blk->var_count; ++i) {
            for (size_t j = 0; j < blk->vars[i].count; ++j) {
                trl_vc_free_change(&blk->vars[i].items[j]);
            }
            free(blk->vars[i].items);
        }
    }
    free(blk->vars);
    memset(blk, 0, sizeof(*blk));
}

trl_status_t trl_vc_block_begin(trl_vc_block_t *blk, uint64_t start_time) {
    trl_vc_block_free(blk);
    blk->start_time = start_time;
    blk->end_time = start_time;
    return TRL_OK;
}

trl_status_t trl_vc_block_add_u64(trl_vc_block_t *blk, uint32_t var_id, uint64_t time, uint64_t value) {
    trl_vc_var_changes_t *var = trl_vc_get_var(blk, var_id);
    if (!var) return TRL_ERR_OOM;
    trl_status_t st = trl_vc_var_ensure_items(var, var->count + 1);
    if (st != TRL_OK) return st;
    var->items[var->count].time = time;
    var->items[var->count].kind = TRL_VC_VALUE_U64;
    var->items[var->count].value.u64 = value;
    var->count++;
    if (time > blk->end_time) blk->end_time = time;
    return TRL_OK;
}

trl_status_t trl_vc_block_add_bytes(trl_vc_block_t *blk, uint32_t var_id, uint64_t time, const uint8_t *value, uint32_t len) {
    trl_vc_var_changes_t *var = trl_vc_get_var(blk, var_id);
    if (!var) return TRL_ERR_OOM;
    trl_status_t st = trl_vc_var_ensure_items(var, var->count + 1);
    if (st != TRL_OK) return st;
    uint8_t *dup = NULL;
    if (len) {
        dup = (uint8_t *)malloc(len);
        if (!dup) return TRL_ERR_OOM;
        memcpy(dup, value, len);
    }
    var->items[var->count].time = time;
    var->items[var->count].kind = TRL_VC_VALUE_BYTES;
    var->items[var->count].value.bytes.data = dup;
    var->items[var->count].value.bytes.len = len;
    var->count++;
    if (time > blk->end_time) blk->end_time = time;
    return TRL_OK;
}

trl_status_t trl_vc_block_add_str(trl_vc_block_t *blk, uint32_t var_id, uint64_t time, const char *value) {
    trl_vc_var_changes_t *var = trl_vc_get_var(blk, var_id);
    if (!var) return TRL_ERR_OOM;
    trl_status_t st = trl_vc_var_ensure_items(var, var->count + 1);
    if (st != TRL_OK) return st;
    const char *s = value ? value : "";
    size_t len = strlen(s);
    char *dup = trl_strdup_len(s, len);
    if (!dup) return TRL_ERR_OOM;
    var->items[var->count].time = time;
    var->items[var->count].kind = TRL_VC_VALUE_STRING;
    var->items[var->count].value.str.str = dup;
    var->items[var->count].value.str.len = (uint32_t)len;
    var->count++;
    if (time > blk->end_time) blk->end_time = time;
    return TRL_OK;
}

trl_status_t trl_vc_block_add_real(trl_vc_block_t *blk, uint32_t var_id, uint64_t time, double value) {
    trl_vc_var_changes_t *var = trl_vc_get_var(blk, var_id);
    if (!var) return TRL_ERR_OOM;
    trl_status_t st = trl_vc_var_ensure_items(var, var->count + 1);
    if (st != TRL_OK) return st;
    var->items[var->count].time = time;
    var->items[var->count].kind = TRL_VC_VALUE_REAL;
    var->items[var->count].value.real = value;
    var->count++;
    if (time > blk->end_time) blk->end_time = time;
    return TRL_OK;
}

static trl_status_t trl_vc_encode_init_value(trl_buf_t *payload, const trl_signal_type_entry_t *sig, const trl_vc_change_t *chg) {
    uint8_t enc = sig ? sig->encoding : TRL_SE_2STATE;
    uint32_t bw = sig ? sig->bit_width : 1;
    union { double d; uint8_t b[8]; } u;
    if (enc == TRL_SE_REAL) {
        u.d = (chg->kind == TRL_VC_VALUE_REAL) ? chg->value.real : (double)chg->value.u64;
        return trl_buf_append(payload, u.b, 8);
    }
    if (enc == TRL_SE_STRING) {
        const char *s = (chg->kind == TRL_VC_VALUE_STRING) ? chg->value.str.str : "";
        size_t len = s ? strlen(s) : 0;
        trl_status_t st = trl_buf_append_uvarint(payload, len);
        if (st == TRL_OK && len) st = trl_buf_append(payload, s, len);
        return st;
    }
    if (enc == TRL_SE_4STATE) {
        size_t nbytes = bw == 1 ? 1 : (size_t)((bw * 2 + 7) / 8);
        trl_status_t st = trl_buf_append_uvarint(payload, bw);
        if (st != TRL_OK) return st;
        if (chg->kind == TRL_VC_VALUE_BYTES) return trl_append_exact_bytes(payload, chg->value.bytes.data, chg->value.bytes.len, nbytes);
        uint8_t v = (uint8_t)(chg->value.u64 & 0x3u);
        return trl_append_exact_bytes(payload, &v, 1, nbytes);
    }
    if (enc == TRL_SE_9STATE) {
        size_t nbytes = (size_t)((bw + 1) / 2);
        if (!nbytes) nbytes = 1;
        trl_status_t st = trl_buf_append_uvarint(payload, bw);
        if (st != TRL_OK) return st;
        if (chg->kind == TRL_VC_VALUE_BYTES) return trl_append_exact_bytes(payload, chg->value.bytes.data, chg->value.bytes.len, nbytes);
        uint8_t v = (uint8_t)(chg->value.u64 & 0xFu);
        return trl_append_exact_bytes(payload, &v, 1, nbytes);
    }
    {
        size_t nbytes = (size_t)((bw + 7) / 8);
        if (!nbytes) nbytes = 1;
        trl_status_t st = trl_buf_append_uvarint(payload, bw);
        if (st != TRL_OK) return st;
        if (chg->kind == TRL_VC_VALUE_BYTES) return trl_append_exact_bytes(payload, chg->value.bytes.data, chg->value.bytes.len, nbytes);
        uint8_t tmp[8] = {0};
        for (size_t i = 0; i < nbytes && i < sizeof(tmp); ++i) tmp[nbytes - 1 - i] = (uint8_t)(chg->value.u64 >> (8 * i));
        return trl_append_exact_bytes(payload, tmp, nbytes, nbytes);
    }
}

static trl_status_t trl_vc_encode_wave(trl_buf_t *dst, const trl_vc_var_changes_t *var, const trl_signal_type_entry_t *sig, const uint64_t *times, size_t time_count) {
    uint8_t enc = sig ? sig->encoding : TRL_SE_2STATE;
    uint32_t bw = sig ? sig->bit_width : 1;
    trl_status_t st = trl_buf_append_uvarint(dst, var->count);
    int64_t prev_tidx = -1;
    for (size_t i = 0; st == TRL_OK && i < var->count; ++i) {
        const trl_vc_change_t *chg = &var->items[i];
        size_t tidx = trl_time_index(times, time_count, chg->time);
        uint64_t delta = (uint64_t)((int64_t)tidx - (prev_tidx + 1));
        prev_tidx = (int64_t)tidx;
        if (enc == TRL_SE_2STATE && bw == 1) {
            uint64_t v = (chg->kind == TRL_VC_VALUE_U64) ? (chg->value.u64 & 1u) : (chg->value.bytes.len ? (chg->value.bytes.data[chg->value.bytes.len - 1] & 1u) : 0u);
            st = trl_buf_append_uvarint(dst, (delta << 2) | (v << 1) | 1u);
        } else if (enc == TRL_SE_2STATE) {
            size_t nbytes = (size_t)((bw + 7) / 8); if (!nbytes) nbytes = 1;
            st = trl_buf_append_uvarint(dst, (delta << 1) | 1u);
            if (st == TRL_OK) {
                if (chg->kind == TRL_VC_VALUE_BYTES) st = trl_append_exact_bytes(dst, chg->value.bytes.data, chg->value.bytes.len, nbytes);
                else {
                    uint8_t tmp[8] = {0};
                    for (size_t j = 0; j < nbytes && j < sizeof(tmp); ++j) tmp[nbytes - 1 - j] = (uint8_t)(chg->value.u64 >> (8 * j));
                    st = trl_append_exact_bytes(dst, tmp, nbytes, nbytes);
                }
            }
        } else if (enc == TRL_SE_4STATE && bw == 1) {
            uint64_t state = (chg->kind == TRL_VC_VALUE_BYTES && chg->value.bytes.len) ? (chg->value.bytes.data[chg->value.bytes.len - 1] & 0x3u) : (chg->value.u64 & 0x3u);
            st = trl_buf_append_uvarint(dst, (delta << 3) | (state << 1) | 1u);
        } else if (enc == TRL_SE_4STATE) {
            size_t binary_bytes = (size_t)((bw + 7) / 8); if (!binary_bytes) binary_bytes = 1;
            size_t packed_bytes = (size_t)((bw * 2 + 7) / 8); if (!packed_bytes) packed_bytes = 1;
            int is_binary = !(chg->kind == TRL_VC_VALUE_BYTES && chg->value.bytes.len > binary_bytes);
            st = trl_buf_append_uvarint(dst, (delta << 1) | (uint64_t)(is_binary ? 1 : 0));
            if (st == TRL_OK) {
                if (chg->kind == TRL_VC_VALUE_BYTES) st = trl_append_exact_bytes(dst, chg->value.bytes.data, chg->value.bytes.len, is_binary ? binary_bytes : packed_bytes);
                else {
                    uint8_t v = (uint8_t)(chg->value.u64 & 1u);
                    st = trl_append_exact_bytes(dst, &v, 1, is_binary ? binary_bytes : packed_bytes);
                }
            }
        } else if (enc == TRL_SE_9STATE) {
            size_t binary_bytes = (size_t)((bw + 7) / 8); if (!binary_bytes) binary_bytes = 1;
            size_t packed_bytes = (size_t)((bw + 1) / 2); if (!packed_bytes) packed_bytes = 1;
            int is_binary = !(chg->kind == TRL_VC_VALUE_BYTES && chg->value.bytes.len > binary_bytes);
            st = trl_buf_append_uvarint(dst, (delta << 1) | (uint64_t)(is_binary ? 1 : 0));
            if (st == TRL_OK) {
                if (chg->kind == TRL_VC_VALUE_BYTES) st = trl_append_exact_bytes(dst, chg->value.bytes.data, chg->value.bytes.len, is_binary ? binary_bytes : packed_bytes);
                else {
                    uint8_t v = (uint8_t)(chg->value.u64 & 1u);
                    st = trl_append_exact_bytes(dst, &v, 1, is_binary ? binary_bytes : packed_bytes);
                }
            }
        } else if (enc == TRL_SE_REAL) {
            union { double d; uint8_t b[8]; } u;
            u.d = (chg->kind == TRL_VC_VALUE_REAL) ? chg->value.real : (double)chg->value.u64;
            st = trl_buf_append_uvarint(dst, delta);
            if (st == TRL_OK) st = trl_buf_append(dst, u.b, 8);
        } else if (enc == TRL_SE_STRING) {
            const char *s = (chg->kind == TRL_VC_VALUE_STRING) ? chg->value.str.str : "";
            size_t slen = s ? strlen(s) : 0;
            st = trl_buf_append_uvarint(dst, delta);
            if (st == TRL_OK) st = trl_buf_append_uvarint(dst, slen);
            if (st == TRL_OK && slen) st = trl_buf_append(dst, s, slen);
        }
    }
    return st;
}

trl_status_t trl_vc_block_encode(const trl_vc_block_t *blk, const uint32_t *var_sig_type_ids, size_t var_sig_cap, const trl_type_registry_t *reg, trl_compress_t compress, trl_buf_t *out) {
    trl_status_t st = TRL_OK;
    trl_buf_t payload, wave_data;
    trl_buf_init(&payload);
    trl_buf_init(&wave_data);
    size_t total_changes = 0;
    for (size_t i = 0; i < blk->var_count; ++i) total_changes += blk->vars[i].count;
    uint64_t *times = total_changes ? (uint64_t *)malloc(total_changes * sizeof(uint64_t)) : NULL;
    trl_vc_var_changes_t **ordered = blk->var_count ? (trl_vc_var_changes_t **)malloc(blk->var_count * sizeof(trl_vc_var_changes_t *)) : NULL;
    if ((total_changes && !times) || (blk->var_count && !ordered)) {
        free(times); free(ordered); return TRL_ERR_OOM;
    }
    size_t ti = 0;
    for (size_t i = 0; i < blk->var_count; ++i) {
        ordered[i] = (trl_vc_var_changes_t *)&blk->vars[i];
        qsort(ordered[i]->items, ordered[i]->count, sizeof(trl_vc_change_t), cmp_change);
        for (size_t j = 0; j < ordered[i]->count; ++j) times[ti++] = ordered[i]->items[j].time;
    }
    qsort(ordered, blk->var_count, sizeof(trl_vc_var_changes_t *), cmp_var);
    qsort(times, total_changes, sizeof(uint64_t), cmp_u64);
    size_t uniq_count = 0;
    for (size_t i = 0; i < total_changes; ++i) {
        if (!uniq_count || times[i] != times[uniq_count - 1]) times[uniq_count++] = times[i];
    }

    st = trl_buf_append_le64(&payload, blk->start_time);
    if (st == TRL_OK) st = trl_buf_append_le64(&payload, blk->end_time);
    if (st == TRL_OK) st = trl_buf_append_uvarint(&payload, uniq_count);
    uint64_t prev = blk->start_time;
    for (size_t i = 0; st == TRL_OK && i < uniq_count; ++i) {
        st = trl_buf_append_uvarint(&payload, times[i] - prev);
        prev = times[i];
    }
    if (st == TRL_OK) st = trl_buf_append_uvarint(&payload, blk->var_count);
    for (size_t i = 0; st == TRL_OK && i < blk->var_count; ++i) {
        const trl_vc_var_changes_t *var = ordered[i];
        const trl_signal_type_entry_t *sig = trl_vc_lookup_signal(var_sig_type_ids, var_sig_cap, reg, var->var_id);
        st = trl_buf_append_uvarint(&payload, var->var_id);
        if (st == TRL_OK) st = trl_vc_encode_init_value(&payload, sig, &var->items[0]);
    }
    int use32 = 1;
    uint64_t *positions = blk->var_count ? (uint64_t *)calloc(blk->var_count, sizeof(uint64_t)) : NULL;
    if (blk->var_count && !positions) st = TRL_ERR_OOM;
    for (size_t i = 0; st == TRL_OK && i < blk->var_count; ++i) {
        const trl_vc_var_changes_t *var = ordered[i];
        const trl_signal_type_entry_t *sig = trl_vc_lookup_signal(var_sig_type_ids, var_sig_cap, reg, var->var_id);
        positions[i] = wave_data.size;
        st = trl_vc_encode_wave(&wave_data, var, sig, times, uniq_count);
    }
    if (st == TRL_OK) st = trl_buf_append_u8(&payload, use32 ? 4 : 8);
    for (size_t i = 0; st == TRL_OK && i < blk->var_count; ++i) {
        if (use32) st = trl_buf_append_le32(&payload, (uint32_t)positions[i]);
        else st = trl_buf_append_le64(&payload, positions[i]);
    }
    if (st == TRL_OK) st = trl_buf_append_uvarint(&payload, wave_data.size);
    if (st == TRL_OK) st = trl_buf_append(&payload, wave_data.data, wave_data.size);

    free(times); free(ordered); free(positions); trl_buf_free(&wave_data);
    if (st != TRL_OK) {
        trl_buf_free(&payload);
        return st;
    }
    if (compress == TRL_COMPRESS_ZLIB && payload.size) {
        trl_buf_t comp;
        trl_buf_init(&comp);
        st = trl_zlib_compress_buf(payload.data, payload.size, &comp);
        trl_buf_free(&payload);
        if (st != TRL_OK) { trl_buf_free(&comp); return st; }
        st = trl_make_block(TRL_BLK_VC_DATA, TRL_FLAG_COMPRESSED, comp.data, comp.size, out);
        trl_buf_free(&comp);
        return st;
    }
    st = trl_make_block(TRL_BLK_VC_DATA, 0, payload.data, payload.size, out);
    trl_buf_free(&payload);
    return st;
}

static trl_status_t trl_skip_init_value(const uint8_t *data, size_t len, size_t *offset, uint8_t enc, uint32_t bw) {
    uint64_t tmp = 0;
    if (enc == TRL_SE_REAL) {
        if (*offset + 8 > len) return TRL_ERR_CORRUPT;
        *offset += 8;
        return TRL_OK;
    }
    if (enc == TRL_SE_STRING) {
        trl_status_t st = trl_decode_uvarint(data, len, offset, &tmp);
        if (st != TRL_OK || *offset + tmp > len) return TRL_ERR_CORRUPT;
        *offset += (size_t)tmp;
        return TRL_OK;
    }
    if (trl_decode_uvarint(data, len, offset, &tmp) != TRL_OK) return TRL_ERR_CORRUPT;
    if (enc == TRL_SE_4STATE) bw = (uint32_t)tmp, tmp = (bw == 1) ? 1 : (uint64_t)((bw * 2 + 7) / 8);
    else if (enc == TRL_SE_9STATE) bw = (uint32_t)tmp, tmp = (uint64_t)((bw + 1) / 2 ? (bw + 1) / 2 : 1);
    else bw = (uint32_t)tmp, tmp = (uint64_t)((bw + 7) / 8 ? (bw + 7) / 8 : 1);
    if (*offset + tmp > len) return TRL_ERR_CORRUPT;
    *offset += (size_t)tmp;
    return TRL_OK;
}

trl_status_t trl_vc_iter_payload(const uint8_t *payload, size_t payload_len, uint8_t flags, const uint32_t *var_sig_type_ids, size_t var_sig_cap, const trl_type_registry_t *reg, trl_vc_cb cb, void *user) {
    trl_status_t st = TRL_OK;
    const uint8_t *data = payload;
    size_t len = payload_len;
    uint8_t *owned = NULL;
    size_t offset = 0;
    uint64_t start_time = 0, end_time = 0, count = 0;

    if (flags & TRL_FLAG_COMPRESSED) {
        uLongf out_len = payload_len ? (uLongf)(payload_len * 16 + 64) : 64;
        while (1) {
            uint8_t *tmp = (uint8_t *)realloc(owned, out_len);
            if (!tmp) { free(owned); return TRL_ERR_OOM; }
            owned = tmp;
            uLongf actual = out_len;
            int rc = uncompress(owned, &actual, payload, (uLong)payload_len);
            if (rc == Z_OK) { data = owned; len = (size_t)actual; break; }
            if (rc != Z_BUF_ERROR) { free(owned); return TRL_ERR_COMPRESS; }
            out_len *= 2;
        }
    }
    if (len < 16) { free(owned); return TRL_ERR_CORRUPT; }
    start_time = trl_read_le64(data + offset); offset += 8;
    end_time = trl_read_le64(data + offset); offset += 8; (void)end_time;
    st = trl_decode_uvarint(data, len, &offset, &count);
    uint64_t *times = count ? (uint64_t *)malloc((size_t)count * sizeof(uint64_t)) : NULL;
    if (count && !times) { free(owned); return TRL_ERR_OOM; }
    uint64_t prev = start_time;
    for (uint64_t i = 0; st == TRL_OK && i < count; ++i) {
        uint64_t delta = 0;
        st = trl_decode_uvarint(data, len, &offset, &delta);
        prev += delta;
        times[i] = prev;
    }
    uint64_t var_count = 0;
    if (st == TRL_OK) st = trl_decode_uvarint(data, len, &offset, &var_count);
    uint32_t *var_ids = var_count ? (uint32_t *)malloc((size_t)var_count * sizeof(uint32_t)) : NULL;
    uint8_t *encs = var_count ? (uint8_t *)malloc((size_t)var_count) : NULL;
    uint32_t *bws = var_count ? (uint32_t *)malloc((size_t)var_count * sizeof(uint32_t)) : NULL;
    if ((var_count && !var_ids) || (var_count && !encs) || (var_count && !bws)) {
        free(owned); free(times); free(var_ids); free(encs); free(bws); return TRL_ERR_OOM;
    }
    for (uint64_t i = 0; st == TRL_OK && i < var_count; ++i) {
        uint64_t var_id = 0;
        const trl_signal_type_entry_t *sig;
        st = trl_decode_uvarint(data, len, &offset, &var_id);
        if (st != TRL_OK) break;
        sig = trl_vc_lookup_signal(var_sig_type_ids, var_sig_cap, reg, (uint32_t)var_id);
        encs[i] = sig ? sig->encoding : TRL_SE_2STATE;
        bws[i] = sig ? sig->bit_width : 1;
        var_ids[i] = (uint32_t)var_id;
        st = trl_skip_init_value(data, len, &offset, encs[i], bws[i]);
    }
    if (st != TRL_OK) {
        free(owned); free(times); free(var_ids); free(encs); free(bws); return st;
    }
    if (offset >= len) { free(owned); free(times); free(var_ids); free(encs); free(bws); return TRL_ERR_CORRUPT; }
    uint8_t pos_size = data[offset++];
    uint64_t *positions = var_count ? (uint64_t *)malloc((size_t)var_count * sizeof(uint64_t)) : NULL;
    if (var_count && !positions) { free(owned); free(times); free(var_ids); free(encs); free(bws); return TRL_ERR_OOM; }
    for (uint64_t i = 0; i < var_count; ++i) {
        if (pos_size == 4) {
            if (offset + 4 > len) { st = TRL_ERR_CORRUPT; break; }
            positions[i] = trl_read_le32(data + offset);
            offset += 4;
        } else {
            if (offset + 8 > len) { st = TRL_ERR_CORRUPT; break; }
            positions[i] = trl_read_le64(data + offset);
            offset += 8;
        }
    }
    uint64_t wave_len = 0;
    if (st == TRL_OK) st = trl_decode_uvarint(data, len, &offset, &wave_len);
    if (st != TRL_OK || offset + wave_len > len) {
        free(owned); free(times); free(var_ids); free(encs); free(bws); free(positions); return TRL_ERR_CORRUPT;
    }
    const uint8_t *wave = data + offset;
    size_t wave_size = (size_t)wave_len;

    for (uint64_t vi = 0; st == TRL_OK && vi < var_count; ++vi) {
        size_t p = (size_t)positions[vi];
        uint64_t rec_count = 0;
        int64_t prev_tidx = -1;
        if (p >= wave_size) { continue; }
        st = trl_decode_uvarint(wave, wave_size, &p, &rec_count);
        for (uint64_t ri = 0; st == TRL_OK && ri < rec_count; ++ri) {
            uint64_t header = 0, delta = 0, tidx = 0;
            uint8_t small[8];
            const uint8_t *val = NULL;
            uint32_t vlen = 0;
            if (encs[vi] == TRL_SE_2STATE && bws[vi] == 1) {
                st = trl_decode_uvarint(wave, wave_size, &p, &header);
                if (st != TRL_OK) break;
                small[0] = (uint8_t)((header >> 1) & 1u);
                delta = header >> 2;
                tidx = (uint64_t)(prev_tidx + 1) + delta;
                prev_tidx = (int64_t)tidx;
                val = small; vlen = 1;
            } else if (encs[vi] == TRL_SE_2STATE) {
                size_t nbytes = (size_t)((bws[vi] + 7) / 8); if (!nbytes) nbytes = 1;
                st = trl_decode_uvarint(wave, wave_size, &p, &header);
                if (st != TRL_OK || p + nbytes > wave_size) break;
                delta = header >> 1;
                tidx = (uint64_t)(prev_tidx + 1) + delta;
                prev_tidx = (int64_t)tidx;
                val = wave + p; vlen = (uint32_t)nbytes; p += nbytes;
            } else if (encs[vi] == TRL_SE_4STATE && bws[vi] == 1) {
                st = trl_decode_uvarint(wave, wave_size, &p, &header);
                if (st != TRL_OK) break;
                small[0] = (uint8_t)((header >> 1) & 0x3u);
                delta = header >> 3;
                tidx = (uint64_t)(prev_tidx + 1) + delta;
                prev_tidx = (int64_t)tidx;
                val = small; vlen = 1;
            } else if (encs[vi] == TRL_SE_4STATE) {
                size_t binary_bytes = (size_t)((bws[vi] + 7) / 8); if (!binary_bytes) binary_bytes = 1;
                size_t packed_bytes = (size_t)((bws[vi] * 2 + 7) / 8); if (!packed_bytes) packed_bytes = 1;
                st = trl_decode_uvarint(wave, wave_size, &p, &header);
                if (st != TRL_OK) break;
                delta = header >> 1;
                tidx = (uint64_t)(prev_tidx + 1) + delta;
                prev_tidx = (int64_t)tidx;
                size_t need = (header & 1u) ? binary_bytes : packed_bytes;
                if (p + need > wave_size) { st = TRL_ERR_CORRUPT; break; }
                val = wave + p; vlen = (uint32_t)need; p += need;
            } else if (encs[vi] == TRL_SE_9STATE) {
                size_t binary_bytes = (size_t)((bws[vi] + 7) / 8); if (!binary_bytes) binary_bytes = 1;
                size_t packed_bytes = (size_t)((bws[vi] + 1) / 2); if (!packed_bytes) packed_bytes = 1;
                st = trl_decode_uvarint(wave, wave_size, &p, &header);
                if (st != TRL_OK) break;
                delta = header >> 1;
                tidx = (uint64_t)(prev_tidx + 1) + delta;
                prev_tidx = (int64_t)tidx;
                size_t need = (header & 1u) ? binary_bytes : packed_bytes;
                if (p + need > wave_size) { st = TRL_ERR_CORRUPT; break; }
                val = wave + p; vlen = (uint32_t)need; p += need;
            } else if (encs[vi] == TRL_SE_REAL) {
                st = trl_decode_uvarint(wave, wave_size, &p, &delta);
                if (st != TRL_OK || p + 8 > wave_size) break;
                tidx = (uint64_t)(prev_tidx + 1) + delta;
                prev_tidx = (int64_t)tidx;
                val = wave + p; vlen = 8; p += 8;
            } else if (encs[vi] == TRL_SE_STRING) {
                st = trl_decode_uvarint(wave, wave_size, &p, &delta);
                if (st != TRL_OK) break;
                tidx = (uint64_t)(prev_tidx + 1) + delta;
                prev_tidx = (int64_t)tidx;
                st = trl_decode_uvarint(wave, wave_size, &p, &header);
                if (st != TRL_OK || p + header > wave_size) break;
                val = wave + p; vlen = (uint32_t)header; p += (size_t)header;
            }
            if (tidx >= count) { st = TRL_ERR_CORRUPT; break; }
            cb(var_ids[vi], times[tidx], val, vlen, user);
        }
    }
    free(owned); free(times); free(var_ids); free(encs); free(bws); free(positions);
    return st;
}
