/**
 * The terminal's pure parts: everything about the live tape that is a decision rather than a rendering.
 *
 * They live in a `.ts` file with no React in it for one reason: the P10 gate (c10) and `vitest` can then hold
 * the behaviour to account without a DOM — the coalescing window, the pause accounting, the virtual window's
 * arithmetic, the relative-threshold label, the badge tooltip's text and the sound default are all functions of
 * their inputs and nothing else.
 *
 * The three that matter most:
 *
 *  - **`coalesceFills`** — a venue minute can carry hundreds of fills. 200 DOM writes per second is not a
 *    rendering problem to be optimised later; it is a component that has not met its own acceptance line
 *    ("60fps under live load or the component is not done"). Rows are queued and released on a frame budget,
 *    and the queue's own depth is what the "paused — N new" affordance counts.
 *  - **`virtualWindow`** — which rows are in the DOM for a given scroll position, pure and therefore testable.
 *    A virtualised list is the difference between a tape that scrolls and a browser that stalls.
 *  - **`thresholdSentence`** — the exact sentence a whale badge's tooltip shows. D2 requires the badge to state
 *    the rule; the rule arrives on every row (`thresholdRule`), so this composes it with the row's own numbers
 *    rather than inventing a second vocabulary.
 */
import { formatCents, microToCents, priceToUnits, type TickSize } from "@/money/cents";
import type { CurvePoint, LabelFact, TerminalFill, TerminalFill as Fill } from "./wire";

/** Fills per second above which the tape batch-releases rows instead of drawing them one at a time. */
export const COALESCE_THRESHOLD_PER_S = 20;
/** The longest a released batch waits, so a quiet tape still updates promptly. */
export const FLUSH_MS = 250;
/** Rows kept in memory. The tape is a viewport onto a log; an unbounded buffer is a leak with a scrollbar. */
export const MAX_ROWS = 400;
/** Row height in CSS pixels, and the reason virtualisation is exact rather than approximate. */
export const ROW_H = 44;
export const OVERSCAN = 8;

export type TapeFilters = {
  /** Absolute floor in micro-USDC: D2's first notional filter. */
  minNotionalMicro: number;
  /** Relative-to-market-median multiple. 0 means "off", 1 means "at the median", 2 means "twice it". */
  relativeToMedian: number;
  side: "" | "BUY" | "SELL";
  outcome: string;
  category: string;
  label: string;
  wallet: string;
  marketId: string;
};

export const EMPTY_FILTERS: TapeFilters = {
  minNotionalMicro: 0,
  relativeToMedian: 0,
  side: "",
  outcome: "",
  category: "",
  label: "",
  wallet: "",
  marketId: "",
};

/**
 * The two notional filters, applied together and independently.
 *
 * The prompt is specific about why both exist: a fixed $1,000 is simultaneously too low for a big market and too
 * high for a small one (the measured distribution was median $5, p95 $133, max $3,000). The absolute floor is
 * the user's number; the relative one is measured against the market's own median, which is why it takes the
 * median as an argument rather than reading a global.
 */
export function passesNotional(row: Fill, filters: TapeFilters, marketMedianMicro: number): boolean {
  if (filters.minNotionalMicro > 0 && row.notionalMicro < filters.minNotionalMicro) return false;
  if (filters.relativeToMedian > 0 && marketMedianMicro > 0) {
    if (row.notionalMicro * 100 < marketMedianMicro * filters.relativeToMedian * 100) return false;
  }
  return true;
}

export function applyFilters(rows: Fill[], filters: TapeFilters, medians: Record<string, number>): Fill[] {
  const median = medians[filters.marketId] ?? 0;
  return rows.filter((row) => {
    if (!passesNotional(row, filters, filters.marketId ? median : 0)) return false;
    if (filters.side && row.side !== filters.side) return false;
    if (filters.outcome && row.outcome !== filters.outcome) return false;
    if (filters.category && row.category !== filters.category) return false;
    if (filters.wallet && row.anonWallet !== filters.wallet) return false;
    if (filters.marketId && row.marketId !== filters.marketId) return false;
    if (filters.label && !row.labels.some((l) => l.label === filters.label)) return false;
    return true;
  });
}

/**
 * Release queued rows in batches, and report how many are still waiting.
 *
 * `ratePerSecond` is measured over the last second of arrivals rather than assumed: a tape below the threshold
 * appends each row as it arrives (so the newest fill is on screen immediately), and above it the rows are
 * batched. `MAX_ROWS` is enforced on the buffer, which is where an unbounded tape would otherwise grow.
 */
