#ifndef TRL_API_H
#define TRL_API_H
/*
 * TRL_API marks the functions a trlog shared library exports.
 *
 * On Windows a DLL exports only what is marked __declspec(dllexport), and
 * callers should see __declspec(dllimport).  Each library defines its own
 * TRL_BUILDING_<lib> when compiling itself (set by CMake), so a header
 * included from another library or a program gets dllimport.
 *
 * Elsewhere the libraries are built with -fvisibility=hidden, so the same
 * markings decide the exported symbol set on Linux and macOS too.
 *
 * Define TRL_STATIC when linking a static libtrl on Windows.
 */
#if defined(_WIN32) || defined(__CYGWIN__)
#  define TRL_DLL_EXPORT __declspec(dllexport)
#  define TRL_DLL_IMPORT __declspec(dllimport)
#elif defined(__GNUC__) || defined(__clang__)
#  define TRL_DLL_EXPORT __attribute__((visibility("default")))
#  define TRL_DLL_IMPORT __attribute__((visibility("default")))
#else
#  define TRL_DLL_EXPORT
#  define TRL_DLL_IMPORT
#endif

#if defined(TRL_STATIC)
#  define TRL_API
#elif defined(TRL_BUILDING_TRL)
#  define TRL_API TRL_DLL_EXPORT
#else
#  define TRL_API TRL_DLL_IMPORT
#endif

#endif /* TRL_API_H */
