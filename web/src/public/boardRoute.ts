/**
 * The one place a public board URL is turned into an API read.
 *
 * Three route files render a board (`/leaderboard/<board>`, `/…/w/<window>`, `/…/c/<category>`) and all three
 * need the same four things: the read, the metadata built from it, and a 404 that is the API's 404 rather than
 * ours. Splitting the grammar across three files is deliberate — the API's canonical URL shape is
 * `/leaderboard/<board>/w/<window>` and `/leaderboard/<board>/c/<category>`, and a single optional catch-all
 * cannot carry an `opengraph-image` child (Next requires the catch-all to be last), which is exactly the
 * constraint that would have forced the card to be generated for a different URL than the page it describes.
 */
import "server-only";

import { publicRead } from "@/api/public-read";
import type { PublicLeaderboardPage } from "./wire";

export type BoardQuery = { window?: string; category?: string };

/** The path the API serves, built from the path the reader sees. */
export function boardPath(board: string, query: BoardQuery = {}): string {
  const parts: string[] = [];
  if (query.window) parts.push(`window=${encodeURIComponent(query.window)}`);
  if (query.category) parts.push(`category=${encodeURIComponent(query.category)}`);
  return `/v1/public/leaderboard/${encodeURIComponent(board)}${parts.length ? "?" + parts.join("&") : ""}`;
}

export function loadBoard(board: string, query: BoardQuery = {}) {
  return publicRead<PublicLeaderboardPage>("GET", boardPath(board, query));
}
