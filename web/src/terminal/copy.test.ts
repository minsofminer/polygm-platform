/**
 * D7's rules as tests. The three that matter most:
 *
 *  * the warning is derived from OUR measured deviations and says so when there are none;
 *  * going live is blocked by evidence (dry-run history) and not only by a checkbox;
 *  * the money a user types is parsed digit by digit into micro-USDC, never through a float.
 */
import { describe, expect, it } from "vitest";
import {
  DEFAULT_SORT,
  EMPTY_DRAFT,
  SORT_KEYS,
  bpsText,
  discoveryRow,
  draftBody,
  draftProblems,
  dryRunHistory,
  goLiveBlockers,
  guardBody,
  microFromDecimal,
  monitorRows,
  moneyMicroText,
  pauseAllBodies,
  pauseBody,
  perSourceVerdict,
  slippageWarning,
  sourceRecord,
} from "./copy";
import type { CopyConfig, CopyMonitor, SlippageFacts } from "./wire";

const MICRO = 1_000_000;

function facts(over: Partial<SlippageFacts> = {}): SlippageFacts {
  return {
    samples: 6,
    medianSlippageBps: 25,
    p90SlippageBps: 90,
    worstSlippageBps: 260,
    copied: 8,
    skipped: 4,
    skipRateBps: 3_333,
    default: "skip instead of chase: a fill that has moved more than 2 cents is skipped, not followed",
    warning: "by the time we see a fill the price has moved",
    ...over,
  };
}

function config(over: Partial<CopyConfig> = {}): CopyConfig {
  return {
    configId: "cfg-seed01",
    sourceAnon: "w_5c037b2ab8",
    mode: "ratio",
    ratioBps: 2_500,
    maxOrderMicro: 250 * MICRO,
    maxDailyMicro: 1_000 * MICRO,
    enabled: false,
    createdMs: 1,
    dryRun: true,
    guardsFromRow: true,
    skipIfMovedCents: 2,
    doNotEnterWithinHours: 24,
    categoryFilter: "Politics",
    minPriceMicro: 50_000,
    maxPriceMicro: 900_000,
    takeProfitMicro: 850_000,
    stopLossMicro: 200_000,
    warning: facts(),
    sourceStats: {
      ranking: "sources are ranked risk-adjusted",
      windows: [
        { windowDays: 7, closedTrades: 7, netAfterFeesMicro: -122 * MICRO, maxDrawdownMicro: 180 * MICRO, winRateBps: 4_285, insufficientSample: false, riskAdjustedBps: -6_777, avgLatencyMs: 820 },
        { windowDays: 30, closedTrades: 4, netAfterFeesMicro: 631 * MICRO, maxDrawdownMicro: 410 * MICRO, winRateBps: null, insufficientSample: true, riskAdjustedBps: 15_390, avgLatencyMs: 760 },
      ],
    },
    ...over,
  };
}

function monitor(over: Partial<CopyMonitor> = {}): CopyMonitor {
  return {
    configId: "cfg-seed01",
    sourceAnon: "w_5c037b2ab8",
    live: [{ action: "copied", reason: "", deviationBps: 12, atMs: 1_700_000_000_000, intentId: "i1", dryRun: false }],
    wouldDo: [
      { action: "enter", shares: "120", price: "0.41", sourcePrice: "0.408", deviationBps: 49, reason: "would copy: within the limit", atMs: 1_700_000_100_000, marketId: "0xM2", dryRun: true },
    ],
    skips: [{ action: "skip", reason: "do_not_enter_within_24h: resolves soon", atMs: 1_700_000_200_000 }],
    slippage: facts(),
    sourceStats: config().sourceStats,
    skipReasons: ["skip_if_moved", "resolved_soon"],
    ...over,
  };
}

