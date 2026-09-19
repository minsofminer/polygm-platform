/**
 * D4 · the self-rank panel's rules, as pure functions.
 *
 * The kit's D4 asks for three things that are decisions rather than layout, and all three live here so the
 * component cannot quietly disagree with them:
 *
 *  - **The pin is drawn when the reader's own row is off the page.** That is arithmetic (rank vs page size vs the
 *    board the strip sits under), and it is the server's answer, not a guess: `pinFor` picks the row for the board
 *    being displayed and `pinLine` says in words why it is pinned.
 *  - **The unranked state is a to-do list.** The API already answers with the numbers that refused the wallet;
 *    `selfStateText` and `stepsFor` turn that into what the reader does next, and a wallet that is simply not
 *    linked yet gets the one instruction that makes a standing possible at all.
 *  - **Private is not invisible, and the copy says so every time.** `identityText`, `identityDetail` and
 *    `nudgeText` are the three places this is stated, because it is the one thing a user will assume wrongly:
 *    that staying private keeps them off the board.
 *
 * No number is formatted here — the API serves the strings and the number layer renders them. The only arithmetic
 * is integer arithmetic on counts and ranks.
 */
import { bpsText } from "./copy";
import { gapSentence, badgeText, type OrderUnits, type StandingGap } from "./board";

export type SelfBoard = {
  board: string;
  category?: string | null;
  anon: string;
  label: string;
  window?: string | null;
  state: "ranked" | "unranked" | "unknown";
  rank: number | null;
  rankedTotal: number;
  rankBadge?: { rank: number; rankedTotal: number; text: string } | null;
  percentileBps?: number | null;
  orderField: string;
  orderUnits: string;
  pageSize: number;
  offPage: boolean;
  rankedAhead?: number | null;
  rankedBehind?: number | null;
  rankedOnPage?: number | null;
  gap?: {
    rankAbove: number;
    anonAbove: string;
    field: string;
    units: string;
    value: number;
    valueAbove: number;
    delta: number | null;
    toPass: number | null;
    note?: string;
  } | null;
  row?: Record<string, unknown> | null;
  unranked?: { reasons?: string[]; settledMarkets?: number; verifiedVolumeMicro?: number; note?: string } | null;
  reasons?: string[];
  note?: string;
};

export type SelfWallet = {
  anon: string;
  boards: SelfBoard[];
  ranked: number;
  unranked: number;
  best?: { board: string; label: string; rank: number; rankedTotal: number; rankBadge?: { text: string } | null } | null;
  nextSteps: string[];
  note: string;
};

export type SelfRank = {
  identity: Identity;
  wallets: SelfWallet[];
  walletCount: number;
  primary: { anon: string; defaultBoard: string; pin: SelfBoard | null; best?: SelfWallet["best"]; nextSteps: string[] } | null;
  pageSize: number;
  links?: { identity?: string; methodology?: string };
  note: string;
};

export type Identity = {
  state: "private" | "listed";
  handle: string;
  listedMs?: number | null;
  updatedMs?: number | null;
  decided?: boolean;
  note?: string;
};

export type IdentityView = {
  identity: Identity;
  handle: { claimed: string; published: string; rules: string; reserved?: string[] };
  wallets?: { anon: string; claimedMs?: number }[];
  changes: string[];
  doesNotChange: string[];
  nudge: string;
  note: string;
};

/**
 * The strip's row: the wallet's standing on the board in front of the reader, matched EXACTLY.
 *
 * No fallback, and that is the decision rather than an omission. A fallback is how a panel ends up showing a
 * risk-adjusted rank under a win-rate selector: the reader picked a board, and a strip that answers about a
 * different one is worse than no strip. `category` is the ambiguous case — the category board is four boards
 * wearing one name, so with no category chosen there is nothing to pin and the panel's own table (which lists
 * all four) is the answer.
 */
export function pinFor(me: SelfRank, board: string, category = ""): SelfBoard | null {
  const wallets = me.wallets ?? [];
  for (const w of wallets) {
    const hit = w.boards.find((b) => b.board === board && (b.category ?? "") === category);
    if (hit) return hit;
  }
  return null;
}

