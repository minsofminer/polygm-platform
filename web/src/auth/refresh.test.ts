import { describe, expect, it } from "vitest";
import { accessExpired, AT_COOKIE, refreshOnce, RT_COOKIE, splitAccess, type CookieJar, type RefreshDeps } from "./refresh";

type SetCall = { name: string; value: string; opts: { maxAgeS: number; sameSite: string; secure: boolean } };

function jar(initial: Record<string, string> = {}) {
  const values: Record<string, string | null> = { ...initial };
  const sets: SetCall[] = [];
  return {
    values,
    sets,
    get: (n: string) => values[n] ?? undefined,
    set: (n: string, v: string, opts: { maxAgeS: number; sameSite: "lax" | "none"; secure: boolean }) => {
      sets.push({ name: n, value: v, opts });
      values[n] = v;
    },
    clear: (n: string) => {
      values[n] = null;
    },
  };
}

function depsWith(
  over: { callRefresh: RefreshDeps["callRefresh"]; crossSite?: boolean },
  now = 1_700_000_000_000,
): RefreshDeps {
  return { now: () => now, crossSite: over.crossSite ?? false, callRefresh: over.callRefresh };
}

describe("single-flight refresh", () => {
  it("N parallel requests with one spent token produce exactly one upstream refresh", async () => {
    let upstream = 0;
    let release: () => void = () => undefined;
    const gate = new Promise<void>((r) => (release = r));
    const deps = depsWith({
      callRefresh: async () => {
        upstream++;
        await gate;
        return { ok: true, accessToken: "at_new", refreshToken: "rt_new", expiresInMs: 300_000, userId: "u_1" };
      },
    });
    const j = jar({ [RT_COOKIE]: "rt_spent" });
    const flights = [1, 2, 3, 4, 5].map(() => refreshOnce(deps, j));
    release();
    const settled = await Promise.all(flights);
    expect(upstream).toBe(1);
    expect(settled.every((s) => s.kind === "rotated" && s.accessToken === "at_new")).toBe(true);
    // Rotating the refresh cookie is the other half of the contract: the spent token must not stay on disk.
    expect(j.values[RT_COOKIE]).toBe("rt_new");
  });

  it("does not merge two different sessions into one flight", async () => {
    let upstream = 0;
    const deps = depsWith({
      callRefresh: async () => {
        upstream++;
        return { ok: true, accessToken: "at" + upstream, refreshToken: "rt" + upstream, expiresInMs: 300_000, userId: "u" };
      },
    });
    const a = jar({ [RT_COOKIE]: "rt_a" });
    const b = jar({ [RT_COOKIE]: "rt_b" });
    await Promise.all([refreshOnce(deps, a), refreshOnce(deps, b)]);
    expect(upstream).toBe(2);
  });

  it("a spent-token replay upstream ends the session and clears both cookies", async () => {
    const deps = depsWith({
      callRefresh: async () => ({
        ok: false as const,
        error: { code: "REFRESH_REUSED", message: "this session was ended because a refresh token was used twice", retryable: false, requestId: "r", status: 401 },
      }),
    });
    const j = jar({ [RT_COOKIE]: "rt_replayed", [AT_COOKIE]: "1699" });
    const out = await refreshOnce(deps, j);
    expect(out.kind).toBe("failed");
    if (out.kind === "failed") {
      expect(out.endSession).toBe(true);
      expect(out.error.code).toBe("REFRESH_REUSED");
    }
    expect(j.values[RT_COOKIE]).toBeNull();
    expect(j.values[AT_COOKIE]).toBeNull();
  });

  it("a device with no refresh cookie never calls upstream", async () => {
    let hit = 0;
    const deps = depsWith({
      callRefresh: async () => {
        hit++;
        return { ok: false as const, error: { code: "INTERNAL", message: "", retryable: false, requestId: "", status: 500 } };
      },
    }, 0);
    const out = await refreshOnce(deps, jar());
    expect(hit).toBe(0);
    expect(out.kind === "failed" && out.error.code).toBe("UNAUTHENTICATED");
  });

  it("asks for SameSite=None only in the cross-site (webview) context, and Secure in both", async () => {
    for (const crossSite of [false, true]) {
      const j = jar({ [RT_COOKIE]: "rt_x" });
      const deps = depsWith({
        crossSite,
        callRefresh: async () => ({ ok: true as const, accessToken: "at", refreshToken: "rt_new", expiresInMs: 300_000, userId: "u" }),
      });
      await refreshOnce(deps, j);
      expect(j.sets.length).toBe(2);
      for (const call of j.sets) {
        expect(call.opts.sameSite).toBe(crossSite ? "none" : "lax");
        expect(call.opts.secure).toBe(true);
      }
      // The reader in the proxy must be able to read exactly what this writer wrote. The previous version of
      // this test asserted the cookie was a bare expiry instant — true, and useless: `server.ts` split it on a
      // colon that was never there, so a rotated session had no token and refreshed on every request.
      expect(splitAccess(j.values[AT_COOKIE] ?? undefined)).toEqual({ expiresAt: String(1_700_000_000_000 + 300_000), token: "at" });
    }
  });
});

