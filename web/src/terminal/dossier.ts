/**
 * D3 · the trader dossier, as decisions rather than as markup.
 *
 * Everything here is a pure function over the wire shapes, because the dossier is the screen where a wrong
 * *sentence* is more dangerous than a wrong pixel: a win rate shown without its sample, a PnL curve drawn
 * without its drawdown, a behaviour label shown without its rule. Each of those is enforceable in a unit test
 * here and invisible in a screenshot.
 *
 * The rules this file exists to hold:
 *
 *  1. **The window switcher recomputes the WHOLE metric set.** The server sends four windows of one metric set;
 *     the screen never mixes a 7-day win rate with a 90-day volume, because the switcher picks one window and
 *     every number on the screen comes from it.
 *  2. **A win rate below the gate is not a number.** `winRateText` returns "insufficient sample" with the
 *     server's sentence, and there is no code path that renders a percentage for an ungated window.
 *  3. **PnL and its drawdown travel together.** `curvePoints` draws both, and `metricRows` refuses to show
 *     realised PnL without the drawdown of the same window.
 *  4. **A behaviour label without its rule is a horoscope.** `behaviourRows` carries rule + disclaimer, and
 *     drops a label fact that arrived without them rather than rendering a bare accusation.
 *  5. **The header never claims to know an address.** The API is pseudonymous by design (gate c2), so the
 *     header offers the pseudonym to copy and says plainly that the on-chain address is not resolvable from
 *     here — instead of an explorer link that would 404 or, worse, guess.
 */
import { formatCents, microToCents, type Cents } from "@/money/cents";
import { curvePoints as sharedCurvePoints, formatMicro, priceUnitsFor } from "./tape";
import type { CurvePoint, LabelFact, MetricWindow, TraderDossier } from "./wire";

export const DOSSET_WINDOWS = ["7d", "30d", "90d", "all"] as const;
export type WindowKey = (typeof DOSSET_WINDOWS)[number];

/** Which window a screen opens on, and why: 30 days is long enough to contain a drawdown and short enough that
 *  the numbers are about the trader who exists now. */
export const DEFAULT_WINDOW: WindowKey = "30d";

export function isWindowKey(value: string): value is WindowKey {
  return (DOSSET_WINDOWS as readonly string[]).includes(value);
}

/** The switcher, with the server's own list honoured first: a build that learns a new window shows it. */
export function windowChoices(dossier: Pick<TraderDossier, "windows">): string[] {
  const known = (dossier.windows ?? []).filter((w) => isWindowKey(w));
  return known.length ? [...known].sort((a, b) => DOSSET_WINDOWS.indexOf(a as WindowKey) - DOSSET_WINDOWS.indexOf(b as WindowKey)) : [...DOSSET_WINDOWS];
}

export type MetricRow = {
  id: string;
  /** Already formatted, through the number layer: never a raw number in the markup. */
  value: string;
  /** The sentence that must appear next to the value when it needs one (sample, drawdown reason). */
  note: string;
  tone: "neutral" | "good" | "bad" | "unknown";
};

/**
 * The metric set for ONE window. Called by the switcher, and by the tests that assert a window change recomputes
 * every row rather than swapping two numbers in place.
 */
export function metricRows(window: MetricWindow | undefined, sampleGate: number): MetricRow[] {
  if (!window) {
    return [{ id: "unavailable", value: "—", note: "", tone: "unknown" }];
  }
  const win = winRateText(window, sampleGate);
  const realised = microToCents(window.realisedMicro);
  const unrealised = microToCents(window.unrealisedMicro);
  const drawdown = microToCents(window.maxDrawdownMicro);
  return [
    { id: "volume", value: formatCents(microToCents(window.volumeMicro), { currency: "$" }), note: "", tone: "neutral" },
    { id: "realised", value: formatCents(realised, { signed: true, currency: "$" }), note: "", tone: toneOf(realised) },
    { id: "unrealised", value: formatCents(unrealised, { signed: true, currency: "$" }), note: "", tone: toneOf(unrealised) },
    { id: "winRate", value: win.value, note: win.note, tone: win.tone },
    // The drawdown sits directly under the PnL rows and carries the window's own number: a curve can show a
    // shape, but the metric set is where it becomes a quantity a user can compare across windows.
    { id: "maxDrawdown", value: formatCents(drawdown, { currency: "$" }), note: "", tone: drawdown > 0 ? "bad" : "neutral" },
    { id: "avgHold", value: holdText(window.avgHoldMs), note: `${window.medianHoldMs ? holdText(window.medianHoldMs) + " median" : ""}`.trim(), tone: "neutral" },
    { id: "best", value: formatCents(microToCents(window.bestMicro), { signed: true, currency: "$" }), note: "", tone: toneOf(microToCents(window.bestMicro)) },
    { id: "worst", value: formatCents(microToCents(window.worstMicro), { signed: true, currency: "$" }), note: "", tone: toneOf(microToCents(window.worstMicro)) },
    { id: "fills", value: String(window.fills), note: `${window.distinctMarkets} markets`, tone: "neutral" },
  ];
}

