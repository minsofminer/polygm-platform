/**
 * The client data layer's cache core: the Map, the in-flight de-duplication, and the readers over it.
 *
 * Moved out of `data.ts` on 2026-09-29, which is a `"use client"` module. Everything exported from a client
 * module becomes a *client reference* for a server importer, so `invalidate`/`peek`/`resetCache` — three plain
 * functions — were callable from a client component and fatal from a server one. They are pure state operations
 * with no React in them at all, which is exactly the test for which side of the boundary they belong on; the
 * hooks (`useResource`, `useAction`) stay in `data.ts`, because a hook is a client thing by definition.
 *
 * `src/client-boundary.test.ts` enforces the rule; the `/terminal` 500 of the same date is why it exists.
 */
export type QueryKey = readonly unknown[];
export type Fetcher<T> = () => Promise<T>;

export interface Entry<T = unknown> {
  readonly key: QueryKey;
  readonly data: T | undefined;
  readonly error: Error | undefined;
  readonly at: number; // when `data` was written (ms); 0 means "never / stale"
  ttl: number;
  fn: Fetcher<unknown> | null;
  inflight: Promise<void> | null;
  readonly subs: Set<() => void>;
}

export const CACHE = new Map<string, Entry>();
export const EMPTY: Entry = { key: [], data: undefined, error: undefined, at: 0, ttl: 30_000, fn: null, inflight: null, subs: new Set() };

export const DEFAULT_STALE_MS = 30_000;
export const k = (key: QueryKey) => JSON.stringify(key);

export function entryFor<T>(key: QueryKey, ttl = DEFAULT_STALE_MS): Entry<T> {
  const id = k(key);
  let e = CACHE.get(id) as Entry<T> | undefined;
  if (!e) {
    e = { key, data: undefined, error: undefined, at: 0, ttl, fn: null, inflight: null, subs: new Set() };
    CACHE.set(id, e as Entry);
  }
  return e;
}

/** Copy-on-write: `useSyncExternalStore` compares snapshots by identity, so a mutation that must re-render is a
 *  new entry object. The subscriber set is shared, because the subscribers are the same listeners. */
export function replace<T>(e: Entry<T>, patch: Partial<Entry<T>>): Entry<T> {
  const next = { ...e, ...patch, subs: e.subs } as Entry<T>;
  CACHE.set(k(e.key), next as Entry);
  for (const s of e.subs) s();
  return next;
}

export function isStale(e: Entry, now = Date.now()): boolean {
  return e.data === undefined || e.at === 0 || now - e.at >= e.ttl;
}

/** One request per key at a time; everyone asking for the same key joins the same promise. */
export function ensure<T>(key: QueryKey, fn: Fetcher<T>, force = false): Promise<void> {
  const e = entryFor<T>(key);
  e.fn = fn as Fetcher<unknown>;
  if (e.inflight) return e.inflight;
  if (!force && !isStale(e)) return Promise.resolve();
  const p = fn().then(
    (data) => {
      replace(entryFor<T>(key), { data, error: undefined, at: Date.now(), inflight: null });
    },
    (err: unknown) => {
      replace(entryFor<T>(key), { error: err instanceof Error ? err : new Error(String(err)), at: 0, inflight: null });
    },
  );
  entryFor<T>(key).inflight = p;
  return p;
}

/** Mark every entry whose key starts with `prefix` stale, and re-fetch the mounted ones now. */
export function invalidate(prefix: QueryKey): void {
  for (const e of [...CACHE.values()]) {
    if (prefix.length > e.key.length) continue;
    if (!prefix.every((part, i) => Object.is(part, e.key[i]))) continue;
    const fresh = e.at === 0 ? e : replace(e, { at: 0 });
    if (fresh.fn && fresh.subs.size > 0) void ensure(fresh.key, fresh.fn, true);
  }
}

/** Read a cached value without subscribing — for tests and for callers that already have the data. */
export function peek<T>(key: QueryKey): T | undefined {
  return CACHE.get(k(key))?.data as T | undefined;
}

/** Drop everything. Used by tests; nothing in the product calls it (a stale read is not a bug here). */
export function resetCache(): void {
  CACHE.clear();
}

