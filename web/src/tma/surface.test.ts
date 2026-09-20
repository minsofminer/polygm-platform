/**
 * What the Mini App's own domain answers — the allowlist is a security decision, so it is tested as one.
 *
 * The failure this prevents is not hypothetical: a prefix match without a segment boundary would let `/apianalytics`
 * or `/tma-admin` inherit the Mini App's CSP and its `frame-ancestors`, which is a page being frameable by Telegram
 * that nobody decided should be.
 */
import { describe, expect, it } from "vitest";
import { MINI_APP_PATHS, surfaceAllows } from "./surface";

describe("the Mini App surface", () => {
  it("answers the URL that is registered with BotFather, and the canonical route", () => {
    expect(surfaceAllows("/")).toBe(true);
    expect(surfaceAllows("/tma")).toBe(true);
    expect(surfaceAllows("/tma/")).toBe(true);
  });

  it("answers what the screen needs to render and work", () => {
    for (const path of ["/api/v1/markets/m-1/book", "/api/session/telegram", "/_next/static/chunk.js",
                        "/favicon.ico", "/manifest.webmanifest", "/robots.txt"]) {
      expect(surfaceAllows(path), path).toBe(true);
    }
  });

  it("refuses the rest of the product, including the near-misses", () => {
    for (const path of ["/sign-in", "/admin", "/terminal", "/markets", "/trader/0xabc", "/wallet",
                        "/apianalytics", "/tma-admin", "/_nextfoo", "/profile/keys"]) {
      expect(surfaceAllows(path), path).toBe(false);
    }
  });

  it("is not fooled by a query string or a trailing slash", () => {
    expect(surfaceAllows("/tma?startapp=fed-cut-sept")).toBe(true);
    expect(surfaceAllows("/admin?x=/tma")).toBe(false);
  });

  it("keeps the list short enough to read in a review", () => {
    expect(MINI_APP_PATHS.length).toBeLessThanOrEqual(12);
    expect(MINI_APP_PATHS).toContain("/api");
  });
});
