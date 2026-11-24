/**
 * Copy the WASM build products (src/wasm/trlog.{js,wasm}) into dist/ after
 * tsc, which only compiles .ts files.
 */
import { copyFileSync, existsSync, mkdirSync } from 'node:fs';
import { dirname, join, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

const tsDir = resolve(dirname(fileURLToPath(import.meta.url)), '..');
const from = join(tsDir, 'src', 'wasm');
const to = join(tsDir, 'dist', 'wasm');
const artifacts = ['trlog.js', 'trlog.wasm'];

const missing = artifacts.filter((f) => !existsSync(join(from, f)));
if (missing.length) {
  console.error(`copy-wasm: missing ${missing.join(', ')} in ${from}; run \`npm run gen:wasm\` first`);
  process.exit(1);
}
mkdirSync(to, { recursive: true });
for (const f of artifacts) copyFileSync(join(from, f), join(to, f));
console.log(`copy-wasm: ${artifacts.join(', ')} -> dist/wasm`);
