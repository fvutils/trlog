#ifndef TRL_PORTABLE_H
#define TRL_PORTABLE_H
/*
 * trl_portable.h — small helpers for what GCC/Clang/POSIX provide but MSVC
 * does not: checked int64 arithmetic, 64-bit fseek, strdup, and fnmatch.
 * Header-only (static inline), so libtrl and libtrlog_vpi both use it
 * without libtrl exporting anything for it.
 */

#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

/* ---- checked int64 arithmetic: return true on overflow ---------------- */

#if defined(__GNUC__) || defined(__clang__)
static inline bool trl_add_overflow_i64(int64_t a, int64_t b, int64_t *r) { return __builtin_add_overflow(a, b, r); }
static inline bool trl_sub_overflow_i64(int64_t a, int64_t b, int64_t *r) { return __builtin_sub_overflow(a, b, r); }
static inline bool trl_mul_overflow_i64(int64_t a, int64_t b, int64_t *r) { return __builtin_mul_overflow(a, b, r); }
#else
static inline bool trl_add_overflow_i64(int64_t a, int64_t b, int64_t *r) {
    if ((b > 0 && a > INT64_MAX - b) || (b < 0 && a < INT64_MIN - b)) return true;
    *r = a + b;
    return false;
}
static inline bool trl_sub_overflow_i64(int64_t a, int64_t b, int64_t *r) {
    if ((b < 0 && a > INT64_MAX + b) || (b > 0 && a < INT64_MIN + b)) return true;
    *r = a - b;
    return false;
}
static inline bool trl_mul_overflow_i64(int64_t a, int64_t b, int64_t *r) {
    if (a == 0 || b == 0) { *r = 0; return false; }
    if ((a == -1 && b == INT64_MIN) || (b == -1 && a == INT64_MIN)) return true;
    if (a > 0 ? (b > 0 ? a > INT64_MAX / b : b < INT64_MIN / a)
              : (b > 0 ? a < INT64_MIN / b : a < INT64_MAX / b))
        return true;
    *r = a * b;
    return false;
}
#endif

/* ---- 64-bit file offsets ---------------------------------------------- */

static inline int trl_fseek64(FILE *fp, int64_t off, int whence) {
#if defined(_WIN32)
    return _fseeki64(fp, off, whence);
#else
    return fseeko(fp, (off_t)off, whence);
#endif
}

/* ---- strdup ------------------------------------------------------------ */

static inline char *trl_strdup(const char *s) {
    size_t n = strlen(s) + 1;
    char *d = (char *)malloc(n);
    if (d) memcpy(d, s, n);
    return d;
}

/* ---- glob matching ------------------------------------------------------ *
 * The subset of fnmatch(pat, s, 0) trlog uses, on every platform so the
 * behaviour is the same everywhere: '*' (also matches '/'), '?', bracket
 * sets "[abc]", "[a-z]", "[!x]"/"[^x]" (']' first is literal), and '\'
 * escaping the next character (a trailing '\' matches nothing).  An
 * unclosed '[' matches itself.  Returns true on a match.
 */

/* Match one bracket set at *pp against c. Sets *pp past the set and returns
 * 1/0 for match/no match, or -1 if the set is unclosed (*pp unchanged). */
static inline int trl_glob_set(const char **pp, unsigned char c) {
    const char *p = *pp + 1;
    bool neg = (*p == '!' || *p == '^');
    if (neg) p++;
    bool hit = false, first = true;
    while (*p && (first || *p != ']')) {
        unsigned char lo = (unsigned char)*p, hi;
        if (lo == '\\' && p[1]) lo = (unsigned char)*++p;
        p++;
        if (p[0] == '-' && p[1] && p[1] != ']') {
            hi = (unsigned char)p[1];
            if (hi == '\\' && p[2]) { hi = (unsigned char)p[2]; p++; }
            p += 2;
        } else {
            hi = lo;
        }
        if (c >= lo && c <= hi) hit = true;
        first = false;
    }
    if (*p != ']') return -1;
    *pp = p + 1;
    return hit != neg;
}

static inline bool trl_glob_match(const char *pat, const char *s) {
    const char *star_p = NULL, *star_s = NULL;
    while (*s) {
        const char *p = pat;
        int m;
        if (*p == '*') {
            while (*p == '*') p++;
            star_p = p; star_s = s; pat = p;
            continue;
        }
        if (*p == '?') { pat = p + 1; s++; continue; }
        if (*p == '[' && (m = trl_glob_set(&p, (unsigned char)*s)) >= 0) {
            if (m) { pat = p; s++; continue; }
        } else {
            if (*p == '\\' && !*++p) return false;   /* trailing '\\' never matches */
            if (*p && *p == *s) { pat = p + 1; s++; continue; }
        }
        if (!star_p) return false;   /* mismatch: backtrack to the last '*' */
        pat = star_p; s = ++star_s;
    }
    while (*pat == '*') pat++;
    return *pat == '\0';
}

#endif /* TRL_PORTABLE_H */
