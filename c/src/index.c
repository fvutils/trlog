#include "varint_internal.h"

static trl_status_t trl_index_ensure(trl_index_entry_t **arr, size_t *cap, size_t need) {
    if (need <= *cap) {
        return TRL_OK;
    }
    size_t new_cap = *cap ? *cap : 8;
    while (new_cap < need) {
        new_cap *= 2;
    }
    trl_index_entry_t *n = (trl_index_entry_t *)realloc(*arr, new_cap * sizeof(trl_index_entry_t));
    if (!n) {
        return TRL_ERR_OOM;
    }
    *arr = n;
    *cap = new_cap;
    return TRL_OK;
}

void trl_index_init(trl_index_t *idx) {
    memset(idx, 0, sizeof(*idx));
}

void trl_index_free(trl_index_t *idx) {
    free(idx->vc_entries);
    free(idx->txn_entries);
    free(idx->hier_offsets);
    memset(idx, 0, sizeof(*idx));
}

trl_status_t trl_index_add_hier_offset(trl_index_t *idx, uint64_t offset) {
    if (idx->hier_count >= idx->hier_cap) {
        size_t new_cap = idx->hier_cap ? idx->hier_cap * 2 : 4;
        uint64_t *n = (uint64_t *)realloc(idx->hier_offsets, new_cap * sizeof(uint64_t));
        if (!n) return TRL_ERR_OOM;
        idx->hier_offsets = n;
        idx->hier_cap = new_cap;
    }
    idx->hier_offsets[idx->hier_count++] = offset;
    return TRL_OK;
}

trl_status_t trl_index_add_vc(trl_index_t *idx, uint64_t start, uint64_t end, uint64_t offset) {
    trl_status_t st = trl_index_ensure(&idx->vc_entries, &idx->vc_cap, idx->vc_count + 1);
    if (st != TRL_OK) return st;
    idx->vc_entries[idx->vc_count++] = (trl_index_entry_t){start, end, offset};
    return TRL_OK;
}

trl_status_t trl_index_add_txn(trl_index_t *idx, uint64_t start, uint64_t end, uint64_t offset) {
    trl_status_t st = trl_index_ensure(&idx->txn_entries, &idx->txn_cap, idx->txn_count + 1);
    if (st != TRL_OK) return st;
    idx->txn_entries[idx->txn_count++] = (trl_index_entry_t){start, end, offset};
    return TRL_OK;
}

trl_status_t trl_index_encode_block(const trl_index_t *idx, trl_buf_t *out) {
    trl_buf_t payload;
    trl_status_t st;
    trl_buf_init(&payload);
    st = trl_buf_append_uvarint(&payload, idx->vc_count);
    for (size_t i = 0; st == TRL_OK && i < idx->vc_count; ++i) {
        st = trl_buf_append_le64(&payload, idx->vc_entries[i].start);
        if (st == TRL_OK) st = trl_buf_append_le64(&payload, idx->vc_entries[i].end);
        if (st == TRL_OK) st = trl_buf_append_le64(&payload, idx->vc_entries[i].offset);
    }
    if (st == TRL_OK) st = trl_buf_append_uvarint(&payload, idx->txn_count);
    for (size_t i = 0; st == TRL_OK && i < idx->txn_count; ++i) {
        st = trl_buf_append_le64(&payload, idx->txn_entries[i].start);
        if (st == TRL_OK) st = trl_buf_append_le64(&payload, idx->txn_entries[i].end);
        if (st == TRL_OK) st = trl_buf_append_le64(&payload, idx->txn_entries[i].offset);
    }
    /* var-range count — not produced by the C writer yet (mirrors 0). */
    if (st == TRL_OK) st = trl_buf_append_uvarint(&payload, 0);
    /* Extended metadata-offset section (sentinel 0x01), written
     * unconditionally as the Python reference does (_index.py). */
    if (st == TRL_OK) st = trl_buf_append_u8(&payload, 0x01);
    if (st == TRL_OK) st = trl_buf_append_le64(&payload, idx->strtab_offset);
    if (st == TRL_OK) st = trl_buf_append_le64(&payload, idx->typereg_offset);
    if (st == TRL_OK) st = trl_buf_append_uvarint(&payload, idx->hier_count);
    for (size_t i = 0; st == TRL_OK && i < idx->hier_count; ++i)
        st = trl_buf_append_le64(&payload, idx->hier_offsets[i]);
    if (st == TRL_OK) st = trl_make_block(TRL_BLK_INDEX, 0, payload.data, payload.size, out);
    trl_buf_free(&payload);
    return st;
}

trl_status_t trl_index_decode_payload(trl_index_t *idx, const uint8_t *payload, size_t payload_len) {
    size_t offset = 0;
    uint64_t count = 0;
    trl_status_t st = trl_decode_uvarint(payload, payload_len, &offset, &count);
    for (uint64_t i = 0; st == TRL_OK && i < count; ++i) {
        if (offset + 24 > payload_len) return TRL_ERR_CORRUPT;
        st = trl_index_add_vc(idx, trl_read_le64(payload + offset), trl_read_le64(payload + offset + 8), trl_read_le64(payload + offset + 16));
        offset += 24;
    }
    if (st != TRL_OK) return st;
    st = trl_decode_uvarint(payload, payload_len, &offset, &count);
    for (uint64_t i = 0; st == TRL_OK && i < count; ++i) {
        if (offset + 24 > payload_len) return TRL_ERR_CORRUPT;
        st = trl_index_add_txn(idx, trl_read_le64(payload + offset), trl_read_le64(payload + offset + 8), trl_read_le64(payload + offset + 16));
        offset += 24;
    }
    if (st != TRL_OK) return st;
    /* var-range count (C does not consume the ranges themselves yet). */
    uint64_t var_ranges = 0;
    st = trl_decode_uvarint(payload, payload_len, &offset, &var_ranges);
    for (uint64_t i = 0; st == TRL_OK && i < var_ranges; ++i) {
        uint64_t vid;
        st = trl_decode_uvarint(payload, payload_len, &offset, &vid);
        if (st == TRL_OK) {
            if (offset + 16 > payload_len) return TRL_ERR_CORRUPT;
            offset += 16;   /* first/last change times (u64 each) */
        }
    }
    if (st != TRL_OK) return st;
    /* Extended metadata-offset section (sentinel 0x01). */
    if (offset < payload_len && payload[offset] == 0x01) {
        offset += 1;
        if (offset + 16 > payload_len) return TRL_ERR_CORRUPT;
        idx->strtab_offset  = trl_read_le64(payload + offset);
        idx->typereg_offset = trl_read_le64(payload + offset + 8);
        offset += 16;
        uint64_t hcount = 0;
        st = trl_decode_uvarint(payload, payload_len, &offset, &hcount);
        for (uint64_t i = 0; st == TRL_OK && i < hcount; ++i) {
            if (offset + 8 > payload_len) return TRL_ERR_CORRUPT;
            st = trl_index_add_hier_offset(idx, trl_read_le64(payload + offset));
            offset += 8;
        }
    }
    return st;
}

const trl_index_entry_t *trl_index_seek_vc(const trl_index_t *idx, uint64_t time) {
    size_t lo = 0, hi = idx->vc_count;
    while (lo < hi) {
        size_t mid = lo + (hi - lo) / 2;
        if (idx->vc_entries[mid].start < time) {
            lo = mid + 1;
        } else {
            hi = mid;
        }
    }
    if (lo >= idx->vc_count) {
        return NULL;
    }
    return &idx->vc_entries[lo];
}
