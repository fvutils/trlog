#pragma once
#include <stddef.h>
#include <stdint.h>
#include "trl/trl_api.h"

#ifdef __cplusplus
extern "C" {
#endif

typedef struct trl_writer trl_writer_t;
typedef struct trl_reader trl_reader_t;

typedef enum {
    TRL_OK = 0,
    TRL_ERR_IO,
    TRL_ERR_MAGIC,
    TRL_ERR_VERSION,
    TRL_ERR_COMPRESS,
    TRL_ERR_CORRUPT,
    TRL_ERR_OOM,
    TRL_ERR_INVALID
} trl_status_t;

typedef enum {
    TRL_COMPRESS_NONE = 0,
    TRL_COMPRESS_ZLIB
} trl_compress_t;

typedef enum {
    TRL_SE_2STATE = 0,
    TRL_SE_4STATE = 1,
    TRL_SE_9STATE = 2,
    TRL_SE_REAL = 3,
    TRL_SE_STRING = 4
} trl_signal_enc_t;

/* Field type codes. These are wire values and must match the Python reference
 * (trlog._types.FieldType). */
typedef enum {
    TRL_FT_U8       = 0x00,
    TRL_FT_U16      = 0x01,
    TRL_FT_U32      = 0x02,
    TRL_FT_U64      = 0x03,
    TRL_FT_I8       = 0x04,
    TRL_FT_I16      = 0x05,
    TRL_FT_I32      = 0x06,
    TRL_FT_I64      = 0x07,
    TRL_FT_F32      = 0x08,
    TRL_FT_F64      = 0x09,
    TRL_FT_BOOL     = 0x0A,
    TRL_FT_STRING   = 0x0B,
    TRL_FT_BITVEC   = 0x0C,
    TRL_FT_BYTES    = 0x0D,
    TRL_FT_TIME     = 0x0E,
    TRL_FT_ENUM     = 0x0F,
    TRL_FT_LOGICVEC = 0x10
} trl_field_type_t;

/* Transaction record tags */
typedef enum {
    TRL_TR_FULL  = 0x01,
    TRL_TR_BEGIN = 0x02,
    TRL_TR_ATTR  = 0x03,
    TRL_TR_END   = 0x04,
    TRL_TR_LINK  = 0x05,
    TRL_TR_META  = 0x06   /* key/value string metadata attached to a transaction */
} trl_txn_record_tag_t;

typedef struct {
    uint32_t name_str_id;
    trl_field_type_t field_type;
} trl_field_def_t;

/* One attribute value. Every pointer is read during the call that receives
 * the attribute (trl_txn_full) and the value is copied; the caller's memory
 * need not outlive that call.
 *
 *   u64       U8..U64, BOOL, TIME, ENUM
 *   i64       I8..I64
 *   f64       F32, F64
 *   str       STRING (NUL-terminated UTF-8; NULL is the empty string)
 *   vec       BYTES:    data[len]
 *             BITVEC:   width bits; data[ceil(width/8)], big-endian
 *             LOGICVEC: width bits; data holds aval[ceil(width/8)] then
 *                       bval[ceil(width/8)], each big-endian
 */
typedef struct {
    uint32_t field_idx;
    trl_field_type_t field_type;
    union {
        uint64_t u64;
        int64_t i64;
        double f64;
        const char *str;
        struct {
            const uint8_t *data;
            uint32_t len;
            uint32_t width;
        } vec;
    } value;
} trl_attr_t;

/* stream_inst_id of a transaction block that may hold any stream's records */
#define TRL_STREAM_MIXED 0xFFFFFFFFu

/* ----------------------------------------------------------------------- *
 * Well-known attribute key strings (H_ATTR on hierarchy nodes, TR_META on  *
 * transaction records).  Line-number values are decimal ASCII strings.      *
 * ----------------------------------------------------------------------- */

/* Source location of a hierarchy node (scope, var, stream instance) */
#define TRL_WATTR_SRC_FILE          "trlog.src.file"
#define TRL_WATTR_SRC_LINE          "trlog.src.line"

/* Driver location for a signal variable */
#define TRL_WATTR_DRIVER_FILE       "trlog.driver.file"
#define TRL_WATTR_DRIVER_LINE       "trlog.driver.line"

/* Origin location for a transaction (e.g. the sequence that created it) */
#define TRL_WATTR_ORIGIN_FILE       "trlog.origin.file"
#define TRL_WATTR_ORIGIN_LINE       "trlog.origin.line"
#define TRL_WATTR_ORIGIN_COMPONENT  "trlog.origin.component"

TRL_API trl_writer_t *trl_writer_open(const char *path, int timescale_exp, trl_compress_t compress);
/* Set the header's time offset (default 0). Call any time before close. */
TRL_API trl_status_t trl_writer_set_timezero(trl_writer_t *, int64_t timezero);
TRL_API uint32_t trl_intern(trl_writer_t *, const char *s);
TRL_API uint32_t trl_add_signal_type(trl_writer_t *, uint8_t encoding, uint32_t bit_width, uint8_t radix);
TRL_API uint32_t trl_add_txn_schema(trl_writer_t *, uint32_t name_id, uint32_t field_count, const trl_field_def_t *fields);
TRL_API trl_status_t trl_begin_hierarchy(trl_writer_t *, uint32_t hier_id, uint8_t kind, uint32_t name_id);
TRL_API trl_status_t trl_hier_begin_scope(trl_writer_t *, uint8_t scope_type, uint32_t name_id, uint32_t component_id,
                                  uint32_t src_file_str_id, uint32_t src_line);
TRL_API trl_status_t trl_hier_end_scope(trl_writer_t *);
TRL_API uint32_t trl_hier_add_var(trl_writer_t *, uint32_t name_id, uint32_t sig_type_id, uint8_t dir,
                          uint32_t src_file_str_id, uint32_t src_line);
/* Attach a key/value string attribute (H_ATTR) to the most recently declared
 * scope, var or stream of the open hierarchy. Returns TRL_ERR_INVALID if no
 * node has been declared yet. */
TRL_API trl_status_t trl_hier_add_attr(trl_writer_t *, uint32_t key_str_id, uint32_t value_str_id);
TRL_API uint32_t trl_hier_add_stream(trl_writer_t *, uint32_t stream_type_id, uint32_t name_id);
TRL_API trl_status_t trl_flush_hierarchy(trl_writer_t *);
TRL_API trl_status_t trl_vc_begin(trl_writer_t *, uint64_t start_time);
TRL_API trl_status_t trl_vc_change_u64(trl_writer_t *, uint32_t var_id, uint64_t time, uint64_t value);
TRL_API trl_status_t trl_vc_change_bytes(trl_writer_t *, uint32_t var_id, uint64_t time, const uint8_t *value, uint32_t len);
TRL_API trl_status_t trl_vc_change_str(trl_writer_t *, uint32_t var_id, uint64_t time, const char *value);
TRL_API trl_status_t trl_vc_change_real(trl_writer_t *, uint32_t var_id, uint64_t time, double value);
TRL_API trl_status_t trl_vc_flush(trl_writer_t *);
TRL_API trl_status_t trl_txn_begin_block(trl_writer_t *, uint64_t start_time);
/* Begin a block that holds records of one stream only. The block's stream id
 * goes into the per-stream index; trl_txn_full with another stream id
 * returns TRL_ERR_INVALID. TRL_STREAM_MIXED is the same as
 * trl_txn_begin_block. */
TRL_API trl_status_t trl_txn_begin_block_stream(trl_writer_t *, uint64_t start_time, uint32_t stream_inst_id);
TRL_API trl_status_t trl_txn_full(trl_writer_t *, uint32_t stream_inst_id, uint32_t txn_type_id, uint64_t txn_id,
                           uint64_t start, uint64_t end, uint64_t parent,
                           uint32_t attr_count, const trl_attr_t *attrs);
TRL_API trl_status_t trl_txn_meta(trl_writer_t *, uint64_t txn_id,
                           uint32_t key_str_id, uint32_t value_str_id);
TRL_API trl_status_t trl_txn_end_block(trl_writer_t *);
TRL_API trl_status_t trl_writer_close(trl_writer_t *);

TRL_API trl_reader_t *trl_reader_open(const char *path);
/* Opens a trace held in memory (the whole file's bytes). The buffer is
 * borrowed, not copied: it must stay valid and unchanged until
 * trl_reader_close. Returns NULL if the bytes are not a valid trace. */
TRL_API trl_reader_t *trl_reader_open_mem(const void *buf, size_t len);

/* Value-change callback. `val`/`len` are the stored value bytes: 2-state
 * is ceil(width/8) bytes big-endian (at most 8), REAL the 8 bytes of a
 * double, STRING the bytes without a terminator. */
typedef void (*trl_vc_cb)(uint32_t var_id, uint64_t time, const uint8_t *val, uint32_t len, void *user);
/* Transaction callback used by trl_txn_codec_t.decode_block (trl_codec.h).
 * trl.h has no transaction iterator; read transactions with trl_query.h. */
typedef void (*trl_txn_cb_t)(uint32_t txn_type, uint32_t stream_inst, uint64_t txn_id, uint64_t start, uint64_t end, void *user);

/* Calls cb for every value change, block by block in file order. Within a
 * block the changes are grouped by variable, not sorted by time. */
TRL_API trl_status_t trl_iter_vc(trl_reader_t *, trl_vc_cb, void *user);

/* Hierarchy walk events, in declaration order. Each hierarchy starts with
 * TRL_HE_HIER (name = hierarchy name, scope_type = hierarchy kind); every
 * TRL_HE_SCOPE is closed by a TRL_HE_UPSCOPE. */
typedef enum {
    TRL_HE_HIER = 0,
    TRL_HE_SCOPE,
    TRL_HE_UPSCOPE,
    TRL_HE_VAR,
    TRL_HE_STREAM
} trl_hier_event_kind_t;

typedef struct {
    uint8_t kind;           /* trl_hier_event_kind_t */
    uint32_t hier_id;
    const char *name;       /* resolved, "" when unnamed; valid during the call */
    uint8_t scope_type;     /* SCOPE: scope type; HIER: hierarchy kind */
    uint32_t id;            /* VAR: var id; STREAM: stream instance id */
    uint32_t type_id;       /* VAR: signal type id; STREAM: stream type id */
    uint8_t encoding;       /* VAR: trl_signal_enc_t of the signal type */
    uint32_t bit_width;     /* VAR: width of the signal type */
    uint8_t dir;            /* VAR: direction */
    uint32_t alias_of;      /* VAR: aliased var id, 0 = none */
} trl_hier_event_t;

typedef void (*trl_hier_cb)(const trl_hier_event_t *ev, void *user);
TRL_API trl_status_t trl_iter_hier(trl_reader_t *, trl_hier_cb, void *user);
TRL_API int trl_reader_timescale_exp(const trl_reader_t *);
TRL_API trl_status_t trl_reader_close(trl_reader_t *);

#ifdef __cplusplus
}
#endif
