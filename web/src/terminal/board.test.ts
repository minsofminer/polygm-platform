/**
 * D3 · the leaderboard surfaces' decisions, unit-tested.
 *
 * These tests exist because every rule in `board.ts` fails *quietly* in a browser: a rank shown without its board
 * still renders, an empty sparkline drawn as a flat line still renders, and a comparison that crowns the first
 * wallet the user clicked looks exactly like one that crowned the leader. Each of those is a sentence, so each of
 * them is testable here.
 */
import { describe, expect, it } from "vitest";
import {
  badgeText,
  comparisonCells,
  comparisonVerdict,
  fieldText,
  followAction,
  followStateText,
  gapSentence,
  historySentence,
  percentileText,
  refusalLines,
  sparkPath,
  stateText,
  type Standing,
} from "./board";

function standing(over: Partial<Standing> = {}): Standing {
  return {
    anon: "w_a945dde868",
    board: "risk_adjusted",
    label: "Risk-adjusted PnL",
    window: "30d",
    state: "ranked",
    rank: 47,
    rankedTotal: 64,
    percentileBps: 7344,
    orderField: "scoreBps",
    orderUnits: "bps",
    rankBadge: { rank: 47, rankedTotal: 64, text: "#47" },
    above: { anon: "w_57c7dcc029" },
    below: { anon: "w_a23a3310e7" },
    gap: {
      rankAbove: 46,
      anonAbove: "w_57c7dcc029",
      field: "scoreBps",
      units: "bps",
      value: 5000,
      valueAbove: 5625,
      delta: 625,
      toPass: 5626,
      note: "",
    },
    reasons: [],
    note: "",
    history: {
      days: 30,
      points: [
        { tsMs: 1, rank: 51 },
        { tsMs: 2, rank: 49 },
        { tsMs: 3, rank: 47 },
      ],
      snapshots: 3,
      latestRank: 47,
      bestRank: 47,
      worstRank: 51,
      delta: -4,
      note: "",
    },
    ...over,
  };
}

describe("a rank is never shown alone", () => {
  it("carries the board's size, because #47 of 64 and #47 of 9,000 are different sentences", () => {
    expect(badgeText(standing())).toBe("#47 of 64");
    expect(badgeText(standing({ rankBadge: { rank: 1, rankedTotal: 3, text: "#1" } }))).toBe("#1 of 3");
  });

  it("says so when there is no placing at all, rather than rendering #0", () => {
    expect(badgeText(standing({ rankBadge: null }))).toBe("not ranked");
  });

  it("reads the percentile off the server's number instead of re-deriving it", () => {
    expect(percentileText(standing())).toBe("top 73.44% of 64");
    expect(percentileText(standing({ percentileBps: 1000, rank: 1 }))).toBe("top 10% of 64");
    expect(percentileText(standing({ percentileBps: null, rank: null }))).toBe("");
  });
});

describe("the gap is in the board's own field", () => {
  it("states the units the server said, not the ones the number suggests", () => {
    expect(fieldText(625, "bps")).toBe("625 bps");
    expect(fieldText(19_200_000_000, "micro")).toBe("19200 USDC");
    expect(fieldText(3, "count")).toBe("3 copiers");
    expect(fieldText(1, "count")).toBe("1 copier");
  });

  it("says what would move the row, because toPass is above + 1 and a tie does not pass", () => {
    const text = gapSentence(standing().gap, "bps");
    expect(text).toContain("625 bps behind w_57c7dcc029");
    expect(text).toContain("5625 bps");
    expect(text).toContain("5626 bps would pass them");
  });

  it("does not invent a gap when there is none", () => {
    expect(gapSentence(null, "bps")).toBe("");
    expect(gapSentence({ ...standing().gap!, delta: 0 }, "bps")).toContain("level with");
  });
});

