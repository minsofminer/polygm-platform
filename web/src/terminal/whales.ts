/**
 * D4 · the whale tracker, as decisions rather than as markup.
 *
 * The tracker's job is to make a *threshold* a thing the user owns. Four rules, and each is a function here:
 *
 *  1. **The rule travels with the feed.** `thresholds` arrives per market; the screen renders each market's own
 *     sentence (`thresholdRule`, `thresholdReason`) instead of declaring one global line, because $5,000 is a
 *     whale in one market and the median fill in another.
 *  2. **Severity is a ratio, and the formula is on screen.** `severityOrder`/`severityTone` order the feed and
 *     colour it; the sentence itself comes from the server (`severityRule`), so the number and the explanation
 *     cannot drift.
 *  3. **A saved view is a filter plus a decision.** `viewToFilters`/`filtersToBody` round-trip a view through the
 *     same knobs the API stores, so "load this view" is reproducible from its own row and "save this view" writes
 *     the knobs the user was just looking at. A notifying view needs a market target — the API refuses it with
 *     409, and `notifyRefusal` explains it BEFORE the request rather than after.
 *  4. **Buckets are defaults, not laws.** `bucketDefaults` states the floor each market-size bucket starts at and
 *     that it is tunable, because a floor a user cannot move is a floor that is wrong for every market they care
 *     about.
 */
import type { WhaleView } from "./wire";

export const SEVERITIES = ["info", "notice", "urgent"] as const;
export type Severity = (typeof SEVERITIES)[number];

export const CHANNELS = ["telegram", "email", "webhook"] as const;
export type Channel = (typeof CHANNELS)[number];

/** The market-size buckets the API's thresholds are derived from, with the floors the seed ships. */
export const BUCKETS: { id: string; label: string; floorMicro: number }[] = [
  { id: "small", label: "small markets", floorMicro: 500_000_000 },
  { id: "mid", label: "mid markets", floorMicro: 2_000_000_000 },
  { id: "large", label: "large markets", floorMicro: 10_000_000_000 },
];

/** Higher is louder. Used for sorting a mixed feed and for the tone of a badge. */
export function severityOrder(severity: string): number {
  const index = SEVERITIES.indexOf(severity as Severity);
  return index === -1 ? 0 : index;
}

export function severityTone(severity: string): "info" | "notice" | "urgent" {
  return (SEVERITIES.includes(severity as Severity) ? severity : "info") as "info" | "notice" | "urgent";
}

/** A ratio in basis points as the multiple a user reads: 120 000 bps = 12x, 9 450 bps = 0.9x. */
export function ratioText(ratioBps: number): string {
  const whole = Math.trunc(ratioBps / 10_000);
  const frac = Math.abs(ratioBps % 10_000);
  if (!frac) return `${whole}x`;
  const decimals = String(frac).padStart(4, "0").replace(/0+$/, "");
  return `${whole}.${decimals}x`;
}

export type WhaleFilters = {
  scope: "global" | "market";
  marketId: string;
  windowMs: number;
  multiple: number | null;
  minSeverity: Severity;
  /** Set by the badge filter: classification labels come from the fill, not from the query. */
  label: string;
};

export const EMPTY_WHALE_FILTERS: WhaleFilters = {
  scope: "global",
  marketId: "",
  windowMs: 86_400_000,
  multiple: null,
  minSeverity: "info",
  label: "",
};

/** The query the feed takes. `multiple` is omitted rather than sent as null: the API's pattern rejects null. */
export function feedQuery(filters: WhaleFilters): Record<string, string | number | undefined> {
  return {
    scope: filters.scope,
    marketId: filters.scope === "market" ? filters.marketId : undefined,
    windowMs: filters.windowMs,
    multiple: filters.multiple ?? undefined,
    minSeverity: filters.minSeverity,
    limit: 100,
  };
}

/**
 * A saved view's stored knobs, back into the screen's filter state.
 *
 * `filters_json` is written by the API from the same three knobs, and this is the reader for it: an unknown
 * severity or a missing multiple falls back to the empty state rather than putting the screen into a filter the
 * API would reject.
 */
export function viewToFilters(view: Pick<WhaleView, "filters" | "scope" | "marketId" | "severity">): WhaleFilters {
  const stored = (view.filters ?? {}) as Record<string, unknown>;
  const minSeverity = SEVERITIES.includes(stored.minSeverity as Severity) ? (stored.minSeverity as Severity) : view.severity;
  return {
    ...EMPTY_WHALE_FILTERS,
    scope: view.scope === "market" ? "market" : "global",
    marketId: view.marketId ?? "",
    minSeverity: SEVERITIES.includes(minSeverity as Severity) ? (minSeverity as Severity) : "notice",
    multiple: typeof stored.multiple === "number" ? Number(stored.multiple) : null,
  };
}

/** The body a view is saved with. `marketId` decides the scope, exactly as the API's own rule states. */
export function filtersToBody(name: string, filters: WhaleFilters, channel: Channel | null): Record<string, unknown> {
  return {
    name: name.trim(),
    minSeverity: filters.minSeverity,
    // The API stores the knobs that produced the view, so the body carries them and not a rendered sentence.
    ...(filters.multiple ? { multiple: Math.trunc(filters.multiple) } : {}),
    ...(filters.scope === "market" && filters.marketId ? { marketId: filters.marketId } : {}),
    ...(channel ? { channel } : {}),
  };
}

