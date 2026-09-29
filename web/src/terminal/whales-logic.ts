/**
 * The whale tracker's market picker options, built from the tape's own facets.
 *
 * Moved out of `WhaleTracker.tsx` on 2026-09-29 — that module is `"use client"`, so a plain function exported
 * from it is a client reference for a server importer (src/client-boundary.test.ts). Pure mapping, no React.
 */
/** The market picker's options, from the tape's own facets: the markets with fills, biggest first. */
export function marketOptions(facets: { markets?: { marketId: string; question: string }[] } | null | undefined) {
  return (facets?.markets ?? []).map((m) => ({ marketId: m.marketId, question: m.question }));
}
