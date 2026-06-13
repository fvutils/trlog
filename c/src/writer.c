#include "varint_internal.h"

static trl_status_t trl_writer_write_all(trl_writer_t *w, const uint8_t *data, size_t len) {
    return fwrite(data, 1, len, w->fp) == len ? TRL_OK : TRL_ERR_IO;
}

static trl_status_t trl_writer_write_buf(trl_writer_t *w, trl_buf_t *buf) {
    trl_status_t st = trl_writer_write_all(w, buf->data, buf->size);
    trl_buf_free(buf);
    return st;
}

static trl_status_t trl_writer_ensure_global_var_cap(trl_writer_t *w, size_t need) {
    if (need <= w->global_var_cap) return TRL_OK;
    size_t new_cap = w->global_var_cap ? w->global_var_cap : 16;
    while (new_cap < need) new_cap *= 2;
    uint32_t *n = (uint32_t *)realloc(w->global_var_sig_type_ids, new_cap * sizeof(uint32_t));
    if (!n) return TRL_ERR_OOM;
    memset(n + w->global_var_cap, 0, (new_cap - w->global_var_cap) * sizeof(uint32_t));
    w->global_var_sig_type_ids = n;
    w->global_var_cap = new_cap;
    return TRL_OK;
}

static trl_status_t trl_writer_write_header(trl_writer_t *w) {
    trl_buf_t payload, block;
    trl_status_t st;
    uint8_t magic[8] = TRL_MAGIC_BYTES;
    trl_buf_init(&payload);
    trl_buf_init(&block);
    st = trl_buf_append(&payload, magic, sizeof(magic));
    if (st == TRL_OK) st = trl_buf_append_u8(&payload, TRL_VERSION_MAJOR);
    if (st == TRL_OK) st = trl_buf_append_u8(&payload, TRL_VERSION_MINOR);
    if (st == TRL_OK) st = trl_buf_append_u8(&payload, (uint8_t)(int8_t)w->timescale_exp);
    if (st == TRL_OK) st = trl_buf_append_le64(&payload, (uint64_t)(int64_t)0);
    if (st == TRL_OK) st = trl_buf_append_le32(&payload, 0);
    if (st == TRL_OK) st = trl_buf_append_uvarint(&payload, 0);
    if (st == TRL_OK) st = trl_buf_append_uvarint(&payload, 0);
    if (st == TRL_OK) st = trl_buf_append_le64(&payload, 0);
    for (int i = 0; st == TRL_OK && i < 16; ++i) st = trl_buf_append_u8(&payload, 0);
    if (st == TRL_OK) st = trl_make_block(TRL_BLK_FILE_HDR, 0, payload.data, payload.size, &block);
    trl_buf_free(&payload);
    if (st != TRL_OK) { trl_buf_free(&block); return st; }
    return trl_writer_write_buf(w, &block);
}

static trl_status_t trl_writer_flush_string_table(trl_writer_t *w) {
    trl_buf_t block; trl_buf_init(&block);
    trl_status_t st = trl_string_table_encode_block(&w->strtab, w->compress, &block);
    if (st != TRL_OK) return st;
    w->index.strtab_offset = (uint64_t)ftell(w->fp);   /* for the index 0x01 section */
    return trl_writer_write_buf(w, &block);
}

static trl_status_t trl_writer_flush_type_registry(trl_writer_t *w) {
    trl_buf_t block; trl_buf_init(&block);
    trl_status_t st = trl_type_registry_encode_block(&w->typereg, w->compress, &block);
    if (st != TRL_OK) return st;
    w->index.typereg_offset = (uint64_t)ftell(w->fp);
    return trl_writer_write_buf(w, &block);
}

trl_writer_t *trl_writer_open(const char *path, int timescale_exp, trl_compress_t compress) {
    trl_writer_t *w = (trl_writer_t *)calloc(1, sizeof(trl_writer_t));
    if (!w) return NULL;
    w->fp = fopen(path, "wb+");
    if (!w->fp) { free(w); return NULL; }
    w->timescale_exp = timescale_exp;
    w->compress = compress;
    trl_string_table_init(&w->strtab);
    trl_type_registry_init(&w->typereg);
    trl_index_init(&w->index);
    trl_hierarchy_init(&w->hierarchy);
    trl_vc_block_init(&w->vc);
    trl_txn_block_init(&w->txn);
    w->status = trl_writer_write_header(w);
    if (w->status != TRL_OK) {
        trl_writer_close(w);
        return NULL;
    }
    return w;
}

