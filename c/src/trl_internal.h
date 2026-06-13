#pragma once

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <zlib.h>
#include <lz4.h>

#include "trl/trl.h"
#include "trl/trl_codec.h"

#define TRL_MAGIC_BYTES "TRL\x00\x00\x00\x01\x00"
#define TRL_MAGIC_LEN 8
#define TRL_VERSION_MAJOR 2
#define TRL_VERSION_MINOR 1
#define TRL_BLOCK_HEADER_SIZE 10
#define TRL_INDEX_OFFSET_FIELD_POS 35

#define TRL_BLK_FILE_HDR 0x00
#define TRL_BLK_STR_TABLE 0x01
#define TRL_BLK_TYPE_REG 0x02
#define TRL_BLK_HIERARCHY 0x03
#define TRL_BLK_VC_DATA 0x10
#define TRL_BLK_TXN_DATA 0x11
#define TRL_BLK_EXT 0x20
#define TRL_BLK_INDEX 0xFE

#define TRL_FLAG_COMPRESSED 0x01
#define TRL_FLAG_COMPRESS_ALG 0x02
/* BLK_VC_DATA encoding flags (survive whole-block decompression) */
#define TRL_FLAG_WAVE_XOR_DELTA 0x10
/* Per-wave LZ4 compression (sub-section flag; valid only when the block is not
 * whole-block compressed). Each wave entry is prefixed with a flag byte. */
#define TRL_FLAG_WAVE_LZ4 0x08
/* Per-wave zlib compression (sub-section flag, like FLAG_WAVE_LZ4) */
#define TRL_FLAG_WAVE_ZLIB 0x20
/* Seekability footer: last 8 bytes of payload = u64 offset to position table */
#define TRL_FLAG_SEEKABLE 0x40
/* BLK_TXN_DATA: txn_id / timestamps are delta-encoded with zigzag varint */
#define TRL_FLAG_TXN_DELTA 0x04
/* BLK_TXN_DATA: payload uses column-oriented layout */
#define TRL_FLAG_TXN_COLUMN 0x08

/* Column IDs for column-oriented BLK_TXN_DATA */
#define TRL_COL_TAG            0
#define TRL_COL_STREAM_INST_ID 1
#define TRL_COL_TXN_TYPE_ID    2
#define TRL_COL_TXN_ID_DELTA   3
#define TRL_COL_TIME_DELTA     4
#define TRL_COL_DURATION       5
#define TRL_COL_PARENT         6
#define TRL_COL_LINK_TYPE      7
#define TRL_COL_LINK_TGT       8
#define TRL_COL_LINK_LABEL     9
#define TRL_COL_META_KEY       10
#define TRL_COL_META_VAL       11
#define TRL_COL_ATTR_BASE      0x80

#define TRL_TYPE_TAG_SIGNAL 0x01
#define TRL_TYPE_TAG_TXN_SCHEMA 0x02
#define TRL_TYPE_TAG_ENUM 0x03
#define TRL_TYPE_TAG_STREAM_DECL 0x04

#define TRL_H_SCOPE 0x01
#define TRL_H_UPSCOPE 0x02
#define TRL_H_VAR 0x03
#define TRL_H_STREAM 0x04
#define TRL_H_ATTR 0x05

#define TRL_TXN_FULL 0x01
#define TRL_TXN_BEGIN 0x02
#define TRL_TXN_ATTR 0x03
#define TRL_TXN_END 0x04
#define TRL_TXN_LINK 0x05

typedef struct {
    uint8_t *data;
    size_t size;
    size_t cap;
} trl_buf_t;

typedef struct {
    char *key;
    uint32_t id;
} trl_str_entry_t;

typedef struct {
    char **id_to_str;
    uint32_t count;
    uint32_t cap;
    trl_str_entry_t *map;
    size_t map_cap;
    size_t map_count;
} trl_string_table_t;

typedef struct {
    uint8_t encoding;
    uint32_t bit_width;
    uint8_t radix;
} trl_signal_type_entry_t;

typedef struct {
    uint32_t name_id;
    uint32_t field_count;
    trl_field_def_t *fields;
} trl_txn_schema_entry_t;

