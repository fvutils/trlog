#include "varint_internal.h"
#include "trl_portable.h"

size_t trl_reader_src_read(trl_reader_t *r, void *buf, size_t n) {
    if (r->fp) return fread(buf, 1, n, r->fp);
    size_t avail = r->mem_pos < r->mem_len ? r->mem_len - r->mem_pos : 0;
    if (n > avail) n = avail;
    if (n) memcpy(buf, r->mem + r->mem_pos, n);
    r->mem_pos += n;
    return n;
}

int trl_reader_src_seek(trl_reader_t *r, uint64_t off) {
    if (r->fp) {
        clearerr(r->fp);
        return trl_fseek64(r->fp, (int64_t)off, SEEK_SET);
    }
    if (off > r->mem_len) return -1;
    r->mem_pos = (size_t)off;
    return 0;
}

/* Reads the next block header. Any short read (clean end of data or a
 * truncated trailing header) is TRL_ERR_IO; callers stop there. */
static trl_status_t trl_reader_read_header(trl_reader_t *r, uint8_t *type, uint8_t *flags, uint64_t *length) {
    uint8_t hdr[TRL_BLOCK_HEADER_SIZE];
    if (trl_reader_src_read(r, hdr, sizeof(hdr)) != sizeof(hdr)) return TRL_ERR_IO;
    *type = hdr[0];
    *flags = hdr[1];
    *length = trl_read_le64(hdr + 2);
    return TRL_OK;
}

