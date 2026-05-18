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
    memset(idx, 0, sizeof(*idx));
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
    if (st == TRL_OK) st = trl_buf_append_uvarint(&payload, 0);
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
