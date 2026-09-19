/**
 * D4's rules, in the cases a screenshot cannot show: the threshold sentence a row carries, the ratio the badge
 * prints, the view that round-trips through its own stored knobs, the channel that cannot be chosen without a
 * market, and the multiple the API would refuse.
 */
import { describe, expect, it } from "vitest";
import {
  BUCKETS,
  CHANNELS,
  EMPTY_WHALE_FILTERS,
  bucketDefaults,
  bucketFor,
  budgetText,
  exportTargets,
  feedCounts,
  feedQuery,
  filtersToBody,
  notifyRefusal,
  parseMultiple,
  ratioText,
  severityOrder,
  severityTone,
  usd,
  viewSummary,
  viewToFilters,
} from "./whales";
import type { WhaleView } from "./wire";

function view(over: Partial<WhaleView> = {}): WhaleView {
  return {
    viewId: "wv-seed-01",
    name: "big fills",
    filters: { minSeverity: "notice", minNotionalMicro: 0, multiple: 5 },
    channel: null,
    severity: "notice",
    scope: "global",
    marketId: null,
    ruleId: null,
    createdMs: 1,
    notifies: false,
    firesPerWindow: null,
    ruleWindowMs: null,
    ruleEnabled: null,
    ...over,
  };
}

describe("the severity ladder", () => {
  it("orders the three levels and treats an unknown one as the quietest", () => {
    expect(severityOrder("info")).toBeLessThan(severityOrder("notice"));
    expect(severityOrder("notice")).toBeLessThan(severityOrder("urgent"));
    expect(severityOrder("banana")).toBe(0);
    expect(severityTone("urgent")).toBe("urgent");
    expect(severityTone("")).toBe("info");
  });

  it("renders a ratio as a multiple without a float: 120000 bps is 12x, 9450 is 0.9x", () => {
    expect(ratioText(120_000)).toBe("12x");
    expect(ratioText(15_000)).toBe("1.5x");
    expect(ratioText(9_450)).toBe("0.945x");      // meaningful digits kept: a threshold at 0.945x is not 0.9x
    expect(ratioText(12_000)).toBe("1.2x");
    expect(ratioText(10_100)).toBe("1.01x");
  });
});

describe("the feed's query", () => {
  it("sends a marketId only in market scope, and never a null multiple", () => {
    const global = feedQuery(EMPTY_WHALE_FILTERS);
    expect(global.marketId).toBeUndefined();
    expect(global.multiple).toBeUndefined();
    const scoped = feedQuery({ ...EMPTY_WHALE_FILTERS, scope: "market", marketId: "0xM1", multiple: 5 });
    expect(scoped.marketId).toBe("0xM1");
    expect(scoped.multiple).toBe(5);
  });

  it("sends the scope it is actually showing, not the one with a market id lying around", () => {
    // A market id left over from the previous scope must not turn a global feed into a market feed.
    expect(feedQuery({ ...EMPTY_WHALE_FILTERS, scope: "global", marketId: "0xM1" }).marketId).toBeUndefined();
  });
});

describe("a saved view round-trips through its own knobs", () => {
  it("reads the filters the API stored, rather than re-deriving them from the name", () => {
    const filters = viewToFilters(view());
    expect(filters.multiple).toBe(5);
    expect(filters.minSeverity).toBe("notice");
    expect(filters.scope).toBe("global");
    expect(filters.marketId).toBe("");
  });

  it("falls back to the empty state for a stored severity it does not know", () => {
    const filters = viewToFilters(view({ filters: { minSeverity: "loud" }, severity: "urgent" }));
    expect(filters.minSeverity).toBe("urgent");
    expect(viewToFilters(view({ filters: {}, severity: "banana" })).minSeverity).toBe("notice");
  });

  it("writes the knobs the user was looking at, and a marketId only when there is a market", () => {
    const body = filtersToBody("  fed whales  ", { ...EMPTY_WHALE_FILTERS, scope: "market", marketId: "0xM1", multiple: 5 }, "telegram");
    expect(body).toEqual({ name: "fed whales", minSeverity: "info", multiple: 5, marketId: "0xM1", channel: "telegram" });
    expect(filtersToBody("f", { ...EMPTY_WHALE_FILTERS, scope: "market" }, null)).toEqual({ name: "f", minSeverity: "info" });
  });
});

