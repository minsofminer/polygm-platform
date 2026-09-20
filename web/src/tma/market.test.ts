/**
 * The payload mapper, where three product rules live.
 *
 * 1. A missing price is "—", never 0 — a market with no offers that renders `0.0¢` looks like a free lottery ticket.
 * 2. The NO side is derived from the YES ask (`1 - ask`), which is what a binary market means.
 * 3. The age shown is the *older* of the two reads, because a snapshot from a minute ago beside a row from now is
 *    still a minute-old price.
 */
import { describe, expect, it, vi } from "vitest";
import { ageText, centsFromMicro, centsFromMicros, closingText, endsSoon, loadMarketForSheet,
         microFromPrice, yesAskFromBook } from "./market";
import type { TmaRead } from "./market";

describe("money on the wire", () => {
  it("turns decimal strings into cents text, and never invents one", () => {
    expect(centsFromMicro("0.62")).toBe("62.0¢");
    expect(centsFromMicro("0.385")).toBe("38.5¢");
    expect(centsFromMicro("0.5")).toBe("50.0¢");
    expect(centsFromMicro(null)).toBe("—");
    expect(centsFromMicro("")).toBe("—");
    expect(centsFromMicro("1")).toBe("—");      // 100¢ is not a quote, it is a market that has already resolved
    expect(centsFromMicro("0")).toBe("—");
    expect(centsFromMicro("not-a-price")).toBe("—");
    // Digits, not `Number()`: a float in this path is how `0.62 - 0.61` becomes 0.010000000000000009.
    expect(microFromPrice("0.62")).toBe(620_000);
    expect(microFromPrice("0.000001")).toBe(1);
    expect(microFromPrice("0.6200001")).toBeNull();          // more precision than a price has
    expect(centsFromMicros(null)).toBe("—");
  });

  it("takes the best ask, refuses empty levels, and derives the other side", () => {
    const book = {
      asks: [{ price: "0.63", shares: "100" }, { price: "0.62", shares: "900" }],
      bids: [{ price: "0.61", shares: "500" }, { price: "0.60", shares: "0" }],
    };
    expect(yesAskFromBook(book)).toEqual({ yes: "62.0¢", no: "38.0¢", spread: "1.0¢" });
  });

  it("says nothing rather than zero when a side is empty", () => {
    expect(yesAskFromBook({ asks: [], bids: [{ price: "0.61", shares: "5" }] }))
      .toEqual({ yes: "—", no: "—", spread: "—" });
    expect(yesAskFromBook(null)).toEqual({ yes: "—", no: "—", spread: "—" });
  });
});

describe("freshness, in the product's words", () => {
  const now = 1_800_000_000_000;

  it("describes age the way the rest of the app does", () => {
    expect(ageText(now - 400, now)).toBe("as of just now");
    expect(ageText(now - 4_400, now)).toBe("as of 4 seconds ago");
    expect(ageText(now - 130_000, now)).toBe("as of 2 minutes ago");
    expect(ageText(now - 7_300_000, now)).toBe("as of 2 hours ago");
    // A clock skew must not produce "as of -3 seconds ago".
    expect(ageText(now + 5_000, now)).toBe("as of just now");
  });

  it("marks a market that is about to resolve, and one that already has", () => {
    expect(endsSoon(now + 3_600_000, now)).toBe(true);
    expect(endsSoon(now + 30 * 3_600_000, now)).toBe(false);
    expect(endsSoon(now - 60_000, now)).toBe(false);
    expect(closingText(now - 60_000, now)).toBe("closed");
    expect(closingText(now + 40 * 60_000, now)).toBe("closes in 40 min");
    expect(closingText(now + 20 * 3_600_000, now)).toBe("closes in 20 h");
  });
});

describe("the two reads", () => {
  const page = {
    ok: true as const,
    data: { marketId: "m-1", slug: "fed-cut-sept", question: "Will the Fed cut?",
            endDate: Date.now() + 86_400_000, asOf: 1_700_000_000_000 },
  };
  const book = { ok: true as const, data: { asks: [{ price: "0.62", shares: "900" }],
                                            bids: [{ price: "0.61", shares: "900" }], asOf: 1_700_000_030_000 } };

  it("resolves the deep link's slug, prices it, and shows the older stamp", async () => {
    const get = vi.fn(async (key: string) => (key === "publicMarketPage" ? page : book)) as unknown as
      <T>(key: "publicMarketPage" | "book", params: Record<string, string>) => Promise<TmaRead<T>>;
    const out = await loadMarketForSheet(get, "fed-cut-sept");
    expect("view" in out).toBe(true);
    if (!("view" in out)) return;
    expect(out.view.yesAsk).toBe("62.0¢");
    expect(out.view.noAsk).toBe("38.0¢");
    expect(out.view.marketId).toBe("m-1");
    // The book's stamp is newer than the page's, so the *page's* age is what the sheet shows.
    expect(out.view.ageText).toContain("hours ago");
  });

  it("turns a missing market into a sentence, not an exception", async () => {
    const get = vi.fn(async () => ({ ok: false as const, code: "NOT_FOUND", message: "no such market" })) as unknown as
      <T>(key: "publicMarketPage" | "book", params: Record<string, string>) => Promise<TmaRead<T>>;
    const out = await loadMarketForSheet(get, "gone");
    expect("error" in out && out.error).toContain("not listed");
  });
});