typedef struct {
    trl_signal_type_entry_t *signal_types;
    size_t signal_count;
    size_t signal_cap;
    trl_txn_schema_entry_t *txn_schemas;
    size_t txn_count;
    size_t txn_cap;
} trl_type_registry_t;

typedef enum {
    TRL_VC_VALUE_U64,
    TRL_VC_VALUE_BYTES,
    TRL_VC_VALUE_STRING,
    TRL_VC_VALUE_REAL
} trl_vc_value_kind_t;

typedef struct {
    uint64_t time;
    trl_vc_value_kind_t kind;
    union {
        uint64_t u64;
        double real;
        struct {
            uint8_t *data;
            uint32_t len;
        } bytes;
        struct {
            char *str;
            uint32_t len;
        } str;
    } value;
} trl_vc_change_t;

typedef struct {
    uint32_t var_id;
    trl_vc_change_t *items;
    size_t count;
    size_t cap;
} trl_vc_var_changes_t;

typedef struct {
    uint64_t start_time;
    uint64_t end_time;
    trl_vc_var_changes_t *vars;
    size_t var_count;
    size_t var_cap;
} trl_vc_block_t;

typedef struct {
    uint32_t stream_inst_id;
    uint32_t txn_type_id;
    uint64_t txn_id;
    uint64_t start;
    uint64_t end;
    uint64_t parent;
    uint32_t attr_count;
    trl_attr_t *attrs;
} trl_txn_full_rec_t;

typedef struct {
    uint64_t start_time;
    uint64_t end_time;
    trl_txn_full_rec_t *records;
    size_t count;
    size_t cap;
} trl_txn_block_t;

typedef struct {
    uint32_t hier_id;
    uint8_t kind;
    uint32_t name_id;
    trl_buf_t body;
    uint32_t next_var_id;
    uint32_t next_stream_id;
    uint32_t *var_sig_type_ids;
    size_t var_sig_cap;
    int scope_depth;
} trl_hierarchy_t;

typedef struct {
    uint64_t start;
    uint64_t end;
    uint64_t offset;
} trl_index_entry_t;

typedef struct {
    trl_index_entry_t *vc_entries;
    size_t vc_count;
    size_t vc_cap;
    trl_index_entry_t *txn_entries;
    size_t txn_count;
    size_t txn_cap;
    /* Extended metadata-offset section (sentinel 0x01) — block offsets so a
     * reader can open without a linear scan (mirrors Python _index.py). */
    uint64_t strtab_offset;
    uint64_t typereg_offset;
    uint64_t *hier_offsets;
    size_t hier_count;
    size_t hier_cap;
} trl_index_t;

struct trl_writer {
    FILE *fp;
    trl_status_t status;
    int timescale_exp;
    trl_compress_t compress;
    trl_string_table_t strtab;
    trl_type_registry_t typereg;
    trl_index_t index;
    trl_hierarchy_t hierarchy;
    bool hierarchy_active;
    trl_vc_block_t vc;
    bool vc_active;
    trl_txn_block_t txn;
    bool txn_active;
    uint32_t *global_var_sig_type_ids;
    size_t global_var_cap;
    /* Phase 3.5: selected non-core value-change codec + its per-stream state.
     * NULL vc_codec == the core value-change codec (default path). */
    const trl_vc_codec_t *vc_codec;
    void *vc_codec_self;
};

struct trl_reader {
    FILE *fp;
    trl_status_t status;
    int timescale_exp;
    int64_t timezero;
    uint8_t version_major;
    uint8_t version_minor;
    trl_type_registry_t typereg;
    uint32_t *var_sig_type_ids;
    size_t var_sig_cap;
    trl_index_t index;
};

/* Storage-SPI handles (opaque in trl/trl_codec.h, defined here for the core
 * and built-in codecs). For Phase 1 the built-in codecs reach the existing
 * internals through the bound writer/reader; the richer SPI accretes later. */
struct trl_store {
    trl_writer_t *writer;
    trl_reader_t *reader;
};

struct trl_blkout {
    trl_buf_t *buf;   /* the codec appends its (block-framed) bytes here */
};