describe("the warning arrives before the confirm", () => {
  it("quotes the deviations we measured, in bps, and the skip rate", () => {
    const w = slippageWarning(facts());
    expect(w.measured).toContain("6 measured");
    expect(w.measured).toContain("median 0.25%");
    expect(w.measured).toContain("90th percentile 0.90%");
    expect(w.measured).toContain("worst 2.60%");
    expect(w.skipRate).toContain("4 skipped");
    expect(w.stance).toContain("skip instead of chase");
  });

  it("says there is nothing to show rather than showing a zero", () => {
    const w = slippageWarning(facts({ samples: 0, copied: 0, medianSlippageBps: 0, p90SlippageBps: 0, worstSlippageBps: 0 }));
    expect(w.headline).toContain("no copies of this source yet");
    expect(w.measured).not.toContain("median 0%");
  });
});

describe("going live is blocked by evidence", () => {
  it("needs the acknowledgement AND dry-run history", () => {
    const both = goLiveBlockers(config(), 3, true);
    expect(both).toEqual([]);
    const noAck = goLiveBlockers(config(), 3, false);
    expect(noAck.join(" ")).toContain("acknowledge");
    const noHistory = goLiveBlockers(config(), 0, true);
    expect(noHistory.join(" ")).toContain("no dry-run history");
    // Both missing is both reasons: fixing one and being refused for the other is the loop that teaches a user
    // to click through warnings.
    expect(goLiveBlockers(config(), 0, false).length).toBe(2);
  });

  it("counts the history from the same two lists the monitor shows", () => {
    expect(dryRunHistory(monitor())).toBe(2);
    expect(dryRunHistory(null)).toBe(0);
  });

  it("has nothing to block once a config is already live", () => {
    expect(goLiveBlockers(config({ dryRun: false }), 0, false)).toEqual([]);
  });
});

describe("the default sort is the one D7 asks for", () => {
  it("is risk-adjusted, and raw PnL is offered but not defaulted to", () => {
    expect(DEFAULT_SORT).toBe("riskAdjusted");
    expect(SORT_KEYS[0]).toBe("riskAdjusted");
    expect(SORT_KEYS).toContain("netAfterFees");
  });

  it("shows the ratio next to the drawdown it was divided by", () => {
    const row = discoveryRow({
      anonWallet: "w_a", closedTrades: 21, netAfterFeesMicro: 631 * MICRO, riskAdjustedBps: 15_384,
      maxDrawdownMicro: 410 * MICRO, winRateBps: 5_714, insufficientSample: false, sampleNote: "",
      copierCount: 3, currentlyCopying: false, rank: 1,
    });
    expect(row.riskText).toBe("1.53x");
    expect(row.drawdownText).toBe("$ 410.00");
    expect(row.netText).toBe("$ 631.00");
  });

  it("prints no win rate below the gate, whatever the number says", () => {
    const row = discoveryRow({
      anonWallet: "w_a", closedTrades: 4, netAfterFeesMicro: 84 * MICRO, riskAdjustedBps: 14_000,
      maxDrawdownMicro: 60 * MICRO, winRateBps: 5_000, insufficientSample: true,
      sampleNote: "insufficient sample: 4 settled markets", copierCount: 0, currentlyCopying: false, rank: 3,
    });
    expect(row.winRateText).toBe("insufficient sample");
    expect(row.winRateText).not.toContain("50");
  });
});

