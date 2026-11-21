#pragma once
#include "trl/trl.h"

/* ----------------------------------------------------------------------- *
 * trl_rec.h: the L0 recording session (sv_api_design.md §7.1, phase 2).
 *
 * This is a session layer over trl.h's block-level writer: it owns stream
 * and type bookkeeping, string interning, lazy hierarchy declaration for
 * streams, and open-transaction handles, so a caller never has to manage
 * schema ids, stream ids or block boundaries itself.
 *
 * Scope trimmed from the design doc for this phase (see sv_impl_status.md
 * for the full list):
 *   - enum label tables (trl_renum_t) are in-memory only; FT_ENUM values on
 *     the wire are plain integers, as they were in phase 1. No TYPE_TAG_ENUM
 *     entry is written yet.
 *   - a stream's "kind" string and its scope/parent nesting are not written
 *     to the hierarchy; streams are declared flat, under one HK_BEHAVIORAL
 *     tree per db. `trl_stream_find` and qualified type names still use the
 *     dotted parent path, so this is purely a hierarchy-block simplification.
 *   - trl_type_num_members / trl_type_member (nested-type introspection) are
 *     deferred; flat field_index/name/type/width cover everything trl_rec.h
 *     itself needs. A reader wanting the nested view can still recover it
 *     from the "outer.inner" field-name convention trl_type_add_struct uses.
 *   - the stream-set index section (0x05) for coalescing several small
 *     per-stream buffers into one block is deferred; each stream's buffer is
 *     flushed as its own single-stream block (phase 1's
 *     trl_txn_begin_block_stream), which is correct, just less dense for
 *     many low-traffic streams.
 * ----------------------------------------------------------------------- */

typedef struct trl_db     trl_db_t;
typedef struct trl_rtype  trl_rtype_t;
typedef struct trl_renum  trl_renum_t;
typedef struct trl_stream trl_stream_t;
typedef struct trl_txn    trl_txn_t;

typedef struct {
    const char *path;
    int         timescale_exp;
    trl_compress_t compress;
    uint32_t    block_bytes;        /* per-stream auto-flush threshold; 0 = a built-in default */
    const char *stream_include;     /* comma-separated glob list, NULL = all */
    const char *stream_exclude;     /* comma-separated glob list, NULL = none */
    uint32_t    flags;              /* reserved */
} trl_db_cfg_t;

/* Process-wide registry, so VPI and DPI can share one db by name. */
trl_db_t     *trl_db_open(const char *name, const trl_db_cfg_t *cfg);
trl_db_t     *trl_db_find(const char *name);
trl_status_t  trl_db_close(trl_db_t *);
trl_writer_t *trl_db_writer(trl_db_t *);
uint32_t      trl_db_intern(trl_db_t *, const char *s);
const char   *trl_last_error(void);

/* Types */
trl_rtype_t *trl_type_new  (trl_db_t *, const char *name, trl_stream_t *scope);
trl_rtype_t *trl_type_find (trl_db_t *, const char *name, trl_stream_t *scope);
int32_t      trl_type_add_field (trl_rtype_t *, const char *name, trl_field_type_t,
                                 uint32_t width, trl_renum_t *);
int32_t      trl_type_add_struct(trl_rtype_t *, const char *name, trl_rtype_t *sub);
int32_t      trl_type_add_array (trl_rtype_t *, const char *name, trl_field_type_t,
                                 uint32_t width, uint32_t n);
int32_t      trl_type_field_get_or_add(trl_rtype_t *, const char *name,
                                       trl_field_type_t, uint32_t width);
trl_status_t trl_type_seal(trl_rtype_t *);
const char  *trl_type_name(const trl_rtype_t *);
uint32_t     trl_type_num_fields(const trl_rtype_t *);
int32_t      trl_type_field_index(const trl_rtype_t *, const char *name);
const char  *trl_type_field_name(const trl_rtype_t *, uint32_t f);
trl_field_type_t trl_type_field_type(const trl_rtype_t *, uint32_t f);
uint32_t     trl_type_field_width(const trl_rtype_t *, uint32_t f);

trl_renum_t *trl_enum_new(trl_db_t *, const char *name);
trl_status_t trl_enum_add(trl_renum_t *, const char *label, int64_t value);

/* Streams */
trl_stream_t *trl_stream_new(trl_db_t *, trl_stream_t *parent, const char *name,
                             const char *kind, trl_rtype_t *dflt, int *enabled);
trl_stream_t *trl_stream_find(trl_db_t *, const char *path);
trl_rtype_t  *trl_stream_adhoc_type(trl_stream_t *);
uint32_t      trl_stream_inst_id(const trl_stream_t *);

/* Recording */
uint64_t   trl_rec_full (trl_stream_t *, trl_rtype_t *, uint64_t start, uint64_t end,
                         uint64_t parent, const uint64_t *vals, uint32_t nvals);
trl_txn_t *trl_rec_begin(trl_stream_t *, trl_rtype_t *, uint64_t start, uint64_t parent);
uint64_t   trl_rec_id   (const trl_txn_t *);
void trl_rec_set_u64  (trl_txn_t *, uint32_t f, uint64_t v);
void trl_rec_set_i64  (trl_txn_t *, uint32_t f, int64_t v);
void trl_rec_set_f64  (trl_txn_t *, uint32_t f, double v);
void trl_rec_set_str  (trl_txn_t *, uint32_t f, const char *s);
void trl_rec_set_str_id(trl_txn_t *, uint32_t f, uint32_t sid);
void trl_rec_set_bits (trl_txn_t *, uint32_t f, uint32_t width, const uint8_t *be_bytes);
void trl_rec_set_logic(trl_txn_t *, uint32_t f, uint32_t width,
                       const uint8_t *aval_be, const uint8_t *bval_be);
void trl_rec_set_bytes(trl_txn_t *, uint32_t f, const uint8_t *p, uint32_t n);
void trl_rec_link     (trl_txn_t *, uint64_t tgt, uint8_t ltype, uint32_t label_sid);
void trl_rec_link_ids (trl_db_t *, uint64_t src, uint64_t tgt, uint8_t ltype, uint32_t label_sid);
void trl_rec_meta     (trl_txn_t *, uint32_t key_sid, uint32_t val_sid);
void trl_rec_end      (trl_txn_t *, uint64_t end);