static inline void trl_write_le64(uint8_t *dst, uint64_t v) {
    dst[0] = (uint8_t)(v);
    dst[1] = (uint8_t)(v >> 8);
    dst[2] = (uint8_t)(v >> 16);
    dst[3] = (uint8_t)(v >> 24);
    dst[4] = (uint8_t)(v >> 32);
    dst[5] = (uint8_t)(v >> 40);
    dst[6] = (uint8_t)(v >> 48);
    dst[7] = (uint8_t)(v >> 56);
}

static inline uint64_t trl_read_le64(const uint8_t *src) {
    return ((uint64_t)src[0]) | ((uint64_t)src[1] << 8) | ((uint64_t)src[2] << 16) |
           ((uint64_t)src[3] << 24) | ((uint64_t)src[4] << 32) | ((uint64_t)src[5] << 40) |
           ((uint64_t)src[6] << 48) | ((uint64_t)src[7] << 56);
}

static inline void trl_write_le32(uint8_t *dst, uint32_t v) {
    dst[0] = (uint8_t)(v);
    dst[1] = (uint8_t)(v >> 8);
    dst[2] = (uint8_t)(v >> 16);
    dst[3] = (uint8_t)(v >> 24);
}

static inline uint32_t trl_read_le32(const uint8_t *src) {
    return ((uint32_t)src[0]) | ((uint32_t)src[1] << 8) | ((uint32_t)src[2] << 16) | ((uint32_t)src[3] << 24);
}

static inline void trl_buf_init(trl_buf_t *buf) {
    buf->data = NULL;
    buf->size = 0;
    buf->cap = 0;
}

static inline void trl_buf_free(trl_buf_t *buf) {
    free(buf->data);
    buf->data = NULL;
    buf->size = 0;
    buf->cap = 0;
}

static inline trl_status_t trl_buf_reserve(trl_buf_t *buf, size_t need) {
    if (need <= buf->cap) {
        return TRL_OK;
    }
    size_t new_cap = buf->cap ? buf->cap : 64;
    while (new_cap < need) {
        if (new_cap > ((size_t)-1) / 2) {
            new_cap = need;
            break;
        }
        new_cap *= 2;
    }
    uint8_t *n = (uint8_t *)realloc(buf->data, new_cap);
    if (!n) {
        return TRL_ERR_OOM;
    }
    buf->data = n;
    buf->cap = new_cap;
    return TRL_OK;
}

static inline trl_status_t trl_buf_append(trl_buf_t *buf, const void *data, size_t len) {
    trl_status_t st = trl_buf_reserve(buf, buf->size + len);
    if (st != TRL_OK) {
        return st;
    }
    memcpy(buf->data + buf->size, data, len);
    buf->size += len;
    return TRL_OK;
}

static inline trl_status_t trl_buf_append_u8(trl_buf_t *buf, uint8_t value) {
    return trl_buf_append(buf, &value, 1);
}

static inline trl_status_t trl_buf_append_le64(trl_buf_t *buf, uint64_t value) {
    uint8_t tmp[8];
    trl_write_le64(tmp, value);
    return trl_buf_append(buf, tmp, sizeof(tmp));
}

static inline trl_status_t trl_buf_append_le32(trl_buf_t *buf, uint32_t value) {
    uint8_t tmp[4];
    trl_write_le32(tmp, value);
    return trl_buf_append(buf, tmp, sizeof(tmp));
}

static inline trl_status_t trl_buf_append_le16(trl_buf_t *buf, uint16_t value) {
    uint8_t tmp[2];
    tmp[0] = (uint8_t)(value);
    tmp[1] = (uint8_t)(value >> 8);
    return trl_buf_append(buf, tmp, sizeof(tmp));
}

static inline char *trl_strdup_len(const char *src, size_t len) {
    char *dst = (char *)malloc(len + 1);
    if (!dst) {
        return NULL;
    }
    memcpy(dst, src, len);
    dst[len] = '\0';
    return dst;
}

static inline uint64_t trl_hash_bytes(const void *data, size_t len) {
    const uint8_t *p = (const uint8_t *)data;
    uint64_t h = 1469598103934665603ull;
    for (size_t i = 0; i < len; ++i) {
        h ^= (uint64_t)p[i];
        h *= 1099511628211ull;
    }
    return h;
}

