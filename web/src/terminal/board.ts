/**
 * D3 · the leaderboard surfaces, as decisions rather than as markup.
 *
 * The rating panel is where a user meets their own standing, so the rules this file holds are the ones a wrong
 * rendering would turn into a lie:
 *
 *  1. **A rank is always shown with the board it is on.** `#47` alone means nothing: 47th of 64 on risk-adjusted
 *     PnL is a very different sentence from 47th of 9,000 on volume, and the badge carries both.
 *  2. **The gap is in the board's own field.** 625 bps of score, $1,200 of turnover, one copier — the server
 *     says which (`orderField`/`orderUnits`), and this module never guesses. `toPass` is what it is for the same
 *     reason: a tie falls through the stated tie-breaks, so the number that moves you is `above + 1`.
 *  3. **An empty sparkline is empty.** History that has not been written yet renders as "no history yet", not as
 *     a flat line at rank 0 — a flat line is a shape, and a shape is a claim about the past.
 *  4. **A follow is not a copy.** The row says what it is, and the button's label is the state, so nobody can
 *     read "follow" as "trade like them".
 *  5. **Nothing is formatted here.** Money goes through the display strings the server already sent; a rank, a
 *     percentile and a bps are rendered as counts, which is what they are.
 */
import { microToCents, type Cents } from "@/money/cents";
import { bpsText } from "./copy";

/** What one board orders by, and in which unit. Sourced from the server, never assumed. */
export type OrderUnits = "bps" | "micro" | "count";

export type StandingGap = {
  rankAbove: number;
  anonAbove: string;
  field: string;
  units: OrderUnits;
  value: number;
  valueAbove: number;
  delta: number | null;
  toPass: number | null;
  note: string;
};

export type Standing = {
  anon: string;
  board: string;
  label: string;
  window: string;
  state: "ranked" | "provisional" | "blew_up" | "unranked" | "unknown";
  rank: number | null;
  rankedTotal: number;
  percentileBps: number | null;
  orderField: string;
  orderUnits: OrderUnits;
  rankBadge?: { rank: number; rankedTotal: number; text: string } | null;
  above?: { anon: string } | null;
  below?: { anon: string } | null;
  gap?: StandingGap | null;
  reasons: string[];
  note: string;
  formula?: string;
  gate?: string;
  history?: {
    days: number;
    points: { tsMs: number; rank: number }[];
    snapshots: number;
    latestRank: number | null;
    bestRank: number | null;
    worstRank: number | null;
    delta: number | null;
    note: string;
  };
};

/** One field, said in its own unit. The server's `orderUnits` decides; nothing here infers it from the number. */
export function fieldText(value: number | null, units: OrderUnits): string {
  if (value === null || value === undefined) return "—";
  if (units === "micro") return `${microToCents(value) / 100} USDC`;
  if (units === "count") return `${value} ${value === 1 ? "copier" : "copiers"}`;
  return `${value} bps`;
}

/** `#47 of 64` — a rank is never shown without the board it is on, and `rankBadge` is the server's own object. */
export function badgeText(standing: Pick<Standing, "rank" | "rankedTotal" | "rankBadge">): string {
  const badge = standing.rankBadge;
  if (!badge) return "not ranked";
  const total = badge.rankedTotal ?? standing.rankedTotal;
  return `${badge.text} of ${total}`;
}

/**
 * The gap sentence, built from the two values rather than from the delta alone.
 *
 * "625 bps behind w_57c7dcc029" is the readable half; the sentence also carries what would move the row, because
 * a gap a user cannot close is a fact and a gap with an instruction is a product.
 */
export function gapSentence(gap: StandingGap | null | undefined, units: OrderUnits): string {
  if (!gap || gap.delta === null) return "";
  if (gap.delta <= 0) return `level with ${gap.anonAbove} at ${fieldText(gap.value, units)}`;
  return `${fieldText(gap.delta, units)} behind ${gap.anonAbove} (${fieldText(gap.valueAbove, units)} to your ${fieldText(
    gap.value,
    units,
  )}); ${fieldText(gap.toPass, units)} would pass them`;
}

/** The state, as a sentence: a badge with a colour and no words is a badge nobody can act on. */
export function stateText(standing: Standing): string {
  if (standing.state === "blew_up") return "blew up: the account went to zero and is still on the board";
  if (standing.state === "provisional") return `provisional: a wallet under 7 days old is labelled, not hidden`;
  if (standing.state === "unranked") return "not ranked yet";
  if (standing.state === "unknown") return "no activity we can rank";
  return "ranked";
}

/**
 * Where a rank sits in the board, as a readable share.
 *
 * `percentileBps` is the server's rounded-up rank share, so this divides rather than re-deriving it: two
 * roundings of the same fraction is how a screen and its API end up disagreeing by one place.
 */
export function percentileText(standing: Pick<Standing, "percentileBps" | "rank" | "rankedTotal">): string {
  if (standing.percentileBps === null || standing.percentileBps === undefined || !standing.rank) return "";
  // One decimal, always, formatted with INTEGER arithmetic: a percentile shown to one decimal is a rounded
  // number, and rounding it with a float is the same class of mistake as doing it with money. `bpsText` is the
  // app's one bps formatter (D7 wrote it), so a percentage looks the same on this screen as on the copy screen.
  return `top ${bpsText(standing.percentileBps)} of ${standing.rankedTotal}`;
}

