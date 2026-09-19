/**
 * D7 · the copy screen's rules.
 *
 * The phase's constraint on this screen is one sentence — "the latency warning, in the UI, before they
 * confirm" — and it changes the shape of everything here:
 *
 *  - **The warning is derived from our own attempts, not from a general truth.** `slippageWarning` reads the
 *    `SlippageFacts` the API computes from what this source's copies actually cost us, including the skip rate.
 *    A screen that says "slippage may occur" has said nothing; a screen that says "your copies of this wallet
 *    landed a median 12 bps away and one in five was skipped for moving" has said something a user can decide on.
 *  - **Going live is blocked by evidence, not by a checkbox.** `goLiveBlockers` returns what is missing — an
 *    acknowledgement AND dry-run history in this account — so the button is disabled with a reason instead of
 *    failing with a 409 the user has to decode. The API refuses both ways; this is the same refusal, earlier.
 *  - **The defaults are the cautious ones.** A new config is a dry run by construction (the create schema has no
 *    field to make it otherwise), `skipIfMovedCents` defaults to 2, and a source that would resolve within 24h
 *    is skipped: "skip instead of chase" is the default, not an option a user has to find.
 *  - **The negative window is rendered.** `sourceRecord` returns every window the API sent, including the one
 *    where the source lost money, because a performance panel that hides it is the feature working as a trap.
 *
 * Money never becomes a float here: dollars typed into the form are parsed digit by digit into micro-USDC.
 */
import type { CopyConfig, CopyMonitor, SlippageFacts, SourceStats } from "./wire";

export const SORT_KEYS = ["riskAdjusted", "netAfterFees", "closedTrades", "drawdown"] as const;
export type CopySort = (typeof SORT_KEYS)[number];
export const DEFAULT_SORT: CopySort = "riskAdjusted";

/* The sort labels are `t()` calls in CopyView, not keys here: a table of key strings would be a second place
   the copy lives, and the i18n check only sees literal calls. */

/** Basis points as a percentage, in integer arithmetic. 125 → "1.25%". */
export function bpsText(bps: number): string {
  const sign = bps < 0 ? "-" : "";
  const abs = Math.abs(Math.trunc(bps));
  const whole = Math.trunc(abs / 100);
  const frac = abs % 100;
  return frac === 0 ? `${sign}${whole}%` : `${sign}${whole}.${String(frac).padStart(2, "0")}%`;
}

/**
 * The warning, in the three sentences it needs: what we measured, how often we just skipped, and what the
 * default does about it. Empty samples say so rather than showing a comforting zero.
 */
export function slippageWarning(facts: SlippageFacts): { headline: string; measured: string; skipRate: string; stance: string } {
  if (facts.samples === 0 && facts.copied === 0) {
    return {
      headline: "no copies of this source yet, so there is no measured slippage to show you",
      measured: "the first copies will be dry runs, and the number here is what those attempts cost",
      skipRate: "",
      stance: facts.default,
    };
  }
  return {
    headline: `by the time we see one of this wallet's fills, the price has already moved`,
    measured: `${facts.copied} copied, ${facts.samples} measured: median ${bpsText(facts.medianSlippageBps)}, 90th percentile ${bpsText(facts.p90SlippageBps)}, worst ${bpsText(facts.worstSlippageBps)}`,
    skipRate: `${facts.skipped} skipped (${bpsText(facts.skipRateBps)} of the signals we saw)`,
    stance: facts.default,
  };
}

/** How much dry-run history this account has on this config. The API counts rows; this counts the same rows. */
export function dryRunHistory(monitor: CopyMonitor | null): number {
  if (!monitor) return 0;
  return monitor.wouldDo.length + monitor.skips.length;
}

/**
 * What still stands between this config and live copying.
 *
 * Exactly the API's two conditions, phrased as reasons rather than as a 409: the acknowledgement, and the
 * dry-run history. Both are shown together, because fixing one and being refused for the other is the kind of
 * loop that teaches a user to click through warnings.
 */