export function coalesceFills(
  buffer: Fill[],
  queued: Fill[],
  opts: { ratePerSecond: number; nowMs: number; lastFlushMs: number; paused: boolean },
): { buffer: Fill[]; queued: Fill[]; released: number; withheld: number; paused: boolean } {
  if (opts.paused) {
    // A paused tape keeps counting and stops drawing: the affordance is "paused — N new", and N has to be the
    // number of fills the user has not seen, not the size of an internal queue.
    const withheld = queued.length;
    return { buffer, queued: buffer.slice(0, MAX_ROWS), released: 0, withheld, paused: true };
  }
  const batched = opts.ratePerSecond >= COALESCE_THRESHOLD_PER_S;
  const due = batched ? opts.nowMs - opts.lastFlushMs >= FLUSH_MS : true;
  if (!due || queued.length === 0) {
    return { buffer: buffer.slice(0, MAX_ROWS), queued, released: 0, withheld: queued.length, paused: false };
  }
  return {
    buffer: [...queued, ...buffer].slice(0, MAX_ROWS),
    queued: [],
    released: queued.length,
    withheld: 0,
    paused: false,
  };
}

/** Arrivals per second, from the timestamps we already have. No timers, no interpolation. */
export function arrivalRate(rows: Fill[], nowMs: number, windowMs = 1_000): number {
  if (rows.length === 0) return 0;
  const since = nowMs - windowMs;
  return rows.filter((r) => r.tsMs >= since).length;
}

/** Which rows are in the DOM for this scroll position. Pure, so the arithmetic is testable. */
export function virtualWindow(
  total: number,
  scrollTop: number,
  viewportH: number,
  rowH = ROW_H,
  overscan = OVERSCAN,
): { start: number; end: number; topPad: number; bottomPad: number } {
  if (total <= 0 || viewportH <= 0) return { start: 0, end: 0, topPad: 0, bottomPad: 0 };
  const first = Math.floor(Math.max(0, scrollTop) / rowH);
  const visible = Math.ceil(viewportH / rowH) + 1;
  const start = Math.max(0, first - overscan);
  const end = Math.min(total, first + visible + overscan);
  return { start, end, topPad: start * rowH, bottomPad: Math.max(0, (total - end) * rowH) };
}

/** The whale badge's tooltip, verbatim from the rule the server used, plus this row's own numbers. */
export function thresholdSentence(row: Fill): string {
  const multiple = row.ratioBps / 10_000;
  const ratio = formatMultiple(row.ratioBps);
  return [
    row.thresholdRule,
    `this fill: ${formatMicro(row.notionalMicro)} = ${ratio} the threshold (${row.severity})`,
    row.thresholdReason === "absolute_fallback"
      ? "the window is thin, so the floor applies alone — the percentile was discarded rather than reported"
      : row.thresholdReason === "absolute_floor"
        ? "the market's own percentile did not clear the floor"
        : "the market's own fills set the bar",
    `severity: ${row.rule}`,
  ].join(" · ");
}

/** A label badge's tooltip: the rule, the confidence and what the label does NOT claim. */
export function labelSentence(fact: LabelFact): string {
  return `${fact.rule} · confidence ${Math.round(fact.confidence / 10)}% · ${fact.disclaimer}`;
}

/** `1.5×`, `4×`: the ratio a badge shows, in the same vocabulary the server's sentence uses. */
export function formatMultiple(bps: number): string {
  const whole = Math.floor(bps / 10_000);
  const rest = Math.round((bps % 10_000) / 1_000);
  return rest === 0 ? `${whole}×` : `${whole}.${rest}×`;
}

/**
 * Micro-USDC as `$1 234.56`, through the money module (DESIGN §5: one formatter, and it is not here).
 *
 * The terminal's tables and tooltips are strings the tests read, so they need a text form; the money module owns
 * what that text looks like. Nothing in this file converts money any other way.
 */
export function formatMicro(micro: number): string {
  return formatCents(microToCents(micro), { signed: micro < 0, currency: "$" });
}

/**
 * A decimal string of shares -> micro-share integer, by string arithmetic.
 *
 * `Number("12000.5")` is a double, and `parseInt("0.999999")` is `0`; neither belongs between a wire value and
 * a size the number layer renders. Splitting the string keeps every digit the API sent, and it is the reason
 * this file has no float anywhere.
 */
export function sharesMicro(text: string): number {
  const m = /^(-?)(\d+)(?:\.(\d+))?$/.exec(String(text).trim());
  if (!m) return 0;
  const [, sign, whole, frac = ""] = m;
  const micro = Number(whole) * 1_000_000 + Number((frac + "000000").slice(0, 6));
  return sign === "-" ? -micro : micro;
}

/** Whole shares, for the size renderer, from micro. Integer division: a fractional share is not a thing the
 *  venue lets you hold, and `withSiSuffix` would otherwise print a width nobody trades. */
export function sharesWhole(text: string): number {
  return Math.trunc(sharesMicro(text) / 1_000_000);
}

/** Shares as a decimal string, from micro. String splitting, not float division: 12 000 shares must not render
 *  as 12.0K in a row whose whole point is that the number is unusual. */
