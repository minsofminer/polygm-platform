/**
 * The terminal's pure parts, in the cases a screenshot cannot show: the coalescing window at a busy venue, the
 * pause counter a user reads, the two notional filters, the tooltip that states the whales rule, the sound
 * default, the fee arithmetic behind the 5-minute crypto template, and the share parsing that must not go
 * through a float.
 */
import { describe, expect, it } from "vitest";
import {
  COALESCE_THRESHOLD_PER_S,
  EMPTY_FILTERS,
  arrivalRate,
  coalesceFills,
  crypto5mTemplate,
  curvePoints,
  formatMicro,
  formatMultiple,
  formatShares,
  labelSentence,
  passesNotional,
  sharePercent,
  sharesMicro,
  sharesWhole,
  shouldChime,
  soundEnabled,
  thresholdSentence,
  virtualWindow,
  walletHref,
  applyFilters,
  rowTarget,
} from "./tape";
import type { CurvePoint, LabelFact, TerminalFill } from "./wire";

const MICRO = 1_000_000;

function fill(over: Partial<TerminalFill> = {}): TerminalFill {
  return {
    tsMs: 1_700_000_000_000,
    conditionId: "0xC1",
    tokenId: "1",
    marketId: "0xM1",
    marketSlug: "fed-cut-march",
    question: "Will the Fed cut in March?",
    category: "Politics",
    tick: "0.01",
    side: "BUY",
    outcome: "Yes",
    price: "0.5",
    shares: "12000",
    notionalMicro: 6_000 * MICRO,
    anonWallet: "w_5c037b2ab8",
    labels: [],
    source: "venue",
    lagMs: 120,
    thresholdMicro: 500 * MICRO,
    thresholdRule: "whale = max(the p99.5 fill of this market's 900 fills ($150.00), $500.00 absolute floor) = "
      + "$500.00: this market's own fills set the bar",
    thresholdReason: "relative",
    severity: "notice",
    ratioBps: 120_000,
    rule: "severity is the fill's ratio to the threshold: urgent at 4×, notice at 1.5×",
    isWhale: true,
    ...over,
  };
}

describe("the tape's notional filters", () => {
  it("applies the absolute floor and the market-relative multiple independently", () => {
    const row = fill({ notionalMicro: 300 * MICRO });
    // $300 is under a $500 floor, and 6× the market's $50 median: the floor is what rejects it.
    expect(passesNotional(row, { ...EMPTY_FILTERS, minNotionalMicro: 500 * MICRO }, 50 * MICRO)).toBe(false);
    expect(passesNotional(row, { ...EMPTY_FILTERS, relativeToMedian: 2 }, 50 * MICRO)).toBe(true);
    expect(passesNotional(row, { ...EMPTY_FILTERS, relativeToMedian: 10 }, 50 * MICRO)).toBe(false);
  });

  it("filters by the facets the API returned, not by a client-side re-derivation", () => {
    const rows = [fill(), fill({ anonWallet: "w_other", side: "SELL", outcome: "No" })];
    expect(applyFilters(rows, { ...EMPTY_FILTERS, side: "SELL" }, {}).length).toBe(1);
    expect(applyFilters(rows, { ...EMPTY_FILTERS, wallet: "w_5c037b2ab8" }, {}).length).toBe(1);
    expect(applyFilters(rows, { ...EMPTY_FILTERS, outcome: "No" }, {}).length).toBe(1);
  });
});

describe("coalescing under load", () => {
  it("releases immediately when the venue is quiet", () => {
    const out = coalesceFills([], [fill()], { ratePerSecond: 3, nowMs: 1_000, lastFlushMs: 0, paused: false });
    expect(out.released).toBe(1);
    expect(out.buffer.length).toBe(1);
    expect(out.withheld).toBe(0);
  });

  it("holds a batch inside the flush window above the threshold, and counts what is waiting", () => {
    const out = coalesceFills([], [fill(), fill()], {
      ratePerSecond: COALESCE_THRESHOLD_PER_S + 5,
      nowMs: 1_000,
      lastFlushMs: 900,
      paused: false,
    });
    expect(out.released).toBe(0);
    expect(out.withheld).toBe(2);
  });

  it("a pause counts the unseen fills rather than freezing them silently", () => {
    const out = coalesceFills([fill({ tsMs: 1 })], [fill({ tsMs: 2 }), fill({ tsMs: 3 })], {
      ratePerSecond: 1,
      nowMs: 5_000,
      lastFlushMs: 0,
      paused: true,
    });
    expect(out.released).toBe(0);
    expect(out.withheld).toBe(2);
  });

  it("measures the arrival rate from timestamps, not a timer", () => {
    const rows = [fill({ tsMs: 900 }), fill({ tsMs: 950 }), fill({ tsMs: 400 }), fill({ tsMs: 20 })];
    expect(arrivalRate(rows, 1_000, 500)).toBe(2);        // the last half-second only
    expect(arrivalRate([], 1_000)).toBe(0);
  });
});

