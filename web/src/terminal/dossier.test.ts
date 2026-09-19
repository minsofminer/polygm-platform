/**
 * D3's rules: the gate that refuses to print a win rate, the window switcher that recomputes the whole set, the
 * drawdown that cannot be separated from the curve, the label that cannot appear without its rule, and the header
 * that states what we do not know.
 */
import { describe, expect, it } from "vitest";
import {
  DOSSET_WINDOWS,
  accountAgeText,
  behaviourRows,
  confidenceText,
  curvePoints,
  drawdownSentence,
  fillVerdict,
  filterHistory,
  historyMarkets,
  holdText,
  identityNote,
  labelSentence,
  marketHref,
  metricRows,
  methodologyLines,
  shareText,
  windowChoices,
  winRateText,
} from "./dossier";
import type { CurvePoint, LabelFact, MetricWindow, TraderDossier } from "./wire";

const MICRO = 1_000_000;

function window_(over: Partial<MetricWindow> = {}): MetricWindow {
  return {
    fills: 120,
    resolvedMarkets: 40,
    wins: 24,
    winRateBps: 6_000,
    insufficientSample: false,
    sampleNote: "",
    sampleGate: 20,
    volumeMicro: 12_000 * MICRO,
    realisedMicro: 340 * MICRO,
    unrealisedMicro: -12 * MICRO,
    bestMicro: 200 * MICRO,
    worstMicro: -80 * MICRO,
    maxDrawdownMicro: 150 * MICRO,
    avgHoldMs: 4 * 3_600_000,
    medianHoldMs: 2 * 3_600_000,
    openFills: 6,
    matchedPositions: 3,
    distinctMarkets: 12,
    categories: 4,
    asOfMs: 1,
    ...over,
  };
}

describe("the win rate, and the gate in front of it", () => {
  it("prints a percentage only when the window is gated", () => {
    const out = winRateText(window_(), 20);
    expect(out.value).toBe("60%");
    expect(out.note).toContain("40 settled markets");
    expect(out.tone).toBe("good");
  });

  it("refuses to print a number under the gate, and shows the server's sentence instead", () => {
    const out = winRateText(
      window_({ winRateBps: null, insufficientSample: true, resolvedMarkets: 4, sampleNote: "insufficient sample: 4 settled markets; a win rate needs 20" }),
      20,
    );
    expect(out.value).toBe("insufficient sample");
    expect(out.value).not.toContain("%");
    expect(out.note).toContain("needs 20");
    expect(out.tone).toBe("unknown");
  });

  it("writes its own sentence when the server sent a null without one, using the gate it was given", () => {
    const out = winRateText(window_({ winRateBps: null, insufficientSample: true, resolvedMarkets: 3, sampleNote: "" }), 20);
    expect(out.note).toContain("needs 20");
    expect(out.note).toContain("3");
  });

  it("renders hundredths without a float: 7631 bps is 76.31%", () => {
    expect(winRateText(window_({ winRateBps: 7_631 }), 20).value).toBe("76.31%");
    expect(winRateText(window_({ winRateBps: 5_000 }), 20).value).toBe("50%");
    expect(winRateText(window_({ winRateBps: 0 }), 20).value).toBe("0%");
  });
});

describe("the window switcher recomputes the whole metric set", () => {
  it("emits one row per metric, and the rows change with the window", () => {
    const seven = metricRows(window_({ realisedMicro: 10 * MICRO, maxDrawdownMicro: 4 * MICRO }), 20);
    const ninety = metricRows(window_({ realisedMicro: -60 * MICRO, maxDrawdownMicro: 90 * MICRO }), 20);
    expect(seven.map((r) => r.id)).toEqual(ninety.map((r) => r.id));
    expect(seven.find((r) => r.id === "realised")?.value).not.toBe(ninety.find((r) => r.id === "realised")?.value);
    expect(ninety.find((r) => r.id === "realised")?.tone).toBe("bad");
    expect(seven.find((r) => r.id === "maxDrawdown")?.tone).toBe("bad");
    // Every metric that is money goes through the formatter, so no row can be a raw number.
    for (const row of ninety) expect(row.value).not.toMatch(/^-?\d+\.\d{3,}$/);
  });

  it("says so rather than inventing values when a window has no metrics", () => {
    expect(metricRows(undefined, 20)).toEqual([{ id: "unavailable", value: "—", note: "", tone: "unknown" }]);
  });

  it("lists the server's windows in the product's order, and falls back to all four", () => {
    expect(windowChoices({ windows: ["all", "7d", "90d", "30d"] })).toEqual(["7d", "30d", "90d", "all"]);
    expect(windowChoices({ windows: [] })).toEqual([...DOSSET_WINDOWS]);
    expect(windowChoices({ windows: ["5m", "30d"] })).toEqual(["30d"]);
  });
});

describe("the curve never travels without its drawdown", () => {
  const curve: CurvePoint[] = [
    { tsMs: 1, cumMicro: 100, peakMicro: 100, drawdownMicro: 0 },
    { tsMs: 2, cumMicro: 400, peakMicro: 400, drawdownMicro: 0 },
    { tsMs: 3, cumMicro: -200, peakMicro: 400, drawdownMicro: 600 },
  ];

  it("draws both series from one walk over the same points", () => {
    const geometry = curvePoints(curve, 300, 100);
    expect(geometry.pnl.split(" ")).toHaveLength(3);
    expect(geometry.drawdown.split(" ")).toHaveLength(6);      // peak line out, curve line back
    expect(geometry.min).toBe(-200);
    expect(geometry.max).toBe(400);
  });

  it("states the worst drawdown in money, and says when there was none", () => {
    expect(drawdownSentence(curve, 600 * MICRO)).toContain("worst drawdown");
    expect(drawdownSentence(curve, 600 * MICRO)).toContain("high-water mark");
    const flat: CurvePoint[] = [{ tsMs: 1, cumMicro: 10, peakMicro: 10, drawdownMicro: 0 }];
    expect(drawdownSentence(flat, 0)).toContain("no drawdown in this window");
  });

  it("is empty rather than broken for a trader with no history", () => {
    expect(curvePoints([], 300, 100)).toEqual({ pnl: "", drawdown: "", min: 0, max: 0 });
  });
});