/** The refusal, as one sentence per reason: the numbers are the server's, and they are the whole point. */
export function refusalLines(standing: Standing): string[] {
  if (standing.state !== "unranked") return [];
  return standing.reasons.length ? standing.reasons : [standing.note];
}

export type SparkPoint = { x: number; y: number };

/**
 * The sparkline's geometry, from the history the server sent.
 *
 * Two rules, both about honesty rather than about pixels: an empty history returns `[]` (the caller renders the
 * "no history yet" sentence and no line at all), and a single point returns one point rather than a
 * zero-length line that would draw as nothing and look like missing data. Ranks are inverted on the y axis: rank
 * 1 is the top of the chart, and a line that fell means the trader rose.
 */
export function sparkPath(
  points: { rank: number; tsMs: number }[] | undefined,
  width = 120,
  height = 24,
): SparkPoint[] {
  const pts = points ?? [];
  if (pts.length === 0) return [];
  const ranks = pts.map((p) => p.rank);
  const lo = Math.min(...ranks);
  const hi = Math.max(...ranks);
  const span = Math.max(1, hi - lo);
  const step = pts.length === 1 ? 0 : width / (pts.length - 1);
  return pts.map((p, i) => ({
    x: Math.round(i * step),
    y: Math.round(((p.rank - lo) / span) * height),
  }));
}

/** "your best rank was #12" — the two facts a sparkline cannot say in words, and the reason to keep the words. */
export function historySentence(standing: Standing): string {
  const h = standing.history;
  if (!h || h.snapshots === 0) return "no history yet: the rank history is written by the recompute";
  const delta = h.delta ?? 0;
  const move = delta === 0 ? "unchanged" : delta < 0 ? `up ${Math.abs(delta)}` : `down ${delta}`;
  return `${h.snapshots} snapshots over ${h.days} days; best #${h.bestRank}, worst #${h.worstRank}, ${move} since the first`;
}

export type ComparisonRow = Standing["rankBadge"] extends never ? never : Record<string, unknown>;

/** One comparison row's cells: the label, the value already formatted by the server, and the ranked flag. */
export function comparisonCells(row: Record<string, unknown>): { label: string; value: string }[] {
  const cells = [
    { label: "rank", value: row.rank ? `#${row.rank}` : "unranked" },
    { label: "realised PnL", value: String(row.realised ?? "—") },
    { label: "drawdown", value: String(row.drawdown ?? "—") },
    { label: "settled markets", value: String(row.settledMarkets ?? "—") },
    { label: "verified turnover", value: String(row.verifiedVolume ?? "—") },
  ];
  const share = row.bestTradeShareBps;
  if (typeof share === "number") {
    cells.push({ label: "best trade", value: `${bpsText(share)} of realised` });
  }
  return cells;
}

/**
 * The comparison's verdict sentence, from `order` rather than from `rows`.
 *
 * `rows` is the order the caller asked for; the board's order lives in the pairwise sentences. A screen that
 * crowned `rows[0]` would crown whoever the user clicked first.
 */
export function comparisonVerdict(payload: {
  rows?: Record<string, unknown>[];
  order?: { a: string; b: string; why: string; aAbove?: boolean }[];
  verdict?: string;
}): string {
  const pairs = payload.order ?? [];
  if (!pairs.length) return String(payload.verdict ?? "");
  const leading = pairs.filter((p) => p.aAbove).map((p) => p.a);
  const winner = leading.length === 1 ? leading[0] : (payload.verdict ?? "").split(" ")[0];
  const why = pairs[0]?.why ?? "";
  return `${winner} is ahead in every stated pair; ${why}`;
}

/** A follow row's state, as the sentence beside the button. */
export function followStateText(row: {
  state: "ranked" | "unranked" | "absent";
  rank: number | null;
  reasons?: string[];
  sampleNote?: string;
}): string {
  if (row.state === "ranked") return row.rank ? `#${row.rank}` : "ranked";
  if (row.state === "unranked") return "off the board";
  return "no activity we can rank";
}

/** The follow button's label: the action the user will take, in the vocabulary of the thing being changed. */
export function followAction(following: boolean): "follow" | "unfollow" {
  return following ? "unfollow" : "follow";
}

/** A followed wallet's drawdown cell: a PnL row without it is the failure the whole product forbids. */
export function followCells(row: Record<string, unknown>): { label: string; value: string; bad: boolean }[] {
  const realised = Number(row.realisedMicro ?? 0);
  return [
    { label: "realised PnL", value: String(row.realised ?? "—"), bad: realised < 0 },
    { label: "max drawdown", value: String(row.drawdown ?? "—"), bad: false },
  ];
}

/** Money as a Cents value, for the few places a component needs the number layer rather than a string. */
export function centsOf(micro: number | null | undefined): Cents {
  return microToCents(micro ?? 0);
}
