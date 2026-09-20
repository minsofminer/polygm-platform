/**
 * The anti-gaming screen's pure half: what a finding is called, what it is worth reading for, and what a click
 * sends. No fetch, no JSX — so the parts that must be right (which finding belongs in which section, which
 * buttons a severity justifies, and the exact body and key a decision is recorded with) are testable without a
 * browser, and the screen is left with nothing to decide.
 *
 * Three rules this module exists to enforce:
 *
 *  - **Every row shows both readings.** A finding arrives with its rule and with the innocent reading of the same
 *    shape, and `pairing()` refuses to hand a row to the screen without both. A list that shows only the damning
 *    reading is a list that gets acted on before it is read.
 *  - **A decision carries a reason.** `decision()` returns null rather than posting a short one: the row is the
 *    record that answers an appeal, and "suspicious" is not a record.
 *  - **A cluster is one decision about several wallets.** The API takes one wallet per call, so the screen sends
 *    the worst pair's first wallet and says so — an operator excludes the wallet they can justify, not a group.
 */
import { newIdempotencyKey } from "@/api/client";
import type { RouteKey } from "@/api/routes";

export type Rule = { rule: string; innocent: string };
export type Finding = {
  kind: string;
  severity: number;
  suggested: string;
  rule: string;
  evidence: string[];
  decided?: { action: string; atMs: number };
  wallet?: string;
  anon?: string;
  board?: string;
  climb?: number;
  fromRank?: number;
  toRank?: number;
  settled?: number;
  medianClimb?: number;
  percentileBps?: number;
  wallets?: string[];
  anonWallets?: string[];
  walletCount?: number;
  pairs?: number;
  worstOverlapBps?: number;
  coTimed?: number;
  windowMs?: number;
  referrer?: string;
  anonReferrer?: string;
  referees?: string[];
  anonReferees?: string[];
  refereeCount?: number;
  sharedFunding?: number;
  sharedDevice?: number;
  fastReferees?: string[];
  floorReferees?: string[];
  accrualMicro?: number;
  userId?: string;
  orders?: number;
  markets?: number;
  burstMarkets?: number;
  volumeMicro?: number;
  unpaidBps?: number;
};
export type Dashboard = {
  atMs: number;
  limit: number;
  rules: Record<string, Rule>;
  climbers: Finding[];
  clusters: Finding[];
  chains: Finding[];
  builder: Finding[];
  counts: Record<string, number>;
  evidence: { historyRows: number; decisions: number; newestSnapshotMs: number; fills: number };
};

/** The four sections, in the order an operator should read them: the loudest, most actionable question first. */
export const SECTIONS = ["climbers", "clusters", "chains", "builder"] as const;
export type SectionKey = (typeof SECTIONS)[number];

/** The wallets a finding is about, as the pseudonyms a human quotes — never the raw id. */
export function subjects(f: Finding): string[] {
  if (f.anonWallets?.length) return f.anonWallets;
  if (f.anonReferees?.length) return [...(f.anonReferrer ? [f.anonReferrer] : []), ...f.anonReferees];
  return f.anon ? [f.anon] : [];
}

/** The one wallet a decision would be recorded against, and why that one. */
export function decideTarget(f: Finding): string {
  if (f.wallet) return f.wallet;
  if (f.wallets?.length) return f.wallets[0] ?? "";
  return f.referrer ?? "";
}

/** Both readings, or nothing: a row that cannot show its rule does not render an action. */
export function pairing(f: Finding, rules: Record<string, Rule>): Rule | null {
  const r = rules?.[f.kind];
  if (!r || !r.rule || !r.innocent) return null;
  return r;
}

export function severityLabel(n: number): string {
  return n >= 3 ? "high" : n >= 2 ? "medium" : "low";
}

export type Decision = { key: RouteKey; body: Record<string, unknown>; idempotencyKey: string };

/**
 * The body a click sends, or null when the reason is too short to be a record.
 *
 * The idempotency key is minted per click: a retried click (a double-tap, a flaky network) must replay the first
 * answer rather than writing a second row, because the boards replay the NEWEST row and a duplicate reads as a
 * later decision. Each click is its own key on purpose — two deliberate clicks are two decisions.
 */
export function decision(f: Finding, action: string, reason: string, board = ""): Decision | null {
  const wallet = decideTarget(f);
  if (!wallet) return null;
  const text = reason.trim();
  if (text.length < 8) return null;
  return {
    key: "adminGamingDecide",
    idempotencyKey: newIdempotencyKey(`gaming-${f.kind}-${wallet.slice(0, 8)}`),
    body: { wallet, action, reason: text, board: board || "all", finding: f.kind },
  };
}

/** Which actions a row offers: the suggested one first, its reversal always, and never both of a pair. */
export function actionsFor(f: Finding): string[] {
  if (f.decided?.action === "exclude") return ["include", "flag"];
  if (f.decided?.action === "flag") return ["exclude", "clear"];
  return f.suggested === "exclude" ? ["exclude", "flag"] : ["flag", "exclude"];
}
