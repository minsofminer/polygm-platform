/**
 * The read-only card's precondition, pinned.
 *
 * A deep link into a plain browser has no `initData` and never will, so any read the card needs must be one the
 * proxy serves without a session. That set is exactly two paths, and the interesting assertions are the negative
 * ones: `/v1/public/blocks` sits under the same prefix as the market page and is `x-auth: admin`, and `/v1/orders`
 * is a POST-shaped money route that must never be reachable by adding `anonymous` to something adjacent.
 *
 * If this file has to be edited to make a screen work, that edit is the decision — which is the point.
 */
import { describe, expect, it } from "vitest";
import { isAnonymousRead } from "@/auth/anonymous";
import { ROUTES, type RouteDecl } from "@/api/routes";

describe("anonymous reads", () => {
  it("is exactly the two rows the read-only card needs, and nothing else", () => {
    const flagged = (Object.entries(ROUTES) as [string, RouteDecl][])
      .filter(([, decl]) => decl.anonymous === true)
      .map(([key]) => key)
      .sort();
    expect(flagged).toEqual(["book", "publicMarketPage"]);
  });

  it("serves a public market page and a book without a session", () => {
    expect(isAnonymousRead("GET", "/v1/public/market/fed-cut-sept")).toBe(true);
    expect(isAnonymousRead("GET", "/v1/markets/0xM1/book")).toBe(true);
    // Query strings and a trailing slash come through the same hop; both must match the template.
    expect(isAnonymousRead("GET", "/v1/markets/0xM1/book?asOf=1758000000000")).toBe(true);
    expect(isAnonymousRead("GET", "/v1/public/market/fed-cut-sept/")).toBe(true);
    expect(isAnonymousRead("get", "/v1/public/market/fed-cut-sept")).toBe(true);
  });

  it("does not widen to the admin route that shares the public prefix", () => {
    expect(isAnonymousRead("GET", "/v1/public/blocks")).toBe(false);
    expect(isAnonymousRead("GET", "/v1/public/sitemap")).toBe(false);
    expect(isAnonymousRead("GET", "/v1/public/leaderboard/weekly")).toBe(false);
  });

  it("does not turn a neighbouring read or any mutation anonymous", () => {
    expect(isAnonymousRead("GET", "/v1/markets/0xM1/fills")).toBe(false);
    expect(isAnonymousRead("GET", "/v1/markets/0xM1")).toBe(false);
    expect(isAnonymousRead("GET", "/v1/tape")).toBe(false);
    expect(isAnonymousRead("POST", "/v1/public/market/fed-cut-sept")).toBe(false);
    expect(isAnonymousRead("POST", "/v1/markets/0xM1/book")).toBe(false);
    expect(isAnonymousRead("POST", "/v1/orders")).toBe(false);
    expect(isAnonymousRead("POST", "/v1/telegram/order")).toBe(false);
  });

  it("refuses a path that only looks like the template", () => {
    // `{slug}` is one segment: a *deeper* path is a different route and inherits nothing.
    expect(isAnonymousRead("GET", "/v1/public/market/fed-cut-sept/order")).toBe(false);
    expect(isAnonymousRead("GET", "/v1/public/market/")).toBe(false);
    expect(isAnonymousRead("GET", "/v1/markets//book")).toBe(false);
  });
});
