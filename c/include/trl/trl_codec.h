/*
 * trl_codec.h — public, versioned codec SPI + vtable ABI (design §4, §6.1).
 *
 * This is the core/codec boundary in C: the storage SPI the core offers a codec
 * (services on an opaque `trl_store`) and the codec vtables the core calls
 * (`encode_block`/`decode_block` and lifecycle). The *entire* surface —
 * including `finalize` and `input_streams` — is declared in Phase 1 so Phase 6
 * can freeze it without additions, even though the re-homed built-ins use no-op
 * `finalize`/`input_streams`.
 *
 * ABI discipline (impl-plan §3, decision 5): every entry point is a plain
 * `extern "C"` symbol with a stable signature, every vtable slot is a function
 * pointer, and `trl_store`/`trl_blkout` are opaque handles — no macros or
 * `static inline` in the contract surface. This is what makes pairing 3 (a
 * Python codec driven by the C container via ctypes, Phase 3.5) possible:
 * ctypes declares these functions and synthesizes CFUNCTYPE thunks for a Python
 * codec's vtable.
 */
#pragma once

#include <stddef.h>
#include <stdint.h>

#include "trl/trl.h"

#ifdef __cplusplus
extern "C" {
#endif

/* Codec SPI/ABI version. Frozen as v1 in Phase 6 (header-snapshot test). */
#define TRL_CODEC_ABI_VERSION 1

/* Per-block flag: payload produced by the stream-type's structural codec, vs
 * the legacy built-in path (impl-plan §2.2). Mirrors Python FLAG_STRUCT_CODEC.
 * 0 == legacy. Top bit, free across the VC and TXN flag spaces. */
#define TRL_FLAG_STRUCT_CODEC 0x80

/* Type-registry entry tag carrying per-stream-type codec identity + params
 * (impl-plan §2.1, decision 6). Length-prefixed so old readers skip it. */
#define TRL_TYPE_TAG_STREAM_V2 0x05

/* Reverse-DNS ids of the built-in codecs. The prefix `org.fvutils.trlog.` is
 * reserved for built-ins (design §4.1/§4.2). */
#define TRL_CODEC_CORE_VALUECHANGE "org.fvutils.trlog.core.valuechange"
#define TRL_CODEC_CORE_RECORD      "org.fvutils.trlog.core.record"

/* Codec capability bits (design §4.1). */
typedef enum {
    TRL_CAP_NONE                = 0,
    TRL_CAP_RANDOM_ACCESS       = 1u << 0,
    TRL_CAP_NEEDS_INPUT_STREAMS = 1u << 1,
    TRL_CAP_HAS_RAW_FALLBACK    = 1u << 2,
    TRL_CAP_LOSSLESS            = 1u << 3,
    TRL_CAP_MATERIALIZED        = 1u << 4
} trl_codec_caps_t;

/* Opaque handles the core hands a codec. */
typedef struct trl_store  trl_store;   /* storage-SPI services + bound trace */
typedef struct trl_blkout trl_blkout;  /* append buffer for one output block */

/* ----------------------------------------------------------------------- *
 * Codec vtables. `self` is the codec-private per-stream state returned by
 * open_writer/open_reader (NULL for the stateless built-ins). The decoder
 * emits reconstructed data through the same callbacks the public reader uses
 * (trl_vc_cb / trl_txn_cb_t from trl.h).
 * ----------------------------------------------------------------------- */

typedef struct trl_vc_codec {
    const char *codec_id;   /* reverse-DNS, canonical form */
    uint16_t    version;    /* codec-internal format version */
    uint32_t    caps;       /* trl_codec_caps_t bitset */

    void *(*open_writer)(trl_store *st, uint32_t stream_id,
                         const void *params, size_t param_len);
    void *(*open_reader)(trl_store *st, uint32_t stream_id,
                         const void *params, size_t param_len);
    void  (*close)(void *self);

    /* Called once per writer at trace close, after the last encode_block and
     * before the index/footer; serializes trace-wide accumulated metadata
     * (design §4.7). Returns 0 on success. */
    int (*finalize)(void *self, trl_store *st);

    /* Flush accumulated state into the block-output buffer `out`. The core
     * compresses+indexes the result. Returns 0 on success. */
    int (*encode_block)(void *self, trl_store *st, trl_blkout *out);

    /* Reconstruct value-changes from a decompressed payload and emit each. */
    int (*decode_block)(void *self, trl_store *st,
                        const uint8_t *payload, size_t len, uint8_t flags,
                        trl_vc_cb emit, void *user);

    /* Stream ids that must be materialized first (derived codecs). 0 entries
     * for self-contained codecs. */
    int (*input_streams)(void *self, trl_store *st,
                         uint32_t **out_ids, size_t *n);
} trl_vc_codec_t;

typedef struct trl_txn_codec {
    const char *codec_id;
    uint16_t    version;
    uint32_t    caps;

    void *(*open_writer)(trl_store *st, uint32_t stream_id,
                         const void *params, size_t param_len);
    void *(*open_reader)(trl_store *st, uint32_t stream_id,
                         const void *params, size_t param_len);
    void  (*close)(void *self);
    int   (*finalize)(void *self, trl_store *st);
    int   (*encode_block)(void *self, trl_store *st, trl_blkout *out);
    int   (*decode_block)(void *self, trl_store *st,
                          const uint8_t *payload, size_t len, uint8_t flags,
                          trl_txn_cb_t emit, void *user);
    int   (*input_streams)(void *self, trl_store *st,
                           uint32_t **out_ids, size_t *n);
} trl_txn_codec_t;

/* ----------------------------------------------------------------------- *
 * Registry (design §4.2). Codecs are keyed by codec_id string. Core codecs
 * self-register at library init; extensions register before trl_writer_open /
 * after trl_reader_open. Returns 0 on success, non-zero on invalid id / OOM.
 * The registry stores the pointer; the vtable must outlive the trace.
 * ----------------------------------------------------------------------- */
int trl_register_vc_codec(const trl_vc_codec_t *codec);
int trl_register_txn_codec(const trl_txn_codec_t *codec);
const trl_vc_codec_t  *trl_lookup_vc_codec(const char *codec_id);
const trl_txn_codec_t *trl_lookup_txn_codec(const char *codec_id);

/* ----------------------------------------------------------------------- *
 * Storage SPI surface offered on `trl_store` (subset; grows in later phases).
 * Plain extern symbols so ctypes can call them directly.
 * ----------------------------------------------------------------------- */

/* Intern a string in the trace's string table (write side). Returns the id. */
uint32_t trl_store_intern(trl_store *st, const char *s);

/* Append raw bytes (already block-framed) into a block-output buffer. */
int trl_blkout_append(trl_blkout *out, const uint8_t *data, size_t len);

/* ----------------------------------------------------------------------- *
 * Phase 3.5 (ctypes bridge): writer codec selection + the value-change
 * change-accessor SPI. These let the C container drive a *non-core* codec —
 * including a Python codec reached via ctypes (pairing 3, impl-plan §0/§3.5).
 * A codec's encode_block reads the block's accumulated changes through the
 * accessor and writes its payload back via trl_blkout_append, so the codec
 * never needs the writer's internals. Minimal slice: U64-kind changes.
 * ----------------------------------------------------------------------- */

/* Select the value-change codec the writer uses for subsequent blocks. The id
 * must already be registered (trl_register_vc_codec). open_writer is invoked now
 * and its per-stream state threaded through encode_block/finalize/close. Pass
 * NULL to revert to the core value-change codec. Returns 0 on success, non-zero
 * if the id is unknown. */
int trl_writer_set_vc_codec(trl_writer_t *w, const char *codec_id);

/* Value-change kinds reported by the accessor (mirror trl_vc_value_kind_t). */
typedef enum {
    TRL_VC_KIND_U64    = 0,
    TRL_VC_KIND_BYTES  = 1,
    TRL_VC_KIND_STRING = 2,
    TRL_VC_KIND_REAL   = 3
} trl_vc_change_kind_t;

/* The current block's accumulated value-changes, flattened var-major to a
 * linear index. Valid only inside encode_block (write side). */
size_t   trl_store_vc_change_count(trl_store *st);
uint64_t trl_store_vc_start_time(trl_store *st);
uint64_t trl_store_vc_end_time(trl_store *st);

/* Fetch the i-th change. Out-params may be NULL. `kind` is a
 * trl_vc_change_kind_t; `u64val` is meaningful for the U64 kind (this slice).
 * Returns 0 on success, non-zero if i is out of range. */
int trl_store_vc_change_at(trl_store *st, size_t i, uint32_t *var_id,
                           uint64_t *time, uint32_t *kind, uint64_t *u64val);

#ifdef __cplusplus
}
#endif