export function goLiveBlockers(config: CopyConfig | null, history: number, acknowledged: boolean): string[] {
  if (!config) return ["pick a config first"];
  if (!config.dryRun) return [];
  const problems: string[] = [];
  if (!acknowledged) {
    problems.push("acknowledge the measured slippage above — the API refuses a live switch without it, and the refusal is the point");
  }
  if (history === 0) {
    problems.push("this config has no dry-run history yet: it must record what it would have done, in your account, before it can trade");
  }
  return problems;
}

/** The monitor's two lists, merged and labelled, because a user asks "what did it do" across both. */
export function monitorRows(monitor: CopyMonitor | null): {
  id: string;
  kind: "copied" | "skipped" | "would";
  sourcePrice: string;
  ourPrice: string;
  slipBps: number;
  reason: string;
  atMs: number;
  marketId: string;
}[] {
  if (!monitor) return [];
  const rows = monitor.live.map((e, i) => ({
    id: `live-${i}-${e.atMs}`,
    kind: (e.action === "copied" ? "copied" : "skipped") as "copied" | "skipped",
    sourcePrice: "",
    ourPrice: "",
    slipBps: e.deviationBps,
    reason: e.reason,
    atMs: e.atMs,
    marketId: "",
  }));
  // The API keeps the engine's skips in their OWN list (`skips`), apart from the simulated fills. Merging them
  // here is the point of this function: a monitor that showed only the fills would answer "what did it do" with
  // the good news, and the skip reasons are why a user can tell "no signals" from "every signal was refused".
  const skipped = monitor.skips.map((e, i) => ({
    id: `skip-${i}-${e.atMs}`,
    kind: "skipped" as const,
    sourcePrice: "",
    ourPrice: "",
    slipBps: 0,
    reason: e.reason,
    atMs: e.atMs,
    marketId: "",
  }));
  const simulated = monitor.wouldDo.map((e, i) => ({
    id: `dry-${i}-${e.atMs}`,
    kind: (e.action === "enter" ? "would" : "skipped") as "would" | "skipped",
    sourcePrice: e.sourcePrice,
    ourPrice: e.price,
    slipBps: e.deviationBps,
    reason: e.reason,
    atMs: e.atMs,
    marketId: e.marketId,
  }));
  return [...rows, ...skipped, ...simulated].sort((a, b) => b.atMs - a.atMs);
}

/**
 * The source's record, honestly.
 *
 * Every window the API sent is returned, and `worst` is the most negative one so the screen can lead with it
 * rather than bury it. `insufficientSample` windows carry no rate at all — the panel says "insufficient sample"
 * where a percentage would be.
 */
export function sourceRecord(stats: SourceStats | null | undefined): {
  windowDays: number;
  netAfterFeesMicro: number;
  netText: string;
  maxDrawdownMicro: number;
  winRateText: string;
  riskAdjustedBps: number;
  closedTrades: number;
  avgLatencyMs: number;
}[] {
  if (!stats) return [];
  return stats.windows.map((w) => ({
    windowDays: w.windowDays,
    netAfterFeesMicro: w.netAfterFeesMicro,
    netText: moneyMicroText(w.netAfterFeesMicro),
    maxDrawdownMicro: w.maxDrawdownMicro,
    winRateText: w.insufficientSample || w.winRateBps === null ? "insufficient sample" : bpsText(w.winRateBps),
    riskAdjustedBps: w.riskAdjustedBps,
    closedTrades: w.closedTrades,
    avgLatencyMs: w.avgLatencyMs,
  }));
}

/** The sentence under the table: whether copying this wallet is working, after slippage, including "no". */
export function perSourceVerdict(stats: SourceStats | null | undefined): string {
  const windows = sourceRecord(stats);
  if (windows.length === 0) return "no record for this source yet: nothing has closed under our copy";
  const losing = windows.filter((w) => w.netAfterFeesMicro < 0);
  if (losing.length === windows.length) {
    return `copying this source has lost money in every window we have (worst ${losing[0]?.netText})`;
  }
  if (losing.length > 0) {
    return `this source is profitable in some windows and negative in ${losing.length}: the losses are shown above, not averaged away`;
  }
  return "every window we have is positive after fees — which is not a promise about the next one";
}

// ----------------------------------------------------------------------------------- the config draft

export type Draft = {
  sourceAnon: string;
  mode: "cap" | "ratio";
  ratioBps: string;
  maxOrder: string;
  maxDaily: string;
  categoryFilter: string;
  minPrice: string;
  maxPrice: string;
  takeProfit: string;
  stopLoss: string;
  skipIfMovedCents: string;
  doNotEnterWithinHours: string;
};