/**
 * The win rate, or the refusal. `null` is `null` for one documented reason and the sentence comes from the
 * server, so the screen and the API cannot disagree about what "not enough data" means.
 */
export function winRateText(window: Pick<MetricWindow, "winRateBps" | "insufficientSample" | "sampleNote" | "resolvedMarkets">, sampleGate: number): {
  value: string;
  note: string;
  tone: MetricRow["tone"];
} {
  // The flag wins over the number. The API sends `null` below the gate, and this screen refuses to print a
  // percentage whenever `insufficientSample` is set even if a rate came with it: the failure this guards is a
  // server bug (or a future field) that ships a percentage nobody can justify, and the safe direction is to show
  // the refusal. A win rate is the number this phase is most likely to get wrong in public.
  if (window.insufficientSample || window.winRateBps === null || window.winRateBps === undefined) {
    return {
      value: "insufficient sample",
      note: window.sampleNote || `a win rate needs ${sampleGate} settled markets; this trader has ${window.resolvedMarkets}`,
      tone: "unknown",
    };
  }
  // Basis points to a percentage by integer arithmetic on the whole part and the hundredths: 7631 bps is
  // "76.31%", and no float multiplies it on the way to the screen.
  const bps = window.winRateBps;
  const whole = Math.trunc(bps / 100);
  const frac = Math.abs(bps % 100);
  return {
    value: `${whole}%`.replace("%", frac ? `.${String(frac).padStart(2, "0")}%` : "%"),
    note: `${window.resolvedMarkets} settled markets`,
    tone: bps >= 5_000 ? "good" : "neutral",
  };
}

/**
 * The header's "account age", from the first fill we hold.
 *
 * Deliberately NOT "wallet age": we know when this pseudonym first traded in the data we hold, and calling that
 * a wallet's age would be a claim about the chain we cannot make from a tape.
 */
export function accountAgeText(firstFillMs: number | null | undefined, nowMs: number): { value: string; note: string } {
  if (!firstFillMs) return { value: "—", note: "no fills in the stored tape" };
  const days = Math.max(0, Math.floor((nowMs - firstFillMs) / 86_400_000));
  const value = days >= 60 ? `${Math.floor(days / 30)} months` : days >= 1 ? `${days} days` : "today";
  return { value, note: `first fill we hold: ${isoDay(firstFillMs)} (the tape's own history, not the wallet's age)` };
}

/** The badge row: publishable labels only, each with its rule and disclaimer (rule 4 of this file). */
export function behaviourRows(facts: LabelFact[] | undefined): LabelFact[] {
  return (facts ?? []).filter((f) => f.publishable !== false && Boolean(f.rule) && Boolean(f.disclaimer));
}

/** A behaviour label's sentence, one line per fact: what it means, how it was decided, what it does not claim. */
export function labelSentence(fact: LabelFact): string {
  return `${fact.rule} · ${fact.disclaimer}`;
}

/** Confidence as a percentage by integer arithmetic: 820 -> "82%". */
export function confidenceText(confidence: number): string {
  const pct = Math.max(0, Math.min(100, Math.trunc(confidence / 10)));
  return `${pct}%`;
}

export type HistoryFilter = { side: "" | "BUY" | "SELL"; market: string; outcome: string; resolvedOnly: boolean };
export const EMPTY_HISTORY_FILTER: HistoryFilter = { side: "", market: "", outcome: "", resolvedOnly: false };

/** The trade-history filters. `resolvedOnly` exists because a mixed list makes a win rate look worse than it is. */
export function filterHistory(fills: TraderDossier["fills"], filter: HistoryFilter): TraderDossier["fills"] {
  return fills.filter((f) => {
    if (filter.side && f.side !== filter.side) return false;
    if (filter.market && f.marketId !== filter.market) return false;
    if (filter.outcome && f.outcome !== filter.outcome) return false;
    if (filter.resolvedOnly && !f.resolved) return false;
    return true;
  });
}

/** Markets a filter dropdown can offer, in the order the history mentions them. */
export function historyMarkets(fills: TraderDossier["fills"]): { marketId: string; question: string }[] {
  const seen = new Map<string, string>();
  for (const f of fills) if (!seen.has(f.marketId)) seen.set(f.marketId, f.question);
  return [...seen.entries()].map(([marketId, question]) => ({ marketId, question }));
}