describe("a notifying view needs a target, and the screen says so first", () => {
  it("refuses a channel on a global view before the request, with the reason", () => {
    const refusal = notifyRefusal(EMPTY_WHALE_FILTERS, "telegram");
    expect(refusal).toContain("needs a market");
    expect(notifyRefusal({ ...EMPTY_WHALE_FILTERS, scope: "market" }, "telegram")).toContain("needs a market");
    expect(notifyRefusal({ ...EMPTY_WHALE_FILTERS, scope: "market", marketId: "0xM1" }, "telegram")).toBeNull();
    expect(notifyRefusal(EMPTY_WHALE_FILTERS, null)).toBeNull();
  });

  it("states a rule's fire budget, and that a filter has none", () => {
    expect(budgetText(view({ notifies: true, firesPerWindow: 4, ruleWindowMs: 3_600_000, ruleEnabled: true })))
      .toBe("4 fires per 1h");
    expect(budgetText(view({ notifies: true, firesPerWindow: 6, ruleWindowMs: 900_000, ruleEnabled: false })))
      .toBe("6 fires per 15m · paused");
    expect(budgetText(view())).toContain("never fires");
  });

  it("summarises a view in one sentence a list can show", () => {
    expect(viewSummary(view({ scope: "market", marketId: "0xM1", name: "fed" })))
      .toContain("market 0xM1");
    expect(viewSummary(view({ notifies: true, channel: "email" }))).toContain("notifies on email");
    expect(viewSummary(view())).toContain("filter you look at");
  });
});

describe("the tunable multiple", () => {
  it("accepts whole numbers inside the API's range and nothing else", () => {
    expect(parseMultiple("5")).toBe(5);
    expect(parseMultiple(" 20 ")).toBe(20);
    expect(parseMultiple("1000")).toBe(1000);
    expect(parseMultiple("5000")).toBe(1000);      // clamped, never forwarded past the documented maximum
    expect(parseMultiple("1")).toBeNull();         // the API's own floor is 2
    expect(parseMultiple("3.5")).toBeNull();
    expect(parseMultiple("1e3")).toBeNull();
    expect(parseMultiple("")).toBeNull();
    expect(parseMultiple("-4")).toBeNull();
  });
});

describe("the floors are defaults, not laws", () => {
  it("states every bucket's floor and that it can be tuned", () => {
    const rows = bucketDefaults();
    expect(rows).toHaveLength(BUCKETS.length);
    for (const row of rows) {
      expect(row.sentence).toContain("by default");
      expect(row.sentence).toContain("tunable");
    }
    expect(rows[0]?.sentence).toContain("500.00");
  });

  it("labels a market by its own volume without claiming to set the threshold", () => {
    expect(bucketFor(1_000_000_000_000)).toBe("large");
    expect(bucketFor(30_000_000_000)).toBe("mid");
    expect(bucketFor(1_000_000)).toBe("small");
  });
});

describe("what a feed row offers to do with a whale", () => {
  it("exports to a watchlist, a follow and a copy config — the copy one in dry-run", () => {
    const targets = exportTargets({ anonWallet: "w_abc", marketId: "0xM1" });
    expect(targets.map((t) => t.id)).toEqual(["watchlist", "follow", "copy"]);
    const copy = targets.find((t) => t.id === "copy");
    expect(copy?.href).toContain("dryRun=1");
    expect(copy?.note).toContain("dry-run");
  });

  it("renders a count sentence from the server's own numbers, and survives a missing block", () => {
    expect(feedCounts({ overThreshold: 9, returned: 5, marketsWithFills: 3 }))
      .toBe("5 of 9 fills over threshold across 3 markets");
    expect(feedCounts(undefined)).toContain("0 of 0");
  });

  it("knows the three channels the API accepts, and only those", () => {
    expect([...CHANNELS]).toEqual(["telegram", "email", "webhook"]);
  });

  it("formats dollars by integer arithmetic", () => {
    expect(usd(500_000_000)).toBe("$ 500.00");
    expect(usd(5_000_000_000)).toBe("$ 5\u2009000.00");
    expect(usd(-1_234_000)).toBe("-$ 1.23");
  });
});
