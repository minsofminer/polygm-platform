/**
 * The precondition of every read a signed-out visitor makes, pinned.
 *
 * A deep link into a plain browser has no `initData` and never will, so any read a public page needs must be one
 * the proxy serves without a session. This file used to name those reads by hand — six of them — and the day the
 * site's own market list turned out to be missing from the list, a signed-out visitor got a 401 from the web's
 * own hop and `/markets` rendered "Something failed" for every stranger who clicked it. A hand-written list is a
 * list somebody has to remember to extend; the contract already knows the answer, so the assertion is derived
 * from it (`src/api/contract-public.ts`) and the only way to widen the set is to widen it in the contract.
 *
 * The interesting assertions are still the negative ones: `/v1/public/blocks` sits under the same prefix as the
 * market page and is `x-auth: admin`, and `/v1/orders/amount` is the money route that must never become
 * reachable by adding `anonymous` to something adjacent.
 *
 * If this file has to be edited to make a screen work, that edit is the decision — which is the point.
 */
import { existsSync, readFileSync } from "node:fs";
import { resolve } from "node:path";
import { describe, expect, it } from "vitest";
import { isAnonymousRead } from "@/auth/anonymous";
import { contractPublicReads } from "@/api/contract-public";
import { ROUTES, type RouteDecl } from "@/api/routes";

describe("anonymous reads", () => {
  it("is exactly the reads the contract serves to nobody — no more, and no fewer", () => {
    const ledger = (Object.entries(ROUTES) as [string, RouteDecl][]).map(([key, decl]) => ({ key, decl }));
    const flagged = ledger.filter(({ decl }) => decl.anonymous === true);
    // The other direction is `src/api/public-reads.test.ts`'s job (it holds the whole ledger to the contract, both
    // ways, including the rows no screen reads). Here the question is narrower and one-sided: whatever the proxy
    // will serve without a session must be exactly what the contract says a stranger may read.
    const declared = new Set(ledger.map(({ decl }) => `${decl.method} ${decl.path}`));
    const expected = contractPublicReads()
      .map((r) => `${r.method} ${r.path}`)
      .filter((key) => declared.has(key))
      .sort();
    const flaggedMap = new Map(flagged.map(({ key, decl }) => [`${decl.method} ${decl.path}`, key]));
    expect([...flaggedMap.keys()].sort()).toEqual(expected);
    // Same size as the expected list, and the same length as its own key set: no row is counted twice, and no
    // flagged row is a path the contract does not publish.
    expect(flagged).toHaveLength(expected.length);
    // A flag on anything but a read would mean the proxy had been told to forward a mutation with no session.
    expect(flagged.filter(({ decl }) => decl.method !== "GET")).toEqual([]);
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

  it("still refuses the reads and the writes that need a session", () => {
    // Account-shaped reads: the proxy must keep treating these as sessions to be refreshed, however public their
    // neighbourhood is. `/v1/whale-views` is the saved views of *this* account, and the whales screen fires it
    // for every visitor — the screen tolerates the 401, and this is the flag that must never make it a 200.
    expect(isAnonymousRead("GET", "/v1/wallet/balance")).toBe(false);
    expect(isAnonymousRead("GET", "/v1/whale-views")).toBe(false);
    expect(isAnonymousRead("GET", "/v1/leaderboard/me")).toBe(false);
    // Money and mutations.
    expect(isAnonymousRead("POST", "/v1/orders/amount")).toBe(false);
    expect(isAnonymousRead("POST", "/v1/public/market/fed-cut-sept")).toBe(false);
    expect(isAnonymousRead("POST", "/v1/markets/0xM1/book")).toBe(false);
    expect(isAnonymousRead("POST", "/v1/telegram/order")).toBe(false);
    // The reads that became anonymous in the same change this comment is about are still reads the contract serves
    // to nobody: if one of these ever answers false again, a public screen has gone back to failing for strangers.
    expect(isAnonymousRead("GET", "/v1/markets")).toBe(true);
    expect(isAnonymousRead("GET", "/v1/markets/0xM1")).toBe(true);
  });

  it("refuses a path that only looks like the template", () => {
    // `{slug}` is one segment: a *deeper* path is a different route and inherits nothing.
    expect(isAnonymousRead("GET", "/v1/public/market/fed-cut-sept/order")).toBe(false);
    expect(isAnonymousRead("GET", "/v1/public/market/")).toBe(false);
    expect(isAnonymousRead("GET", "/v1/markets//book")).toBe(false);
  });
});