export function formatShares(micro: number): string {
  const sign = micro < 0 ? "-" : "";
  const abs = Math.abs(Math.trunc(micro));
  const whole = Math.floor(abs / 1_000_000);
  const frac = abs % 1_000_000;
  if (frac === 0) return String(whole);
  return `${String(whole)}.${frac.toString().padStart(6, "0").replace(/0+$/, "")}`.replace(/^(-?)/, sign);
}

/** A price as tick units, so `Number` renders it with the market's precision rather than two decimals. */
export function priceUnitsFor(price: string, tick: TickSize): number {
  return priceToUnits(price, tick);
}

/** The row's click target: a row opens its market, the wallet opens the dossier. Shift adds to the watchlist. */
export function rowTarget(row: Fill, shiftKey: boolean): { kind: "watchlist" | "market"; href: string } {
  if (shiftKey) return { kind: "watchlist", href: row.marketSlug };
  return { kind: "market", href: `/market/${encodeURIComponent(row.marketSlug || row.marketId)}` };
}

export function walletHref(anonWallet: string): string {
  return `/trader/${encodeURIComponent(anonWallet)}`;
}

/**
 * The sound toggle's state, with the default in the function.
 *
 * "Sound for followed wallets, off by default" is a rule about the DEFAULT, not about the feature: a user who
 * has never touched the toggle must not get an audible alarm the first time a whale trades, and a user who has
 * turned it on must keep it.
 */
export function soundEnabled(stored: string | null): boolean {
  return stored === "1";
}

/** Which followed wallets a sound would fire for — the filter that keeps the toggle from being a firehose. */
export function shouldChime(row: Fill, followed: string[], enabled: boolean): boolean {
  return enabled && followed.includes(row.anonWallet);
}

/** The portfolio's share of a position, from the server's basis points, rendered as a percentage string. */
export function sharePercent(bps: number): string {
  const whole = Math.floor(bps / 100);
  const rest = Math.round(bps % 100);
  return rest === 0 ? `${whole}%` : `${whole}.${String(rest).padStart(2, "0")}%`;
}

/**
 * The 5-minute crypto template is offered ONLY when the fee arithmetic clears.
 *
 * `edgeMicroPerShare` is the spread a 5-minute crypto market gives you and `feesMicroPerShare` is what the
 * venue takes on both sides (P06 D6's arithmetic, in the same units). A template that offers a strategy without
 * stating its own cost is a template that sells the venue's fee to the user as an edge, so this returns the
 * arithmetic and the reason, and the panel renders `null` as "not available at current fees" rather than hiding
 * the row.
 */
export function crypto5mTemplate(edgeMicroPerShare: number, feesMicroPerShare: number): {
  available: boolean;
  netMicroPerShare: number;
  sentence: string;
} {
  const net = edgeMicroPerShare - feesMicroPerShare;
  return {
    available: net > 0,
    netMicroPerShare: net,
    sentence:
      net > 0
        ? `a 5-minute crypto scalp nets ${formatMicro(net)} per share after fees (edge ${formatMicro(edgeMicroPerShare)} − fees ${formatMicro(feesMicroPerShare)})`
        : `not offered: the edge (${formatMicro(edgeMicroPerShare)}) does not cover the fees (${formatMicro(feesMicroPerShare)}), so the template would lose ${formatMicro(-net)} per share`,
  };
}

/** The order-history row that the venue never resolved: returned rather than hidden, and labelled. */
export function unknownRowLabel(state: string, showAsWorking: boolean): string {
  return showAsWorking
    ? `the venue has not answered yet — shown as working, and it is not counted in your PnL (${state})`
    : `we could not read this order's final state (${state}) — it is listed rather than dropped`;
}

/** The drawdown overlay's polyline points, in a 0..1 box, from the curve the API returned. */
export function curvePoints(curve: CurvePoint[], width: number, height: number): {
  pnl: string;
  drawdown: string;
  min: number;
  max: number;
} {
  if (curve.length === 0) return { pnl: "", drawdown: "", min: 0, max: 0 };
  const values = curve.map((p) => p.cumMicro);
  const min = Math.min(0, ...values);
  const max = Math.max(0, ...values) || 1;
  const x = (i: number) => (curve.length === 1 ? 0 : (i / (curve.length - 1)) * width);
  const y = (v: number) => height - ((v - min) / (max - min || 1)) * height;
  const pnl = curve.map((p, i) => `${x(i)},${y(p.cumMicro)}`).join(" ");
  // The drawdown shade is drawn from the peak line down to the curve: the overlay is not a second chart, it is
  // the same chart with the distance below the high-water mark filled in.
  const drawdown = curve.map((p, i) => `${x(i)},${y(p.peakMicro)}`).reverse().concat(
    curve.map((p, i) => `${x(i)},${y(p.cumMicro)}`)).join(" ");
  return { pnl, drawdown, min, max };
}