export const EMPTY_DRAFT: Draft = {
  sourceAnon: "",
  mode: "cap",
  ratioBps: "25",
  maxOrder: "100",
  maxDaily: "400",
  categoryFilter: "",
  minPrice: "",
  maxPrice: "",
  takeProfit: "",
  stopLoss: "",
  // The cautious defaults, spelled out rather than left empty: 2 cents of movement skips, 24 hours of resolve
  // time skips. A user who wants to chase has to change a number, and the default never chases.
  skipIfMovedCents: "2",
  doNotEnterWithinHours: "24",
};

/** Dollars (or a plain integer of cents) as integer micro-USDC, digit by digit. No float touches the money. */
export function microFromDecimal(input: string): number | null {
  const text = input.trim().replace(/[,$\s]/g, "");
  if (!/^\d{1,9}(\.\d{0,6})?$/.test(text)) return null;
  const [whole = "0", frac = ""] = text.split(".");
  return Number.parseInt(whole, 10) * 1_000_000 + Number.parseInt((frac + "000000").slice(0, 6) || "0", 10);
}

/** Micro integer from a digits-only field with a range, the way the rail's own inputs are read. */
function intIn(input: string, lo: number, hi: number): number | null {
  if (!/^\d{1,6}$/.test(input.trim())) return null;
  const value = Number.parseInt(input.trim(), 10);
  return value >= lo && value <= hi ? value : null;
}

/**
 * What the form cannot send.
 *
 * The API refuses each of these with a 422 or a 409, and this list is the same refusals written where the user
 * can still fix them. It is deliberately not a "best effort" filter: a form that silently corrects a cap is a
 * form whose config is not the one the user typed.
 */
export function draftProblems(draft: Draft): { field: string; why: string }[] {
  const problems: { field: string; why: string }[] = [];
  if (draft.sourceAnon.length < 4) problems.push({ field: "sourceAnon", why: "pick a source from the list above" });
  const maxOrder = microFromDecimal(draft.maxOrder);
  const maxDaily = microFromDecimal(draft.maxDaily);
  if (maxOrder === null || maxOrder < 1_000_000) {
    problems.push({ field: "maxOrder", why: "the per-trade cap must be at least $1.00 — the API's floor, in dollars" });
  }
  if (maxDaily === null || maxDaily < 1_000_000) {
    problems.push({ field: "maxDaily", why: "the daily cap must be at least $1.00" });
  }
  if (maxOrder !== null && maxDaily !== null && maxOrder > maxDaily) {
    // The API refuses this rather than clamping: a per-trade cap above the daily cap cannot fire twice.
    problems.push({ field: "maxDaily", why: "the daily cap must be at least the per-trade cap, or the config can never fire twice" });
  }
  if (draft.mode === "ratio") {
    const ratio = intIn(draft.ratioBps, 1, 10_000);
    if (ratio === null) problems.push({ field: "ratioBps", why: "a multiplier is 1-10000 basis points (1x-100x)" });
  }
  const skip = intIn(draft.skipIfMovedCents, 0, 50);
  if (skip === null) problems.push({ field: "skipIfMovedCents", why: "0-50 cents: how far the price may move before we skip instead of chase" });
  const resolve = intIn(draft.doNotEnterWithinHours, 0, 168);
  if (resolve === null) problems.push({ field: "doNotEnterWithinHours", why: "0-168 hours: a market resolving inside this window is skipped" });
  for (const [field, value] of [["minPrice", draft.minPrice], ["maxPrice", draft.maxPrice], ["takeProfit", draft.takeProfit], ["stopLoss", draft.stopLoss]] as const) {
    if (value.trim() !== "" && microFromDecimal(value) === null) {
      problems.push({ field, why: "a price or a trigger, in dollars up to six decimals (0.000001 - 0.999999)" });
    }
  }
  const min = microFromDecimal(draft.minPrice);
  const max = microFromDecimal(draft.maxPrice);
  if (min !== null && max !== null && min > max) {
    problems.push({ field: "maxPrice", why: "the upper price bound is below the lower one, so nothing could ever pass it" });
  }
  return problems;
}

