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

trl_status_t trl_buf_append_uvarint_be(trl_buf_t *buf, const uint8_t *be, size_t n) {
    if (n <= 8) {
        uint64_t v = 0;
        for (size_t i = 0; i < n; ++i) v = (v << 8) | be[i];
        return trl_buf_append_uvarint(buf, v);
    }
    size_t lead = 0;
    while (lead < n && !be[lead]) ++lead;
    if (lead == n) return trl_buf_append_u8(buf, 0);
    size_t nbits = (n - lead - 1) * 8;
    for (uint8_t top = be[lead]; top; top >>= 1) ++nbits;
    trl_status_t st = TRL_OK;
    for (size_t bit = 0; st == TRL_OK && bit < nbits; bit += 7) {
        uint8_t b = 0;
        for (size_t k = 0; k < 7 && bit + k < nbits; ++k) {
            size_t pos = bit + k;
            if ((be[n - 1 - pos / 8] >> (pos % 8)) & 1u) b |= (uint8_t)(1u << k);
        }
        if (bit + 7 < nbits) b |= 0x80u;
        st = trl_buf_append_u8(buf, b);
    }
    return st;
}

trl_status_t trl_decode_uvarint_be(const uint8_t *data, size_t len, size_t *offset, uint8_t *be, size_t n) {
    memset(be, 0, n);
    size_t bit = 0;
    while (*offset < len) {
        uint8_t byte = data[(*offset)++];
        for (size_t k = 0; k < 7; ++k, ++bit) {
            if (!((byte >> k) & 1u)) continue;
            if (bit >= n * 8) return TRL_ERR_CORRUPT;
            be[n - 1 - bit / 8] |= (uint8_t)(1u << (bit % 8));
        }
        if (!(byte & 0x80u)) return TRL_OK;
    }
    return TRL_ERR_CORRUPT;
}
