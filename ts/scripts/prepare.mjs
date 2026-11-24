/**
 * npm's `prepare` hook: a source checkout (ivpm dependency, or `npm install`
 * in ts/) builds the WASM module and the TypeScript here. npm does not run it
 * for a registry tarball, so consumers of the published @fvutils/trlog need
 * no Emscripten toolchain. Skipped when SKIP_TRLOG_PREPARE is set (CI jobs
 * that built the artifact in an earlier step).
 */
import { spawnSync } from 'node:child_process';
import { existsSync } from 'node:fs';
import { dirname, join, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

const tsDir = resolve(dirname(fileURLToPath(import.meta.url)), '..');
const rootDir = resolve(tsDir, '..');

if (process.env.SKIP_TRLOG_PREPARE) {
  console.log('prepare: skipped (SKIP_TRLOG_PREPARE is set)');
  process.exit(0);
}
if (!existsSync(join(tsDir, 'src', 'index.ts'))) {
  console.log('prepare: no sources present -- nothing to build');
  process.exit(0);
}
const emsdk = join(rootDir, 'packages', 'emsdk');
if (!existsSync(join(emsdk, 'emscripten', 'emcc')) && !existsSync(join(emsdk, 'install', 'emscripten', 'emcc'))) {
  console.error(
    `prepare: no Emscripten toolchain at ${emsdk}\n` +
      'Use the published @fvutils/trlog package (prebuilt WebAssembly), or fetch\n' +
      'the toolchain with `ivpm update -d ts-build` to build from source.'
  );
  process.exit(1);
}
for (const argv of [['run', 'gen:wasm'], ['run', 'build']]) {
  console.log(`prepare: npm ${argv.join(' ')}`);
  const r = spawnSync('npm', argv, { cwd: tsDir, stdio: 'inherit' });
  if (r.status !== 0) process.exit(r.status ?? 1);
}