/**
 * The row's own verdict: a realised fill says won or lost; an open one says nothing.
 *
 * "Open" is not "break-even". A fill whose market has not resolved has no realised PnL, and rendering `0` for it
 * is the single most common way a trade history flatters a trader.
 */
export function fillVerdict(fill: TraderDossier["fills"][number]): { text: string; tone: MetricRow["tone"]; micro: number | null } {
  if (!fill.resolved) return { text: "open", tone: "unknown", micro: null };
  return {
    text: fill.winner ? "won" : "lost",
    tone: fill.winner ? "good" : "bad",
    micro: fill.realisedMicro,
  };
}

/** A category's share of the trader's volume, from the server's own basis points, rendered without a float. */
export function shareText(shareBps: number): string {
  const whole = Math.trunc(shareBps / 100);
  const frac = Math.abs(shareBps % 100);
  return frac ? `${whole}.${String(frac).padStart(2, "0")}%` : `${whole}%`;
}

/**
 * The PnL curve plus its mandated drawdown overlay, in one function.
 *
 * Both series come out of the same walk over the same points, so there is no call site that can draw the first
 * without the second: the caller receives `pnl` and `drawdown` together, and `drawdownArea` is the closed shape a
 * chart fills under the high-water line. Points the server marked with `drawdownMicro` are trusted for the
 * overlay's depth, and the peak line is drawn from `peakMicro`, so a curve that is under water shows the
 * distance to its own high-water mark rather than a second PnL line.
 */
export function curvePoints(curve: CurvePoint[], width: number, height: number) {
  return sharedCurvePoints(curve, width, height);
}

/** The window's worst drawdown as the sentence under the curve, so the overlay has a number beside it. */
export function drawdownSentence(curve: CurvePoint[], maxDrawdownMicro: number): string {
  const worst = curve.reduce((acc, p) => Math.max(acc, p.drawdownMicro), 0);
  const micro = maxDrawdownMicro || worst;
  if (!micro) return "no drawdown in this window: the curve never went below its own high-water mark";
  return `worst drawdown ${formatCents(microToCents(micro), { currency: "$" })}: the distance below the high-water mark, shown on the curve`;
}

/** The methodology block, rendered as sentences: the screen links to it, and the gate fails if it goes missing. */
export function methodologyLines(methodology: TraderDossier["methodology"] | undefined): string[] {
  if (!methodology) return [];
  const keys = ["winRate", "drawdown", "hold", "sample", "behaviour"] as const;
  const lines = keys.map((k) => methodology[k]).filter((v): v is string => typeof v === "string" && v.length > 0);
  return lines.length ? lines : [String(methodology.path ?? "")].filter(Boolean);
}

/** A market link: the dossier's rows go to the market, and the market's own screen is where the fill's chain
 *  evidence lives. The link carries the fill so the destination can highlight it rather than making the user
 *  find it again. */
export function marketHref(fill: { marketId: string; tokenId: string; tsMs: number }): string {
  return `/market/${encodeURIComponent(fill.marketId)}?fill=${encodeURIComponent(fill.tokenId)}@${fill.tsMs}`;
}

/** The honest header: what we know, and the one thing we deliberately do not. */
export function identityNote(anonWallet: string): string {
  return `pseudonym ${anonWallet} · the product does not resolve a pseudonym to an on-chain address (P10 gate c2), so there is no explorer link here: the market's own screen links the fills it can evidence`;
}

/** A price string to tick units, for rows that render a price rather than money. */
export function priceCell(price: string, tick: string) {
  return priceUnitsFor(price, tick);
}

export function holdText(ms: number): string {
  if (!ms) return "—";
  const minutes = Math.floor(ms / 60_000);
  if (minutes < 60) return `${minutes}m`;
  const hours = Math.floor(minutes / 60);
  if (hours < 48) return `${hours}h`;
  return `${Math.floor(hours / 24)}d`;
}

export function toneOf(cents: Cents): MetricRow["tone"] {
  if (cents > 0) return "good";
  if (cents < 0) return "bad";
  return "neutral";
}

export function isoDay(ms: number): string {
  const d = new Date(ms);
  const pad = (n: number) => String(n).padStart(2, "0");
  return `${d.getUTCFullYear()}-${pad(d.getUTCMonth() + 1)}-${pad(d.getUTCDate())}`;
}

function round(n: number): number {
  return Math.round(n * 100) / 100;
}

/** Re-exported so a screen imports the number layer from here too, rather than reaching into money directly. */
export { formatMicro };