uint32_t trl_intern(trl_writer_t *w, const char *s) {
    return trl_string_table_intern(&w->strtab, s);
}

uint32_t trl_add_signal_type(trl_writer_t *w, uint8_t encoding, uint32_t bit_width, uint8_t radix) {
    return trl_type_registry_add_signal_type(&w->typereg, encoding, bit_width, radix);
}

uint32_t trl_add_txn_schema(trl_writer_t *w, uint32_t name_id, uint32_t field_count, const trl_field_def_t *fields) {
    return trl_type_registry_add_txn_schema(&w->typereg, name_id, field_count, fields);
}

trl_status_t trl_begin_hierarchy(trl_writer_t *w, uint32_t hier_id, uint8_t kind, uint32_t name_id) {
    w->hierarchy_active = true;
    return w->status = trl_hierarchy_begin(&w->hierarchy, hier_id, kind, name_id);
}

trl_status_t trl_hier_begin_scope(trl_writer_t *w, uint8_t scope_type, uint32_t name_id, uint32_t component_id,
                                 uint32_t src_file_str_id, uint32_t src_line) {
    return w->status = trl_hierarchy_begin_scope(&w->hierarchy, scope_type, name_id, component_id,
                                                 src_file_str_id, src_line);
}

trl_status_t trl_hier_end_scope(trl_writer_t *w) {
    return w->status = trl_hierarchy_end_scope(&w->hierarchy);
}

uint32_t trl_hier_add_var(trl_writer_t *w, uint32_t name_id, uint32_t sig_type_id, uint8_t dir,
                         uint32_t src_file_str_id, uint32_t src_line) {
    uint32_t var_id = trl_hierarchy_add_var(&w->hierarchy, name_id, sig_type_id, dir,
                                            src_file_str_id, src_line);
    if (!var_id) return 0;
    if (trl_writer_ensure_global_var_cap(w, (size_t)var_id + 1) != TRL_OK) return 0;
    w->global_var_sig_type_ids[var_id] = sig_type_id;
    return var_id;
}

trl_status_t trl_flush_hierarchy(trl_writer_t *w) {
    trl_buf_t block; trl_buf_init(&block);
    trl_status_t st = trl_hierarchy_encode_block(&w->hierarchy, w->compress, &block);
    if (st == TRL_OK) st = trl_index_add_hier_offset(&w->index, (uint64_t)ftell(w->fp));
    if (st == TRL_OK) st = trl_writer_write_buf(w, &block);
    else trl_buf_free(&block);
    w->hierarchy_active = false;
    return w->status = st;
}

trl_status_t trl_vc_begin(trl_writer_t *w, uint64_t start_time) {
    w->vc_active = true;
    return w->status = trl_vc_block_begin(&w->vc, start_time);
}

trl_status_t trl_vc_change_u64(trl_writer_t *w, uint32_t var_id, uint64_t time, uint64_t value) {
    return w->status = trl_vc_block_add_u64(&w->vc, var_id, time, value);
}

trl_status_t trl_vc_change_bytes(trl_writer_t *w, uint32_t var_id, uint64_t time, const uint8_t *value, uint32_t len) {
    return w->status = trl_vc_block_add_bytes(&w->vc, var_id, time, value, len);
}

trl_status_t trl_vc_change_str(trl_writer_t *w, uint32_t var_id, uint64_t time, const char *value) {
    return w->status = trl_vc_block_add_str(&w->vc, var_id, time, value);
}

trl_status_t trl_vc_change_real(trl_writer_t *w, uint32_t var_id, uint64_t time, double value) {
    return w->status = trl_vc_block_add_real(&w->vc, var_id, time, value);
}

trl_status_t trl_vc_flush(trl_writer_t *w) {
    trl_buf_t block; trl_buf_init(&block);
    long off = ftell(w->fp);
    /* Dispatch the encode through the resolved value-change codec. The default
     * path (design §4.3) is the registered core codec whose wrapper calls the
     * same trl_vc_block_encode, so the bytes are identical; a codec selected via
     * trl_writer_set_vc_codec (Phase 3.5) gets its threaded per-stream state. */
    const trl_vc_codec_t *codec = w->vc_codec ? w->vc_codec
        : trl_lookup_vc_codec(TRL_CODEC_CORE_VALUECHANGE);
    void *self = w->vc_codec ? w->vc_codec_self : NULL;
    trl_store store = { .writer = w, .reader = NULL };
    trl_blkout out = { .buf = &block };
    trl_status_t st = codec->encode_block(self, &store, &out) == 0 ? TRL_OK : TRL_ERR_IO;
    if (st == TRL_OK) st = trl_index_add_vc(&w->index, w->vc.start_time, w->vc.end_time, (uint64_t)off);
    if (st == TRL_OK) st = trl_writer_write_buf(w, &block);
    else trl_buf_free(&block);
    w->vc_active = false;
    return w->status = st;
}

