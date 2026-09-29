"use client";
/**
 * The client data layer: a Map with a TTL, a de-duplicated in-flight promise, and a subscription per key.
 *
 * Why this exists instead of `@tanstack/react-query`. Query's 17 KB gzipped was being configured to do as little
 * as possible: `refetchOnWindowFocus: false` (the feed is the authority on anything live, and a query that
 * re-fetched on focus would show a second, differently-aged copy of the same price), `retry: false` (a refusal
 * here is a decision, not a flake — re-sending it every three seconds turns an honest gap into noise), and a
 * per-call-site `staleTime` (the stamp's own TTL). What was left is a cache keyed by name, one fetch per key at a
 * time, and a way to say "that one is stale now" — which is exactly this file. Measured: removing it took the
 * initial route from 200.4 KB to ~183 KB of first-party JS, which is the difference between inside the P08 budget
 * and outside it, and it is 17 KB on every route for four screens' worth of reads.
 *
 * The semantics are deliberately the *same* ones the call sites already relied on, and are asserted in
 * `data.test.ts`:
 *
 *  - a key is fresh for its `staleTime` (default 30 s, matching the old provider default); a mount with fresh
 *    data serves the cache and does not go to the network;
 *  - a stale or missing key fetches once, and concurrent mounts share that one request;
 *  - `refetch()` ignores freshness; `invalidate(prefix)` marks every matching key stale and re-runs the fetches
 *    for the ones that are mounted, which is what `invalidateQueries` was doing after a delete;
 *  - a failed fetch keeps the error on the entry and does *not* retry by itself.
 *
 * It is a cache of server *reads*. Nothing here decides anything: the server refuses what it refuses, and every
 * write goes through `request()` and its own gate.
 */
import { useCallback, useEffect, useRef, useState, useSyncExternalStore } from "react";
import {
  DEFAULT_STALE_MS, EMPTY, entryFor, ensure, isStale, k, replace,
  type Entry, type Fetcher, type QueryKey,
} from "./data-cache";


/**
 * A discriminated union rather than three independent fields, so `if (r.isError) r.error.message` is a type
 * error the compiler cannot let you write wrongly. (Query surfaced the same three fields; the compiler could not
 * tell that the error was non-null exactly when `isError` was true, which is why every call site carried a
 * `String(…message)` cast.)
 */
export type Resource<T> =
  | { data: T | undefined; error: Error; isPending: false; isError: true; refetch: () => void }
  | { data: T | undefined; error: undefined; isPending: boolean; isError: false; refetch: () => void };

export function useResource<T>(key: QueryKey, fn: Fetcher<T>, opts: { staleTime?: number } = {}): Resource<T> {
  const id = k(key);
  const ttl = opts.staleTime ?? DEFAULT_STALE_MS;
  const fnRef = useRef(fn);
  fnRef.current = fn;

  const subscribe = useCallback((cb: () => void) => {
    const e = entryFor(key, ttl);
    e.subs.add(cb);
    return () => {
      e.subs.delete(cb);
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps -- `id` is the key's identity; the array itself is new
  }, [id]);

  const snapshot = useSyncExternalStore<Entry<T>>(
    subscribe,
    () => entryFor<T>(key, ttl),
    () => entryFor<T>(key, ttl),
  );

  useEffect(() => {
    void ensure<T>(key, () => fnRef.current(), false);
    // eslint-disable-next-line react-hooks/exhaustive-deps -- keyed by `id`, not by the array literal
  }, [id, ttl]);

  const refetch = useCallback(() => {
    void ensure<T>(key, () => fnRef.current(), true);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [id]);

  if (snapshot.error !== undefined) {
    return { data: snapshot.data, error: snapshot.error, isPending: false, isError: true, refetch };
  }
  return {
    data: snapshot.data,
    error: undefined,
    isPending: snapshot.data === undefined,
    isError: false,
    refetch,
  };
}

export interface Action<TIn> {
  mutate: (input: TIn) => void;
  isPending: boolean;
  error: Error | undefined;
}

/** A write: pending state for the button, the error kept for the caller to show, no retry. */
export function useAction<TIn, TOut>(fn: (input: TIn) => Promise<TOut>): Action<TIn> {
  const [state, setState] = useState<{ pending: boolean; error: Error | undefined }>({ pending: false, error: undefined });
  const fnRef = useRef(fn);
  fnRef.current = fn;
  const mutate = useCallback((input: TIn) => {
    setState({ pending: true, error: undefined });
    void fnRef.current(input).then(
      () => setState({ pending: false, error: undefined }),
      (err: unknown) => setState({ pending: false, error: err instanceof Error ? err : new Error(String(err)) }),
    );
  }, []);
  return { mutate, isPending: state.pending, error: state.error };
}