describe("the expiry clock", () => {
  it("treats a token expiring inside the network skew as already expired", () => {
    const now = 1_000_000;
    expect(accessExpired(String(now + 60_000), now)).toBe(false);
    expect(accessExpired(String(now + 4_999), now)).toBe(true);
    expect(accessExpired(undefined, now)).toBe(true);
    expect(accessExpired("not-a-number", now)).toBe(true);
  });
});

describe("the race a browser actually produces", () => {
  // N widgets mount at once with an expired access token. One request wins, and the rest reach
  // `refreshOnce` a few milliseconds later holding the token the winner has already spent. Replaying that
  // token upstream is the signature of theft, and the answer to theft is to revoke every session the user
  // has — so the late arrivals must adopt the rotation instead of presenting the spent token again.
  it("a request that arrives after the rotation settled adopts it, and one past the grace is still a replay", async () => {
    let upstream = 0;
    let now = 1_700_000_000_000;
    const deps: RefreshDeps = {
      now: () => now,
      crossSite: false,
      callRefresh: async () => {
        upstream++;
        return { ok: true, accessToken: "at_new" + upstream, refreshToken: "rt_new" + upstream, expiresInMs: 300_000, userId: "u_1" };
      },
    };
    await refreshOnce(deps, jar({ [RT_COOKIE]: "rt_race" }));
    expect(upstream).toBe(1);

    const late = jar({ [RT_COOKIE]: "rt_race" });
    const out = await refreshOnce(deps, late);
    expect(upstream).toBe(1);
    expect(out.kind).toBe("rotated");
    expect(late.values[RT_COOKIE]).toBe("rt_new1");
    expect(splitAccess(late.values[AT_COOKIE] ?? undefined)?.token).toBe("at_new1");

    // Past the grace window the same token is a replay again, and upstream has to see it: the alarm for a
    // copied refresh token is worth two seconds, not an eternity.
    now += 5_000;
    await refreshOnce(deps, jar({ [RT_COOKIE]: "rt_race" }));
    expect(upstream).toBe(2);
  });

  it("a failed flight is not adopted, so a real outage retries instead of replaying a cached refusal", async () => {
    let upstream = 0;
    const deps = (): RefreshDeps => ({
      now: () => 1_700_000_000_000,
      crossSite: false,
      callRefresh: async () => {
        upstream++;
        return { ok: false as const, error: { code: "UPSTREAM_DOWN", message: "the session service is not answering", retryable: true, requestId: "r", status: 503 } };
      },
    });
    const d = deps();
    await refreshOnce(d, jar({ [RT_COOKIE]: "rt_down" }));
    const out = await refreshOnce(d, jar({ [RT_COOKIE]: "rt_down" }));
    expect(upstream).toBe(2);
    expect(out.kind === "failed" && out.error.code).toBe("UPSTREAM_DOWN");
  });
});
