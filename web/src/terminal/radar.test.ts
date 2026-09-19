/**
 * D5's rules as tests. The four the phase cares about:
 *
 *  * the scan is capped at ten markets and refuses the eleventh instead of dropping one;
 *  * the quota line is the API's own numbers, and a cached scan says it was free;
 *  * the profit ranking's exclusions are returned and stated, with their reason;
 *  * a label's rule and disclaimer are rendered as text, and a label missing either is dropped.
 */
import { describe, expect, it } from "vitest";
import {
  MAX_SCAN_MARKETS,
  labelSentence,
  matchedChips,
  moneyText,
  publishableLabels,
  quotaText,
  rankingMeta,
  rowActions,
  rowsFor,
  scanProblems,
  toggleMarket,
  unrankedNote,
  unrankedRows,
  winRateText,
} from "./radar";
import type { RadarResult, RadarRow } from "./wire";

const MICRO = 1_000_000;

function row(over: Partial<RadarRow> = {}): RadarRow {
  return {
    anonWallet: "w_5c037b2ab8",
    matched: [{ marketId: "0xM1", question: "Fed cuts in March?" }],
    boughtMicro: 400 * MICRO,
    soldMicro: 120 * MICRO,
    realisedMicro: 55 * MICRO,
    winRateBps: 5_714,
    insufficientSample: false,
    labels: [{ label: "whale", confidence: 900, publishable: true, rule: "notional >= the market's line", disclaimer: "size is not intent" }],
    rank: 1,
    reason: "bought $400 and sold $120 across 1 of the markets you picked",
    ...over,
  };
}

function result(over: Partial<RadarResult> = {}): RadarResult {
  return {
    items: [row()],
    ranking: "active",
    markets: ["0xM1"],
    rankings: { active: [row()], profit: [row({ rank: 1, realisedMicro: 900 * MICRO })], earliest: [], overlap: [] },
    rankingsMeta: [
      { id: "active", label: "most active" } as RadarResult["rankingsMeta"][number],
      { id: "profit", label: "highest profit" } as RadarResult["rankingsMeta"][number],
    ],
    unranked: [{ anonWallet: "w_small", fills: 2, markets: ["0xM1"], realisedMicro: 40 * MICRO, winRateBps: null, insufficientSample: true, reason: "2 settled markets is not a profit record" }],
    scanned: 12,
    sampleGate: 20,
    quota: { plan: "free", usedToday: 2, perDay: 20, cached: false, jobId: null, note: "this scan counted against today's allowance" },
    costNote: "a scan reads the durable fill log, not the venue: it is the cheap half of the radar, and the cache is what makes a repeated scan free",
    ...over,
  };
}

describe("the scan's input", () => {
  it("caps at ten markets and says why rather than dropping one", () => {
    let selected: string[] = [];
    for (let i = 0; i < MAX_SCAN_MARKETS; i += 1) selected = toggleMarket(selected, `0xM${i}`).next;
    expect(selected.length).toBe(MAX_SCAN_MARKETS);
    const refused = toggleMarket(selected, "0xM10");
    expect(refused.next).toEqual(selected);
    expect(refused.refused).toContain("10 markets");
    expect(refused.refused).toContain("did not ask");
    // Picking an already-picked market removes it — the same call in both directions.
    expect(toggleMarket(selected, "0xM0").next.length).toBe(MAX_SCAN_MARKETS - 1);
  });

  it("will not run a scan with nothing picked", () => {
    expect(scanProblems([]).length).toBe(1);
    expect(scanProblems(["0xM1"])).toEqual([]);
  });
});

describe("the cost", () => {
  it("counts the scan before it is spent, and quotes the API's own note", () => {
    const text = quotaText(result());
    expect(text).toContain("3 of 20 scans used today");
    expect(text).toContain("this scan counted against today's allowance");
  });

  it("says a cached scan is free", () => {
    const text = quotaText(result({ quota: { plan: "free", usedToday: 2, perDay: 20, cached: true, jobId: null, note: "this scan came from the cache and cost you nothing" } }));
    expect(text).toContain("2 of 20 scans used today");
    expect(text).toContain("cost you nothing");
    expect(text).not.toContain("3 of 20");
  });

  it("has a sentence before the first scan too", () => {
    expect(quotaText(null)).toContain("one of your daily allowance");
  });
});

describe("the sample gate", () => {
  it("lists the excluded wallets with their reasons", () => {
    expect(unrankedRows(result()).length).toBe(1);
    expect(unrankedNote(result())).toContain("20 settled markets is not a profit record");
    expect(unrankedNote(result())).toContain("NOT in the profit ranking");
  });

  it("says nothing when there is nothing to say", () => {
    expect(unrankedNote(result({ unranked: [] }))).toBe("");
    expect(unrankedNote(null)).toBe("");
  });

  it("prints no win rate below the gate", () => {
    expect(winRateText({ winRateBps: null, insufficientSample: true })).toBe("insufficient sample");
    expect(winRateText({ winRateBps: 5_714, insufficientSample: true })).toBe("insufficient sample");
    expect(winRateText({ winRateBps: 5_714, insufficientSample: false })).toBe("57.14%");
  });
});

describe("the four rankings", () => {
  it("come from one payload, so switching tabs costs nothing", () => {
    expect(rowsFor(result(), "profit")[0]?.realisedMicro).toBe(900 * MICRO);
    expect(rowsFor(result(), "earliest")).toEqual([]);
    expect(rowsFor(null, "active")).toEqual([]);
  });

  it("carry the API's own question above the list", () => {
    const meta = rankingMeta(result(), "profit");
    expect(meta?.label).toBe("highest profit");
  });
});

describe("labels and rows", () => {
  it("render a label as its rule and its disclaimer, and drop one missing either", () => {
    expect(labelSentence(row().labels[0]!)).toContain("notional >= the market's line");
    expect(labelSentence(row().labels[0]!)).toContain("size is not intent");
    const incomplete = [
      { label: "whale", confidence: 900, publishable: true, rule: "", disclaimer: "size is not intent" },
      { label: "cluster", confidence: 500, publishable: false, rule: "r", disclaimer: "d" },
      { label: "smart_money", confidence: 800, publishable: true, rule: "r", disclaimer: "d" },
    ];
    const kept = publishableLabels({ labels: incomplete });
    expect(kept.map((l) => l.label)).toEqual(["smart_money"]);
  });

  it("chips the matched markets and links each one", () => {
    const chips = matchedChips(row());
    expect(chips).toEqual([{ marketId: "0xM1", label: "Fed cuts in March?" }]);
    expect(matchedChips(row({ matched: [{ marketId: "0xM2", question: "" }] }))[0]?.label).toBe("0xM2");
  });

  it("gives every row the same four actions, with the copy one in dry-run", () => {
    const actions = rowActions(row());
    expect(actions.map((a) => a.id).sort()).toEqual(["copy", "follow", "open", "watchlist"]);
    expect(actions.find((a) => a.id === "copy")?.href).toContain("dryRun=1");
    expect(actions.find((a) => a.id === "open")?.href).toBe("/trader/w_5c037b2ab8");
  });

  it("writes money through the formatter", () => {
    expect(moneyText(55 * MICRO, true)).toBe("+$ 55.00");
    expect(moneyText(-40 * MICRO, true)).toBe("-$ 40.00");
  });
});
