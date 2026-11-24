#****************************************************************************
#* CMakeLists.txt -- trlog WebAssembly build
#*
#* A separate configuration from the native one: under Emscripten there are
#* no shared libraries, nothing to install, and one link step produces the
#* whole module. Drive it through emcmake:
#*
#*   source scripts/wasm-env.sh
#*   emcmake cmake -S wasm -B build-wasm -DCMAKE_BUILD_TYPE=Release
#*   cmake --build build-wasm -j
#*
#* Output: ts/src/wasm/trlog.{js,wasm} (override with -DTRLOG_WASM_OUT_DIR).
#* Read-only for now: traces are opened from bytes (trl_reader_open_mem),
#* the module has no filesystem.
#****************************************************************************
cmake_minimum_required(VERSION 3.16)
project(trlog_wasm C CXX)

if(NOT EMSCRIPTEN)
  message(FATAL_ERROR
    "wasm/CMakeLists.txt must be configured through emcmake. "
    "Run `source scripts/wasm-env.sh` then `emcmake cmake -S wasm -B build-wasm`.")
endif()

set(CMAKE_C_STANDARD 11)
set(CMAKE_CXX_STANDARD 17)
set(CMAKE_CXX_STANDARD_REQUIRED ON)

option(ENABLE_ASAN "Build with AddressSanitizer (diagnostic only, not shippable)" OFF)

get_filename_component(TRLOG_ROOT "${CMAKE_CURRENT_SOURCE_DIR}/.." ABSOLUTE)

# zlib and lz4 from source, exactly as the native build does
# (PACKAGES_DIR / TRL_ZLIB_SRC / TRL_LZ4_SRC overrides apply).
set(TRL_SYSTEM_DEPS OFF CACHE BOOL "" FORCE)
include("${TRLOG_ROOT}/c/cmake/trl_deps.cmake")

# The same file set as c/CMakeLists.txt's trl_obj, minus nothing: writer.c
# and qscan.c still call fopen, which links (and fails at run time) under
# -sFILESYSTEM=0.
set(TRL_SRC_DIR "${TRLOG_ROOT}/c/src")
add_executable(trlog
  ${TRL_SRC_DIR}/varint.c ${TRL_SRC_DIR}/string_table.c ${TRL_SRC_DIR}/type_registry.c
  ${TRL_SRC_DIR}/hierarchy.c ${TRL_SRC_DIR}/vc_data.c ${TRL_SRC_DIR}/txn_data.c ${TRL_SRC_DIR}/ext.c
  ${TRL_SRC_DIR}/index.c ${TRL_SRC_DIR}/writer.c ${TRL_SRC_DIR}/reader.c ${TRL_SRC_DIR}/codec_registry.c
  ${TRL_SRC_DIR}/rec.c ${TRL_SRC_DIR}/query.c ${TRL_SRC_DIR}/qscan.c
  "${CMAKE_CURRENT_SOURCE_DIR}/bindings.cpp")

target_include_directories(trlog PRIVATE "${TRLOG_ROOT}/c/include" "${TRL_SRC_DIR}")
target_compile_definitions(trlog PRIVATE TRL_BUILDING_TRL)
target_link_libraries(trlog PRIVATE trl_zlib trl_lz4)

set(TRLOG_LINK_FLAGS
  "-lembind"
  "-sMODULARIZE=1"
  "-sEXPORT_ES6=1"
  "-sEXPORT_NAME=createTrlogModule"
  "-sALLOW_MEMORY_GROWTH=1"
  "-sFILESYSTEM=0"
  "-sENVIRONMENT=web,worker,node"
  "-sDYNAMIC_EXECUTION=0"
  # 1 MiB instead of the 64 KiB default; a release build has no overflow check.
  "-sSTACK_SIZE=1048576"
  # A C++ exception from the bindings must reach JS as an Error with its text.
  "-fexceptions"
  "-sDISABLE_EXCEPTION_CATCHING=0")
target_compile_options(trlog PRIVATE $<$<COMPILE_LANGUAGE:CXX>:-fexceptions>)

set(TRLOG_WASM_OUT_DIR "${TRLOG_ROOT}/ts/src/wasm" CACHE PATH "Where trlog.{js,wasm} are written")
if(ENABLE_ASAN)
  # Fixed heap: ASan's shadow memory is reserved against the initial heap.
  list(REMOVE_ITEM TRLOG_LINK_FLAGS "-sALLOW_MEMORY_GROWTH=1" "-sSTACK_SIZE=1048576")
  list(APPEND TRLOG_LINK_FLAGS
    "-fsanitize=address" "-sINITIAL_MEMORY=1073741824" "-sSTACK_SIZE=4194304" "-g2")
  target_compile_options(trlog PRIVATE -fsanitize=address -g)
  # Never overwrite the shipped artifact with the diagnostic one.
  set(TRLOG_WASM_OUT_DIR "${CMAKE_BINARY_DIR}")
  message(STATUS "trlog-wasm: AddressSanitizer ENABLED -- diagnostic build")
endif()
target_link_options(trlog PRIVATE ${TRLOG_LINK_FLAGS})

set_target_properties(trlog PROPERTIES
  OUTPUT_NAME trlog
  SUFFIX ".js"
  RUNTIME_OUTPUT_DIRECTORY "${TRLOG_WASM_OUT_DIR}")