describe("virtualisation", () => {
  it("returns the rows in the DOM plus an overscan, and the paddings that keep the scrollbar honest", () => {
    const win = virtualWindow(400, 440, 440, 44, 4);
    expect(win.start).toBe(6);
    expect(win.end).toBe(25);
    expect(win.topPad).toBe(6 * 44);
    expect(win.bottomPad).toBe((400 - 25) * 44);
  });

  it("is empty rather than negative at the edges", () => {
    expect(virtualWindow(0, 0, 500)).toEqual({ start: 0, end: 0, topPad: 0, bottomPad: 0 });
    const first = virtualWindow(10, 0, 100, 44, 2);
    expect(first.start).toBe(0);
  });
});

describe("what a badge says", () => {
  it("states the exact rule the server used, with this row's own numbers", () => {
    const text = thresholdSentence(fill());
    expect(text).toContain("whale = max(");
    expect(text).toContain("$500.00 absolute floor");
    // the money module's own spacing: unit, space, grouped digits
    expect(text).toContain("$ 6\u2009000.00");
    expect(text).toContain("12×");
    expect(text).toContain("the market's own fills set the bar");
  });

  it("says so when the percentile was discarded rather than reported", () => {
    const text = thresholdSentence(fill({ thresholdReason: "absolute_fallback", ratioBps: 5_000 }));
    expect(text).toContain("the floor applies alone");
    expect(text).toContain("discarded");
  });

  it("carries the rule, the confidence and what the label does NOT claim", () => {
    const fact: LabelFact = {
      label: "whale",
      confidence: 820,
      publishable: true,
      rule: "notional ≥ max(p99.5 of the market's window fills, $500)",
      disclaimer: "size is not intent: a whale may be hedging, not predicting",
    };
    const text = labelSentence(fact);
    expect(text).toContain("notional ≥");
    expect(text).toContain("82%");
    expect(text).toContain("size is not intent");
  });

  it("renders a multiple in the same vocabulary as the rule sentence", () => {
    expect(formatMultiple(15_000)).toBe("1.5×");
    expect(formatMultiple(40_000)).toBe("4×");
    expect(formatMultiple(9_450)).toBe("0.9×");
  });
});

describe("money and size text", () => {
  it("formats micro through the money module", () => {
    expect(formatMicro(6_000 * MICRO)).toBe("$ 6\u2009000.00");
    expect(formatMicro(-122_400_000)).toBe("-$ 122.40");
  });

  it("parses a decimal string of shares without a float and without truncating a fraction", () => {
    expect(sharesMicro("12000")).toBe(12_000 * MICRO);
    expect(sharesMicro("0.5")).toBe(500_000);
    expect(sharesMicro("1.2345678")).toBe(1_234_567);       // six decimals kept, the seventh dropped
    expect(sharesWhole("12000.9")).toBe(12_000);
    expect(formatShares(12_000 * MICRO)).toBe("12000");
    expect(formatShares(500_000)).toBe("0.5");
  });

  it("turns basis points into a percentage without a floating multiplication", () => {
    expect(sharePercent(2_350)).toBe("23.50%");
    expect(sharePercent(4_000)).toBe("40%");
  });
});

describe("the sound toggle", () => {
  it("is off unless the user turned it on", () => {
    expect(soundEnabled(null)).toBe(false);
    expect(soundEnabled("0")).toBe(false);
    expect(soundEnabled("1")).toBe(true);
  });

  it("fires for followed wallets only", () => {
    const row = fill();
    expect(shouldChime(row, [row.anonWallet], true)).toBe(true);
    expect(shouldChime(row, ["w_someone_else"], true)).toBe(false);
    expect(shouldChime(row, [row.anonWallet], false)).toBe(false);
  });
});

describe("navigation from a row", () => {
  it("opens the market, and shift adds it to the watchlist instead of navigating", () => {
    expect(rowTarget(fill(), false).kind).toBe("market");
    expect(rowTarget(fill(), true).kind).toBe("watchlist");
    expect(walletHref("w_abc")).toBe("/trader/w_abc");
  });
});

describe("the 5-minute crypto template", () => {
  it("is offered only when the edge covers the fees, and states the arithmetic either way", () => {
    const offered = crypto5mTemplate(20_000, 6_000);
    expect(offered.available).toBe(true);
    expect(offered.sentence).toContain("$ 0.01");
    const refused = crypto5mTemplate(4_000, 9_000);
    expect(refused.available).toBe(false);
    expect(refused.sentence).toContain("not offered");
    expect(refused.sentence).toContain("$ 0.00");
  });
});

describe("the PnL curve's drawdown overlay", () => {
  const curve: CurvePoint[] = [
    { tsMs: 1, cumMicro: 100, peakMicro: 100, drawdownMicro: 0 },
    { tsMs: 2, cumMicro: 400, peakMicro: 400, drawdownMicro: 0 },
    { tsMs: 3, cumMicro: -200, peakMicro: 400, drawdownMicro: 600 },
  ];

  it("draws the same curve twice: the PnL line and the distance below its high-water mark", () => {
    const out = curvePoints(curve, 300, 100);
    expect(out.pnl.split(" ").length).toBe(3);
    expect(out.drawdown.split(" ").length).toBe(6);          // peak line out, curve line back
    expect(out.min).toBe(-200);
    expect(out.max).toBe(400);
  });

  it("is empty rather than broken for a trader with no history", () => {
    expect(curvePoints([], 300, 100)).toEqual({ pnl: "", drawdown: "", min: 0, max: 0 });
  });
});
