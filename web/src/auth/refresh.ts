/**
 * Refresh rotation, with the one property that makes it survivable: **single-flight**.
 *
 * The upstream refresh token is single-use and its family is revoked on reuse (P07: `REFRESH_REUSED`
 * ends every session in the family — a correct security decision). So N parallel requests that all find
 * an expired access token must produce **one** refresh call. A naive proxy fires N, the second one is a
 * replay of a spent token, and the user's whole family of sessions dies because four components mounted
 * at once. This module is where that is prevented, and the test is the one that matters most in P08.
 *
 * The inflight key is a hash of the spent-looking refresh token, not the user id: two tabs with two
 * sessions must not share a flight, and two requests in the same tab with one session must.
 */
import type { ApiError } from "@/api/envelope";

export type CookieJar = {
  get: (name: string) => string | undefined;
  set: (name: string, value: string, opts: { maxAgeS: number; sameSite: "lax" | "none"; secure: boolean }) => void;
  clear: (name: string) => void;
};

export const AT_COOKIE = "pgm_at";
export const RT_COOKIE = "pgm_rt";

/**
 * The access cookie is `"<expiryEpochMs>:<access token>"`, and the proxy splits it so that deciding whether to
 * refresh costs one `indexOf`.
 *
 * These two functions live *here*, beside the only code that writes the cookie, because the writer used to
 * live here and the reader lived in `server.ts`. Each side had a test that passed: the writer asserted it had
 * written an expiry instant, the reader asserted it could read `expiry:token`. The pair was incompatible, so
 * every request after a rotation found no token, refreshed again, and eventually replayed a spent refresh
 * token upstream — which ends every session the user has. A contract split across two modules is not a
 * contract until one test reads both ends.
 */
export function splitAccess(value: string | undefined): { expiresAt: string | undefined; token: string | null } {
  if (!value) return { expiresAt: undefined, token: null };
  const idx = value.indexOf(":");
  if (idx < 0) return { expiresAt: undefined, token: null };
  return { expiresAt: value.slice(0, idx), token: value.slice(idx + 1) || null };
}

export function joinAccess(expiresAt: number, token: string): string {
  return `${expiresAt}:${token}`;
}

export type RefreshDeps = {
  /** POST /v1/auth/refresh upstream, returning the minted pair or an ApiError-shaped failure. */
  callRefresh: (refreshToken: string) => Promise<RefreshSuccess | RefreshFailure>;
  now: () => number;
  /**
   * Cross-site context: a request that came from inside the Telegram webview (or any embedder) is a
   * *cross-site* navigation as far as the browser is concerned, so its cookies need SameSite=None. On the
   * open web they do not, and asking for None everywhere is how you hand a session cookie to every site that
   * can frame you. The proxy computes this from `Sec-Fetch-Site`/`Sec-Fetch-Dest`, not from a user-agent
   * regex, because the header is what the browser asserts and the UA is what a script can type.
   */
  crossSite: boolean;
};

export type RefreshSuccess = {
  ok: true;
  accessToken: string;
  refreshToken: string;
  expiresInMs: number;
  userId: string;
};
export type RefreshFailure = { ok: false; error: ApiError };

const SKEW_MS = 5_000;
/**
 * How long a finished rotation stays adoptable.
 *
 * A browser fires the widgets of a terminal at once; they all carry the same cookies, so one wins the race
 * and the rest arrive holding a refresh token that has just been spent. If the flight entry is deleted the
 * instant it settles, those late requests each present the spent token, and upstream reads that as theft and
 * revokes the family: the user is logged out on every device because four components mounted at once.
 * Keeping the *result* briefly costs two seconds of reuse-detection granularity — a thief who copied the
 * token and replays it inside two seconds of its own rotation is served the rotation instead of tripping the
 * alarm. That is the better trade: the alarm still fires for the case it is designed for (a copy that
 * outlives the rotation), and the accidental case stops destroying sessions.
 */
const SETTLED_GRACE_MS = 2_000;
type Flight = {
  promise: Promise<RefreshOutcome>;
  /** set only by a *successful* rotation: a failure is never something to adopt, and must reach upstream */
  rotated?: { accessToken: string; refreshToken: string; expiresAt: number; settledAt: number };
};
const flights = new Map<string, Flight>();

export type RefreshOutcome =
  | { kind: "rotated"; accessToken: string; expiresAt: number }
  | { kind: "failed"; error: ApiError; endSession: boolean };

