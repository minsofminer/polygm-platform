/**
 * The board page's rows, as data: every cell is a string the API already printed.
 *
 * This module is the D6 answer to a question D3 asked about the terminal: *who owns the number on screen?* The
 * API sends both halves of every figure — the integer the ranking used (`realisedMicro`, `winRateBps`,
 * `maxDrawdownMicro`) and the string it prints (`realised`, `drawdown`) — and a public page has a hard reason to
 * prefer the string: the row on the page and the row in the crawler's index and the number in the share text
 * must be the same characters. Re-deriving a dollar amount in a component is how a page and its own screenshot
 * disagree about rounding, so this module never formats anything: it selects.
 *
 * `sampleNote` and `insufficientSample` travel with the row for the same reason the terminal shows them. A win
 * rate above a gate that the reader cannot see is a win rate the reader cannot weigh — and on a public page,
 * where the reader has no account and no access to the methodology tab, that is the difference between
 * publishing a measurement and publishing a claim.
 */
import type { PublicBoardRow } from "./wire";

export type BoardCell = { key: string; label: string; value: string; wide?: boolean; bad?: boolean };

/** The columns a board page shows, in order. The renderer draws exactly these; there is no second list. */
export const BOARD_COLUMNS = ["rank", "trader", "score", "settled", "winRate", "drawdown"] as const;
export type BoardColumn = (typeof BOARD_COLUMNS)[number];

/**
 * One row, as cells.
 *
 * `board` decides the score column's label and value, because the score means something different on each
 * board and the API tells us which one it is (`scoreBps` on the risk-adjusted board, `verifiedVolumeMicro` on
 * the volume board, and so on). Read from the row's own `components` where the server put it; never invented.
 */
export function rowCells(row: PublicBoardRow, board: string): BoardCell[] {
  const r = row as Record<string, unknown>;
  const score = scoreCell(r, board);
  const winRate = r.insufficientSample
    ? { key: "winRate", label: "win rate", value: String(r.sampleNote ?? "") || "behind the sample gate", wide: true }
    : { key: "winRate", label: "win rate", value: text(r.winRateText ?? r.winRate ?? r.winRateBps), bad: false };
  return [
    { key: "rank", label: "rank", value: String(row.rank) },
    { key: "trader", label: "trader", value: String(r.handle ?? r.label ?? row.anon) },
    score,
    { key: "settled", label: "settled", value: text(r.settledMarkets) },
    winRate,
    { key: "drawdown", label: "drawdown", value: text(r.drawdown ?? r.maxDrawdown), bad: Boolean(r.maxDrawdownMicro) && Number(r.maxDrawdownMicro) < 0 },
  ];
}

function text(value: unknown): string {
  if (value === null || value === undefined || value === "") return "—";
  return String(value);
}

function scoreCell(r: Record<string, unknown>, board: string): BoardCell {
  if (board === "volume") return { key: "score", label: "verified volume", value: text(r.verifiedVolume ?? r.verifiedVolumeMicro) };
  if (board === "win_rate") return { key: "score", label: "win rate", value: text(r.winRateText ?? r.winRate ?? r.winRateBps) };
  if (board === "copied") return { key: "score", label: "copiers", value: text(r.copiers) };
  if (board === "rising") return { key: "score", label: "change over 7d", value: text(r.risingText ?? r.scoreText ?? r.scoreBps) };
  if (board === "category") return { key: "score", label: "specialist score", value: text(r.scoreText ?? r.scoreBps) };
  return { key: "score", label: "risk-adjusted", value: text(r.scoreText ?? r.scoreBps) };
}

/**
 * The row's own qualification, in one line: the labels it earned and the sample it was decided on.
 *
 * Both come from the payload. A wash finding, a disputed-market exclusion, a lucky-gambler flag — each is a
 * sentence the API wrote, shown beside the number it applies to, which is the standing rule that every
 * classification carries its rule and its disclaimer.
 */
export function rowNotes(row: PublicBoardRow): string[] {
  const r = row as Record<string, unknown>;
  const labels = Array.isArray(r.labels) ? (r.labels as { label?: string; rule?: string }[]) : [];
  const out = labels.map((l) => [l.label, l.rule].filter(Boolean).join(": ")).filter(Boolean) as string[];
  if (r.sampleNote) out.push(String(r.sampleNote));
  if (r.washNote) out.push(String(r.washNote));
  if (r.rankBadgeText && r.rankBadge) out.push(String(r.rankBadge).includes("#") ? String(r.rankBadge) : String(r.rankBadgeText));
  return out;
}

/**
 * Whether the page may invite a crawler in.
 *
 * The API decides (`robots` on the payload) and this only reads it, because a UI that computes indexability
 * independently is a second opinion about the one thing that must not have two: whether a page is published.
 */
export function isIndexable(robots: string | undefined): boolean {
  return String(robots ?? "").trim().toLowerCase().startsWith("index");
}

/** The integrity counts a board carries, as sentences, newest first — empty when the board is clean. */
export function boardFindings(board: {
  excludedTotal?: number;
  blewUpCount?: number;
  provisionalCount?: number;
}): string[] {
  const out: string[] = [];
  if (board.excludedTotal) out.push(`${board.excludedTotal} wallets are excluded from this board by the integrity rules`);
  if (board.blewUpCount) out.push(`${board.blewUpCount} wallets on this board lost more than they deposited and are shown, not dropped`);
  if (board.provisionalCount) out.push(`${board.provisionalCount} wallets are inside their first week and carry a provisional label`);
  return out;
}
