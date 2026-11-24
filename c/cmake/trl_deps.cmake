# Compression dependencies of libtrl: zlib and lz4.
#
# By default both are built from source as static, position-independent
# libraries and linked into trl, so wheels (manylinux/macOS/Windows) and the
# Emscripten build need no system libraries. Sources come from
# <PACKAGES_DIR>/zlib and <PACKAGES_DIR>/lz4, fetched by `ivpm update`
# (pinned in ivpm.yaml); -DTRL_ZLIB_SRC / -DTRL_LZ4_SRC override them.
#
# -DTRL_SYSTEM_DEPS=ON uses the system zlib and liblz4 instead (distro builds).
#
# Defines the targets trl_zlib and trl_lz4 for target_link_libraries.

option(TRL_SYSTEM_DEPS "Link the system zlib and liblz4 instead of building them from source" OFF)

if(TRL_SYSTEM_DEPS)
    find_package(ZLIB REQUIRED)
    find_path(LZ4_INCLUDE_DIR lz4.h REQUIRED)
    find_library(LZ4_LIBRARY NAMES lz4 liblz4 REQUIRED)
    add_library(trl_zlib INTERFACE)
    target_link_libraries(trl_zlib INTERFACE ZLIB::ZLIB)
    add_library(trl_lz4 INTERFACE)
    target_include_directories(trl_lz4 INTERFACE ${LZ4_INCLUDE_DIR})
    target_link_libraries(trl_lz4 INTERFACE ${LZ4_LIBRARY})
    message(STATUS "trlog: using system zlib and liblz4")
    return()
endif()

# The ivpm packages directory: -DPACKAGES_DIR (passed by the ivpm_build
# backend), then <repo>/packages, then $IVPM_PACKAGES, then the directory
# holding the repo (trlog checked out as another project's dependency).
# <repo>/packages comes before $IVPM_PACKAGES because a stale variable from
# another workspace's direnv would otherwise win over the repo's own deps.
get_filename_component(_trl_repo ${CMAKE_CURRENT_LIST_DIR}/../.. ABSOLUTE)
if(NOT PACKAGES_DIR)
    if(EXISTS ${_trl_repo}/packages)
        set(PACKAGES_DIR ${_trl_repo}/packages)
    elseif(DEFINED ENV{IVPM_PACKAGES})
        set(PACKAGES_DIR $ENV{IVPM_PACKAGES})
    else()
        get_filename_component(PACKAGES_DIR ${_trl_repo} DIRECTORY)
    endif()
endif()
set(PACKAGES_DIR ${PACKAGES_DIR} CACHE PATH "ivpm packages (dependency) directory")

set(TRL_ZLIB_SRC ${PACKAGES_DIR}/zlib CACHE PATH "zlib source tree")
set(TRL_LZ4_SRC ${PACKAGES_DIR}/lz4 CACHE PATH "lz4 source tree")

foreach(_f ${TRL_ZLIB_SRC}/zlib.h ${TRL_LZ4_SRC}/lib/lz4.h)
    if(NOT EXISTS ${_f})
        message(FATAL_ERROR
            "${_f} not found. Run `ivpm update` to fetch zlib and lz4, "
            "set -DPACKAGES_DIR / -DTRL_ZLIB_SRC / -DTRL_LZ4_SRC, "
            "or configure with -DTRL_SYSTEM_DEPS=ON to use the system libraries.")
    endif()
endforeach()
message(STATUS "trlog: building zlib from ${TRL_ZLIB_SRC}, lz4 from ${TRL_LZ4_SRC}")

# zlib: the in-memory deflate/inflate core only; trl uses no gz* file I/O.
add_library(trl_zlib STATIC
    ${TRL_ZLIB_SRC}/adler32.c ${TRL_ZLIB_SRC}/compress.c ${TRL_ZLIB_SRC}/crc32.c
    ${TRL_ZLIB_SRC}/deflate.c ${TRL_ZLIB_SRC}/infback.c ${TRL_ZLIB_SRC}/inffast.c
    ${TRL_ZLIB_SRC}/inflate.c ${TRL_ZLIB_SRC}/inftrees.c ${TRL_ZLIB_SRC}/trees.c
    ${TRL_ZLIB_SRC}/uncompr.c ${TRL_ZLIB_SRC}/zutil.c
)
target_include_directories(trl_zlib PUBLIC ${TRL_ZLIB_SRC})
if(NOT WIN32)
    # unistd.h is available; avoids zconf.h's off_t guesswork.
    target_compile_definitions(trl_zlib PRIVATE Z_HAVE_UNISTD_H)
endif()

# lz4: the block API only (LZ4_compress_default / LZ4_decompress_safe).
add_library(trl_lz4 STATIC ${TRL_LZ4_SRC}/lib/lz4.c)
target_include_directories(trl_lz4 PUBLIC ${TRL_LZ4_SRC}/lib)
# lz4.h marks its API visibility("default"); clear that so the copy linked
# into trl is not exported (it must not interpose on another liblz4 in the
# process, e.g. a simulator's or Python's lz4 module).
target_compile_definitions(trl_lz4 PUBLIC "LZ4LIB_VISIBILITY=")

set_target_properties(trl_zlib trl_lz4 PROPERTIES
    POSITION_INDEPENDENT_CODE ON
    C_VISIBILITY_PRESET hidden)
if(MSVC)
    target_compile_definitions(trl_zlib PRIVATE _CRT_SECURE_NO_DEPRECATE _CRT_NONSTDC_NO_DEPRECATE)
endif()
