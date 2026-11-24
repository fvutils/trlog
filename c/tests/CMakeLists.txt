cmake_minimum_required(VERSION 3.14)

# Phase 0 golden / parity harness for the C implementation.
add_executable(test_golden test_golden.c)
target_link_libraries(test_golden PRIVATE trl)
target_include_directories(test_golden PRIVATE ${CMAKE_SOURCE_DIR}/include)

# Output dir for files the harness produces during the test run.
set(_golden_out ${CMAKE_CURRENT_BINARY_DIR}/golden_out)
file(MAKE_DIRECTORY ${_golden_out})

# Python golden fixtures live in the repo at tests/golden/fixtures. This source
# tree is c/tests, so the fixtures are two levels up from CMAKE_CURRENT_SOURCE_DIR.
set(_fixture_dir ${CMAKE_CURRENT_SOURCE_DIR}/../../tests/golden/fixtures)

add_test(
    NAME c_golden
    COMMAND test_golden ${_golden_out} ${_fixture_dir}
)

# Phase 2 harness for the L0 recording session (trl/trl_rec.h).
add_executable(test_rec test_rec.c)
target_link_libraries(test_rec PRIVATE trl)
target_include_directories(test_rec PRIVATE ${CMAKE_SOURCE_DIR}/include)
add_test(
    NAME c_rec
    COMMAND test_rec ${CMAKE_CURRENT_BINARY_DIR}/phase2_session.trl
)

# Phase 3 harness for the L0 reading session (trl/trl_query.h).
add_executable(test_query test_query.c)
target_link_libraries(test_query PRIVATE trl)
target_include_directories(test_query PRIVATE ${CMAKE_SOURCE_DIR}/include)
add_test(
    NAME c_query
    COMMAND test_query ${CMAKE_CURRENT_BINARY_DIR}/phase3_query.trl
)

# Phase 4 harness for the DPI-C glue (trl/trl_sv.c, L1), called with its
# plain-C ABI (no simulator involved; see sv/ for the Verilator smoke test).
add_executable(test_sv test_sv.c)
target_link_libraries(test_sv PRIVATE trl_sv trl)
add_test(
    NAME c_sv
    COMMAND test_sv ${CMAKE_CURRENT_BINARY_DIR}/phase4_sv.trl
)

# Codec registry / ABI-surface unit tests.
add_executable(test_codec test_codec.c)
target_link_libraries(test_codec PRIVATE trl)
target_include_directories(test_codec PRIVATE ${CMAKE_SOURCE_DIR}/include)
add_test(NAME c_codec COMMAND test_codec)

# Query scan engine (trl/trl_qscan.h, query CLI phase Q4), without Python.
# It reads internal tables, which libtrl does not export: link the objects.
add_executable(test_qscan test_qscan.c $<TARGET_OBJECTS:trl_obj>)
target_link_libraries(test_qscan PRIVATE trl_zlib trl_lz4)
target_compile_definitions(test_qscan PRIVATE TRL_STATIC)
target_include_directories(test_qscan PRIVATE
    $<TARGET_PROPERTY:trl_obj,INTERFACE_INCLUDE_DIRECTORIES>)
target_include_directories(test_qscan PRIVATE ${CMAKE_SOURCE_DIR}/include ${CMAKE_SOURCE_DIR}/src)
add_test(
    NAME c_qscan
    COMMAND test_qscan ${CMAKE_CURRENT_BINARY_DIR}/q4_qscan.trl
)

# Wide 2-state and N-bit 4-state / 9-state value encoding (code fix task 1).
add_executable(test_vc_wide test_vc_wide.c)
target_link_libraries(test_vc_wide PRIVATE trl)
target_include_directories(test_vc_wide PRIVATE ${CMAKE_SOURCE_DIR}/include)
add_test(
    NAME c_vc_wide
    COMMAND test_vc_wide ${CMAKE_CURRENT_BINARY_DIR}/vc_wide.trl
)

# C API fixes (code fix task 9): trl_hier_add_attr, trl_rec_full vectors,
# disabled streams. Read back by tests/integration/test_c_api_fixes.py.
add_executable(test_capi test_capi.c)
target_link_libraries(test_capi PRIVATE trl)
target_include_directories(test_capi PRIVATE ${CMAKE_SOURCE_DIR}/include)
add_test(NAME c_capi COMMAND test_capi ${CMAKE_CURRENT_BINARY_DIR})

# The public headers compile and link from C++ (extern "C" guards), when a
# C++ compiler is available.
include(CheckLanguage)
check_language(CXX)
if(CMAKE_CXX_COMPILER)
    enable_language(CXX)
    add_executable(test_cxx_headers test_cxx_headers.cpp)
    target_link_libraries(test_cxx_headers PRIVATE trl)
    target_include_directories(test_cxx_headers PRIVATE ${CMAKE_SOURCE_DIR}/include)
    add_test(NAME c_cxx_headers
             COMMAND test_cxx_headers ${CMAKE_CURRENT_BINARY_DIR}/cxx_headers.trl)
endif()

# trl_reader_open_mem / trl_rdb_open_mem match file-based reading.
add_executable(test_open_mem test_open_mem.c)
target_link_libraries(test_open_mem PRIVATE trl)
target_include_directories(test_open_mem PRIVATE ${CMAKE_SOURCE_DIR}/include)
add_test(NAME c_open_mem COMMAND test_open_mem ${_fixture_dir})
