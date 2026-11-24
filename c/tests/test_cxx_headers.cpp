// test_cxx_headers.cpp — the public C headers can be used from C++ (#16).
//
// Without extern "C" guards the calls below would link against C++-mangled
// names and fail to link.

#include "trl/trl.h"
#include "trl/trl_rec.h"
#include "trl/trl_query.h"
#include "trl/trl_codec.h"
#include "trl/trl_qscan.h"

#include <cstdio>

int main(int argc, char **argv) {
    if (argc < 2) {
        std::fprintf(stderr, "usage: %s <out_path>\n", argv[0]);
        return 2;
    }
    trl_writer_t *w = trl_writer_open(argv[1], -9, TRL_COMPRESS_NONE);
    if (!w) return 1;
    trl_begin_hierarchy(w, 1, 0, trl_intern(w, "design"));
    trl_hier_begin_scope(w, 0, trl_intern(w, "top"), 0, 0, 0);
    trl_hier_add_attr(w, trl_intern(w, "k"), trl_intern(w, "v"));
    trl_hier_end_scope(w);
    trl_flush_hierarchy(w);
    if (trl_writer_close(w) != TRL_OK) return 1;

    trl_rdb_t *rdb = trl_rdb_open("cxx", argv[1]);
    if (!rdb) return 1;
    trl_rdb_close(rdb);
    (void)trl_last_error();
    std::printf("test_cxx_headers: OK\n");
    return 0;
}
