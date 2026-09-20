/**
 * The board page's cells, and the two rules that keep a public ranking honest.
 *
 *  1. **Every cell is a string the API printed.** The tests below feed rows that carry BOTH the integer and the
 *     printed form and assert the printed form is what the cell shows — the client is not allowed a second
 *     opinion about rounding, because the row on the page and the row in the share text have to be the same
 *     characters.
 *  2. **A gated win rate is a sentence.** Below the sample gate the cell carries the API's own `sampleNote`
 *     rather than a number, a dash or an empty cell: `—` reads as zero, and a zero win rate is the one reading
 *     that is definitely wrong.
 */
import { describe, expect, it } from "vitest";
import { boardFindings, isIndexable, rowCells, rowNotes } from "./rows";
import type { PublicBoardRow } from "./wire";

function row(over: Record<string, unknown> = {}): PublicBoardRow {
  return {
    rank: 47,
    anon: "w_57c7dcc029",
    handle: "deep_book",
    state: "ranked",
    settledMarkets: 31,
    labels: [],
    sampleNote: "",
    insufficientSample: false,
    realised: "41200",
    realisedMicro: 41_200_000_000,
    trimmed: "39800",
    drawdown: "1200",
    maxDrawdownMicro: 1_200_000_000,
    verifiedVolume: "1.2M",
    verifiedVolumeMicro: 1_200_000_000,
    winRateBps: 8_333,
    winRate: "83.33%",
    scoreBps: 5_625,
    scoreText: "5625 bps",
    ...over,
  } as unknown as PublicBoardRow;
}

describe("the public board's cells", () => {
  it("shows the row's own printed strings and never re-formats them", () => {
    const cells = rowCells(row(), "risk_adjusted");
    const by = Object.fromEntries(cells.map((c) => [c.key, c.value]));
    expect(by.rank).toBe("47");
    expect(by.trader).toBe("deep_book");
    expect(by.settled).toBe("31");
    expect(by.drawdown).toBe("1200");
    expect(by.winRate).toBe("83.33%");
    expect(by.score).toBe("5625 bps");
  });

  it("names the field each board ranks on", () => {
    expect(rowCells(row(), "volume").find((c) => c.key === "score")?.label).toBe("verified volume");
    expect(rowCells(row(), "copied").find((c) => c.key === "score")?.label).toBe("copiers");
    expect(rowCells(row(), "win_rate").find((c) => c.key === "score")?.value).toBe("83.33%");
  });

  it("prints the refusal instead of a number when the sample gate is closed", () => {
    const cells = rowCells(row({ insufficientSample: true, winRateBps: null, winRate: undefined,
                                 sampleNote: "9 settled markets; this board needs 20" }), "risk_adjusted");
    const win = cells.find((c) => c.key === "winRate");
    expect(win?.value).toBe("9 settled markets; this board needs 20");
    expect(win?.wide).toBe(true);
  });

  it("carries a wallet's labels with their rule, and falls back to the pseudonym", () => {
    const notes = rowNotes(row({
      labels: [{ label: "lucky gambler", rule: "the best market is over half the profit" }],
      washNote: "8,000 of wash volume subtracted",
      handle: null,
    }));
    expect(notes[0]).toBe("lucky gambler: the best market is over half the profit");
    expect(notes[1]).toBe("8,000 of wash volume subtracted");
    expect(rowCells(row({ handle: null }), "risk_adjusted").find((c) => c.key === "trader")?.value).toBe("w_57c7dcc029");
  });

  it("reads indexability from the API rather than deciding it", () => {
    expect(isIndexable("index, follow")).toBe(true);
    expect(isIndexable("noindex, follow")).toBe(false);
    expect(isIndexable(undefined)).toBe(false);
  });

  it("states the board's own integrity counts, and says nothing when there is nothing to say", () => {
    expect(boardFindings({ excludedTotal: 3, blewUpCount: 2, provisionalCount: 11 }).length).toBe(3);
    expect(boardFindings({})).toEqual([]);
  });
});
