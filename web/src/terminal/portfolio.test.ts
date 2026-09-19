/**
 * D6's rules, tested where they are stated: a mark we do not have is not a zero, the drawdown ships beside the
 * equity, the negRisk legs are one bet, and the CSV cannot disagree with the table it was downloaded from.
 */
import { describe, expect, it } from "vitest";
import {
  benchmarkLabel,
  csvFilename,
  csvText,
  curveGeometry,
  emptyTarget,
  endsInText,
  exitHref,
  exposureNote,
  hasPositions,
  markText,
  moneyText,
  mutuallyExclusive,
  orderRows,
  shareText,
  totalRows,
  unresolvedText,
  unrealisedKnown,
} from "./portfolio";
import type { Portfolio, PortfolioPosition } from "./wire";

const MICRO = 1_000_000;

function position(over: Partial<PortfolioPosition> = {}): PortfolioPosition {
  return {
    tokenId: "t1",
    marketId: "0xM1",
    marketSlug: "m1",
    question: "Will it rain?",
    outcome: "Yes",
    category: "Weather",
    size: "120.00",
    avgEntry: "0.4",
    mark: "0.55",
    costBasisMicro: 48 * MICRO,
    valueMicro: 66 * MICRO,
    unrealisedMicro: 18 * MICRO,
    unrealisedBps: 3_750,
    onTick: true,
    endsInMs: 0,
    markSource: "last_fill",
    shareOfPortfolioBps: 6_600,
    ...over,
  };
}

function portfolio(over: Partial<Portfolio> = {}): Portfolio {
  return {
    positions: [position()],
    negRiskGroups: [],
    orders: [
      {
        intentId: "i1",
        marketId: "0xM1",
        state: "filled",
        reason: null,
        shares: "120.00",
        price: "0.4000",
        createdMs: 1_700_000_000_000,
        notionalMicro: 48 * MICRO,
        unknownLifecycle: false,
      },
    ],
    unknownLifecycle: [],
    pnlCurve: [
      { tsMs: 1_700_000_000_000, cumMicro: 0, peakMicro: 0, drawdownMicro: 0 },
      { tsMs: 1_700_086_400_000, cumMicro: 12 * MICRO, peakMicro: 12 * MICRO, drawdownMicro: 0 },
      { tsMs: 1_700_172_800_000, cumMicro: -3 * MICRO, peakMicro: 12 * MICRO, drawdownMicro: 15 * MICRO },
    ],
    maxDrawdownMicro: 15 * MICRO,
    totals: { valueMicro: 66 * MICRO, cashMicro: 20 * MICRO, equityMicro: 86 * MICRO, unrealisedMicro: 18 * MICRO, costBasisMicro: 48 * MICRO },
    benchmark: { kind: "hold_pusd", valueMicro: 100 * MICRO, rateBps: 0, note: "holding pUSD: the deposited cash at 1.0000, unchanged." },
    csv: { columns: ["intentId", "marketId", "state", "shares", "price", "notionalMicro", "createdMs"], note: "columns are in micro-USDC where the name says Micro" },
    emptyState: "/markets - browse markets and place a first order to start a portfolio",
    asOf: 1_700_000_000_000,
    staleAfter: 1_700_000_000_000,
    ...over,
  };
}

describe("a mark we do not have", () => {
  it("is not a zero: the position is unknowable, not worthless", () => {
    const unknown = position({ markSource: "unknown", mark: "0" });
    expect(unrealisedKnown(unknown)).toBe(false);
    const mark = markText(unknown);
    expect(mark.text).toBe("no mark");
    expect(mark.tone).toBe("unknown");
    expect(mark.rule).toContain("no price to mark it at");
    // The rule has to say the LOSS is unknown, not just that a price is missing: this is the sentence that
    // stops "$0.00" from reading as a total loss.
    expect(mark.rule).toContain("not knowable");
  });

  it("is a real mark when the market has traded", () => {
    expect(unrealisedKnown(position())).toBe(true);
    expect(markText(position()).rule).toContain("last fill");
  });

  it("refuses a mark of exactly zero from a source that claims one", () => {
    // `markSource: "last_fill"` with a 0 mark is a server bug or a market that traded at 0 — either way the
    // PnL derived from it is not a number this table gets to print as real.
    expect(unrealisedKnown(position({ mark: "0" }))).toBe(false);
  });
});

describe("drawdown beside the equity it belongs to", () => {
  it("is a row in the totals block, not a footnote", () => {
    const rows = totalRows(portfolio().totals, 15 * MICRO);
    const ids = rows.map((r) => r.id);
    expect(ids).toContain("drawdown");
    expect(ids).toContain("equity");
    const drawdown = rows.find((r) => r.id === "drawdown");
    expect(drawdown?.micro).toBe(15 * MICRO);
    expect(drawdown?.kind).toBe("pnl");
  });

  it("draws the region from the peak the curve fell from, never from zero", () => {
    const geo = curveGeometry(portfolio().pnlCurve, 100 * MICRO);
    // Point strings, the same shape the dossier draws: `x,y x,y …` with the drawdown walking the peaks.
    expect(geo.pnl.split(" ").length).toBe(portfolio().pnlCurve.length);
    expect(geo.drawdown.split(" ").length).toBe(portfolio().pnlCurve.length * 2);
    expect(Number.isFinite(geo.zeroY)).toBe(true);
    expect(geo.benchmarkY).not.toBeNull();
    // No float ever reaches the path: every coordinate is a whole number of viewBox units.
    for (const pair of `${geo.pnl} ${geo.drawdown}`.split(" ")) {
      expect(pair).toMatch(/^-?\d+,-?\d+$/);
    }
  });

  it("declines to draw a benchmark when nothing is deposited", () => {
    const geo = curveGeometry(portfolio().pnlCurve, null);
    expect(geo.benchmarkY).toBeNull();
    expect(benchmarkLabel(portfolio())).toContain("holding pUSD");
  });
});

