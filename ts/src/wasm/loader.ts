/**
 * Loads the Embind module (wasm/bindings.cpp) once per process.
 *
 * The handle types are declared by hand: emcc emits no .d.ts. They must stay
 * in step with EMSCRIPTEN_BINDINGS(trlog) in wasm/bindings.cpp.
 */

/** One hierarchy walk event (trl_hier_event_t in c/include/trl/trl.h). */
export interface HierEvent {
  kind: number; // 0 hierarchy, 1 scope, 2 upscope, 3 var, 4 stream
  hierId: number;
  name: string;
  scopeType: number;
  id: number;
  typeId: number;
  encoding: number;
  width: number;
  dir: number;
  aliasOf: number;
}

export interface RawChanges {
  times: BigUint64Array;
  offsets: Uint32Array;
  bytes: Uint8Array;
}

export interface RawAttr {
  field: number;
  type: number;
  value: bigint | number | string | Uint8Array;
  width?: number;
}

export interface RawTxn {
  id: bigint;
  streamInstId: number;
  typeId: number;
  start: bigint;
  end: bigint;
  parent: bigint;
  attrs: RawAttr[];
}

export interface TraceHandle {
  close(): void;
  delete(): void;
  timescaleExp(): number;
  hierarchy(): HierEvent[];
  valueChanges(varId: number, t0: bigint, t1: bigint): RawChanges;
  streams(): { path: string; instId: number }[];
  transactions(t0: bigint, t1: bigint, stream: string): RawTxn[];
  schema(id: number): { name: string; fields: { name: string; type: number }[] } | null;
}

export interface TrlogModule {
  Trace: new (bytes: Uint8Array) => TraceHandle;
}

let modulePromise: Promise<TrlogModule> | null = null;

/** The WASM module, instantiated on first use. */
export function loadModule(): Promise<TrlogModule> {
  if (!modulePromise) {
    modulePromise = (async () => {
      // @ts-ignore -- emcc glue has no type declaration.
      const glue = await import('./trlog.js');
      const create = glue.default as () => Promise<TrlogModule>;
      return create();
    })();
    modulePromise.catch(() => {
      modulePromise = null;
    });
  }
  return modulePromise;
}
