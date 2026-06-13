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

# Codec registry / ABI-surface unit tests.
add_executable(test_codec test_codec.c)
target_link_libraries(test_codec PRIVATE trl)
target_include_directories(test_codec PRIVATE ${CMAKE_SOURCE_DIR}/include)
add_test(NAME c_codec COMMAND test_codec)
