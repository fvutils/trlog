import { readFileSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';
import { describe, expect, it } from 'vitest';
import { openTrace } from '../src/index.js';

// Reference results from the pure-Python reader (npm run gen:expected).
const here = dirname(fileURLToPath(import.meta.url));
const FIXTURES = join(here, '..', '..', 'tests', 'golden', 'fixtures');

interface Expected {
  timescaleExp: number;
  signals: { path: string; id: number; encoding: number; width: number }[];
  changes: Record<string, { times: string[]; values: (string | number)[] }>;
  txns: { id: string; type: number; start: string }[];
}
const expected: Record<string, Expected> = JSON.parse(
  readFileSync(join(here, 'fixtures', 'expected.json'), 'utf8'),
);

// libtrl skips these silently today, so the WASM build returns nothing for them:
// column-layout transaction blocks (query.c cursor reads only row/delta blocks)
// and the core.bytesplit structural codec. it.fails flips once libtrl reads them.
const KNOWN_GAPS = new Set(['txn_column.trl', 'bytesplit_real.trl']);

const load = (f: string) => openTrace(readFileSync(join(FIXTURES, f)));

describe.each(Object.keys(expected))('%s', (file) => {
  const exp = expected[file]!;

  (KNOWN_GAPS.has(file) ? it.fails : it)('matches the Python reader', async () => {
    const t = await load(file);
    try {
      expect(t.timescaleExp).toBe(exp.timescaleExp);
      expect(t.signals().map((s) => ({ path: s.path, id: s.id, encoding: s.encoding, width: s.width })))
        .toEqual(exp.signals);
      for (const s of exp.signals) expect(t.signal(s.path)?.id).toBe(s.id);

      for (const [id, c] of Object.entries(exp.changes)) {
        const got = t.valueChanges(Number(id));
        expect(Array.from(got.times, String)).toEqual(c.times);
        const vals = Array.from(got.values as ArrayLike<unknown>, (v) => (typeof v === 'bigint' ? String(v) : v));
        expect(vals).toEqual(c.values);
      }

      const txns = [...t.transactions()]
        .map((x) => ({ id: String(x.id), type: x.type, start: String(x.start) }))
        .sort((a, b) => (BigInt(a.id) < BigInt(b.id) ? -1 : BigInt(a.id) > BigInt(b.id) ? 1 : 0));
      expect(txns).toEqual(exp.txns);
    } finally {
      t.close();
    }
  });
});

describe('Trace', () => {
  it('windows value changes to [t0, t1)', async () => {
    const t = await load('vc_2state_32bit.trl');
    const all = t.valueChanges(1);
    const t0 = all.times[2]!;
    const t1 = all.times[5]!;
    const w = t.valueChanges(1, t0, t1);
    expect(Array.from(w.times)).toEqual(Array.from(all.times.slice(2, 5)));
    t.close();
  });

  it('rejects bytes that are not a trace', async () => {
    await expect(openTrace(new Uint8Array([1, 2, 3, 4, 5, 6, 7, 8]))).rejects.toThrow();
  });

  it('can be closed twice and not used after close', async () => {
    const t = await load('hierarchy.trl');
    t.close();
    t.close();
    expect(() => t.valueChanges(1)).toThrow(/closed/);
    expect(() => [...t.transactions()]).toThrow(/closed/);
  });

  it('builds the scope tree', async () => {
    const t = await load('hierarchy.trl');
    const [top] = t.scopes();
    expect(top?.name).toBe('top');
    expect(top?.scopes.map((s) => s.path)).toContain('top.sub');
    t.close();
  });
});
