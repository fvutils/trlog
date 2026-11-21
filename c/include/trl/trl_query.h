#pragma once
#include "trl/trl.h"

/* ----------------------------------------------------------------------- *
 * trl_query.h: L0 reading (sv_api_design.md §7.3, phase 3).
 *
 * This is the read-side counterpart to trl_rec.h: name lookup for streams
 * and transaction schemas, and a cursor that joins TR_BEGIN/TR_ATTR/TR_END
 * (and attaches TR_LINK/TR_META) back into one transaction per txn_id,
 * emitted in (start, txn_id) order.
 *
 * Scope trimmed from the design doc for this phase (see sv_impl_status.md
 * for the full list):
 *   - the read-side schema type is `trl_rschema_t`, not `trl_rtype_t`. The
 *     design sketch reuses `trl_rtype_t` for both recording and reading,
 *     but trl_rec.h's `trl_rtype_t` is a distinct, non-interchangeable
 *     write-side struct (opaque pointers of the same C type name would let
 *     a write-side handle compile cleanly into a read-side accessor without
 *     any diagnostic). A different name keeps the two call sites from being
 *     silently swapped.
 *   - `trl_cursor_new`/`trl_cursor_add_stream` build the whole joined
 *     transaction list up front (one pass over the selected blocks) rather
 *     than the design's incremental, bounded-memory min-heap of per-block
 *     sub-cursors. Correct for the traces this phase's tests exercise;
 *     revisit for very large per-stream traces.
 *   - `trl_cursor_seek(time)` is not implemented; iterate with
 *     `trl_cursor_next` and compare `start` instead.
 *   - a stream's default/declared type is not resolvable from the
 *     hierarchy (trl_rec.h's phase-2 trim leaves `H_STREAM.stream_type_id`
 *     always 0); use the per-transaction `txn_type_id` from the cursor with
 *     `trl_rdb_schema_by_id` instead of a stream-level accessor.
 *   - a transaction left open at file close (no TR_END seen) is reported
 *     with `end == UINT64_MAX`; trl_rec.h does not yet write the
 *     `trlog.truncated=1` metadata the design calls for (§9 Q7), so that
 *     key is not available to check.
 * ----------------------------------------------------------------------- */

typedef struct trl_rdb     trl_rdb_t;
typedef struct trl_rstream trl_rstream_t;
typedef struct trl_rschema trl_rschema_t;
typedef struct trl_cursor  trl_cursor_t;

trl_rdb_t    *trl_rdb_open(const char *name, const char *path);
trl_rdb_t    *trl_rdb_find(const char *name);
trl_status_t  trl_rdb_close(trl_rdb_t *);

uint32_t       trl_rdb_num_streams(trl_rdb_t *);
trl_rstream_t *trl_rdb_stream_at(trl_rdb_t *, uint32_t i);
trl_rstream_t *trl_rdb_stream_find(trl_rdb_t *, const char *path);
const char    *trl_rstream_path(const trl_rstream_t *);
uint32_t       trl_rstream_inst_id(const trl_rstream_t *);

/* Resolves a string-table id captured from a cursor's attrs/metas/links
 * (trl_attr_t's string ids live in the schema's own field data, not here;
 * this is for TR_META's key_sid/val_sid and TR_LINK's label_sid). NULL if
 * the id is out of range. */
const char    *trl_rdb_string_lookup(trl_rdb_t *, uint32_t sid);

/* Transaction-schema introspection, resolved from the file's type registry.
 * A schema name that was re-registered (trl_rec.h's ad-hoc field growth)
 * resolves to its newest/widest version. */
const trl_rschema_t *trl_rdb_schema_find(trl_rdb_t *, const char *qualified_name);
const trl_rschema_t *trl_rdb_schema_by_id(trl_rdb_t *, uint32_t schema_id);
const char       *trl_rschema_name(const trl_rschema_t *);
uint32_t          trl_rschema_num_fields(const trl_rschema_t *);
int32_t           trl_rschema_field_index(const trl_rschema_t *, const char *name);
const char       *trl_rschema_field_name(const trl_rschema_t *, uint32_t f);
trl_field_type_t  trl_rschema_field_type(const trl_rschema_t *, uint32_t f);

typedef struct {
    uint8_t  link_type;
    uint64_t target;
    uint32_t label_sid;
} trl_txn_link_t;

typedef struct {
    uint32_t key_sid;
    uint32_t val_sid;
} trl_txn_meta_t;

/* A joined transaction. Pointers remain valid until the cursor is freed
 * (stronger than the design's "until the next trl_cursor_next" contract). */
typedef struct {
    uint64_t txn_id;
    uint32_t stream_inst_id;
    uint32_t txn_type_id;
    uint64_t start;
    uint64_t end;      /* UINT64_MAX if no TR_END was found (open/truncated) */
    uint64_t parent;
    uint32_t attr_count;
    const trl_attr_t *attrs;
    uint32_t link_count;
    const trl_txn_link_t *links;
    uint32_t meta_count;
    const trl_txn_meta_t *metas;
} trl_txn_hdr_t;

/* stream == NULL means every stream in the db. Narrow further with
 * trl_cursor_add_stream before the first trl_cursor_next call. */
trl_cursor_t *trl_cursor_new(trl_rdb_t *, trl_rstream_t *stream, uint64_t t_from, uint64_t t_to);
trl_status_t  trl_cursor_add_stream(trl_cursor_t *, trl_rstream_t *);
int           trl_cursor_next(trl_cursor_t *, trl_txn_hdr_t *out);   /* 0 = end */
void          trl_cursor_free(trl_cursor_t *);
