/**
 * D5 · the Wallet Radar's rules.
 *
 * The phase's cost control is a rule about the SCREEN as much as about the API: ten markets per scan, a per-day
 * quota per plan, a cache that makes an identical scan free, and a job when the scan would take too long. Every
 * one of those is visible here rather than implied:
 *
 *  - `toggleMarket` refuses the eleventh market with the limit in the sentence, instead of quietly dropping one.
 *    A selection that silently loses a market is a scan that answers a question the user did not ask.
 *  - `quotaText` renders the API's own numbers and its own note ("this scan came from the cache and cost you
 *    nothing"), because the number a user sees has to be the number the server enforces.
 *  - `unrankedNote` exists because the profit ranking applies a SAMPLE GATE: wallets under it are returned in
 *    their own list with their reason. A ranking that silently drops the small sample looks like a ranking with
 *    no small samples in it.
 *
 * Four rankings arrive in ONE response, so switching tabs costs nothing — and `rowsFor` reads them from the
 * payload rather than re-scanning, which is what makes "the second tab is free" true rather than a promise.
 */
import { formatCents, microToCents } from "@/money/cents";
import { exportTargets } from "./whales";
import type { LabelFact, RadarResult, RadarRow, RadarUnranked } from "./wire";

export const MAX_SCAN_MARKETS = 10;

/** Add a market to the scan, or say why it does not fit. Returns the reason when nothing changed. */
export function toggleMarket(selected: string[], marketId: string): { next: string[]; refused: string | null } {
  if (selected.includes(marketId)) {
    return { next: selected.filter((m) => m !== marketId), refused: null };
  }
  if (selected.length >= MAX_SCAN_MARKETS) {
    return {
      next: selected,
      refused: `a scan is up to ${MAX_SCAN_MARKETS} markets: one more would be a scan the API refuses, and a selection that silently dropped one would answer a question you did not ask`,
    };
  }
  return { next: [...selected, marketId], refused: null };
}

export function rowsFor(result: RadarResult | null, rankingId: string): RadarRow[] {
  if (!result) return [];
  return result.rankings[rankingId] ?? result.items ?? [];
}

/** The API's meta entries carry the label and the question; this picks the one being looked at. */
export function rankingMeta(result: RadarResult | null, rankingId: string): { id: string; label: string; question: string } | null {
  const meta = result?.rankingsMeta.find((m) => m.id === rankingId);
  return meta ? { id: meta.id, label: meta.label, question: (meta as { question?: string }).question ?? "" } : null;
}

/**
 * The quota, as the sentence a user needs before pressing the button.
 *
 * `cached` matters more than it looks: a user who is told a scan is free will run it again, and a user who is
 * not told will hoard their quota. The API's own note is appended rather than paraphrased.
 */
export function quotaText(result: RadarResult | null): string {
  if (!result) return "no scan yet: a scan costs one of your daily allowance";
  const { usedToday, perDay, cached, note } = result.quota;
  const spent = cached ? `${usedToday} of ${perDay} scans used today` : `${usedToday + 1} of ${perDay} scans used today after this one`;
  return `${spent} · ${note}`;
}

/** Wallets under the sample gate are shown in their own list. Silence about them is the failure mode. */
export function unrankedNote(result: RadarResult | null): string {
  const rows = result?.unranked ?? [];
  if (!result || rows.length === 0) return "";
  const gate = result.sampleGate ?? 0;
  return `${rows.length} wallet(s) are NOT in the profit ranking: fewer than ${gate} settled markets is not a profit record, and each one is listed below with its own reason`;
}

export function unrankedRows(result: RadarResult | null): RadarUnranked[] {
  return result?.unranked ?? [];
}

/** A row's money, through the one formatter, never as a raw number. */
export function moneyText(micro: number, signed = false): string {
  return formatCents(microToCents(micro), { currency: "$", signed });
}

/**
 * A win rate, or the refusal. Identical rule to the dossier's: the `insufficientSample` flag wins over any rate
 * that arrived with it, because the failure this guards is a percentage nobody can justify.
 */
export function winRateText(row: { winRateBps: number | null; insufficientSample: boolean }): string {
  if (row.insufficientSample || row.winRateBps === null) return "insufficient sample";
  return `${Math.trunc(row.winRateBps / 100)}.${String(row.winRateBps % 100).padStart(2, "0")}%`;
}

/**
 * A classification label's rule and disclaimer, as one sentence.
 *
 * The standing constraint: every label shows the rule that produced it and its disclaimer as TEXT. The radar
 * renders the same sentence the tape and the whale tracker render, from the same payload field, so the three
 * screens cannot drift into three descriptions of one label.
 */
export function labelSentence(fact: LabelFact): string {
  const parts = [fact.label, fact.rule, fact.disclaimer].filter((p): p is string => typeof p === "string" && p.length > 0);
  return parts.join(" · ");
}

/** Labels whose rule or disclaimer is missing are dropped rather than rendered as bare chips. */
export function publishableLabels(row: { labels?: LabelFact[] }): LabelFact[] {
  return (row.labels ?? []).filter(
    (l) => l.publishable === true && typeof l.rule === "string" && l.rule.length > 0 && typeof l.disclaimer === "string" && l.disclaimer.length > 0,
  );
}

/**
 * The one-click actions on a row: track, follow, copy, open.
 *
 * `exportTargets` is the whale tracker's own table, reused so that "follow" means the same thing on both screens
 * — and it is the same table, not a copy of it, because two lists of destinations is two places for one of them
 * to point somewhere else. The copy destination starts the config in dry-run.
 */
export function rowActions(row: RadarRow): { id: string; href: string; note: string }[] {
  const first = row.matched[0]?.marketId ?? "";
  return [
    ...exportTargets({ anonWallet: row.anonWallet, marketId: first }),
    { id: "open", href: `/trader/${encodeURIComponent(row.anonWallet)}`, note: "open this wallet's dossier: its win rate, its drawdown and its behaviour labels" },
  ];
}

/** The matched markets as chips: the market ids the ranking matched, with their questions when we have them. */
export function matchedChips(row: RadarRow): { marketId: string; label: string }[] {
  return row.matched.map((m) => ({ marketId: m.marketId, label: m.question || m.marketId }));
}

/** Whether a scan can be run at all, and the sentence when it cannot. */
export function scanProblems(selected: string[]): string[] {
  if (selected.length === 0) return ["pick at least one market to scan"];
  if (selected.length > MAX_SCAN_MARKETS) return [`a scan is up to ${MAX_SCAN_MARKETS} markets`];
  return [];
}
