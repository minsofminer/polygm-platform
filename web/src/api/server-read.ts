/**
 * A server component's read path. It exists so that RSC code never reaches for `fetch(API_ORIGIN)` on its
 * own: a server-rendered page that invents its own HTTP call is a page whose error handling, timeout and
 * stamping are different from the client's, and the two then disagree on screen about whether a number is
 * fresh. Same proxy, same envelope, one set of rules.
 */
import "server-only";

import { cache } from "react";

import { freshnessOf, stampFrom, type Freshness } from "@/api/envelope";
import { proxy } from "@/auth/server";

/**
 * `freshness` and `ageMs` are part of the read, not an extra the page may ignore: `Number` only draws a
 * `StaleIndicator` when it is given a freshness, and a server-rendered price with no freshness is a price
 * that cannot say it is old. The gate (c10) fails the build on a `kind="price"` without them, so the fields
 * are here so the page can pass them at all.
 */
export type ServerRead<T> =
  | { ok: true; data: T; stampLabel: string; freshness: Freshness; ageMs: number | null }
  | { ok: false; code: string; message: string; status: number; stampLabel: string; freshness: Freshness;
      ageMs: number | null };

/**
 * Same per-request memo as `publicRead`, for the same reason: a route whose `generateMetadata` and whose body
 * both read the same path made that HTTP call twice. `proxy()` underneath is the session path, so the memo is
 * strictly per-request and per-URL — two requests never share an entry, and a read is never answered from
 * another account's render. See `public-read.ts` for why this is invisible to vitest and what stands in for it.
 */
const cachedServerRead = cache(
  async (method: "GET", path: string): Promise<ServerRead<unknown>> => serverReadImpl(method, path),
);

export function serverRead<T>(method: "GET", path: string): Promise<ServerRead<T>> {
  return cachedServerRead(method, path) as Promise<ServerRead<T>>;
}

async function serverReadImpl<T>(method: "GET", path: string): Promise<ServerRead<T>> {
  const out = await proxy(path, { method });
  const parsed = safeJson(out.body);
  const now = Date.now();
  const stamp = stampFrom((parsed ?? {}) as Record<string, unknown>);
  const freshness: Freshness = out.status >= 400 ? "unknown" : freshnessOf(stamp, now);
  const ageMs = stamp ? Math.max(0, now - stamp.asOf) : null;
  if (out.status >= 400) {
    const error = (parsed?.error ?? {}) as Record<string, unknown>;
    return {
      ok: false,
      // `status` as well as the code: the caller has to tell "no such market" (404 -> notFound()) from "the
      // API is down", and a code string is not the right shape for that decision.
      status: out.status,
      code: String(error.code ?? "INTERNAL"),

      message: String(error.message ?? `the API answered ${out.status}`),
      stampLabel: "no data",
      freshness: "unknown",
      ageMs: null,
    };
  }
  const asOf = typeof parsed?.asOf === "number" ? parsed.asOf : null;
  const staleAfter = typeof parsed?.staleAfter === "number" ? parsed.staleAfter : null;
  const stampLabel =
    asOf === null
      ? "this list carries no freshness stamp, so treat every number on it as last-known"
      : `cached upstream; the API considered it fresh until ${new Date(staleAfter ?? asOf).toISOString()}`;
  return { ok: true, data: (parsed ?? {}) as T, stampLabel, freshness, ageMs };
}

function safeJson(text: string): Record<string, unknown> | null {
  try {
    return JSON.parse(text) as Record<string, unknown>;
  } catch {
    return null;
  }
}
