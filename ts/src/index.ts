/**
 * @fvutils/trlog -- read trlog traces (signals and transactions).
 *
 *   import { openTrace } from '@fvutils/trlog';
 *   using t = await openTrace(bytes);
 *   const clk = t.signal('top.clk');
 *   const { times, values } = t.valueChanges(clk!);
 *
 * Times are bigint trace ticks (unit 10^timescaleExp s).
 */
import { loadModule, type HierEvent, type TraceHandle } from './wasm/loader.js';

/** Signal value encodings (trl_signal_enc_t). */
export enum Encoding {
  TwoState = 0,
  FourState = 1,
  NineState = 2,
  Real = 3,
  String = 4,
}

export interface Signal {
  id: number;
  name: string;
  /** Dotted path from the hierarchy root. */
  path: string;
  encoding: Encoding;
  width: number;
  dir: number;
  /** Var id this signal aliases; 0 when none. */
  aliasOf: number;
}

export interface Stream {
  instId: number;
  name: string;
  path: string;
}

export interface Scope {
  name: string;
  path: string;
  type: number;
  hierId: number;
  scopes: Scope[];
  signals: Signal[];
  streams: Stream[];
}

export interface Schema {
  id: number;
  name: string;
  fields: { name: string; type: number }[];
}

export type AttrValue = bigint | number | string | Uint8Array;

export interface Txn {
  id: bigint;
  /** Schema id; see Trace.schema(). */
  type: number;
  stream: number;
  start: bigint;
  end: bigint;
  parent: bigint;
  /** Attribute values by schema field name. */
  attrs: Record<string, AttrValue>;
}

/**
 * Value changes of one signal, sorted by time. `values` is a BigUint64Array
 * for 2-state signals up to 64 bits, a Float64Array for reals, strings for
 * string signals, and the raw stored bytes per change otherwise.
 */
export interface Changes {
  times: BigUint64Array;
  values: BigUint64Array | Float64Array | string[] | Uint8Array[];
}

const T_MAX = 2n ** 64n - 1n;

// Symbol.dispose exists from Node 20 on; give older runtimes one.
const disposeSym: typeof Symbol.dispose =
  (Symbol as { dispose?: typeof Symbol.dispose }).dispose ??
  ((Symbol as { dispose?: symbol }).dispose = Symbol('Symbol.dispose') as typeof Symbol.dispose);

/** Open a trace from its bytes. The bytes are copied into the WASM heap. */
export async function openTrace(bytes: Uint8Array | ArrayBuffer): Promise<Trace> {
  const M = await loadModule();
  const u8 = bytes instanceof Uint8Array ? bytes : new Uint8Array(bytes);
  let h: TraceHandle;
  try {
    h = new M.Trace(u8);
  } catch (e) {
    throw new Error('not a valid trlog trace');
  }
  return new Trace(h);
}

export class Trace {
  #h: TraceHandle | null;
  #scopes: Scope[] | null = null;
  #signals: Signal[] = [];
  #byPath = new Map<string, Signal>();
  #schemas = new Map<number, Schema | undefined>();
  readonly timescaleExp: number;

  /** @internal use openTrace() */
  constructor(h: TraceHandle) {
    this.#h = h;
    this.timescaleExp = h.timescaleExp();
  }

  /** Top-level scopes of every hierarchy, in declaration order. */
  scopes(): Scope[] {
    return this.#tree();
  }

  /** Every signal, depth-first in declaration order. */
  signals(): Signal[] {
    this.#tree();
    return this.#signals;
  }

  /** The signal at a dotted path, e.g. "top.u0.clk". */
  signal(path: string): Signal | undefined {
    this.#tree();
    return this.#byPath.get(path);
  }