static inline trl_status_t trl_make_block(uint8_t type, uint8_t flags, const uint8_t *payload, size_t payload_len, trl_buf_t *out) {
    trl_status_t st;
    uint8_t hdr[TRL_BLOCK_HEADER_SIZE];
    hdr[0] = type;
    hdr[1] = flags;
    trl_write_le64(&hdr[2], (uint64_t)(TRL_BLOCK_HEADER_SIZE + payload_len));
    st = trl_buf_append(out, hdr, sizeof(hdr));
    if (st != TRL_OK) {
        return st;
    }
    return trl_buf_append(out, payload, payload_len);
}

static inline trl_status_t trl_zlib_compress_buf(const uint8_t *src, size_t len, trl_buf_t *out) {
    uLongf dest_len = compressBound((uLong)len);
    trl_status_t st = trl_buf_reserve(out, dest_len);
    if (st != TRL_OK) {
        return st;
    }
    /* Match Python's zlib.compress default (Z_DEFAULT_COMPRESSION = level 6) so
     * whole-block compressed output is byte-identical across implementations. */
    int rc = compress2(out->data, &dest_len, src, (uLong)len, Z_DEFAULT_COMPRESSION);
    if (rc != Z_OK) {
        return TRL_ERR_COMPRESS;
    }
    out->size = (size_t)dest_len;
    return TRL_OK;
}

static inline trl_status_t trl_lz4_compress_buf(const uint8_t *src, size_t len, trl_buf_t *out) {
    int max_dst = LZ4_compressBound((int)len);
    trl_status_t st = trl_buf_reserve(out, (size_t)max_dst);
    if (st != TRL_OK) return st;
    int compressed = LZ4_compress_default((const char *)src, (char *)out->data, (int)len, max_dst);
    if (compressed <= 0) return TRL_ERR_COMPRESS;
    out->size = (size_t)compressed;
    return TRL_OK;
}

static inline trl_status_t trl_lz4_decompress_buf(const uint8_t *src, size_t src_len,
                                                   uint8_t *dst, size_t dst_len) {
    int rc = LZ4_decompress_safe((const char *)src, (char *)dst, (int)src_len, (int)dst_len);
    if (rc < 0 || (size_t)rc != dst_len) return TRL_ERR_COMPRESS;
    return TRL_OK;
}

static inline trl_status_t trl_zlib_uncompress_buf(const uint8_t *src, size_t len, uint8_t *dst, size_t dst_len) {
    uLongf out_len = (uLongf)dst_len;
    int rc = uncompress(dst, &out_len, src, (uLong)len);
    if (rc != Z_OK || out_len != dst_len) {
        return TRL_ERR_COMPRESS;
    }
    return TRL_OK;
}

void trl_string_table_init(trl_string_table_t *tab);
void trl_string_table_free(trl_string_table_t *tab);
uint32_t trl_string_table_intern(trl_string_table_t *tab, const char *s);
const char *trl_string_table_lookup(const trl_string_table_t *tab, uint32_t id);
trl_status_t trl_string_table_encode_block(const trl_string_table_t *tab, trl_compress_t compress, trl_buf_t *out);
trl_status_t trl_string_table_decode_payload(trl_string_table_t *tab, const uint8_t *payload, size_t payload_len, uint8_t flags);

void trl_type_registry_init(trl_type_registry_t *reg);
void trl_type_registry_free(trl_type_registry_t *reg);
uint32_t trl_type_registry_add_signal_type(trl_type_registry_t *reg, uint8_t encoding, uint32_t bit_width, uint8_t radix);
uint32_t trl_type_registry_add_txn_schema(trl_type_registry_t *reg, uint32_t name_id, uint32_t field_count, const trl_field_def_t *fields);
const trl_signal_type_entry_t *trl_type_registry_get_signal_type(const trl_type_registry_t *reg, uint32_t id);
const trl_txn_schema_entry_t *trl_type_registry_get_txn_schema(const trl_type_registry_t *reg, uint32_t id);
trl_status_t trl_type_registry_encode_block(const trl_type_registry_t *reg, trl_compress_t compress, trl_buf_t *out);
trl_status_t trl_type_registry_decode_payload(trl_type_registry_t *reg, const uint8_t *payload, size_t payload_len, uint8_t flags);

