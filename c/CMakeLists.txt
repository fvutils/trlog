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
)
target_include_directories(trl PUBLIC include src)
target_link_libraries(trl PRIVATE ZLIB::ZLIB ${LZ4_LIBRARY})
set_target_properties(trl PROPERTIES OUTPUT_NAME trl)

install(TARGETS trl DESTINATION lib)
install(DIRECTORY include/trl DESTINATION include)

enable_testing()
add_subdirectory(tests)