describe("the empty history is empty", () => {
  it("returns no points at all, so nothing can draw a flat line at rank zero", () => {
    expect(sparkPath([])).toEqual([]);
    expect(sparkPath(undefined)).toEqual([]);
  });

  it("returns ONE point for a single snapshot rather than a zero-length line", () => {
    expect(sparkPath([{ tsMs: 1, rank: 12 }])).toEqual([{ x: 0, y: 0 }]);
  });

  it("puts rank 1 at the top, so a falling line is a trader rising", () => {
    const pts = sparkPath([
      { tsMs: 1, rank: 51 },
      { tsMs: 2, rank: 47 },
    ]);
    expect(pts[0]!.y).toBeGreaterThan(pts[1]!.y);
    expect(pts[0]!.x).toBe(0);
    expect(pts[1]!.x).toBe(120);
  });

  it("puts the two facts a line cannot say into words", () => {
    expect(historySentence(standing())).toContain("3 snapshots over 30 days");
    expect(historySentence(standing())).toContain("up 4");
    const none = standing({ history: { days: 30, points: [], snapshots: 0, latestRank: null, bestRank: null, worstRank: null, delta: null, note: "" } });
    expect(historySentence(none)).toContain("no history yet");
  });
});

describe("a refusal is a sentence, and a state is too", () => {
  it("prints the numbers the server refused the wallet with", () => {
    const out = refusalLines(standing({ state: "unranked", rank: null, reasons: ["9 settled markets; this board needs 20"] }));
    expect(out).toEqual(["9 settled markets; this board needs 20"]);
    expect(refusalLines(standing())).toEqual([]);
  });

  it("says what blew_up means rather than colouring a row red", () => {
    expect(stateText(standing({ state: "blew_up" }))).toContain("still on the board");
    expect(stateText(standing({ state: "provisional" }))).toContain("labelled, not hidden");
    expect(stateText(standing({ state: "unranked" }))).toBe("not ranked yet");
    expect(stateText(standing())).toBe("ranked");
  });
});

describe("the comparison crowns the board's leader, not the first click", () => {
  it("reads the verdict off the pairwise sentences", () => {
    const text = comparisonVerdict({
      rows: [{ anon: "w_second" }, { anon: "w_first" }],
      order: [
        { a: "w_second", b: "w_first", aAbove: false, why: "w_first is rank 1 with scoreBps = 900" },
        { a: "w_second", b: "w_third", aAbove: true, why: "w_second is rank 2" },
      ],
    });
    expect(text).toContain("w_second is ahead");
    expect(text).toContain("w_first is rank 1");
  });

  it("falls back to the server's own sentence when no pair agrees on a leader", () => {
    expect(
      comparisonVerdict({ order: [], verdict: "w_a leads this comparison on risk-adjusted pnl at 5000 bps" }),
    ).toContain("w_a leads");
  });

  it("shows a PnL cell beside its drawdown, always", () => {
    const labels = comparisonCells({ rank: 12, realised: "11800", drawdown: "1200", settledMarkets: 48, verifiedVolume: "19200" }).map(
      (c) => c.label,
    );
    expect(labels).toContain("realised PnL");
    expect(labels).toContain("drawdown");
    expect(labels.indexOf("realised PnL")).toBeLessThan(labels.indexOf("drawdown"));
  });

  it("shows the best-trade share only when the row has one", () => {
    const withShare = comparisonCells({ bestTradeShareBps: 5529 });
    expect(withShare.some((c) => c.label === "best trade" && c.value === "55.29% of realised")).toBe(true);
    expect(comparisonCells({}).some((c) => c.label === "best trade")).toBe(false);
  });
});

describe("a follow is a watch", () => {
  it("labels the button with the action the user will take", () => {
    expect(followAction(true)).toBe("unfollow");
    expect(followAction(false)).toBe("follow");
  });

  it("says why a followed wallet is not on the board", () => {
    expect(followStateText({ state: "ranked", rank: 12 })).toBe("#12");
    expect(followStateText({ state: "unranked", rank: null, reasons: ["9 settled markets"] })).toBe("off the board");
    expect(followStateText({ state: "absent", rank: null })).toBe("no activity we can rank");
  });
});
