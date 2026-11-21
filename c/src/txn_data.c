#include "varint_internal.h"

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
        case TRL_FT_TIME: return trl_buf_append_le64(buf, attr->value.u64);
        case TRL_FT_ENUM: return trl_buf_append_uvarint(buf, attr->value.u64);
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
        case TRL_FT_BYTES: {
            uint32_t len = attr->value.vec.len;
            if (len && !attr->value.vec.data) return TRL_ERR_INVALID;
            trl_status_t st = trl_buf_append_uvarint(buf, len);
            if (st == TRL_OK && len) st = trl_buf_append(buf, attr->value.vec.data, len);
            return st;
        }
        case TRL_FT_BITVEC:
        case TRL_FT_LOGICVEC: {
            uint32_t width = attr->value.vec.width;
            size_t nbytes = ((size_t)width + 7) / 8;
            if (attr->field_type == TRL_FT_LOGICVEC) nbytes *= 2;  /* aval + bval */
            if (nbytes && !attr->value.vec.data) return TRL_ERR_INVALID;
            trl_status_t st = trl_buf_append_uvarint(buf, width);
            if (st == TRL_OK && nbytes) st = trl_buf_append(buf, attr->value.vec.data, nbytes);
            return st;
        }
        default:
            return TRL_ERR_INVALID;
    }
}

void trl_txn_block_init(trl_txn_block_t *blk) {
    memset(blk, 0, sizeof(*blk));
    blk->stream_inst_id = TRL_STREAM_MIXED;
}

void trl_txn_block_free(trl_txn_block_t *blk) {
    trl_buf_free(&blk->recs);
    trl_txn_block_init(blk);
}

trl_status_t trl_txn_block_begin(trl_txn_block_t *blk, uint64_t start_time, uint32_t stream_inst_id) {
    /* Keep the record buffer's allocation; only its contents are reset. */
    blk->recs.size = 0;
    blk->count = 0;
    blk->start_time = start_time;
    blk->end_time = start_time;
    blk->stream_inst_id = stream_inst_id;
    blk->prev_txn_id = 0;
    blk->prev_time = (int64_t)start_time;
    return TRL_OK;
}

trl_status_t trl_txn_block_add_full(trl_txn_block_t *blk, uint32_t stream_inst_id, uint32_t txn_type_id, uint64_t txn_id, uint64_t start, uint64_t end, uint64_t parent, uint32_t attr_count, const trl_attr_t *attrs) {
    if (blk->stream_inst_id != TRL_STREAM_MIXED && stream_inst_id != blk->stream_inst_id)
        return TRL_ERR_INVALID;
    if (attr_count && !attrs) return TRL_ERR_INVALID;
    trl_buf_t *b = &blk->recs;
    size_t mark = b->size;
    trl_status_t st = trl_buf_append_u8(b, TRL_TXN_FULL);
    if (st == TRL_OK) st = trl_buf_append_uvarint(b, stream_inst_id);
    if (st == TRL_OK) st = trl_buf_append_uvarint(b, txn_type_id);
    if (st == TRL_OK) st = trl_buf_append_svarint(b, (int64_t)txn_id - blk->prev_txn_id);
    if (st == TRL_OK) st = trl_buf_append_svarint(b, (int64_t)start - blk->prev_time);
    if (st == TRL_OK) st = trl_buf_append_uvarint(b, end - start); /* duration */
    if (st == TRL_OK) st = trl_buf_append_uvarint(b, parent);
    if (st == TRL_OK) st = trl_buf_append_uvarint(b, attr_count);
    for (uint32_t ai = 0; st == TRL_OK && ai < attr_count; ++ai) {
        /* Match the Python reference row layout: the field *index* (resolved
         * against the schema by the reader), then the value encoded per the
         * field's type. (Not an inline field_type byte.) */
        st = trl_buf_append_uvarint(b, attrs[ai].field_idx);
        if (st == TRL_OK) st = trl_encode_attr_value(b, &attrs[ai]);
    }
    if (st != TRL_OK) {
        b->size = mark;  /* drop the partial record; the block stays valid */
        return st;
    }
    blk->count++;
    blk->prev_txn_id = (int64_t)txn_id;
    blk->prev_time   = (int64_t)end;
    if (end > blk->end_time) blk->end_time = end;
    return TRL_OK;
}

trl_status_t trl_txn_block_add_begin(trl_txn_block_t *blk, uint32_t stream_inst_id, uint32_t txn_type_id, uint64_t txn_id, uint64_t start, uint64_t parent) {
    if (blk->stream_inst_id != TRL_STREAM_MIXED && stream_inst_id != blk->stream_inst_id)
        return TRL_ERR_INVALID;
    trl_buf_t *b = &blk->recs;
    size_t mark = b->size;
    trl_status_t st = trl_buf_append_u8(b, TRL_TXN_BEGIN);
    if (st == TRL_OK) st = trl_buf_append_uvarint(b, stream_inst_id);
    if (st == TRL_OK) st = trl_buf_append_uvarint(b, txn_type_id);
    if (st == TRL_OK) st = trl_buf_append_svarint(b, (int64_t)txn_id - blk->prev_txn_id);
    if (st == TRL_OK) st = trl_buf_append_svarint(b, (int64_t)start - blk->prev_time);
    if (st == TRL_OK) st = trl_buf_append_uvarint(b, parent);
    if (st != TRL_OK) {
        b->size = mark;
        return st;
    }
    blk->count++;
    blk->prev_txn_id = (int64_t)txn_id;
    blk->prev_time   = (int64_t)start;
    if (start > blk->end_time) blk->end_time = start;
    return TRL_OK;
}

