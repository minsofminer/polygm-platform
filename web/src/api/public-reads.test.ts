/**
 * Every read the contract serves to nobody must be `anonymous: true` in the ledger.
 *
 * `src/auth/anonymous.ts` states the rule the proxy follows — a route is public only if the ledger declares it —
 * and until this file existed nothing checked the ledger against the source of truth. Thirteen reads were
 * missing the flag while the contract said `x-auth: none` for them, which meant a signed-out visitor got a 401
 * from the web's own hop for a read the API answers to anybody. The visible one was the site's market list:
 * **`/markets` rendered "Something failed" for every stranger who clicked it**, on the landing page's own nav.
 *
 * So this test is a cross-check of two files, not a unit test of one: the contract's `x-auth` values are parsed
 * from `contracts/openapi.yaml`, the ledger is imported, and the two are required to agree for every route the
 * ledger declares. It fails in both directions that matter — a public route left session-gated (the bug above),
 * and a route declared anonymous that the contract does not serve publicly (which would let the proxy pass a
 * credentialed read with no session attached, and the API's own 401 would then surface as a confusing error).
 *
 * The contract is read through `src/api/contract-public.ts`, which is the same reader
 * `src/auth/anonymous-read.test.ts` uses — two tests parsing the same file two ways is how the two of them would
 * come to disagree, and the point of the pair is that the ledger and the contract cannot. If the contract's
 * layout ever changes, the first test below fails rather than this file quietly checking nothing.
 */
import { describe, expect, it } from "vitest";

import { contractPublicReads, contractRoutes, NO_SESSION_NEEDED } from "./contract-public";
import { ROUTES, type RouteDecl } from "./routes";

const ledger = Object.entries(ROUTES as Record<string, RouteDecl>).map(([key, decl]) => ({ key, decl }));

describe("the ledger and the contract agree about what is public", () => {
  it("parses the contract at all — the file, the methods and the x-auth values are where this test expects", () => {
    const routes = contractPublicReads();
    const xAuthValues = contractRoutes().map((r) => r.auth);
    // If the parser ever stops matching, this fails here rather than passing a suite that checks nothing.
    expect(routes.length).toBeGreaterThanOrEqual(20);
    expect(xAuthValues).toContain("none");
    expect(xAuthValues).toContain("public");
    expect(xAuthValues).toContain("user");
    expect(routes.some((r) => r.path === "/v1/markets" && r.method === "GET")).toBe(true);
  });

  it("declares `anonymous: true` for every public GET the ledger names", () => {
    const routes = contractPublicReads();
    const publicSet = new Set(routes.map((r) => `${r.method} ${r.path}`));
    const missing = ledger
      .filter(({ decl }) => decl.method.toUpperCase() === "GET" && publicSet.has(`GET ${decl.path}`))
      .filter(({ decl }) => decl.anonymous !== true)
      .map(({ key, decl }) => `${key} (${decl.path}) is x-auth: none in the contract but not anonymous in the ledger`);
    expect(missing).toEqual([]);
  });

  it("declares nothing anonymous that the contract actually gates", () => {
    const routes = contractPublicReads();
    const publicSet = new Set(routes.map((r) => `${r.method} ${r.path}`));
    const wrong = ledger
      .filter(({ decl }) => decl.anonymous === true)
      .filter(({ decl }) => !publicSet.has(`${decl.method.toUpperCase()} ${decl.path}`))
      .map(({ key, decl }) => `${key} (${decl.path}) is anonymous in the ledger but not x-auth: none in the contract`);
    expect(wrong).toEqual([]);
  });

  it("keeps the exception the ledger's own comment implies: a POST is never an anonymous *read*", () => {
    // `POST /v1/telegram/session` and `POST /v1/telegram/webhook` carry Telegram's header secret, and
    // `POST /v1/auth/*` are the sign-in paths themselves. All five are `x-auth: none`/`public` in the contract
    // and none is a *read*, so none belongs in the ledger's anonymous set: that flag answers exactly one
    // question — "may this read go out with no session attached?".
    const publicPosts = contractRoutes().filter((r) => r.method === "POST" && NO_SESSION_NEEDED.has(r.auth)).map((r) => r.path).sort();
    expect(publicPosts).toEqual(["/v1/auth/login", "/v1/auth/refresh", "/v1/auth/telegram", "/v1/telegram/session", "/v1/telegram/webhook"]);
    const flaggedPosts = ledger.filter(({ decl }) => decl.method.toUpperCase() === "POST" && decl.anonymous === true);
    expect(flaggedPosts).toEqual([]);
  });
});