/** The create body: only the fields the API declared, and money as integer micro-USDC. */
export function draftBody(draft: Draft): Record<string, unknown> {
  const body: Record<string, unknown> = {
    sourceAnon: draft.sourceAnon,
    mode: draft.mode,
    maxOrderMicro: microFromDecimal(draft.maxOrder) ?? 0,
    maxDailyMicro: microFromDecimal(draft.maxDaily) ?? 0,
  };
  if (draft.mode === "ratio") body.ratioBps = intIn(draft.ratioBps, 1, 10_000) ?? 0;
  return body;
}

/**
 * The guard body the form would send, which is where the knobs that are not part of creation live.
 *
 * Empty fields mean "no bound", and they are sent as `null` rather than omitted: the API stores nulls for an
 * unset bound, and a form that omitted them would leave whatever a previous save wrote.
 */
export function guardBody(draft: Draft, configId: string, extra: Record<string, unknown> = {}): Record<string, unknown> {
  const price = (text: string) => (text.trim() === "" ? null : microFromDecimal(text));
  return {
    configId,
    skipIfMovedCents: intIn(draft.skipIfMovedCents, 0, 50) ?? 2,
    doNotEnterWithinHours: intIn(draft.doNotEnterWithinHours, 0, 168) ?? 24,
    categoryFilter: draft.categoryFilter,
    minPriceMicro: price(draft.minPrice),
    maxPriceMicro: price(draft.maxPrice),
    takeProfitMicro: price(draft.takeProfit),
    stopLossMicro: price(draft.stopLoss),
    ...extra,
  };
}

/** Stop is a dry run. There is no third state, and pretending there is one would be a switch that does nothing. */
export function pauseBody(configId: string): Record<string, unknown> {
  return { configId, dryRun: true };
}

/** The global pause: every live config, back to paper. Returned as bodies so the screen can show the count. */
export function pauseAllBodies(configs: CopyConfig[]): Record<string, unknown>[] {
  return configs.filter((c) => !c.dryRun).map((c) => pauseBody(c.configId));
}

/** Money as text, for the one place a sentence needs it. Kept integer-only, like the money layer. */
export function moneyMicroText(micro: number): string {
  const sign = micro < 0 ? "-" : "";
  const abs = Math.abs(Math.trunc(micro));
  const dollars = Math.trunc(abs / 1_000_000);
  const cents = Math.trunc((abs % 1_000_000) / 10_000);
  const grouped = String(dollars).replace(/\B(?=(\d{3})+(?!\d))/g, " ");
  return `${sign}$ ${grouped}.${String(cents).padStart(2, "0")}`;
}

/** A discovered source's row, in the shape the table renders (and the tests assert). */
export function discoveryRow(row: {
  anonWallet: string;
  closedTrades: number;
  netAfterFeesMicro: number;
  riskAdjustedBps: number;
  maxDrawdownMicro: number;
  winRateBps: number | null;
  insufficientSample: boolean;
  sampleNote: string;
  copierCount: number;
  currentlyCopying: boolean;
  rank: number;
}): {
  anonWallet: string;
  rank: number;
  netText: string;
  drawdownText: string;
  riskText: string;
  winRateText: string;
  closedTrades: number;
  copierText: string;
} {
  return {
    anonWallet: row.anonWallet,
    rank: row.rank,
    netText: moneyMicroText(row.netAfterFeesMicro),
    drawdownText: moneyMicroText(row.maxDrawdownMicro),
    // Net per unit of drawdown, as a multiple: 15384 bps is $1.53 earned per $1 of drawdown. Two decimals always,
    // because a column of "1x / 2x / 12x" hides the difference between 1.01x and 1.99x.
    riskText: `${Math.trunc(row.riskAdjustedBps / 10_000)}.${String(Math.trunc((row.riskAdjustedBps % 10_000) / 100)).padStart(2, "0")}x`,
    winRateText: row.insufficientSample || row.winRateBps === null ? "insufficient sample" : bpsText(row.winRateBps),
    closedTrades: row.closedTrades,
    copierText: row.currentlyCopying ? `you + ${Math.max(0, row.copierCount - 1)}` : String(row.copierCount),
  };
}
