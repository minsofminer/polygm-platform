/**
 * A server component's read of a **public** API route: no session, no cookies, no refresh.
 *
 * Why this exists next to `server-read.ts` rather than inside it: `serverRead` goes through `proxy()`, and
 * `proxy()` is the session path — with no access cookie it treats the request as a session to be refreshed, calls
 * `refreshOnce`, finds no refresh token, and answers **401 UNAUTHENTICATED**. That is correct for every
 * account-shaped surface in the app and fatal for these three pages, whose entire audience is a stranger with no
 * cookies at all: every public page rendered a 200 with an error state in the body and `noindex` in the head.
 * (The D6 gate's c28 caught it by fetching the running pair with no cookie jar — an in-process test could not,
 * because the API itself answers these routes fine.)
 *
 * The envelope handling is deliberately identical to `serverRead`: the same `stampFrom`/`freshnessOf`, the same
 * `ServerRead<T>` shape, so a page cannot end up disagreeing with the client about whether a number is fresh. The
 * one difference is what must be different — this path never reads, writes or rotates a cookie, so a public page
 * cannot log anybody out, and it carries `cache: "no-store"` because the cache headers that matter here are the
 * API's own (`Cache-Control`, `ETag`, `X-RateLimit-*`), and Next's fetch cache would answer from a copy that has
 * forgotten them.
 */
import "server-only";

import { freshnessOf, stampFrom, type Freshness } from "@/api/envelope";
// The declaration of which paths are public lives on the route ledger, and this read is one of the two
// transports that honour it: the other is the client-side proxy (`src/auth/server.ts`), which had the same bug
// for the Mini App's reads. Sharing the predicate is what stops the two from drifting into two truths.
import { isAnonymousRead } from "@/auth/anonymous";
import type { ServerRead } from "@/api/server-read";

const API_ORIGIN = process.env.PGM_API_ORIGIN ?? "http://127.0.0.1:8000";

export async function publicRead<T>(method: "GET", path: string): Promise<ServerRead<T>> {
  // A public page reading a path the ledger does not declare anonymous would render a 401 as this file's "no data"
  // state — the exact failure it was written to end, one layer down. It throws instead, where the developer sees it,
  // because the alternative is a public page that looks fine to whoever wrote it and empty to a crawler.
  if (!isAnonymousRead(method, path)) {
    throw new Error(`publicRead(${method} ${path}) is not declared anonymous on the route ledger`);
  }
  let status = 0;
  let text = "";
  try {
    const response = await fetch(API_ORIGIN + path, {
      method,
      headers: { accept: "application/json" },
      cache: "no-store",
    });
    status = response.status;
    text = await response.text();
  } catch (err) {
    return {
      ok: false,
      status: 0,
      code: "NETWORK",
      message: `this page could not reach the API: ${(err as Error).message}`,
      stampLabel: "no data",
      freshness: "unknown",
      ageMs: null,
    };
  }
  const parsed = safeJson(text);
  const now = Date.now();
  const stamp = stampFrom((parsed ?? {}) as Record<string, unknown>);
  const freshness: Freshness = status >= 400 ? "unknown" : freshnessOf(stamp, now);
  const ageMs = stamp ? Math.max(0, now - stamp.asOf) : null;
  if (status >= 400 || !parsed) {
    const error = (parsed?.error ?? {}) as Record<string, unknown>;
    return {
      ok: false,
      status,
      code: String(error.code ?? "INTERNAL"),
      message: String(error.message ?? `the API answered ${status}`),
      stampLabel: "no data",
      freshness: "unknown",
      ageMs: null,
    };
  }
  const asOf = typeof parsed.asOf === "number" ? parsed.asOf : null;
  const staleAfter = typeof parsed.staleAfter === "number" ? parsed.staleAfter : null;
  const stampLabel =
    asOf === null
      ? "this page carries no freshness stamp, so treat every number on it as last-known"
      : `cached upstream; the API considered it fresh until ${new Date(staleAfter ?? asOf).toISOString()}`;
  return { ok: true, data: parsed as T, stampLabel, freshness, ageMs };
}

function safeJson(text: string): Record<string, unknown> | null {
  try {
    return JSON.parse(text) as Record<string, unknown>;
  } catch {
    return null;
  }
}
