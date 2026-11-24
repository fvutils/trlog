cmake_minimum_required(VERSION 3.14)
project(libtrl C)
set(CMAKE_C_STANDARD 11)

# zlib + lz4: vendored static builds by default, system libs with
# -DTRL_SYSTEM_DEPS=ON (see cmake/trl_deps.cmake).
include(${CMAKE_CURRENT_SOURCE_DIR}/cmake/trl_deps.cmake)

# The sources build once as an object library: libtrl is made from it, and
# C tests that need internal (unexported) functions link it directly.
add_library(trl_obj OBJECT
  src/varint.c src/string_table.c src/type_registry.c
  src/hierarchy.c src/vc_data.c src/txn_data.c src/ext.c
  src/index.c src/writer.c src/reader.c src/codec_registry.c
  src/rec.c src/query.c src/qscan.c
)
target_include_directories(trl_obj PUBLIC include src)
target_link_libraries(trl_obj PRIVATE trl_zlib trl_lz4)
# Only TRL_API-marked functions (trl/trl_api.h) are exported. Hidden by
# default everywhere, so Linux builds catch a missing mark that would
# otherwise only show up as a missing export in the Windows DLL.
target_compile_definitions(trl_obj PRIVATE TRL_BUILDING_TRL)
set_target_properties(trl_obj PROPERTIES POSITION_INDEPENDENT_CODE ON
    C_VISIBILITY_PRESET hidden VISIBILITY_INLINES_HIDDEN ON)

add_library(trl SHARED $<TARGET_OBJECTS:trl_obj>)
target_include_directories(trl PUBLIC include src)
target_link_libraries(trl PRIVATE trl_zlib trl_lz4)
set_target_properties(trl PROPERTIES OUTPUT_NAME trl)

install(TARGETS trl DESTINATION lib)
install(DIRECTORY include/trl DESTINATION include)

# trl_sv: DPI-C glue for the SystemVerilog API (phase 4, L1). trl_sv.c has
# no svdpi.h dependency of its own (chandle/string/int map to plain C types
# at the DPI-C boundary with no header needed), so this library builds
# unconditionally; only the Verilator smoke test below is conditional on
# finding a simulator.
add_library(trl_sv SHARED src/trl_sv.c)
target_link_libraries(trl_sv PRIVATE trl)
target_include_directories(trl_sv PUBLIC include)
set_target_properties(trl_sv PROPERTIES C_VISIBILITY_PRESET hidden)
install(TARGETS trl_sv DESTINATION lib)

# Wheel builds turn the tests off: only the libraries ship. The ivpm_build
# backend passes no -D options of its own, so the default can also come from
# the environment (cibuildwheel sets TRL_BUILD_TESTS=OFF, see pyproject.toml).
if(DEFINED ENV{TRL_BUILD_TESTS})
    set(_trl_tests_default $ENV{TRL_BUILD_TESTS})
else()
    set(_trl_tests_default ON)
endif()
option(TRL_BUILD_TESTS "Build the C test harnesses (ctest)" ${_trl_tests_default})
if(TRL_BUILD_TESTS)
    enable_testing()
    add_subdirectory(tests)
endif()