  /** Changes of a signal with t0 <= time < t1. */
  valueChanges(sig: Signal | number, t0: bigint = 0n, t1: bigint = T_MAX): Changes {
    const s = typeof sig === 'number' ? this.signals().find((x) => x.id === sig) : sig;
    const id = typeof sig === 'number' ? sig : sig.id;
    const raw = this.#handle().valueChanges(id, t0, t1);
    const n = raw.times.length;
    const slice = (i: number) => raw.bytes.subarray(raw.offsets[i]!, raw.offsets[i + 1]!);
    const enc = s?.encoding;
    if (enc === Encoding.TwoState && (s?.width ?? 0) <= 64) {
      const v = new BigUint64Array(n);
      for (let i = 0; i < n; i++) {
        let x = 0n;
        for (const b of slice(i)) x = (x << 8n) | BigInt(b);
        v[i] = x;
      }
      return { times: raw.times, values: v };
    }
    if (enc === Encoding.Real) {
      const v = new Float64Array(n);
      for (let i = 0; i < n; i++) {
        const b = slice(i);
        v[i] = new DataView(b.buffer, b.byteOffset, b.byteLength).getFloat64(0, true);
      }
      return { times: raw.times, values: v };
    }
    if (enc === Encoding.String) {
      const d = new TextDecoder();
      return { times: raw.times, values: Array.from({ length: n }, (_, i) => d.decode(slice(i))) };
    }
    return { times: raw.times, values: Array.from({ length: n }, (_, i) => slice(i).slice()) };
  }

  /** Transaction streams. */
  streams(): Stream[] {
    return this.#handle()
      .streams()
      .map((s) => ({ instId: s.instId, path: s.path, name: s.path.slice(s.path.lastIndexOf('.') + 1) }));
  }

  /** The transaction schema with this id. */
  schema(id: number): Schema | undefined {
    if (!this.#schemas.has(id)) {
      const s = this.#handle().schema(id);
      this.#schemas.set(id, s ? { id, name: s.name, fields: s.fields } : undefined);
    }
    return this.#schemas.get(id);
  }

  /** Transactions overlapping [t0, t1), of one stream (by path) or all. */
  *transactions(opts: { t0?: bigint; t1?: bigint; stream?: string } = {}): IterableIterator<Txn> {
    const raw = this.#handle().transactions(opts.t0 ?? 0n, opts.t1 ?? T_MAX, opts.stream ?? '');
    for (const r of raw) {
      const fields = this.schema(r.typeId)?.fields;
      const attrs: Record<string, AttrValue> = {};
      for (const a of r.attrs) attrs[fields?.[a.field]?.name ?? String(a.field)] = a.value;
      yield { id: r.id, type: r.typeId, stream: r.streamInstId, start: r.start, end: r.end, parent: r.parent, attrs };
    }
  }

  /** Free the WASM memory held by this trace. Safe to call twice. */
  close(): void {
    if (this.#h) {
      this.#h.close();
      this.#h.delete();
      this.#h = null;
    }
  }

  [disposeSym](): void {
    this.close();
  }

  #handle(): TraceHandle {
    if (!this.#h) throw new Error('trace is closed');
    return this.#h;
  }

  #tree(): Scope[] {
    if (this.#scopes) return this.#scopes;
    const roots: Scope[] = [];
    const stack: Scope[] = [];
    const add = (path: string, name: string) => (path ? `${path}.${name}` : name);
    for (const e of this.#handle().hierarchy() as HierEvent[]) {
      const top = stack[stack.length - 1];
      switch (e.kind) {
        case 0:
          stack.length = 0;
          break;
        case 1: {
          const sc: Scope = {
            name: e.name, path: add(top?.path ?? '', e.name), type: e.scopeType, hierId: e.hierId,
            scopes: [], signals: [], streams: [],
          };
          (top ? top.scopes : roots).push(sc);
          stack.push(sc);
          break;
        }
        case 2:
          stack.pop();
          break;
        case 3: {
          const s: Signal = {
            id: e.id, name: e.name, path: add(top?.path ?? '', e.name), encoding: e.encoding as Encoding,
            width: e.width, dir: e.dir, aliasOf: e.aliasOf,
          };
          top?.signals.push(s);
          this.#signals.push(s);
          if (!this.#byPath.has(s.path)) this.#byPath.set(s.path, s);
          break;
        }
        case 4:
          top?.streams.push({ instId: e.id, name: e.name, path: add(top?.path ?? '', e.name) });
          break;
      }
    }
    this.#scopes = roots;
    return roots;
  }
}
