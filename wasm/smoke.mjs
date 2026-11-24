// smoke.mjs -- load the WASM module in node and read the golden fixtures.
//
//   node wasm/smoke.mjs [<dir with trlog.js>] [--json]
//
// Default module dir: ts/src/wasm. Opens every tests/golden/fixtures/*.trl
// from bytes and prints value-change / stream / transaction counts per file
// (--json: the full summary, for comparing with the native reader).
import { readFileSync, readdirSync } from 'node:fs';
import { join, resolve, dirname } from 'node:path';
import { fileURLToPath, pathToFileURL } from 'node:url';

const root = resolve(dirname(fileURLToPath(import.meta.url)), '..');
const args = process.argv.slice(2);
const json = args.includes('--json');
const modDir = resolve(args.find((a) => !a.startsWith('--')) ?? join(root, 'ts/src/wasm'));
const { default: createTrlogModule } = await import(pathToFileURL(join(modDir, 'trlog.js')).href);
const M = await createTrlogModule();

const fixtures = join(root, 'tests/golden/fixtures');
const ALL = 2n ** 64n - 1n;
const summary = {};
for (const f of readdirSync(fixtures).filter((n) => n.endsWith('.trl')).sort()) {
  const t = new M.Trace(readFileSync(join(fixtures, f)));
  const varIds = new Set(t.hierarchy().filter((e) => e.kind === 3).map((e) => e.id));
  let vc = 0;
  for (const id of varIds) vc += t.valueChanges(id, 0n, ALL).times.length;
  const streams = t.streams();
  const txns = t.transactions(0n, ALL, '');
  summary[f] = {
    vc,
    streams: streams.map((s) => s.path),
    txn: txns.length,
    attrs: txns.reduce((n, x) => n + x.attrs.length, 0),
  };
  t.close();
  t.delete();
}

let bad = 0;
for (const junk of [new Uint8Array(0), new Uint8Array([1, 2, 3, 4]), new TextEncoder().encode('not a trace at all, really')]) {
  try {
    new M.Trace(junk);
    console.log(`FAIL: ${junk.length}-byte junk buffer opened`);
    bad++;
  } catch (e) {
    // expected
  }
}

if (json) {
  console.log(JSON.stringify(summary, null, 1));
} else {
  for (const [f, s] of Object.entries(summary))
    console.log(`${f}: ${s.vc} value changes, ${s.streams.length} streams, ${s.txn} txns, ${s.attrs} attrs`);
}
if (bad) process.exit(1);
console.log(`PASS: ${Object.keys(summary).length} fixtures read, junk rejected`);
