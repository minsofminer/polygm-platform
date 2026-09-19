import { describe, expect, it } from "vitest";
import { canTradeGivenFreshness, freshnessOf, parseError, stampFrom, stampAge } from "./envelope";

const stamp = { asOf: 1_000_000, staleAfter: 1_004_000, ttlMs: 2_000 };

describe("the stamp is the age of the data", () => {
  it("refuses to invent freshness when a route forgot to stamp", () => {
    expect(stampFrom({ items: [] })).toBeNull();
    expect(freshnessOf(null, 0)).toBe("unknown");
    expect(canTradeGivenFreshness("unknown")).toBe(false);
  });
  it("ages live -> stale -> blocking, in that order, and only blocks after passing through stale", () => {
    expect(freshnessOf(stamp, stamp.asOf + 1_000)).toBe("live");
    expect(freshnessOf(stamp, stamp.staleAfter)).toBe("live"); // the boundary itself is still fresh
    expect(freshnessOf(stamp, stamp.staleAfter + 1)).toBe("stale");
    expect(freshnessOf(stamp, stamp.asOf + 8_000)).toBe("stale"); // 2x the window, inclusive
    expect(freshnessOf(stamp, stamp.asOf + 8_001)).toBe("blocking");
    expect(freshnessOf(stamp, stamp.staleAfter + 5_000)).toBe("blocking");
    expect(canTradeGivenFreshness("stale")).toBe(false);
  });
  it("never reports a negative age when the client clock is ahead", () => {
    expect(stampAge(stamp, stamp.asOf - 50_000)).toBe(0);
    expect(freshnessOf(stamp, stamp.asOf - 50_000)).toBe("live");
  });
});

describe("the error envelope", () => {
  it("reads code/message/retryable/requestId and ignores anything else the server sent", () => {
    const e = parseError(
      { error: { code: "TOTP_REQUIRED", message: "an authenticator is needed for this", retryable: false, requestId: "req_1", detail: "secret" } },
      403,
      new Headers({ "retry-after": "30" }),
    );
    expect(e).toMatchObject({ code: "TOTP_REQUIRED", status: 403, retryable: false, requestId: "req_1", retryAfterS: 30 });
    expect("detail" in e).toBe(false);
  });
  it("survives a non-JSON body without inventing a code that is retryable", () => {
    const e = parseError("<html>502</html>", 502, new Headers());
    expect(e.code).toBe("INTERNAL");
    expect(e.retryable).toBe(false);
  });
});
