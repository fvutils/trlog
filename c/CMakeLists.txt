cmake_minimum_required(VERSION 3.14)
project(libtrl C)
set(CMAKE_C_STANDARD 11)

find_package(ZLIB REQUIRED)
# libtrl now calls LZ4 directly (per-wave VC compression), so link liblz4.
find_library(LZ4_LIBRARY NAMES lz4 liblz4 REQUIRED)

add_library(trl SHARED
  src/varint.c src/string_table.c src/type_registry.c
  src/hierarchy.c src/vc_data.c src/txn_data.c src/ext.c
  src/index.c src/writer.c src/reader.c src/codec_registry.c
  src/rec.c src/query.c src/qscan.c
)
target_include_directories(trl PUBLIC include src)
target_link_libraries(trl PRIVATE ZLIB::ZLIB ${LZ4_LIBRARY})
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
install(TARGETS trl_sv DESTINATION lib)

enable_testing()
add_subdirectory(tests)
