#pragma once
#include <stddef.h>
#include <stdint.h>

#include "trl/trl.h"

#ifdef __cplusplus
extern "C" {
#endif

/* ----------------------------------------------------------------------- *
 * trl_qscan.h: the query scan engine behind `trlog.query` (query CLI
 * design §7.1, phase Q4).
 *
 * This is an optional accelerator for the pure-Python engine in
 * python/src/trlog/query/_engine.py, and it must give exactly the same
 * answers. To keep the two engines from disagreeing, the caller (Python)
 * supplies everything that is a semantic choice:
 *
 *   - the block plan: TXN block offsets in visit order, with each block's
 *     declared start and stream id (for upper-bound block skipping);
 *   - the decoder's field types per txn type (for TR_FULL attrs);
 *   - per txn type, each field's name slot and value normalisation;
 *   - the filter, compiled to the bytecode described below.
 *
 * The engine reads blocks at the given offsets, decodes and joins the
 * records the way `Trace.scan` does, applies the stream/type restriction,
 * the inclusive time window [lo, hi] and the filter, and returns the
 * surviving transactions in packed arrays.
 *
 * Filter results are three-valued. A row is KEEP, DROP, or DEFER; DEFER
 * means the engine cannot be sure of the Python answer (strings, floats,
 * ints beyond int64, ...), so the caller evaluates the full filter on it.
 *
 * trl_qscan_run returns TRL_QS_FALLBACK when the file holds anything this
 * engine does not decode exactly as Python does (zstd, column layout, an
 * unknown record tag, invalid UTF-8, values outside int64, a short read).
 * The caller then reruns the query on the Python engine.
 * ----------------------------------------------------------------------- */

typedef struct trl_qscan trl_qscan_t;

/* Bumped on any change to the bytecode, the packed records or the calls
 * below; the Python side uses the engine only when it knows this version. */
#define TRL_QSCAN_ABI 2
int trl_qscan_abi_version(void);

#define TRL_QS_OK        0
#define TRL_QS_FALLBACK  100

/* trl_qscan_set_options flags */
#define TRL_QS_WANT_LINKS 0x01u   /* attach TR_LINK records (both ends)       */
#define TRL_QS_WANT_META  0x02u   /* attach TR_META records                   */
#define TRL_QS_HAS_T_HI   0x04u   /* t_hi: count open txns, skip late blocks  */
#define TRL_QS_FILTER     0x08u   /* apply window and program to the output   */
#define TRL_QS_HAS_LO     0x10u
#define TRL_QS_HAS_HI     0x20u
#define TRL_QS_HAS_MAX    0x40u   /* stop after the block where max_txns started */
#define TRL_QS_PROJECT    0x80u   /* decided rows go to the projection columns */

/* Field value normalisation (query/_values.py normalize_field) */
#define TRL_QS_NORM_PLAIN    0
#define TRL_QS_NORM_BITVEC   1
#define TRL_QS_NORM_LOGICVEC 2
#define TRL_QS_NORM_ENUM     3

/* Output row flags */
#define TRL_QS_ROW_OPEN  0x01u    /* no TR_END seen; `end` is 0             */
#define TRL_QS_ROW_DEFER 0x02u    /* the caller must evaluate the filter    */

/* Projected value tags (trl_qs_proj_t.vtag) */
#define TRL_QS_V_MISS 0           /* missing                                  */
#define TRL_QS_V_INT  1           /* int                                      */
#define TRL_QS_V_ENUM 2           /* enum: aux = (type_id << 32) | field_idx  */
#define TRL_QS_V_BOOL 5           /* bool                                     */

/* Projection columns (TRL_QS_PROJECT): row i is id[i], start[i], ...;
 * value k of row i is vtag/val/aux[i * nvals + k]. `end` is 0 when open. */
typedef struct {
    const int64_t  *id, *start, *end, *parent;
    const uint32_t *stream, *type;
    const uint8_t  *open;
    const uint8_t  *vtag;
    const int64_t  *val;
    const uint64_t *aux;
} trl_qs_proj_t;

/* Packed output records. Little-endian on every supported platform; the
 * Python side reads them with struct formats that mirror these layouts. */
typedef struct {
    int64_t  id, start, end, parent;    /* parent 0 = none */
    uint32_t stream, type;
    uint32_t attr_first, attr_n;
    uint32_t meta_first, meta_n;
    uint32_t link_first, link_n;
    uint32_t flags, pad;
} trl_qs_row_t;                         /* 72 bytes, Python '<qqqqIIIIIIIIII' */

typedef struct {
    uint32_t field_idx;
    uint32_t ft;                        /* trl_field_type_t as decoded */
    uint64_t v;                         /* ints (raw bits), f64 bits   */
    uint32_t off, len, width, pad;      /* heap slice for STRING/BYTES/BITVEC/LOGICVEC */
} trl_qs_attr_t;                        /* 32 bytes, Python '<IIQIIII' */

typedef struct {
    uint32_t key_sid, val_sid;
} trl_qs_meta_t;                        /* 8 bytes, Python '<II' */

typedef struct {
    int64_t  other;                     /* the txn at the other end */
    uint32_t label_sid;
    uint8_t  kind;                      /* TR_LINK link_type */
    uint8_t  dir;                       /* 0 = out (this is the source), 1 = in */
    uint16_t pad;
} trl_qs_link_t;                        /* 16 bytes, Python '<qIBBH' */

trl_qscan_t *trl_qscan_new(const char *path);
void         trl_qscan_free(trl_qscan_t *);

/* Decoder field types of a txn type (what TR_FULL attrs are decoded as).
 * Persistent across queries. */
trl_status_t trl_qscan_set_schema(trl_qscan_t *, uint32_t type_id, uint32_t n,
                                  const uint8_t *field_types);

/* Clear per-query settings and results; schemas are kept. */
void         trl_qscan_reset(trl_qscan_t *);

/* Per query: for each catalog txn type, each field's name slot (-1 = not
 * used by the program) and normalisation. Attrs of types without a catalog
 * entry, or with field_idx >= n, are "unmapped". */
trl_status_t trl_qscan_set_catalog(trl_qscan_t *, uint32_t type_id, uint32_t n,
                                   const int32_t *slots, const uint8_t *norms);
trl_status_t trl_qscan_set_plan(trl_qscan_t *, size_t n, const int64_t *block_starts,
                                const uint64_t *offsets, const uint32_t *stream_ids);
/* Restrict which txns are built; not calling it means all. */
trl_status_t trl_qscan_set_streams(trl_qscan_t *, size_t n, const uint32_t *ids);
trl_status_t trl_qscan_set_types(trl_qscan_t *, size_t n, const uint32_t *ids);
/* code: the bytecode (see qscan.c); sets: label and id sets it refers to.
 * strict_unmapped: an unmapped attr makes LOAD/HAS defer (the program
 * names a field like `f3` that could match one). */
trl_status_t trl_qscan_set_program(trl_qscan_t *, size_t ncode, const int64_t *code,
                                   size_t nsets, const int64_t *sets, int strict_unmapped);
/* Entry points into the program's code: the filter (-1 = none) and the
 * value expressions projected with TRL_QS_PROJECT (each ends with OP_END
 * and leaves its value). */
trl_status_t trl_qscan_set_entries(trl_qscan_t *, int64_t filter_entry, size_t nvals,
                                   const int64_t *val_entries);
void         trl_qscan_set_options(trl_qscan_t *, uint32_t flags, int64_t t_hi,
                                   int64_t lo, int64_t hi, int64_t max_txns);

/* TRL_QS_OK, TRL_QS_FALLBACK, or a trl_status_t error. */
int          trl_qscan_run(trl_qscan_t *);
const char  *trl_qscan_fallback_reason(const trl_qscan_t *);

size_t trl_qscan_rows(const trl_qscan_t *, const trl_qs_row_t **out);
size_t trl_qscan_attrs(const trl_qscan_t *, const trl_qs_attr_t **out);
size_t trl_qscan_metas(const trl_qscan_t *, const trl_qs_meta_t **out);
size_t trl_qscan_links(const trl_qscan_t *, const trl_qs_link_t **out);
size_t trl_qscan_heap(const trl_qscan_t *, const uint8_t **out);
/* With TRL_QS_PROJECT: rows C decided in full (filter TRUE, every value
 * exact) are here, the rest are in trl_qscan_rows. Returns the row count. */
size_t trl_qscan_proj(const trl_qscan_t *, trl_qs_proj_t *out);
/* out[0] blocks_read, [1] duplicate_ids, [2] rows kept, [3] rows deferred */
void   trl_qscan_counts(const trl_qscan_t *, int64_t out[4]);

/* Free the results and scan buffers (the caller has copied what it needs);
 * per-query settings and schemas are kept. */
void   trl_qscan_trim(trl_qscan_t *);

#ifdef __cplusplus
}
#endif
