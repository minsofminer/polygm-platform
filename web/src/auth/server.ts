/**
 * Server-only session plumbing. This file may read `process.env` (it is never imported by a client
 * component: `scripts/assert-env.mjs` allows it precisely because `package.json` marks it and the file sits
 * beside no `"use client"`). The browser talks to these routes, and the bearer token exists only between this
 * server and the API — which is the whole reason a proxy exists instead of the page calling the API directly.
 *
 * The cookie contract:
 *   pgm_at  httpOnly  "<expiryEpochMs>:<access token>"   the proxy splits it, so the expiry check costs
 *                                                            nothing and no client script can read either half
 *   pgm_rt  httpOnly  the single-use refresh token, rotated on every refresh (P07's family rule)
 *
 * CSRF: SameSite=Lax already refuses a cross-site POST on the open web. Inside the Telegram webview the
 * cookies must be SameSite=None (a cross-site top-level context), which turns CSRF protection off by
 * construction — so the proxy refuses every mutating request whose `Origin` is absent or foreign. A
 * hand-rolled client without an Origin header gets refused; a browser always sends one for a POST.
 */
import "server-only";

import { cookies, headers } from "next/headers";
import { accessExpired, AT_COOKIE, joinAccess, RT_COOKIE, refreshOnce, splitAccess, type CookieJar, type RefreshOutcome } from "./refresh";
// The guard itself is a pure function in its own module (src/auth/server.csrf.ts) so it can be tested
// without a request, and so this file has exactly one copy of the rule.
import { csrfOk } from "./server.csrf";
// The anonymous-read predicate is a pure function in its own module for the same reason the guard is: it is
// arithmetic on a path and the route ledger, and it is worth a test that needs no request.
import { isAnonymousRead } from "./anonymous";

const API_ORIGIN = process.env.PGM_API_ORIGIN ?? "http://127.0.0.1:8000";
const ALLOWED_ORIGINS: string[] = (process.env.PGM_ALLOWED_ORIGINS ?? "").split(",").map((s) => s.trim()).filter(Boolean);

/**
 * Whatever the upstream said, plus a status the route can echo. Internal refusals are synthesised into the
 * same envelope, because the client's refusal renderer parses one shape and a second shape is a second bug:
 * a UI that cannot read the code cannot show the reason, and a reason-less refusal is what P07's whole
 * "refusals are text" rule is against.
 */
export type ProxyResult = { status: number; body: string; contentType: string };

export function envelope(code: string, message: string, opts: { retryable?: boolean; requestId?: string } = {}): string {
  return JSON.stringify({ error: { code, message, retryable: opts.retryable === true, requestId: opts.requestId ?? "" } });
}

export async function jar(): Promise<CookieJar> {
  const store = await cookies();
  return {
    get: (name) => store.get(name)?.value,
    set: (name, value, opts) => {
      store.set(name, value, {
        httpOnly: true,
        secure: opts.secure,
        sameSite: opts.sameSite,
        path: "/",
        maxAge: opts.maxAgeS,
      });
    },
    clear: (name) => {
      store.set(name, "", { httpOnly: true, secure: true, sameSite: "lax", path: "/", maxAge: 0 });
    },
  };
}

/* The access-cookie format (`"<expiry>:<token>"`) lives in ./refresh, beside the code that writes it: a
   writer and a reader in separate modules each kept their own green test while the pair was broken. */

async function callUpstream(path: string, init: { method: string; body?: unknown; token?: string | null; headers?: Record<string, string> }) {
  const h: Record<string, string> = { accept: "application/json", ...(init.headers ?? {}) };
  if (init.body !== undefined) h["content-type"] = "application/json";
  if (init.token) h.authorization = `Bearer ${init.token}`;
  const response = await fetch(API_ORIGIN + path, {
    method: init.method,
    headers: h,
    ...(init.body !== undefined ? { body: JSON.stringify(init.body) } : {}),
    cache: "no-store",
  });
  const text = await response.text();
  return { status: response.status, body: text, contentType: response.headers.get("content-type") ?? "application/json", retryAfter: response.headers.get("retry-after") };
}