trl_status_t trl_txn_begin_block(trl_writer_t *w, uint64_t start_time) {
    w->txn_active = true;
    return w->status = trl_txn_block_begin(&w->txn, start_time);
}

trl_status_t trl_txn_full(trl_writer_t *w, uint32_t stream_inst_id, uint32_t txn_type_id, uint64_t txn_id, uint64_t start, uint64_t end, uint64_t parent, uint32_t attr_count, const trl_attr_t *attrs) {
    return w->status = trl_txn_block_add_full(&w->txn, stream_inst_id, txn_type_id, txn_id, start, end, parent, attr_count, attrs);
}

trl_status_t trl_txn_end_block(trl_writer_t *w) {
    trl_buf_t block; trl_buf_init(&block);
    long off = ftell(w->fp);
    /* Dispatch through the registered core record codec (byte-identical). */
    const trl_txn_codec_t *codec = trl_lookup_txn_codec(TRL_CODEC_CORE_RECORD);
    trl_store store = { .writer = w, .reader = NULL };
    trl_blkout out = { .buf = &block };
    trl_status_t st = codec->encode_block(NULL, &store, &out) == 0 ? TRL_OK : TRL_ERR_IO;
    if (st == TRL_OK) st = trl_index_add_txn(&w->index, w->txn.start_time, w->txn.end_time, (uint64_t)off);
    if (st == TRL_OK) st = trl_writer_write_buf(w, &block);
    else trl_buf_free(&block);
    w->txn_active = false;
    return w->status = st;
}

trl_status_t trl_writer_close(trl_writer_t *w) {
    trl_status_t st = TRL_OK;
    if (!w) return TRL_OK;
    if (w->fp) {
        if (w->hierarchy_active && st == TRL_OK) st = trl_flush_hierarchy(w);
        if (w->vc_active && st == TRL_OK) st = trl_vc_flush(w);
        if (w->txn_active && st == TRL_OK) st = trl_txn_end_block(w);
        /* Codec trace-finalize lifecycle (design §4.7): after the last
         * encode_block, before the index/footer. Built-ins are no-ops. */
        {
            trl_store store = { .writer = w, .reader = NULL };
            /* Finalize/close the resolved VC codec (selected one if set, else
             * core) with its threaded per-stream state; then the core TXN. */
            const trl_vc_codec_t *vc = w->vc_codec ? w->vc_codec
                : trl_lookup_vc_codec(TRL_CODEC_CORE_VALUECHANGE);
            void *vc_self = w->vc_codec ? w->vc_codec_self : NULL;
            const trl_txn_codec_t *tx = trl_lookup_txn_codec(TRL_CODEC_CORE_RECORD);
            if (vc) { vc->finalize(vc_self, &store); vc->close(vc_self); }
            if (tx) { tx->finalize(NULL, &store); tx->close(NULL); }
            w->vc_codec_self = NULL;   /* closed; don't double-close */
        }
        if (st == TRL_OK) st = trl_writer_flush_string_table(w);
        if (st == TRL_OK) st = trl_writer_flush_type_registry(w);
        if (st == TRL_OK) {
            trl_buf_t block; trl_buf_init(&block);
            long index_off = ftell(w->fp);
            st = trl_index_encode_block(&w->index, &block);
            if (st == TRL_OK) st = trl_writer_write_buf(w, &block);
            else trl_buf_free(&block);
            if (st == TRL_OK) {
                uint8_t tmp[8];
                trl_write_le64(tmp, (uint64_t)index_off);
                if (fseek(w->fp, TRL_INDEX_OFFSET_FIELD_POS, SEEK_SET) == 0) {
                    if (fwrite(tmp, 1, 8, w->fp) != 8) st = TRL_ERR_IO;
                }
            }
        }
        fclose(w->fp);
        w->fp = NULL;
    }
    trl_string_table_free(&w->strtab);
    trl_type_registry_free(&w->typereg);
    trl_index_free(&w->index);
    trl_hierarchy_free(&w->hierarchy);
    trl_vc_block_free(&w->vc);
    trl_txn_block_free(&w->txn);
    free(w->global_var_sig_type_ids);
    free(w);
    return st;
}
