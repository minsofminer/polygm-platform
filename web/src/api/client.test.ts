import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { MUTATING, newIdempotencyKey, request, urlFor } from "./client";
import { ROUTES } from "./routes";

function jsonResponse(status: number, body: unknown, headers: Record<string, string> = {}) {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "content-type": "application/json", "x-request-id": "req_test", ...headers },
  });
}

const STAMP = { asOf: 1_000, staleAfter: 61_000, serverAsOf: 1_000, cache: { ttlMs: 5_000 } };

describe("the route ledger is enforced in the client", () => {
  it("refuses to call a route the backend does not serve, without touching the network", async () => {
    const fetchSpy = vi.fn();
    vi.stubGlobal("fetch", fetchSpy);
    const out = await request({ key: "signup", body: {} });
    expect(out.ok).toBe(false);
    if (out.ok === false) {
      expect(out.error.code).toBe("ROUTE_NOT_BUILT");
      expect(out.error.message).toContain("/v1/auth/signup");
      expect(out.error.message).toContain("P08-L1");
    }
    expect(fetchSpy).not.toHaveBeenCalled();
  });
});

describe("mutations", () => {
  beforeEach(() => vi.useFakeTimers({ shouldAdvanceTime: true }));
  afterEach(() => vi.useRealTimers());

  it("always carries an idempotency key, and reuses the caller's across retries", async () => {
    const calls: { headers: Headers }[] = [];
    vi.stubGlobal(
      "fetch",
      vi.fn(async (_url: string, init: RequestInit) => {
        calls.push({ headers: new Headers(init.headers) });
        if (calls.length === 1) return jsonResponse(503, { error: { code: "RISK_UNAVAILABLE", message: "no", retryable: true, requestId: "r" } });
        return jsonResponse(200, { accepted: true, ...STAMP });
      }),
    );
    const key = newIdempotencyKey("order");
    const out = await request({ key: "createOrder", body: { side: "BUY" }, idempotencyKey: key, maxAttempts: 2 });
    expect(out.ok).toBe(true);
    expect(calls.length).toBe(2);
    expect(calls[0]!.headers.get("idempotency-key")).toBe(key);
    expect(calls[1]!.headers.get("idempotency-key")).toBe(key);
  });

  it("never retries an order after a timeout, because a second POST is a second order", async () => {
    let n = 0;
    vi.stubGlobal("fetch", vi.fn(async () => {
      n++;
      throw new Error("TimeoutError: the operation was aborted due to timeout");
    }));
    const out = await request({ key: "createOrder", body: {}, timeoutMs: 5, maxAttempts: 3 });
    expect(n).toBe(1);
    expect(out.ok).toBe(false);
    if (out.ok === false) expect(out.error.code).toBe("TIMEOUT");
  });

  it("does not retry a refusal that carries a reason", async () => {
    let n = 0;
    vi.stubGlobal("fetch", vi.fn(async () => {
      n++;
      return jsonResponse(403, { error: { code: "TOTP_REQUIRED", message: "confirm with your authenticator", retryable: false, requestId: "r1" } });
    }));
    const out = await request({ key: "addressRemove", body: { address: "0x1" } });
    expect(n).toBe(1);
    expect(out.ok === false && out.error.code).toBe("TOTP_REQUIRED");
  });

  it("MUTATING covers every verb that needs the key", () => {
    for (const [, decl] of Object.entries(ROUTES)) {
      if (decl.method !== "GET") expect(MUTATING.has(decl.method)).toBe(true);
    }
  });
});

describe("reads", () => {
  it("keeps the stamp out of the payload and reports an unstamped read as a failure", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => jsonResponse(200, { items: [1, 2], ...STAMP })));
    const out = await request<{ items: number[] }>({ key: "sessions" });
    expect(out.ok).toBe(true);
    if (out.ok === true) {
      expect(out.data).toEqual({ items: [1, 2] });
      expect(out.stamp?.asOf).toBe(1_000);
      expect("asOf" in out.data).toBe(false);
    }
    vi.stubGlobal("fetch", vi.fn(async () => jsonResponse(200, { items: [] })));
    const bad = await request({ key: "sessions" });
    expect(bad.ok === false && bad.error.code).toBe("UNSTAMPED_READ");
  });

  it("accepts a mutation ack that carries no stamp, and says so in the type", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => jsonResponse(200, { ended: true })));
    const out = await request<{ ended: boolean }>({ key: "logout" });
    expect(out.ok).toBe(true);
    if (out.ok === true) expect(out.data).toEqual({ ended: true });
  });

  it("fills path parameters and refuses a route whose segment nobody filled", () => {
    expect(urlFor(ROUTES.market, { key: "market", params: { market_id: "0x1/2" } })).toBe("/api/v1/markets/0x1%2F2");
    expect(() => urlFor(ROUTES.market, { key: "market", params: {} })).toThrowError(/missing its \{market_id\} value/);
  });
});