describe("behaviour labels", () => {
  const fact: LabelFact = {
    label: "insider-suspect",
    confidence: 780,
    publishable: true,
    rule: "entered within 12h of a resolution whose outcome moved the price by more than 20c",
    disclaimer: "looks like information, but may be luck or a hedge; not an accusation",
  };

  it("drops a label that arrived without its rule or disclaimer rather than showing a bare accusation", () => {
    expect(behaviourRows([fact])).toHaveLength(1);
    expect(behaviourRows([{ ...fact, rule: "" }])).toHaveLength(0);
    expect(behaviourRows([{ ...fact, disclaimer: "" }])).toHaveLength(0);
    expect(behaviourRows([{ ...fact, publishable: false }])).toHaveLength(0);
    expect(behaviourRows(undefined)).toEqual([]);
  });

  it("carries both halves of the sentence, and the confidence as a whole percentage", () => {
    expect(labelSentence(fact)).toContain("entered within 12h");
    expect(labelSentence(fact)).toContain("not an accusation");
    expect(confidenceText(780)).toBe("78%");
    expect(confidenceText(1_000)).toBe("100%");
  });
});

describe("the trade history", () => {
  const fills = [
    { tsMs: 5, marketId: "0xM1", question: "Fed?", side: "BUY", outcome: "Yes", price: "0.5", tick: "0.01", tokenId: "t1", resolved: true, winner: true, realisedMicro: 10 },
    { tsMs: 4, marketId: "0xM1", question: "Fed?", side: "SELL", outcome: "No", price: "0.4", tick: "0.01", tokenId: "t2", resolved: false, winner: null, realisedMicro: 0 },
    { tsMs: 3, marketId: "0xM2", question: "Rain?", side: "BUY", outcome: "Yes", price: "0.2", tick: "0.01", tokenId: "t3", resolved: true, winner: false, realisedMicro: -5 },
  ] as unknown as TraderDossier["fills"];

  it("filters by side, market and settlement — the last one because an open row has no result", () => {
    expect(filterHistory(fills, { side: "BUY", market: "", outcome: "", resolvedOnly: false })).toHaveLength(2);
    expect(filterHistory(fills, { side: "", market: "0xM1", outcome: "", resolvedOnly: false })).toHaveLength(2);
    expect(filterHistory(fills, { side: "", market: "", outcome: "", resolvedOnly: true })).toHaveLength(2);
  });

  it("offers each market once, in the order the history mentions it", () => {
    expect(historyMarkets(fills)).toEqual([
      { marketId: "0xM1", question: "Fed?" },
      { marketId: "0xM2", question: "Rain?" },
    ]);
  });

  it("calls an unsettled fill open rather than break-even", () => {
    const [won, open, lost] = fills;
    expect(fillVerdict(open!).text).toBe("open");
    expect(fillVerdict(open!)).toEqual({ text: "open", tone: "unknown", micro: null });
    expect(fillVerdict(won!).text).toBe("won");
    expect(fillVerdict(lost!).text).toBe("lost");
    expect(fillVerdict(lost!).micro).toBe(-5);
  });

  it("links a row to the market WITH the fill, so the destination can highlight it", () => {
    const href = marketHref({ marketId: "0xM/1", tokenId: "t1", tsMs: 42 });
    expect(href.startsWith("/market/")).toBe(true);
    expect(href).toContain("fill=");
    expect(href).toContain("42");
  });
});

describe("the header states what is known and what is not", () => {
  it("never offers an explorer link, and says why", () => {
    const note = identityNote("w_5c037b2ab8");
    expect(note).toContain("w_5c037b2ab8");
    expect(note).toContain("does not resolve");
    expect(note).toContain("no explorer link");
  });

  it("calls the first fill we hold what it is, not the wallet's age", () => {
    const now = 1_700_000_000_000;
    const out = accountAgeText(now - 45 * 86_400_000, now);
    expect(out.value).toBe("45 days");
    expect(out.note).toContain("not the wallet's age");
    expect(accountAgeText(now - 200 * 86_400_000, now).value).toBe("6 months");
    expect(accountAgeText(null, now).note).toContain("no fills");
  });

  it("renders a category share from basis points without a float", () => {
    expect(shareText(2_350)).toBe("23.50%");
    expect(shareText(4_000)).toBe("40%");
  });

  it("keeps the methodology lines the screen promises to show", () => {
    const lines = methodologyLines({ path: "/docs/methodology", winRate: "wins over settled markets", drawdown: "peak minus cumulative" });
    expect(lines.length).toBeGreaterThanOrEqual(2);
    expect(lines.join(" ")).toContain("peak minus cumulative");
    expect(methodologyLines(undefined)).toEqual([]);
  });

  it("formats a hold in minutes, hours and days", () => {
    expect(holdText(30 * 60_000)).toBe("30m");
    expect(holdText(5 * 3_600_000)).toBe("5h");
    expect(holdText(72 * 3_600_000)).toBe("3d");
    expect(holdText(0)).toBe("—");
  });
});