static trl_status_t trl_reader_scan(trl_reader_t *r) {
    trl_status_t st = TRL_OK;
    if (trl_reader_src_seek(r, 0) != 0) return TRL_ERR_IO;
    while (1) {
        uint8_t type = 0, flags = 0;
        uint64_t length = 0;
        st = trl_reader_read_header(r, &type, &flags, &length);
        if (st != TRL_OK) break;
        if (length < TRL_BLOCK_HEADER_SIZE) return TRL_ERR_CORRUPT;
        size_t payload_len = (size_t)(length - TRL_BLOCK_HEADER_SIZE);
        uint8_t *payload = payload_len ? (uint8_t *)malloc(payload_len) : NULL;
        if (payload_len && !payload) return TRL_ERR_OOM;
        if (payload_len && trl_reader_src_read(r, payload, payload_len) != payload_len) { free(payload); return TRL_ERR_IO; }
        if (type == TRL_BLK_STR_TABLE) {
            st = trl_string_table_decode_payload(&r->strtab, payload, payload_len, flags);
            if (st != TRL_OK) { free(payload); return st; }
        } else if (type == TRL_BLK_FILE_HDR) {
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
    /* No file-header block: empty or not a trace at all. */
    if (r->version_major == 0) return TRL_ERR_MAGIC;
    return TRL_OK;
}

static trl_reader_t *trl_reader_init(trl_reader_t *r) {
    trl_string_table_init(&r->strtab);
    trl_type_registry_init(&r->typereg);
    trl_index_init(&r->index);
    r->status = trl_reader_scan(r);
    if (r->status != TRL_OK) {
        trl_reader_close(r);
        return NULL;
    }
    return r;
}

trl_reader_t *trl_reader_open(const char *path) {
    trl_reader_t *r = (trl_reader_t *)calloc(1, sizeof(trl_reader_t));
    if (!r) return NULL;
    r->fp = fopen(path, "rb");
    if (!r->fp) { free(r); return NULL; }
    return trl_reader_init(r);
}

trl_reader_t *trl_reader_open_mem(const void *buf, size_t len) {
    if (!buf && len) return NULL;
    trl_reader_t *r = (trl_reader_t *)calloc(1, sizeof(trl_reader_t));
    if (!r) return NULL;
    r->mem = (const uint8_t *)buf;
    r->mem_len = len;
    return trl_reader_init(r);
}

trl_status_t trl_iter_vc(trl_reader_t *r, trl_vc_cb cb, void *user) {
    trl_status_t st = TRL_OK;
    if (trl_reader_src_seek(r, 0) != 0) return TRL_ERR_IO;
    while (1) {
        uint8_t type = 0, flags = 0;
        uint64_t length = 0;
        st = trl_reader_read_header(r, &type, &flags, &length);
        if (st != TRL_OK) break;
        if (length < TRL_BLOCK_HEADER_SIZE) return TRL_ERR_CORRUPT;
        size_t payload_len = (size_t)(length - TRL_BLOCK_HEADER_SIZE);
        uint8_t *payload = payload_len ? (uint8_t *)malloc(payload_len) : NULL;
        if (payload_len && !payload) return TRL_ERR_OOM;
        if (payload_len && trl_reader_src_read(r, payload, payload_len) != payload_len) { free(payload); return TRL_ERR_IO; }
        if (type == TRL_BLK_VC_DATA) {
            /* Dispatch decode through the registered core value-change codec
             * (design §4.3); the wrapper calls the same trl_vc_iter_payload. */
            const trl_vc_codec_t *codec = trl_lookup_vc_codec(TRL_CODEC_CORE_VALUECHANGE);
            if (flags & TRL_FLAG_STRUCT_CODEC) {
                /* Structural-codec block: the payload starts with the interned
                 * codec id. A block whose codec is not registered here is
                 * skipped, never an error (design §4.2). */
                size_t pos = 0;
                uint64_t sid = 0;
                const char *cid = NULL;
                if (trl_decode_uvarint(payload, payload_len, &pos, &sid) == TRL_OK)
                    cid = trl_string_table_lookup(&r->strtab, (uint32_t)sid);
                codec = cid ? trl_lookup_vc_codec(cid) : NULL;
                if (!codec || !codec->decode_block) { free(payload); continue; }
            }
            trl_store store = { .writer = NULL, .reader = r };
            int rc = codec->decode_block(NULL, &store, payload, payload_len, flags, cb, user);
            free(payload);
            if (rc != 0) return (trl_status_t)rc;
        } else {
            free(payload);
        }
    }
    return TRL_OK;
}

trl_status_t trl_iter_hier(trl_reader_t *r, trl_hier_cb cb, void *user) {
    trl_status_t st = TRL_OK;
    if (!r || !cb) return TRL_ERR_INVALID;
    if (trl_reader_src_seek(r, 0) != 0) return TRL_ERR_IO;
    while (1) {
        uint8_t type = 0, flags = 0;
        uint64_t length = 0;
        st = trl_reader_read_header(r, &type, &flags, &length);
        if (st != TRL_OK) break;
        if (length < TRL_BLOCK_HEADER_SIZE) return TRL_ERR_CORRUPT;
        size_t payload_len = (size_t)(length - TRL_BLOCK_HEADER_SIZE);
        uint8_t *payload = payload_len ? (uint8_t *)malloc(payload_len) : NULL;
        if (payload_len && !payload) return TRL_ERR_OOM;
        if (payload_len && trl_reader_src_read(r, payload, payload_len) != payload_len) { free(payload); return TRL_ERR_IO; }
        if (type == TRL_BLK_HIERARCHY)
            st = trl_hierarchy_walk_payload(payload, payload_len, flags, &r->strtab, &r->typereg, cb, user);
        free(payload);
        if (st != TRL_OK) return st;
    }
    return TRL_OK;
}

int trl_reader_timescale_exp(const trl_reader_t *r) {
    return r ? r->timescale_exp : 0;
}

trl_status_t trl_reader_close(trl_reader_t *r) {
    if (!r) return TRL_OK;
    if (r->fp) fclose(r->fp);
    trl_string_table_free(&r->strtab);
    trl_type_registry_free(&r->typereg);
    trl_index_free(&r->index);
    free(r->var_sig_type_ids);
    free(r);
    return TRL_OK;
}
