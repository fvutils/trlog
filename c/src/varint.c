#include "varint_internal.h"

size_t trl_uvarint_size(uint64_t value) {
    size_t n = 1;
    while (value >= 0x80) {
        value >>= 7;
        ++n;
    }
    return n;
}

trl_status_t trl_buf_append_uvarint(trl_buf_t *buf, uint64_t value) {
    uint8_t tmp[10];
    size_t n = 0;
    do {
        uint8_t b = (uint8_t)(value & 0x7Fu);
        value >>= 7;
        if (value) {
            b |= 0x80u;
        }
        tmp[n++] = b;
    } while (value);
    return trl_buf_append(buf, tmp, n);
}

trl_status_t trl_buf_append_svarint(trl_buf_t *buf, int64_t value) {
    uint64_t zigzag = (((uint64_t)value) << 1) ^ (uint64_t)(value >> 63);
    return trl_buf_append_uvarint(buf, zigzag);
}

trl_status_t trl_decode_uvarint(const uint8_t *data, size_t len, size_t *offset, uint64_t *value) {
    uint64_t result = 0;
    unsigned shift = 0;
    while (*offset < len) {
        uint8_t byte = data[(*offset)++];
        result |= ((uint64_t)(byte & 0x7Fu)) << shift;
        if (!(byte & 0x80u)) {
            *value = result;
            return TRL_OK;
        }
        shift += 7;
        if (shift >= 64) {
            return TRL_ERR_CORRUPT;
        }
    }
    return TRL_ERR_CORRUPT;
}

trl_status_t trl_decode_svarint(const uint8_t *data, size_t len, size_t *offset, int64_t *value) {
    uint64_t raw = 0;
    trl_status_t st = trl_decode_uvarint(data, len, offset, &raw);
    if (st != TRL_OK) {
        return st;
    }
    *value = (int64_t)((raw >> 1) ^ (~(raw & 1) + 1));
    return TRL_OK;
}
