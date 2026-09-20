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
import { existsSync, readFileSync } from "node:fs";
import { resolve } from "node:path";
import { describe, expect, it } from "vitest";
import { isAnonymousRead } from "@/auth/anonymous";
import { ROUTES, type RouteDecl } from "@/api/routes";

describe("anonymous reads", () => {
  it("is exactly the reads a session-less surface makes, and nothing else", () => {
    const flagged = (Object.entries(ROUTES) as [string, RouteDecl][])
      .filter(([, decl]) => decl.anonymous === true)
      .map(([key]) => key)
      .sort();
    // Six rows: every `x-auth: public` read on the ledger — P11's four page reads and the referral terms, which now
    // share this one declaration instead of each transport keeping its own list — plus the book, which the read-only
    // card cannot price a market without and the contract serves with `x-auth: none`.
    expect(flagged).toEqual(["book", "publicBoardPage", "publicMarketPage", "publicSitemap", "publicTraderPage",
                             "referralsTerms"]);
  });

  it("agrees with the contract about every public route", () => {
    // The declaration and the contract are two files that a person edits separately, which is how "public in the
    // contract, session-gated in the proxy" happened in the first place. This reads the contract and holds the two
    // together: every route the contract serves to `public` must be anonymous here, and nothing the contract calls
    // `admin` may be.
    // `import.meta.url` is not a file: URL under the test transform, so the contract is found from the project root
    // instead (the suite always runs from `web/`), with the sibling layout as a fallback.
    const root = resolve(process.cwd(), "..");
    const contract = existsSync(resolve(root, "contracts", "openapi.yaml"))
      ? resolve(root, "contracts", "openapi.yaml")
      : resolve(process.cwd(), "contracts", "openapi.yaml");
    const yaml = readFileSync(contract, "utf8");
    const publicPaths: string[] = [];
    const adminPaths: string[] = [];
    for (const block of yaml.split(/\n  \/v1\//).slice(1)) {
      const path = "/v1/" + (block.split(":\n")[0] ?? "").trim();
      if (/^\s*x-auth:\s*public$/m.test(block)) publicPaths.push(path);
      if (/^\s*x-auth:\s*admin$/m.test(block)) adminPaths.push(path);
    }
    expect(publicPaths.length).toBeGreaterThanOrEqual(4);
    // Every contract-public GET the ledger knows about is anonymous here. A public route no screen reads is not this
    // ledger's business, and the P08 gate separately holds the ledger to the contract.
    for (const path of publicPaths) {
      const row = Object.values(ROUTES).find((d) => d.path === path && d.method === "GET");
      if (!row) continue;                       // a public route no screen reads is not this ledger's business
      expect({ path, anonymous: (row as RouteDecl).anonymous === true }).toEqual({ path, anonymous: true });
    }
    // And the one admin route under the same prefix is never anonymous, whatever else changes here.
    expect(adminPaths).toContain("/v1/public/blocks");
    expect(isAnonymousRead("GET", "/v1/public/blocks")).toBe(false);
  });

  it("serves a public market page and a book without a session", () => {
    expect(isAnonymousRead("GET", "/v1/public/market/fed-cut-sept")).toBe(true);
    expect(isAnonymousRead("GET", "/v1/markets/0xM1/book")).toBe(true);
    // The three other contract-public reads: P11's pages go through a server-side helper that shares this predicate.
    expect(isAnonymousRead("GET", "/v1/public/leaderboard/weekly")).toBe(true);
    expect(isAnonymousRead("GET", "/v1/public/trader/0xabc")).toBe(true);
    expect(isAnonymousRead("GET", "/v1/public/sitemap")).toBe(true);
    // Query strings and a trailing slash come through the same hop; both must match the template.
    expect(isAnonymousRead("GET", "/v1/markets/0xM1/book?asOf=1758000000000")).toBe(true);
    expect(isAnonymousRead("GET", "/v1/public/market/fed-cut-sept/")).toBe(true);
    expect(isAnonymousRead("get", "/v1/public/market/fed-cut-sept")).toBe(true);
  });

  it("does not widen to the admin route that shares the public prefix", () => {
    expect(isAnonymousRead("GET", "/v1/public/blocks")).toBe(false);
    // A deeper path under a public route is a different route: the ledger declares templates, not prefixes.
    expect(isAnonymousRead("GET", "/v1/public/leaderboard/weekly/followers")).toBe(false);
    expect(isAnonymousRead("GET", "/v1/public/market/fed-cut-sept/order")).toBe(false);
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
