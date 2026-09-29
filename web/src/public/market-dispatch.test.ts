/**
 * The dispatch has two jobs and this file checks both: an id must reach the app's detail page, and a slug must
 * keep reaching the public odds page. The first one is here because it failed silently in production data —
 * all 159 seeded markets are `0xM…`, none of them hexadecimal, and every one of them 404'd.
 */
import { describe, expect, it } from "vitest";
import { isMarketId } from "./market-dispatch";

describe("which page a /market/<segment> address is", () => {
  it("takes the fixture ids the database actually holds", () => {
    for (const id of ["0xM1", "0xM159", "0xm1"]) expect(isMarketId(id)).toBe(true);
  });

  it("takes a real venue condition id", () => {
    expect(isMarketId(`0x${"a".repeat(64)}`)).toBe(true);
    expect(isMarketId("0xABCdef0123456789")).toBe(true);
  });

  it("leaves slugs to the public page", () => {
    // A slug is lowercase words: it never begins `0x`, and it never carries a `…` from a shortened address.
    for (const slug of ["fed-cut-sept", "btc-150k", "governance-vote-outcome"]) expect(isMarketId(slug)).toBe(false);
  });

  it("refuses the shapes that are neither: empty, a bare prefix, or an absurd length", () => {
    for (const bad of ["", "0x", "0x ", "0x-1", `0x${"a".repeat(128)}`]) expect(isMarketId(bad)).toBe(false);
  });
});
