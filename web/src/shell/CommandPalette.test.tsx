import { describe, expect, it } from "vitest";
import { filterEntries } from "./CommandPalette";

const markets = [
  { id: "1", question: "Will BTC close above 100k this year?" },
  { id: "2", question: "Fed cuts in September" },
];

describe("palette filtering", () => {
  it("an empty query lists markets and actions, not a blank box", () => {
    const rows = filterEntries("", markets);
    expect(rows.length).toBe(markets.length + 5);
  });
  it("a 0x address offers the trader route, a near-miss does not", () => {
    const good = "0x" + "a".repeat(40);
    expect(filterEntries(good, markets).some((e) => e.group === "traders")).toBe(true);
    expect(filterEntries("0xabc", markets).some((e) => e.group === "traders")).toBe(false);
  });
  it("never produces an href that is not one of our own routes", () => {
    for (const entry of filterEntries("fed", markets)) {
      if (entry.href) expect(["/markets/2", "/markets", "/tape", "/terminal", "/portfolio", "/profile"]).toContain(entry.href);
    }
  });
});