/**
 * The refusal a notifying view would meet, checked in the screen.
 *
 * `alert_rules` requires a target, so a global view cannot notify; the API answers 409 `REFUSED` and this
 * function produces the same sentence before the request. The screen shows it next to the channel control
 * rather than letting a user fill in a form that cannot be saved.
 */
export function notifyRefusal(filters: WhaleFilters, channel: Channel | null): string | null {
  if (!channel) return null;
  if (filters.scope === "market" && filters.marketId) return null;
  return "a notifying view needs a market: alerts have to have a target, so pick a market before choosing a channel";
}

/** A view's one-line summary, used in the list and in a feed header. */
export function viewSummary(view: Pick<WhaleView, "name" | "scope" | "marketId" | "severity" | "channel" | "notifies">): string {
  const scope = view.scope === "market" ? `market ${view.marketId ?? "?"}` : "every market";
  const delivery = view.notifies ? `notifies on ${view.channel ?? "?"}` : "a filter you look at";
  return `${view.name} · ${scope} · ${view.severity} · ${delivery}`;
}

/** The fire budget a view's rule carries, or the honest statement that it has none. */
export function budgetText(view: Pick<WhaleView, "notifies" | "firesPerWindow" | "ruleWindowMs" | "ruleEnabled">): string {
  if (!view.notifies) return "no rule: this view never fires";
  if (view.firesPerWindow === null || view.ruleWindowMs === null) return "rule without a budget (unexpected)";
  const hours = view.ruleWindowMs / 3_600_000;
  const window = Number.isInteger(hours) ? `${hours}h` : `${Math.round(view.ruleWindowMs / 60_000)}m`;
  return `${view.firesPerWindow} fires per ${window}${view.ruleEnabled === false ? " · paused" : ""}`;
}

/** The bucket table, as sentences: the floor, and the fact that it is a default. */
export function bucketDefaults(): { id: string; sentence: string }[] {
  return BUCKETS.map((b) => ({
    id: b.id,
    sentence: `${b.label}: ${usd(b.floorMicro)} floor by default, tunable per view — the relative term still applies`,
  }));
}

/** Where a whale row can be sent: the three actions D4 promises, each naming what it will do. */
export function exportTargets(row: { anonWallet: string; marketId: string }): { id: "watchlist" | "follow" | "copy"; href: string; note: string }[] {
  return [
    { id: "watchlist", href: `/markets?watch=${encodeURIComponent(row.marketId)}`, note: "add this market to a watchlist" },
    { id: "follow", href: `/trader/${encodeURIComponent(row.anonWallet)}?follow=1`, note: "follow this wallet: its fills get a badge and can chime" },
    { id: "copy", href: `/copy?source=${encodeURIComponent(row.anonWallet)}&dryRun=1`, note: "set up a copy config: it starts in dry-run" },
  ];
}

/**
 * The bucket a market falls into, by its own 7-day volume, mirroring the server's own thresholds.
 *
 * Client-side sizing is for LABELLING only: the threshold the feed used arrives with the feed, and the screen
 * never recomputes it. This exists so a row can say "large market" without a second request.
 */
export function bucketFor(volume7dMicro: number): string {
  const large = BUCKETS.find((b) => b.id === "large")?.floorMicro ?? 10_000_000_000;
  const mid = BUCKETS.find((b) => b.id === "mid")?.floorMicro ?? 2_000_000_000;
  if (volume7dMicro >= large * 10) return "large";
  if (volume7dMicro >= mid * 10) return "mid";
  return "small";
}

/**
 * The tunable multiple, from what the user typed.
 *
 * Digits only, then clamped: the API accepts 2..1000 and refuses anything else with a 422, and a screen that
 * forwards "1e3" or "3.5" would be answered by a validation error the user cannot act on. Returning `null` for
 * anything unusable means the request simply omits the knob, which is the documented default.
 */
export function parseMultiple(raw: string): number | null {
  const text = raw.trim();
  if (!/^\d+$/.test(text)) return null;
  const n = Number.parseInt(text, 10);
  if (n < 2) return null;
  return Math.min(1000, n);
}

/** A compact count sentence for a feed header, from the server's own counts. */
export function feedCounts(counts: { overThreshold?: number; returned?: number; marketsWithFills?: number } | undefined): string {
  const over = counts?.overThreshold ?? 0;
  const shown = counts?.returned ?? 0;
  const markets = counts?.marketsWithFills ?? 0;
  return `${shown} of ${over} fills over threshold across ${markets} markets`;
}

export function usd(micro: number): string {
  const sign = micro < 0 ? "-" : "";
  const abs = Math.abs(micro);
  const whole = Math.trunc(abs / 1_000_000);
  const cents = Math.trunc((abs % 1_000_000) / 10_000);
  const grouped = String(whole).replace(/\B(?=(\d{3})+(?!\d))/g, "\u2009");
  return `${sign}$ ${grouped}.${String(cents).padStart(2, "0")}`;
}
