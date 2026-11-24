/**
 * Build libtrl to WebAssembly into src/wasm (wasm/CMakeLists.txt), using the
 * Emscripten toolchain set up by scripts/wasm-env.sh (ivpm dep-set ts-build).
 */
import { spawnSync } from 'node:child_process';
import { dirname, join, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

const tsDir = resolve(dirname(fileURLToPath(import.meta.url)), '..');
const rootDir = resolve(tsDir, '..');
const buildDir = process.env.TRLOG_WASM_BUILD_DIR ?? join(rootDir, 'build-wasm');
const script = [
  `. "${join(rootDir, 'scripts', 'wasm-env.sh')}"`,
  `emcmake cmake -S "${join(rootDir, 'wasm')}" -B "${buildDir}" -DCMAKE_BUILD_TYPE=Release -DTRLOG_WASM_OUT_DIR="${join(tsDir, 'src', 'wasm')}"`,
  `cmake --build "${buildDir}" -j8`,
].join(' && ');
const r = spawnSync('bash', ['-c', script], { stdio: 'inherit' });
process.exit(r.status ?? 1);