export function accessExpired(expiresAtCookie: string | undefined, now: number): boolean {
  if (!expiresAtCookie) return true;
  const at = Number(expiresAtCookie);
  if (!Number.isFinite(at)) return true;
  // A clock five seconds inside the expiry is already expired: the request still has to cross the network
  // and the proxy, and an access token that dies mid-flight is a 401 the user experiences as a bug.
  return now + SKEW_MS >= at;
}

/**
 * A synchronous key, deliberately not a crypto digest.
 *
 * The first version awaited `crypto.subtle.digest` before consulting the map. That is correct-looking code
 * that does nothing: five concurrent calls each suspend at the `await`, each finds the map empty on resume,
 * and each fires a refresh — five rotations of a single-use token, which is exactly the family-reuse event
 * the whole design exists to avoid. A dedupe window may not contain an await.
 *
 * FNV-1a is fine here because the value is only ever a map key: it is never stored, never compared to a
 * secret, never used to decide anything about identity.
 */
function flightKey(token: string): string {
  let hash = 0x811c9dc5;
  for (let i = 0; i < token.length; i++) {
    hash ^= token.charCodeAt(i);
    hash = Math.imul(hash, 0x01000193) >>> 0;
  }
  return hash.toString(16).padStart(8, "0") + ":" + token.length;
}

/**
 * One refresh per spent token per flight. Returns the new access token and *rotates the cookies*; a caller
 * that got `endSession: true` must send the browser to the expired state and clear both cookies.
 */
export async function refreshOnce(deps: RefreshDeps, jar: CookieJar): Promise<RefreshOutcome> {
  const rt = jar.get(RT_COOKIE);
  if (!rt) {
    return {
      kind: "failed",
      endSession: true,
      error: { code: "UNAUTHENTICATED", message: "this device has no session to refresh", retryable: false, requestId: "", status: 401 },
    };
  }
  const key = flightKey(rt);
  const existing = flights.get(key);
  if (existing) {
    if (existing.rotated && deps.now() - existing.rotated.settledAt > SETTLED_GRACE_MS) {
      flights.delete(key);                       // stale: this is a new refresh, or a replay we must not hide
    } else {
      if (existing.rotated) writePair(deps, jar, existing.rotated);   // adopt: this request's cookies move too
      return existing.promise;
    }
  }
  const done: { rotated?: Flight["rotated"] } = {};
  const flight = (async () => {
    const result = await deps.callRefresh(rt);
    if (!result.ok) {
      const reused = result.error.code === "REFRESH_REUSED" || result.error.code === "SESSION_REVOKED";
      if (reused) jar.clear(RT_COOKIE), jar.clear(AT_COOKIE);
      return { kind: "failed", endSession: reused, error: result.error } satisfies RefreshOutcome;
    }
    const now = deps.now();
    const expiresAt = now + result.expiresInMs;
    const pair = { accessToken: result.accessToken, refreshToken: result.refreshToken, expiresAt };
    writePair(deps, jar, pair);
    done.rotated = { ...pair, settledAt: now };
    return { kind: "rotated", accessToken: result.accessToken, expiresAt } satisfies RefreshOutcome;
  })();
  // The entry is published with a getter over `done`, because the closure that fills it starts running before
  // the entry exists: a plain `entry.rotated = …` here is a use-before-declaration, and the earlier shape
  // (`const entry` first, promise assigned after) would let a second request read `promise` as undefined.
  const entry: Flight = { promise: flight, get rotated() { return done.rotated; } };
  flights.set(key, entry);
  // Only a rotation is worth keeping after it settles; a failure is deleted so the next attempt is real.
  void flight.then((outcome) => {
    if (outcome.kind !== "rotated") flights.delete(key);
  });
  return flight;
}

/** Both halves of the pair, written by whoever is answering — the winner *and* an adopting late arrival. */
function writePair(deps: RefreshDeps, jar: CookieJar, pair: { accessToken: string; refreshToken: string; expiresAt: number }) {
  // Each response carries the cookie flags its own request needs: a rotation won by a webview request must
  // not be adopted into an open-web response as SameSite=None, or the same-origin page inherits a
  // cross-site cookie it never asked for.
  const sameSite: "lax" | "none" = deps.crossSite ? "none" : "lax";
  jar.set(AT_COOKIE, joinAccess(pair.expiresAt, pair.accessToken), {
    maxAgeS: Math.max(1, Math.floor((pair.expiresAt - deps.now()) / 1000)),
    sameSite,
    secure: true,
  });
  jar.set(RT_COOKIE, pair.refreshToken, { maxAgeS: 60 * 60 * 24 * 30, sameSite, secure: true });
}

/** Flights still in the air, which is what the name has always meant. */
export function inflightCount(): number {
  let n = 0;
  for (const f of flights.values()) if (!f.rotated) n++;
  return n;
}
