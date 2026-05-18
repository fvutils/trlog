#pragma once

#include "trl_internal.h"

size_t trl_uvarint_size(uint64_t value);
trl_status_t trl_buf_append_uvarint(trl_buf_t *buf, uint64_t value);
trl_status_t trl_buf_append_svarint(trl_buf_t *buf, int64_t value);
trl_status_t trl_decode_uvarint(const uint8_t *data, size_t len, size_t *offset, uint64_t *value);
trl_status_t trl_decode_svarint(const uint8_t *data, size_t len, size_t *offset, int64_t *value);