void trl_hierarchy_init(trl_hierarchy_t *hier);
void trl_hierarchy_free(trl_hierarchy_t *hier);
trl_status_t trl_hierarchy_begin(trl_hierarchy_t *hier, uint32_t hier_id, uint8_t kind, uint32_t name_id);
trl_status_t trl_hierarchy_begin_scope(trl_hierarchy_t *hier, uint8_t scope_type, uint32_t name_id, uint32_t component_id,
                                      uint32_t src_file_str_id, uint32_t src_line);
trl_status_t trl_hierarchy_end_scope(trl_hierarchy_t *hier);
uint32_t trl_hierarchy_add_var(trl_hierarchy_t *hier, uint32_t name_id, uint32_t sig_type_id, uint8_t dir,
                               uint32_t src_file_str_id, uint32_t src_line);
trl_status_t trl_hierarchy_encode_block(const trl_hierarchy_t *hier, trl_compress_t compress, trl_buf_t *out);
trl_status_t trl_hierarchy_decode_payload(const uint8_t *payload, size_t payload_len, uint8_t flags, uint32_t **var_sig_type_ids, size_t *var_sig_cap);

void trl_vc_block_init(trl_vc_block_t *blk);
void trl_vc_block_free(trl_vc_block_t *blk);
trl_status_t trl_vc_block_begin(trl_vc_block_t *blk, uint64_t start_time);
trl_status_t trl_vc_block_add_u64(trl_vc_block_t *blk, uint32_t var_id, uint64_t time, uint64_t value);
trl_status_t trl_vc_block_add_bytes(trl_vc_block_t *blk, uint32_t var_id, uint64_t time, const uint8_t *value, uint32_t len);
trl_status_t trl_vc_block_add_str(trl_vc_block_t *blk, uint32_t var_id, uint64_t time, const char *value);
trl_status_t trl_vc_block_add_real(trl_vc_block_t *blk, uint32_t var_id, uint64_t time, double value);
trl_status_t trl_vc_block_encode(const trl_vc_block_t *blk, const uint32_t *var_sig_type_ids, size_t var_sig_cap, const trl_type_registry_t *reg, trl_compress_t compress, trl_buf_t *out);
trl_status_t trl_vc_iter_payload(const uint8_t *payload, size_t payload_len, uint8_t flags, const uint32_t *var_sig_type_ids, size_t var_sig_cap, const trl_type_registry_t *reg, trl_vc_cb cb, void *user);

void trl_txn_block_init(trl_txn_block_t *blk);
void trl_txn_block_free(trl_txn_block_t *blk);
trl_status_t trl_txn_block_begin(trl_txn_block_t *blk, uint64_t start_time);
trl_status_t trl_txn_block_add_full(trl_txn_block_t *blk, uint32_t stream_inst_id, uint32_t txn_type_id, uint64_t txn_id, uint64_t start, uint64_t end, uint64_t parent, uint32_t attr_count, const trl_attr_t *attrs);
trl_status_t trl_txn_block_encode(const trl_txn_block_t *blk, trl_compress_t compress, trl_buf_t *out);

trl_status_t trl_ext_encode_block(const uint8_t ext_type[4], uint16_t ext_version, const uint8_t *payload, size_t payload_len, trl_buf_t *out);
trl_status_t trl_ext_decode_payload(const uint8_t *payload, size_t payload_len, uint8_t ext_type[4], uint16_t *ext_version, const uint8_t **ext_payload, size_t *ext_payload_len);

void trl_index_init(trl_index_t *idx);
void trl_index_free(trl_index_t *idx);
trl_status_t trl_index_add_vc(trl_index_t *idx, uint64_t start, uint64_t end, uint64_t offset);
trl_status_t trl_index_add_txn(trl_index_t *idx, uint64_t start, uint64_t end, uint64_t offset);
trl_status_t trl_index_add_hier_offset(trl_index_t *idx, uint64_t offset);
trl_status_t trl_index_encode_block(const trl_index_t *idx, trl_buf_t *out);
trl_status_t trl_index_decode_payload(trl_index_t *idx, const uint8_t *payload, size_t payload_len);
const trl_index_entry_t *trl_index_seek_vc(const trl_index_t *idx, uint64_t time);