/** The one entry point for both the proxy route and the RSC reads. */
export async function proxy(path: string, init: { method: string; body?: unknown; headers?: Record<string, string> }): Promise<ProxyResult> {
  const headerList = await headers();
  const guard = csrfOk({
    method: init.method,
    origin: headerList.get("origin"),
    secFetchSite: headerList.get("sec-fetch-site"),
    secFetchDest: headerList.get("sec-fetch-dest"),
    allowedOrigins: ALLOWED_ORIGINS,
  });
  if (!guard.allowed) {
    return { status: 403, body: envelope("CSRF_ORIGIN", "this request did not come from the page that owns the session"), contentType: "application/json" };
  }
  // A read the contract serves to the public must not need a session: an anonymous visitor with a market link gets
  // the market, not a sign-in wall (see the `anonymous` flag's comment in src/api/routes.ts). The CSRF guard above
  // still ran — being public does not mean being a mutation — and the hop still hides the upstream origin and any
  // bearer token, so this is a narrower call, not an unguarded one.
  if (isAnonymousRead(init.method, path)) {
    return callUpstream(path, {
      method: init.method,
      ...(init.body !== undefined ? { body: init.body } : {}),
      token: null,
      ...(init.headers ? { headers: init.headers } : {}),
    });
  }
  const store = await jar();
  const at = splitAccess(store.get(AT_COOKIE));
  let token = at.token;
  if (init.method !== "GET" || !token || accessExpired(at.expiresAt, Date.now())) {
    if (accessExpired(at.expiresAt, Date.now()) || !token) {
      const outcome: RefreshOutcome = await refreshOnce(
        {
          crossSite: guard.crossSite,
          now: () => Date.now(),
          callRefresh: async (refreshToken) => {
            const res = await callUpstream("/v1/auth/refresh", { method: "POST", body: { refreshToken } });
            const parsed = JSON.parse(res.body || "{}") as Record<string, unknown>;
            if (res.status !== 200) {
              const error = (parsed.error ?? {}) as Record<string, unknown>;
              return { ok: false as const, error: { code: String(error.code ?? "INTERNAL"), message: String(error.message ?? "refresh failed"), retryable: error.retryable === true, requestId: String(error.requestId ?? ""), status: res.status } };
            }
            if (typeof parsed.accessToken !== "string" || typeof parsed.refreshToken !== "string" || typeof parsed.expiresInMs !== "number") {
              return { ok: false as const, error: { code: "BAD_REFRESH_SHAPE", message: "the session service answered without a token pair", retryable: false, requestId: "", status: 502 } };
            }
            return { ok: true as const, accessToken: parsed.accessToken, refreshToken: parsed.refreshToken, expiresInMs: parsed.expiresInMs, userId: String((parsed.user as { id?: string })?.id ?? "") };
          },
        },
        store,
      );
      if (outcome.kind === "failed") {
        return { status: outcome.error.status || 401, body: envelope(outcome.error.code, outcome.error.message, { requestId: outcome.error.requestId }), contentType: "application/json" };
      }
      token = outcome.accessToken;
    }
  }
  const res = await callUpstream(path, { method: init.method, ...(init.body !== undefined ? { body: init.body } : {}), token, ...(init.headers ? { headers: init.headers } : {}) });
  if (res.retryAfter) {
    // Preserve the server's own back-off so the login lock's "try again in Ns" survives two hops.
    return { status: res.status, body: patchRetryAfter(res.body, res.retryAfter), contentType: res.contentType };
  }
  return { status: res.status, body: res.body, contentType: res.contentType };
}

function patchRetryAfter(body: string, retryAfter: string): string {
  try {
    const parsed = JSON.parse(body) as Record<string, unknown>;
    if (parsed.error && typeof parsed.error === "object") {
      parsed.error = { ...(parsed.error as Record<string, unknown>), retryAfterS: Number(retryAfter) || undefined };
      return JSON.stringify(parsed);
    }
    return body;
  } catch {
    return body;
  }
}

/** Sign-in writes the cookies here and nowhere else: `accessToken` never crosses into the page. */
export async function establish(args: { path: "/v1/auth/login" | "/v1/auth/telegram"; body: unknown }): Promise<
  { ok: true; userId: string; expiresInMs: number } | { ok: false; status: number; code: string; message: string; requestId: string }
> {
  const headerList = await headers();
  const guard = csrfOk({
    method: "POST",
    origin: headerList.get("origin"),
    secFetchSite: headerList.get("sec-fetch-site"),
    secFetchDest: headerList.get("sec-fetch-dest"),
    allowedOrigins: ALLOWED_ORIGINS,
  });
  if (!guard.allowed) return { ok: false, status: 403, code: "CSRF_ORIGIN", message: "the request did not come from this page", requestId: "" };
  const res = await callUpstream(args.path, { method: "POST", body: args.body });
  const parsed = JSON.parse(res.body || "{}") as Record<string, unknown>;
  if (res.status !== 200) {
    const error = (parsed.error ?? {}) as Record<string, unknown>;
    return { ok: false, status: res.status, code: String(error.code ?? "INTERNAL"), message: String(error.message ?? "sign-in failed"), requestId: String(error.requestId ?? "") };
  }
  const token = parsed.accessToken;
  const refresh = parsed.refreshToken;
  const expiresInMs = parsed.expiresInMs;
  if (typeof token !== "string" || typeof refresh !== "string" || typeof expiresInMs !== "number") {
    return { ok: false, status: 502, code: "BAD_LOGIN_SHAPE", message: "the session service answered without a token pair", requestId: "" };
  }
  const store = await jar();
  store.set(AT_COOKIE, joinAccess(Date.now() + expiresInMs, token), {
    maxAgeS: Math.floor(expiresInMs / 1000),
    sameSite: guard.crossSite ? "none" : "lax",
    secure: true,
  });
  store.set(RT_COOKIE, refresh, { maxAgeS: 60 * 60 * 24 * 30, sameSite: guard.crossSite ? "none" : "lax", secure: true });
  return { ok: true, userId: String((parsed.user as { id?: string })?.id ?? ""), expiresInMs };
}

export async function tear(): Promise<void> {
  const store = await jar();
  const at = splitAccess(store.get(AT_COOKIE));
  if (at.token) await callUpstream("/v1/auth/logout", { method: "POST", token: at.token });
  store.clear(AT_COOKIE);
  store.clear(RT_COOKIE);
}

/** What a server component needs to decide whether to render the shell or the signed-out frame. */
export async function serverAuth(): Promise<{ state: "authenticated" | "unauthenticated" | "expired"; userId: string | null }> {
  const store = await jar();
  const at = splitAccess(store.get(AT_COOKIE));
  if (!at.token) return { state: "unauthenticated", userId: null };
  if (accessExpired(at.expiresAt, Date.now())) {
    // An expired-but-refreshable session is still "authenticated" as far as *rendering* is concerned; the
    // proxy will rotate on first use. Saying otherwise is exactly the logged-out flash the prompt forbids.
    return { state: store.get(RT_COOKIE) ? "authenticated" : "expired", userId: null };
  }
  return { state: "authenticated", userId: null };
}
