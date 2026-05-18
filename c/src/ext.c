#include "trl_internal.h"

trl_status_t trl_ext_encode_block(const uint8_t ext_type[4], uint16_t ext_version, const uint8_t *payload, size_t payload_len, trl_buf_t *out) {
    trl_buf_t body;
    trl_status_t st;
    trl_buf_init(&body);
    st = trl_buf_append(&body, ext_type, 4);
    if (st == TRL_OK) st = trl_buf_append_le16(&body, ext_version);
    if (st == TRL_OK && payload_len) st = trl_buf_append(&body, payload, payload_len);
    if (st == TRL_OK) st = trl_make_block(TRL_BLK_EXT, 0, body.data, body.size, out);
    trl_buf_free(&body);
    return st;
}

trl_status_t trl_ext_decode_payload(const uint8_t *payload, size_t payload_len, uint8_t ext_type[4], uint16_t *ext_version, const uint8_t **ext_payload, size_t *ext_payload_len) {
    if (payload_len < 6) {
        return TRL_ERR_CORRUPT;
    }
    memcpy(ext_type, payload, 4);
    *ext_version = (uint16_t)(payload[4] | (payload[5] << 8));
    *ext_payload = payload + 6;
    *ext_payload_len = payload_len - 6;
    return TRL_OK;
}
