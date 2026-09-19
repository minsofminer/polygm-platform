/**
 * The only fetch in the app. Four obligations, each of which is a control and has a test:
 *
 *  1. Timeout. A hung request that never resolves is how a trade ticket shows a spinner for ninety seconds.
 *     `AbortSignal.timeout` on reads; on the order path the timeout does NOT imply failure — the intent is
 *     read back by id, because a retry of a POST without the idempotency key would be a second order.
 *  2. Idempotency-key injection on every mutation, generated once per *intent*, not per attempt, so the
 *     retries inside this client and a user's impatient double-click collapse into one order (P06's
 *     IDEM_* codes are what makes that true).
 *  3. Retry only what is safe: GETs and mutations whose key we already sent, only when the envelope says
 *     `retryable`, with a capped backoff, and never after a 4xx that carries a refusal reason.
 *  4. Typed errors mapped to the envelope; no caller ever sees a `Response`.
 *
 * Session handling lives on the server (app/api/session): this file sends `credentials: "same-origin"` and
 * has no token parameter, because the token must not exist in this document.
 */
import { env } from "@/lib/env";
import { parseError, stampFrom, type ApiError, type Result } from "./envelope";
import { ROUTES, type RouteKey } from "./routes";
// The shapes come from the contract, not from a hand-copied interface (see src/api/types.ts).
import type { SuccessBody } from "./types";
export type { SuccessBody };

export type RequestOptions = {
  key: RouteKey;
  /** Path parameters for the templated routes (`{market_id}`). */
  params?: Record<string, string | number>;
  query?: Record<string, string | number | boolean | undefined>;
  body?: unknown;
  signal?: AbortSignal;
  /** Only for the order path: the caller owns the key so a user retry reuses it. */
  idempotencyKey?: string;
  maxAttempts?: number;
  timeoutMs?: number;
};

export const MUTATING = new Set(["POST", "PUT", "PATCH", "DELETE"]);
const RETRYABLE_STATUS = new Set([502, 503, 504]);
const BACKOFF_MS = [120, 400, 1200];

/** A key is opaque to the server and must be stable per intent. 16 hex chars is plenty; the P06 store
 *  hashes it anyway. */
export function newIdempotencyKey(scope: string): string {
  const bytes = new Uint8Array(8);
  globalThis.crypto.getRandomValues(bytes);
  const rand = Array.from(bytes, (b) => b.toString(16).padStart(2, "0")).join("");
  return `${scope}:${rand}`;
}

export function urlFor(decl: { method: string; path: string }, opts: RequestOptions): string {
  let path = decl.path;
  for (const [name, value] of Object.entries(opts.params ?? {})) {
    const token = "{" + name + "}";
    if (!path.includes(token)) throw new Error(`route ${opts.key} has no {${name}} segment`);
    path = path.split(token).join(encodeURIComponent(String(value)));
  }
  const unfilled = /\{([a-z_]+)\}/.exec(path);
  if (unfilled) throw new Error(`route ${opts.key} is missing its {${unfilled[1]}} value`);
  const qs = new URLSearchParams();
  for (const [k, v] of Object.entries(opts.query ?? {})) if (v !== undefined) qs.set(k, String(v));
  const suffix = qs.toString();
  return env.apiBase + path + (suffix ? `?${suffix}` : "");
}

function notBuilt(key: RouteKey): ApiError {
  const decl = ROUTES[key];
  return {
    code: "ROUTE_NOT_BUILT",
    message: `${decl.method} ${decl.path} is not served by the API yet (launch item ${decl.owner})`,
    retryable: false,
    requestId: "",
    status: 0,
  };
}

export async function request<T>(opts: RequestOptions): Promise<Result<T>> {
  const decl = ROUTES[opts.key];
  // The refusal happens here, not in the screen, so a route that gains its backend is the only way the UI
  // can start talking: flip `built` in the ledger and the screen begins to work unchanged.
  if (!decl.built) return { ok: false, error: notBuilt(opts.key) };
  const mutating = MUTATING.has(decl.method);
  const headers: Record<string, string> = { accept: "application/json" };
  if (mutating) {
    headers["content-type"] = "application/json";
    headers["idempotency-key"] = opts.idempotencyKey ?? newIdempotencyKey(opts.key);
  }
  const maxAttempts = opts.maxAttempts ?? (decl.method === "GET" ? BACKOFF_MS.length + 1 : BACKOFF_MS.length + 1);
  let last: ApiError | null = null;
  for (let attempt = 0; attempt < maxAttempts; attempt++) {
    if (opts.signal?.aborted) {
      return { ok: false, error: { code: "ABORTED", message: "cancelled", retryable: false, requestId: "", status: 0 } };
    }
    const init: RequestInit = {
      method: decl.method,
      headers,
      credentials: "same-origin",
      signal: AbortSignal.any([AbortSignal.timeout(opts.timeoutMs ?? 8_000), ...(opts.signal ? [opts.signal] : [])]),
    };
    if (mutating && opts.body !== undefined) init.body = JSON.stringify(opts.body);
    let response: Response;
    try {
      response = await fetch(urlFor(decl, opts), init);
    } catch (cause) {
      const timedOut = cause instanceof Error && /TimeoutError/.test(cause.name + cause.message);
      last = {
        code: timedOut ? "TIMEOUT" : "NETWORK",
        message: timedOut
          ? "the server did not answer in time — if this was a trade, check the order list before sending it again"
          : "no answer from the server; the request may or may not have landed",
        retryable: true,
        requestId: "",
        status: 0,
      };
      if (timedOut && mutating) return { ok: false, error: last };
      await sleep(BACKOFF_MS[Math.min(attempt, BACKOFF_MS.length - 1)] ?? 400);
      continue;
    }
    const text = await response.text();
    let body: unknown = null;
    if (text) {
      try {
        body = JSON.parse(text);
      } catch {
        body = null;
      }
    }
    if (response.ok && body && typeof body === "object") {
      const data = body as Record<string, unknown>;
      const stamp = stampFrom(data);
      const { asOf: _a, staleAfter: _s, serverAsOf: _sv, cache: _c, ...rest } = data;
      void _a; void _s; void _sv; void _c;
      // Only reads are required to carry the stamp. A mutation ack (`{"ended": true}`) has no freshness to
      // report, and inventing one is how a UI ends up trusting a timestamp nobody produced.
      if (decl.method === "GET" && !stamp) {
        return {
          ok: false,
          error: {
            code: "UNSTAMPED_READ",
            message: `${decl.path} answered a read without asOf/staleAfter`,
            retryable: false,
            requestId: response.headers.get("x-request-id") ?? "",
            status: response.status,
          },
        };
      }
      return { ok: true, data: rest as T, stamp: stamp ?? null };
    }
    const error = parseError(body, response.status, response.headers);
    last = error;
    const worthRetrying = (error.retryable === true || RETRYABLE_STATUS.has(response.status)) && attempt + 1 < maxAttempts;
    if (!worthRetrying) return { ok: false, error };
    await sleep(BACKOFF_MS[Math.min(attempt, BACKOFF_MS.length - 1)] ?? 400);
  }
  return { ok: false, error: last ?? { code: "INTERNAL", message: "gave up", retryable: false, requestId: "", status: 0 } };
}

function sleep(ms: number): Promise<void> {
  return new Promise((resolve) => setTimeout(resolve, ms));
}