/** "…is not on the first 50 rows, so it is pinned here" — the sentence that explains the strip's existence. */
export function pinLine(entry: SelfBoard | null): string {
  if (!entry) return "";
  if (entry.state !== "ranked") return selfStateText(entry);
  const rank = entry.rank ?? 0;
  const badge = `#${rank} of ${entry.rankedTotal}`;
  if (!entry.offPage) return `${badge} — on page ${entry.rankedOnPage ?? 1} of the board below`;
  return `${badge} — not on the first ${entry.pageSize} rows, so it is pinned here`;
}

/** What the row is, in one line, for a wallet that has no placing on this board. */
export function selfStateText(entry: SelfBoard): string {
  if (entry.state === "ranked") {
    const rank = entry.rank ?? 0;
    return `#${rank} of ${entry.rankedTotal}`;
  }
  if (entry.state === "unranked") return "not ranked yet — the board states which number refused this wallet";
  return entry.category
    ? `not a ${entry.category} specialist — this board only ranks wallets whose resolved markets are mostly ${entry.category}`
    : "no placing on this board";
}

/** Basis points of the field, or the count, in the board's own vocabulary. Integer arithmetic only. */
export function fieldOf(entry: SelfBoard): string {
  const bps = (entry.row?.[entry.orderField] as number | undefined) ?? null;
  if (bps === null || bps === undefined) return "";
  if (entry.orderUnits === "bps") return `${bpsText(bps)}`;
  return String(bps);
}

/** The gap to the place above, reusing D3's sentence so one panel cannot phrase it two ways. */
export function gapOf(entry: SelfBoard): string {
  if (!entry.gap) return "";
  const gap = { ...entry.gap, units: entry.orderUnits as OrderUnits, note: entry.gap.note ?? "" } as StandingGap;
  return gapSentence(gap, entry.orderUnits as OrderUnits);
}

/** Every board's answer as one row of the panel's table, with the label a reader can scan. */
export function boardRows(wallet: SelfWallet): { key: string; label: string; placing: string; gap: string; pinned: boolean }[] {
  return (wallet.boards ?? []).map((b) => ({
    key: `${b.board}:${b.category ?? ""}`,
    label: b.label,
    placing:
      b.state === "ranked"
        ? badgeText({ rank: b.rank, rankedTotal: b.rankedTotal, rankBadge: b.rankBadge ?? null })
        : selfStateText(b),
    gap: b.state === "ranked" ? gapOf(b) : "",
    pinned: Boolean(b.offPage),
  }));
}

/** The three sentences a wallet under the gate is given, in the API's own numbers. */
export function stepsFor(wallet: SelfWallet | null): string[] {
  if (!wallet) return [];
  if (wallet.nextSteps?.length) return wallet.nextSteps;
  if (wallet.ranked > 0) return [];
  return ["this wallet has no placing yet; the board's page states which number refused it, on every board"];
}

/** What the account is published as, in one line. */
export function identityText(identity: Identity | null | undefined): string {
  if (!identity) return "";
  if (identity.state === "listed") return identity.handle ? `listed as ${identity.handle}` : "listed";
  return identity.decided === false ? "private (by default)" : "private";
}

/**
 * The half a user assumes wrongly, said in one line under the control: the setting is about the LINK, and the
 * wallet is ranked either way. `identityDetail` is the version for a private account, `listedDetail` for a
 * listed one, and both name what stays true.
 */
export function identityDetail(identity: Identity | null | undefined): string {
  if (!identity) return "";
  if (identity.state === "listed") {
    return `your rows carry the handle ${identity.handle}; your rank is the same number it was private`;
  }
  return "your rows are on the board under your pseudonym: private removes the link to this account, not the row";
}

/** The opt-in's argument, which is the API's sentence rather than a second copy of it. */
export function nudgeText(view: IdentityView | null): string {
  return view?.nudge ?? "";
}

/** The handle that would be published, and the rule it has to satisfy. */
export function handleDraft(state: "private" | "listed", typed: string, claimed: string): string {
  if (state === "private") return claimed;
  return (typed || claimed).trim().toLowerCase();
}