describe("the money a user types", () => {
  it("becomes integer micro-USDC, digit by digit", () => {
    expect(microFromDecimal("100")).toBe(100 * MICRO);
    expect(microFromDecimal("1.25")).toBe(1_250_000);
    expect(microFromDecimal("0.000001")).toBe(1);
    expect(microFromDecimal("$ 1,000.50")).toBe(1_000_500_000);
    expect(microFromDecimal("abc")).toBeNull();
    expect(microFromDecimal("1.2.3")).toBeNull();
    expect(microFromDecimal("")).toBeNull();
  });

  it("writes money back as text without a float", () => {
    expect(moneyMicroText(1_234_567_890)).toBe("$ 1 234.56");
    expect(moneyMicroText(-122 * MICRO)).toBe("-$ 122.00");
  });

  it("is refused by the form where the API would refuse it, in the user's words", () => {
    const belowFloor = draftProblems({ ...EMPTY_DRAFT, sourceAnon: "w_abc", maxOrder: "0.5" });
    expect(belowFloor.map((p) => p.field)).toContain("maxOrder");
    const inverted = draftProblems({ ...EMPTY_DRAFT, sourceAnon: "w_abc", maxOrder: "500", maxDaily: "100" });
    expect(inverted.map((p) => p.why).join(" ")).toContain("can never fire twice");
    const badSkip = draftProblems({ ...EMPTY_DRAFT, sourceAnon: "w_abc", skipIfMovedCents: "99" });
    expect(badSkip.map((p) => p.field)).toContain("skipIfMovedCents");
    const crossed = draftProblems({ ...EMPTY_DRAFT, sourceAnon: "w_abc", minPrice: "0.80", maxPrice: "0.20" });
    expect(crossed.map((p) => p.why).join(" ")).toContain("below the lower one");
  });

  it("accepts the cautious defaults it ships with", () => {
    expect(draftProblems({ ...EMPTY_DRAFT, sourceAnon: "w_abc" })).toEqual([]);
  });

  it("sends only the fields the create schema declared", () => {
    const body = draftBody({ ...EMPTY_DRAFT, sourceAnon: "w_abc", mode: "ratio", ratioBps: "2500" });
    expect(Object.keys(body).sort()).toEqual(["maxDailyMicro", "maxOrderMicro", "mode", "ratioBps", "sourceAnon"]);
    expect(body.maxOrderMicro).toBe(100 * MICRO);
    // No `dryRun` field can be sent, by construction.
    expect("dryRun" in body).toBe(false);
  });

  it("sends the guard knobs, with an unset bound as null rather than omitted", () => {
    const body = guardBody({ ...EMPTY_DRAFT, sourceAnon: "w_abc", minPrice: "0.10" }, "cfg-1");
    expect(body.configId).toBe("cfg-1");
    expect(body.minPriceMicro).toBe(100_000);
    expect(body.maxPriceMicro).toBeNull();
    expect(body.skipIfMovedCents).toBe(2);
  });

  it("stops by going back to a dry run, and never claims a third state", () => {
    expect(pauseBody("cfg-1")).toEqual({ configId: "cfg-1", dryRun: true });
    expect(pauseAllBodies([config({ configId: "a" }), config({ configId: "b", dryRun: false })])).toEqual([
      { configId: "b", dryRun: true },
    ]);
  });
});

describe("the monitor", () => {
  it("keeps simulations labelled as simulations", () => {
    const rows = monitorRows(monitor());
    expect(rows.map((r) => r.kind).sort()).toEqual(["copied", "skipped", "would"]);
    const would = rows.find((r) => r.kind === "would");
    expect(would?.sourcePrice).toBe("0.408");
    expect(would?.ourPrice).toBe("0.41");
    expect(would?.slipBps).toBe(49);
  });

  it("carries a skip reason on every skipped row", () => {
    const skipped = monitorRows(monitor()).filter((r) => r.kind === "skipped");
    expect(skipped.every((r) => r.reason.length > 0)).toBe(true);
  });

  it("renders the losing window and says so in the verdict", () => {
    const windows = sourceRecord(config().sourceStats);
    expect(windows.map((w) => w.windowDays)).toEqual([7, 30]);
    expect(windows[0]?.netText).toBe("-$ 122.00");
    expect(windows[1]?.winRateText).toBe("insufficient sample");
    expect(perSourceVerdict(config().sourceStats)).toContain("negative in 1");
  });

  it("says a source that only loses is losing, without softening it", () => {
    const losing = {
      ranking: "",
      windows: [{ windowDays: 7, closedTrades: 7, netAfterFeesMicro: -200 * MICRO, maxDrawdownMicro: 300 * MICRO, winRateBps: 4_000, insufficientSample: false, riskAdjustedBps: -6_666, avgLatencyMs: 800 }],
    };
    expect(perSourceVerdict(losing)).toContain("lost money in every window");
  });

  it("writes basis points as percentages", () => {
    expect(bpsText(25)).toBe("0.25%");
    expect(bpsText(3_333)).toBe("33.33%");
    expect(bpsText(0)).toBe("0%");
  });
});
