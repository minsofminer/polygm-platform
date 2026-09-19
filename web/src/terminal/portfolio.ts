/**
 * D6 · the portfolio's rules, as functions.
 *
 * Three of them are the phase's constraints wearing a table:
 *
 *  - **A mark we do not have is not a zero.** `markSource` is either `last_fill` or `unknown`, and an `unknown`
 *    mark means the market has never traded since we started watching. A position marked at 0 renders as a
 *    total loss, which is the single most expensive lie this screen could tell — so `unrealisedKnown` is false
 *    there and the table says "no mark" instead of a number.
 *  - **Drawdown appears wherever PnL appears.** The curve is drawn as cash (realised) with its own drawdown
 *    overlay, and the account's maximum drawdown is a row in the totals block rather than a footnote. The
 *    unrealised PnL is deliberately NOT merged into that curve: the API's points carry `openPositions: 0`
 *    because a cash ledger cannot value it, and a curve that quietly added it would be a curve whose
 *    drawdown is not the drawdown of the line above it.
 *  - **negRisk legs are one risk, not N bets.** `exposureNote` states the event-level exposure and says how
 *    many of the legs can pay at once, because a user long three mutually exclusive candidates is holding one
 *    bet of size `max`, not three bets whose losses offset.
 *
 * The CSV is built from the same payload the table renders (`csv.columns` is the wire's own column list), so
 * the file cannot disagree with the screen it was downloaded from.
 */
import { formatCents, microToCents } from "@/money/cents";
import { curvePoints as sharedCurvePoints } from "./tape";
import type { CurvePoint, NegRiskGroup, Portfolio, PortfolioPosition } from "./wire";

export type Tone = "good" | "bad" | "unknown" | null;

/** The one place this module writes money as text: integer micro-USDC in, `$ 1 234.56` out. */
export function moneyText(micro: number, signed = false): string {
  return formatCents(microToCents(micro), { currency: "$", signed });
}

/**
 * What the mark is, and — when there is not one — what that means.
 *
 * `rule` is the sentence the table shows next to the number. It is not a tooltip: the difference between
 * "$0.00" and "no mark" is the difference between a loss and an unknown, and a user reading a screenshot has
 * to be able to see which one they have.
 */
export function markText(position: PortfolioPosition): { text: string; tone: Tone; rule: string } {
  if (!unrealisedKnown(position)) {
    return {
      text: "no mark",
      tone: "unknown",
      rule: "this market has not traded since we started watching it, so we have no price to mark it at — the size and the cost below are real, the PnL is not knowable yet",
    };
  }
  return {
    text: "last fill",
    tone: null,
    rule: "marked at the last fill we saw, not at a midpoint: a midpoint is a price nobody traded at",
  };
}

/** A position with no mark has no unrealised PnL. Rendering a 0 there would claim the loss is 100% known. */
export function unrealisedKnown(position: PortfolioPosition): boolean {
  return position.markSource !== "unknown" && position.mark !== "0" && position.mark !== "";
}

export function endsInText(endsInMs: number, nowMs: number): string {
  if (endsInMs <= 0) return "no end date published";
  const ms = endsInMs - nowMs;
  if (ms <= 0) return "ended — awaiting resolution";
  const hours = Math.floor(ms / 3_600_000);
  if (hours < 1) return `${Math.max(1, Math.floor(ms / 60_000))}m`;
  if (hours < 48) return `${hours}h`;
  return `${Math.floor(hours / 24)}d ${hours % 24}h`;
}

/** Share of the book, in basis points on the wire and a percentage here. */
export function shareText(bps: number): string {
  const whole = Math.trunc(bps / 100);
  const frac = Math.abs(bps % 100);
  return frac === 0 ? `${whole}%` : `${whole}.${String(frac).padStart(2, "0")}%`;
}

/**
 * Where a quick exit happens, and why it is not a one-click sell on this table.
 *
 * This table is refreshed every 15 seconds. A sell button sitting in a stale row sells into whatever the book
 * is now, which is the one order a user must not place blind — so the control carries the size to the market
 * screen, where the book, the fresh quote and the ticket are. The label names the size for the same reason:
 * a number carried between two screens has to be visible in both.
 */
export function exitHref(position: PortfolioPosition): string {
  return `/market/${encodeURIComponent(position.marketId)}`;
}

/** The heading a group of negRisk legs gets: the event's exposure, and how many legs can pay. */
export function exposureNote(group: NegRiskGroup): string {
  return `${group.legs} legs of one event · ${moneyText(group.exposureMicro)} at risk · at most ${moneyText(group.maxPayoutMicro)} can pay`;
}

/**
 * Whether a set of legs can all win.
 *
 * The kit's point is that these are not independent bets. This function only answers the question the screen
 * has to answer — whether the legs are exclusive — and it answers it from `legs > 1`, because the server only
 * forms a group where the event is negRisk. The note is what the screen renders; the flag is what the tests
 * assert.
 */
export function mutuallyExclusive(group: NegRiskGroup): boolean {
  return group.legs > 1;
}

/** The totals block, in the order a user reads it: what I hold, what it is worth, what is already cash. */
export function totalRows(totals: Portfolio["totals"], maxDrawdownMicro: number): { id: string; micro: number; kind: "money" | "pnl" }[] {
  return [
    { id: "value", micro: totals.valueMicro, kind: "money" },
    { id: "costBasis", micro: totals.costBasisMicro, kind: "money" },
    { id: "unrealised", micro: totals.unrealisedMicro, kind: "pnl" },
    { id: "cash", micro: totals.cashMicro, kind: "money" },
    { id: "equity", micro: totals.equityMicro, kind: "money" },
    // The drawdown is a total, not a property of the chart. It ships beside the equity it belongs to.
    { id: "drawdown", micro: maxDrawdownMicro, kind: "pnl" },
  ];
}