describe("negRisk legs", () => {
  const group = { eventId: "e1", eventTitle: "Who wins?", legs: 3, exposureMicro: 300 * MICRO, sumValuesMicro: 180 * MICRO, maxPayoutMicro: 120 * MICRO, note: "" };

  it("are one bet, and the sentence says how many legs can pay", () => {
    expect(mutuallyExclusive(group)).toBe(true);
    const sentence = exposureNote(group);
    expect(sentence).toContain("3 legs");
    expect(sentence).toContain("$ 300.00");
    expect(sentence).toContain("at most $ 120.00");
  });

  it("does not call a single leg an event risk", () => {
    expect(mutuallyExclusive({ ...group, legs: 1 })).toBe(false);
  });
});

describe("the order history", () => {
  it("keeps the rows we could not resolve, and says why in text", () => {
    const p = portfolio({
      unknownLifecycle: [
        { venueOrderId: "v9", intentId: "i9", state: "unknown", reason: "venue timeout", showAsWorking: false, atMs: 1_700_200_000_000, unknownLifecycle: true },
      ],
    });
    const rows = orderRows(p);
    const unresolved = rows.find((r) => r.unresolved);
    expect(unresolved).toBeDefined();
    expect(unresolvedText(unresolved!)).toContain("we do not model");
    expect(unresolvedText(unresolved!)).toContain("venue timeout");
    // Newest first, and the unresolved row is the newest here — a hidden row cannot be the one you read.
    expect(rows[0]?.unresolved).toBe(true);
  });
});

describe("the tax export", () => {
  it("takes its columns from the payload, so the file and the table are one document", () => {
    const text = csvText(portfolio());
    const lines = text.trim().split("\n");
    expect(lines[0]).toBe("intentId,marketId,state,shares,price,notionalMicro,createdMs");
    // The unit line under the header: a tax preparer should not have to guess whether notionalMicro is dollars.
    expect(lines[1]).toContain("integer micro-USDC");
    expect(lines[1]?.startsWith(",")).toBe(true);
    expect(lines[2]).toContain("i1,0xM1,filled,120.00,0.4000,48000000,1700000000000");
  });

  it("quotes a value that would break the file", () => {
    const p = portfolio();
    p.orders[0]!.marketId = 'has"comma,and quote';
    expect(csvText(p)).toContain('"has""comma,and quote"');
  });

  it("names the file after the day it was taken", () => {
    expect(csvFilename(Date.UTC(2026, 8, 19, 5, 0, 0))).toBe("polygm-portfolio-20260919.csv");
  });
});

describe("small things a user reads without noticing", () => {
  it("states a share of the book as a percentage of a percentage", () => {
    expect(shareText(6_600)).toBe("66%");
    expect(shareText(1_250)).toBe("12.50%");
    expect(shareText(0)).toBe("0%");
  });

  it("keeps the exit on the market screen, where the book is", () => {
    expect(exitHref(position())).toBe("/market/0xM1");
  });

  it("counts time left the way a trader reads it", () => {
    const now = 1_700_000_000_000;
    expect(endsInText(now + 30 * 60_000, now)).toBe("30m");
    expect(endsInText(now + 5 * 3_600_000, now)).toBe("5h");
    expect(endsInText(now + 50 * 3_600_000, now)).toBe("2d 2h");
    expect(endsInText(now - 1, now)).toBe("ended — awaiting resolution");
    expect(endsInText(0, now)).toBe("no end date published");
  });

  it("points the empty state at the markets page the wire named", () => {
    expect(emptyTarget(portfolio())).toBe("/markets");
    expect(emptyTarget(portfolio({ emptyState: "no idea" }))).toBe("/markets");
    expect(hasPositions(portfolio({ positions: [] }))).toBe(false);
  });

  it("writes money through the formatter, never as a raw number", () => {
    // Truncated toward zero, not rounded: micro-USDC below a cent is not a cent, and rounding a balance up is
    // how a screen tells a user they have more money than they do.
    // The grouping separator is U+2009 THIN SPACE, not U+0020: assert the glyph the formatter actually emits,
    // or an assertion passes on a string a user would see differently.
    expect(moneyText(1_234_567_890)).toBe("$ 1\u2009234.56");
    expect(moneyText(999_999)).toBe("$ 0.99");
    expect(moneyText(-400 * MICRO, true)).toBe("-$ 400.00");
  });
});
