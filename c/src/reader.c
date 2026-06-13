#include "varint_internal.h"

static trl_status_t trl_reader_read_header(FILE *fp, uint8_t *type, uint8_t *flags, uint64_t *length) {
    uint8_t hdr[TRL_BLOCK_HEADER_SIZE];
    if (fread(hdr, 1, sizeof(hdr), fp) != sizeof(hdr)) {
        return feof(fp) ? TRL_ERR_IO : TRL_ERR_IO;
    }
    *type = hdr[0];
    *flags = hdr[1];
    *length = trl_read_le64(hdr + 2);
    return TRL_OK;
}

static trl_status_t trl_reader_scan(trl_reader_t *r) {
    trl_status_t st = TRL_OK;
    if (fseek(r->fp, 0, SEEK_SET) != 0) return TRL_ERR_IO;
    while (1) {
        long off = ftell(r->fp);
        uint8_t type = 0, flags = 0;
        uint64_t length = 0;
        if (feof(r->fp)) break;
        if (fgetc(r->fp) == EOF) break;
        fseek(r->fp, off, SEEK_SET);
        st = trl_reader_read_header(r->fp, &type, &flags, &length);
        if (st != TRL_OK) break;
        if (length < TRL_BLOCK_HEADER_SIZE) return TRL_ERR_CORRUPT;
        size_t payload_len = (size_t)(length - TRL_BLOCK_HEADER_SIZE);
        uint8_t *payload = payload_len ? (uint8_t *)malloc(payload_len) : NULL;
        if (payload_len && !payload) return TRL_ERR_OOM;
        if (payload_len && fread(payload, 1, payload_len, r->fp) != payload_len) { free(payload); return TRL_ERR_IO; }
        if (type == TRL_BLK_FILE_HDR) {
            if (payload_len < 8 + 1 + 1 + 1 + 8) { free(payload); return TRL_ERR_CORRUPT; }
            if (memcmp(payload, TRL_MAGIC_BYTES, 8) != 0) { free(payload); return TRL_ERR_MAGIC; }
            r->version_major = payload[8];
            r->version_minor = payload[9];
            if (r->version_major != 1 && r->version_major != 2) { free(payload); return TRL_ERR_VERSION; }
            r->timescale_exp = (int8_t)payload[10];
            r->timezero = (int64_t)trl_read_le64(payload + 11);
        } else if (type == TRL_BLK_TYPE_REG) {
            st = trl_type_registry_decode_payload(&r->typereg, payload, payload_len, flags);
            if (st != TRL_OK) { free(payload); return st; }
        } else if (type == TRL_BLK_HIERARCHY) {
            st = trl_hierarchy_decode_payload(payload, payload_len, flags, &r->var_sig_type_ids, &r->var_sig_cap);
            if (st != TRL_OK) { free(payload); return st; }
        } else if (type == TRL_BLK_INDEX) {
            st = trl_index_decode_payload(&r->index, payload, payload_len);
            if (st != TRL_OK) { free(payload); return st; }
        }
        free(payload);
    }
    clearerr(r->fp);
    return TRL_OK;
}

trl_reader_t *trl_reader_open(const char *path) {
    trl_reader_t *r = (trl_reader_t *)calloc(1, sizeof(trl_reader_t));
    if (!r) return NULL;
    r->fp = fopen(path, "rb");
    if (!r->fp) { free(r); return NULL; }
    trl_type_registry_init(&r->typereg);
    trl_index_init(&r->index);
    r->status = trl_reader_scan(r);
    if (r->status != TRL_OK) {
        trl_reader_close(r);
        return NULL;
    }
    return r;
}

trl_status_t trl_iter_vc(trl_reader_t *r, trl_vc_cb cb, void *user) {
    trl_status_t st = TRL_OK;
    if (fseek(r->fp, 0, SEEK_SET) != 0) return TRL_ERR_IO;
    while (1) {
        long off = ftell(r->fp);
        uint8_t type = 0, flags = 0;
        uint64_t length = 0;
        if (feof(r->fp)) break;
        if (fgetc(r->fp) == EOF) break;
        fseek(r->fp, off, SEEK_SET);
        st = trl_reader_read_header(r->fp, &type, &flags, &length);
        if (st != TRL_OK) break;
        if (length < TRL_BLOCK_HEADER_SIZE) return TRL_ERR_CORRUPT;
        size_t payload_len = (size_t)(length - TRL_BLOCK_HEADER_SIZE);
        uint8_t *payload = payload_len ? (uint8_t *)malloc(payload_len) : NULL;
        if (payload_len && !payload) return TRL_ERR_OOM;
        if (payload_len && fread(payload, 1, payload_len, r->fp) != payload_len) { free(payload); return TRL_ERR_IO; }
        if (type == TRL_BLK_VC_DATA) {
            /* Dispatch decode through the registered core value-change codec
             * (design §4.3); the wrapper calls the same trl_vc_iter_payload. */
            const trl_vc_codec_t *codec = trl_lookup_vc_codec(TRL_CODEC_CORE_VALUECHANGE);
            trl_store store = { .writer = NULL, .reader = r };
            int rc = codec->decode_block(NULL, &store, payload, payload_len, flags, cb, user);
            free(payload);
            if (rc != 0) return (trl_status_t)rc;
        } else {
            free(payload);
        }
    }
    clearerr(r->fp);
    return TRL_OK;
}

trl_status_t trl_reader_close(trl_reader_t *r) {
    if (!r) return TRL_OK;
    if (r->fp) fclose(r->fp);
    trl_type_registry_free(&r->typereg);
    trl_index_free(&r->index);
    free(r->var_sig_type_ids);
    free(r);
    return TRL_OK;
}