/**
 * The order history, with the rows we could not resolve kept and marked.
 *
 * P06's finding: the venue reports lifecycle states we do not model, so an order can be neither open nor
 * closed from where we sit. Those rows are the ones a user most needs ("why is my money gone?"), and a
 * filtered table answers that question with silence.
 */
export function orderRows(portfolio: Portfolio): {
  id: string;
  marketId: string;
  state: string;
  unresolved: boolean;
  reason: string;
  shares: string;
  price: string;
  notionalMicro: number;
  atMs: number;
}[] {
  const rows = portfolio.orders.map((o) => ({
    id: o.intentId,
    marketId: o.marketId,
    state: o.state,
    unresolved: false,
    reason: o.reason ?? "",
    shares: o.shares,
    price: o.price,
    notionalMicro: o.notionalMicro,
    atMs: o.createdMs,
  }));
  for (const u of portfolio.unknownLifecycle) {
    rows.push({
      id: u.venueOrderId || u.intentId,
      marketId: "",
      state: u.state,
      unresolved: true,
      reason: u.reason,
      shares: "",
      price: "",
      notionalMicro: 0,
      atMs: u.atMs,
    });
  }
  return rows.sort((a, b) => b.atMs - a.atMs);
}

/** The sentence an unresolved row carries. Same rule as a mark: the absence is stated, never left blank. */
export function unresolvedText(row: { state: string; reason: string }): string {
  return `the venue's lifecycle for this order is "${row.state}", which we do not model${row.reason ? ` (${row.reason})` : ""} — it is shown here rather than hidden, because an order that vanished from a table is an order whose money vanished with it`;
}

/**
 * The CSV, from the payload.
 *
 * Columns come from the wire so the export and the table are the same document; the values are integers of
 * micro-USDC where the name says `Micro`, which the header row states in the second line rather than leaving a
 * tax preparer to guess whether `notionalMicro` is dollars.
 */
export function csvText(portfolio: Portfolio): string {
  const cols = portfolio.csv.columns;
  const lines = [cols.join(","), cols.map((c) => (c.endsWith("Micro") ? "integer micro-USDC (1e-6 USD)" : "")).join(",")];
  for (const o of portfolio.orders) {
    const cell: Record<string, string> = {
      intentId: o.intentId,
      marketId: o.marketId,
      state: o.state,
      shares: o.shares,
      price: o.price,
      notionalMicro: String(o.notionalMicro),
      createdMs: String(o.createdMs),
    };
    lines.push(cols.map((c) => quoteCsv(cell[c] ?? "")).join(","));
  }
  return lines.join("\n") + "\n";
}

function quoteCsv(value: string): string {
  return /[",\n]/.test(value) ? `"${value.replaceAll('"', '""')}"` : value;
}

export function csvFilename(nowMs: number): string {
  const d = new Date(nowMs);
  const pad = (n: number) => String(n).padStart(2, "0");
  return `polygm-portfolio-${d.getUTCFullYear()}${pad(d.getUTCMonth() + 1)}${pad(d.getUTCDate())}.csv`;
}

/**
 * The curve's geometry.
 *
 * The line and the drawdown shade come from the SAME builder the dossier uses (`curvePoints` in `tape.ts`), and
 * that is a rule rather than a convenience: two screens drawing one shape from two sets of arithmetic is how a
 * drawdown stops matching its own PnL line. The benchmark is the only line this screen adds, and it is a
 * horizontal one, so it needs one number rather than a path.
 *
 * Nothing here rounds money. A curve's y-coordinate is geometry, and it is computed from the integer micro
 * values the payload already carries — the money itself is never a float, and the gate (c7) refuses `.toFixed`
 * anywhere outside `src/money/` for exactly this reason.
 */
export type CurveGeometry = {
  pnl: string;
  drawdown: string;
  zeroY: number;
  benchmarkY: number | null;
  min: number;
  max: number;
};

export function curveGeometry(points: CurvePoint[], benchmarkMicro: number | null, width = 640, height = 170): CurveGeometry {
  const shared = sharedCurvePoints(points, width, height);
  const span = shared.max - shared.min || 1;
  const yOf = (micro: number) => Math.round(height - ((micro - shared.min) / span) * height);
  return {
    pnl: shared.pnl,
    drawdown: shared.drawdown,
    zeroY: yOf(0),
    benchmarkY: benchmarkMicro === null ? null : yOf(benchmarkMicro),
    min: shared.min,
    max: shared.max,
  };
}

/** The line's own label, so the curve is never an unnamed series. */
export function curveLabel(): string {
  return "cumulative cash (realised) — every fill that settled, in the order it settled";
}

export function benchmarkLabel(portfolio: Portfolio): string {
  return portfolio.benchmark.note;
}

/** D6's empty state points at the markets page. The wire names the route; the screen names the sentence. */
export function emptyTarget(portfolio: Portfolio): string {
  const href = portfolio.emptyState.split(" ")[0] ?? "";
  return href.startsWith("/") ? href : "/markets";
}

export function hasPositions(portfolio: Portfolio | null): boolean {
  return (portfolio?.positions.length ?? 0) > 0;
}