trl_status_t trl_txn_block_add_attr(trl_txn_block_t *blk, uint64_t txn_id, const trl_attr_t *attr) {
    trl_buf_t *b = &blk->recs;
    size_t mark = b->size;
    trl_status_t st = trl_buf_append_u8(b, TRL_TXN_ATTR);
    if (st == TRL_OK) st = trl_buf_append_svarint(b, (int64_t)txn_id - blk->prev_txn_id);
    if (st == TRL_OK) st = trl_buf_append_uvarint(b, 1);          /* one attr per record */
    if (st == TRL_OK) st = trl_buf_append_uvarint(b, attr->field_idx);
    if (st == TRL_OK) st = trl_buf_append_u8(b, (uint8_t)attr->field_type);
    if (st == TRL_OK) st = trl_encode_attr_value(b, attr);
    if (st != TRL_OK) {
        b->size = mark;
        return st;
    }
    blk->count++;
    blk->prev_txn_id = (int64_t)txn_id;   /* prev_time unchanged: TR_ATTR carries no time */
    return TRL_OK;
}

trl_status_t trl_txn_block_add_end(trl_txn_block_t *blk, uint64_t txn_id, uint64_t end_time) {
    trl_buf_t *b = &blk->recs;
    size_t mark = b->size;
    trl_status_t st = trl_buf_append_u8(b, TRL_TXN_END);
    if (st == TRL_OK) st = trl_buf_append_svarint(b, (int64_t)txn_id - blk->prev_txn_id);
    if (st == TRL_OK) st = trl_buf_append_svarint(b, (int64_t)end_time - blk->prev_time);
    if (st != TRL_OK) {
        b->size = mark;
        return st;
    }
    blk->count++;
    blk->prev_txn_id = (int64_t)txn_id;
    blk->prev_time   = (int64_t)end_time;
    if (end_time > blk->end_time) blk->end_time = end_time;
    return TRL_OK;
}

trl_status_t trl_txn_block_add_link(trl_txn_block_t *blk, uint8_t link_type, uint64_t src, uint64_t tgt, uint32_t label_sid) {
    trl_buf_t *b = &blk->recs;
    size_t mark = b->size;
    trl_status_t st = trl_buf_append_u8(b, TRL_TXN_LINK);
    if (st == TRL_OK) st = trl_buf_append_u8(b, link_type);
    if (st == TRL_OK) st = trl_buf_append_svarint(b, (int64_t)src - blk->prev_txn_id);
    if (st == TRL_OK) st = trl_buf_append_uvarint(b, tgt);
    if (st == TRL_OK) st = trl_buf_append_uvarint(b, label_sid);
    if (st != TRL_OK) {
        b->size = mark;
        return st;
    }
    blk->count++;
    blk->prev_txn_id = (int64_t)src;      /* prev_time unchanged: TR_LINK carries no time */
    return TRL_OK;
}

trl_status_t trl_txn_block_add_meta(trl_txn_block_t *blk, uint64_t txn_id, uint32_t key_sid, uint32_t val_sid) {
    trl_buf_t *b = &blk->recs;
    size_t mark = b->size;
    trl_status_t st = trl_buf_append_u8(b, TRL_TXN_META);
    if (st == TRL_OK) st = trl_buf_append_svarint(b, (int64_t)txn_id - blk->prev_txn_id);
    if (st == TRL_OK) st = trl_buf_append_uvarint(b, key_sid);
    if (st == TRL_OK) st = trl_buf_append_uvarint(b, val_sid);
    if (st != TRL_OK) {
        b->size = mark;
        return st;
    }
    blk->count++;
    blk->prev_txn_id = (int64_t)txn_id;   /* prev_time unchanged: TR_META carries no time */
    return TRL_OK;
}

trl_status_t trl_txn_block_encode(const trl_txn_block_t *blk, trl_compress_t compress, trl_buf_t *out) {
    trl_buf_t payload;
    trl_status_t st;
    trl_buf_init(&payload);
    st = trl_buf_append_le64(&payload, blk->start_time);
    if (st == TRL_OK) st = trl_buf_append_le64(&payload, blk->end_time);
    if (st == TRL_OK) st = trl_buf_append_uvarint(&payload, blk->count);
    if (st == TRL_OK && blk->recs.size) st = trl_buf_append(&payload, blk->recs.data, blk->recs.size);
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
