#include "varint_internal.h"

static trl_status_t trl_txn_block_ensure(trl_txn_block_t *blk, size_t need) {
    if (need <= blk->cap) {
        return TRL_OK;
    }
    size_t new_cap = blk->cap ? blk->cap : 8;
    while (new_cap < need) new_cap *= 2;
    trl_txn_full_rec_t *n = (trl_txn_full_rec_t *)realloc(blk->records, new_cap * sizeof(trl_txn_full_rec_t));
    if (!n) return TRL_ERR_OOM;
    blk->records = n;
    blk->cap = new_cap;
    return TRL_OK;
}

static trl_status_t trl_encode_attr_value(trl_buf_t *buf, const trl_attr_t *attr) {
    union {
        float f32;
        double f64;
        uint8_t b[8];
    } u;
    switch (attr->field_type) {
        case TRL_FT_U8: return trl_buf_append_u8(buf, (uint8_t)attr->value.u64);
        case TRL_FT_U16: return trl_buf_append_le16(buf, (uint16_t)attr->value.u64);
        case TRL_FT_U32: return trl_buf_append_le32(buf, (uint32_t)attr->value.u64);
        case TRL_FT_U64:
        case TRL_FT_ENUM: return trl_buf_append_le64(buf, attr->value.u64);
        case TRL_FT_I8: return trl_buf_append_u8(buf, (uint8_t)attr->value.i64);
        case TRL_FT_I16: return trl_buf_append_le16(buf, (uint16_t)attr->value.i64);
        case TRL_FT_I32: return trl_buf_append_le32(buf, (uint32_t)attr->value.i64);
        case TRL_FT_I64: return trl_buf_append_le64(buf, (uint64_t)attr->value.i64);
        case TRL_FT_F32:
            u.f32 = (float)attr->value.f64;
            return trl_buf_append(buf, u.b, 4);
        case TRL_FT_F64:
            u.f64 = attr->value.f64;
            return trl_buf_append(buf, u.b, 8);
        case TRL_FT_BOOL:
            return trl_buf_append_u8(buf, attr->value.u64 ? 1 : 0);
        case TRL_FT_STRING: {
            const char *s = attr->value.str ? attr->value.str : "";
            size_t len = strlen(s);
            trl_status_t st = trl_buf_append_uvarint(buf, len);
            if (st == TRL_OK) st = trl_buf_append(buf, s, len);
            return st;
        }
        default:
            return TRL_ERR_CORRUPT;
    }
}

void trl_txn_block_init(trl_txn_block_t *blk) {
    memset(blk, 0, sizeof(*blk));
}

void trl_txn_block_free(trl_txn_block_t *blk) {
    if (blk->records) {
        for (size_t i = 0; i < blk->count; ++i) {
            free(blk->records[i].attrs);
        }
    }
    free(blk->records);
    memset(blk, 0, sizeof(*blk));
}

trl_status_t trl_txn_block_begin(trl_txn_block_t *blk, uint64_t start_time) {
    trl_txn_block_free(blk);
    blk->start_time = start_time;
    blk->end_time = start_time;
    return TRL_OK;
}

trl_status_t trl_txn_block_add_full(trl_txn_block_t *blk, uint32_t stream_inst_id, uint32_t txn_type_id, uint64_t txn_id, uint64_t start, uint64_t end, uint64_t parent, uint32_t attr_count, const trl_attr_t *attrs) {
    trl_status_t st = trl_txn_block_ensure(blk, blk->count + 1);
    if (st != TRL_OK) return st;
    trl_txn_full_rec_t *rec = &blk->records[blk->count++];
    memset(rec, 0, sizeof(*rec));
    rec->stream_inst_id = stream_inst_id;
    rec->txn_type_id = txn_type_id;
    rec->txn_id = txn_id;
    rec->start = start;
    rec->end = end;
    rec->parent = parent;
    rec->attr_count = attr_count;
    if (attr_count) {
        rec->attrs = (trl_attr_t *)malloc((size_t)attr_count * sizeof(trl_attr_t));
        if (!rec->attrs) return TRL_ERR_OOM;
        memcpy(rec->attrs, attrs, (size_t)attr_count * sizeof(trl_attr_t));
    }
    if (end > blk->end_time) blk->end_time = end;
    return TRL_OK;
}

trl_status_t trl_txn_block_encode(const trl_txn_block_t *blk, trl_compress_t compress, trl_buf_t *out) {
    trl_buf_t payload;
    trl_status_t st;
    trl_buf_init(&payload);
    st = trl_buf_append_le64(&payload, blk->start_time);
    if (st == TRL_OK) st = trl_buf_append_le64(&payload, blk->end_time);
    if (st == TRL_OK) st = trl_buf_append_uvarint(&payload, blk->count);

    /* Delta-encoding state */
    int64_t prev_txn_id = 0;
    int64_t prev_time   = (int64_t)blk->start_time;

    for (size_t i = 0; st == TRL_OK && i < blk->count; ++i) {
        const trl_txn_full_rec_t *rec = &blk->records[i];
        st = trl_buf_append_u8(&payload, TRL_TXN_FULL);
        if (st == TRL_OK) st = trl_buf_append_uvarint(&payload, rec->stream_inst_id);
        if (st == TRL_OK) st = trl_buf_append_uvarint(&payload, rec->txn_type_id);
        if (st == TRL_OK) st = trl_buf_append_svarint(&payload, (int64_t)rec->txn_id - prev_txn_id);
        if (st == TRL_OK) st = trl_buf_append_svarint(&payload, (int64_t)rec->start - prev_time);
        if (st == TRL_OK) st = trl_buf_append_uvarint(&payload, rec->end - rec->start); /* duration */
        if (st == TRL_OK) st = trl_buf_append_uvarint(&payload, rec->parent);
        if (st == TRL_OK) st = trl_buf_append_uvarint(&payload, rec->attr_count);
        for (uint32_t ai = 0; st == TRL_OK && ai < rec->attr_count; ++ai) {
            /* Match the Python reference row layout: the field *index* (resolved
             * against the schema by the reader), then the value encoded per the
             * field's type. (Not an inline field_type byte.) */
            st = trl_buf_append_uvarint(&payload, rec->attrs[ai].field_idx);
            if (st == TRL_OK) st = trl_encode_attr_value(&payload, &rec->attrs[ai]);
        }
        prev_txn_id = (int64_t)rec->txn_id;
        prev_time   = (int64_t)rec->end;
    }
    if (st != TRL_OK) {
        trl_buf_free(&payload);
        return st;
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
        st = trl_make_block(TRL_BLK_TXN_DATA, (uint8_t)(TRL_FLAG_COMPRESSED | TRL_FLAG_TXN_DELTA), comp.data, comp.size, out);
        trl_buf_free(&comp);
        return st;
    }
    st = trl_make_block(TRL_BLK_TXN_DATA, TRL_FLAG_TXN_DELTA, payload.data, payload.size, out);
    trl_buf_free(&payload);
    return st;
}
