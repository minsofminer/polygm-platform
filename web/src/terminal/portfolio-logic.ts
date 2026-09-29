/**
 * The portfolio summary line, needed by the route's server-side first paint. Moved out of `PortfolioView.tsx`
 * (a `"use client"` module) — see src/client-boundary.test.ts.
 */
import {
  benchmarkLabel,
  csvFilename,
  csvText,
  curveGeometry,
  curveLabel,
  emptyTarget,
  exitHref,
  endsInText,
  exposureNote,
  hasPositions,
  markText,
  moneyText,
  orderRows,
  shareText,
  totalRows,
  unresolvedText,
  unrealisedKnown,
} from "./portfolio";
import type { Portfolio, PortfolioPosition } from "./wire";

/** Kept for the route's server-side first paint and for tests. */
export function portfolioSummaryText(book: Portfolio): string {
  return moneyText(book.totals.equityMicro);
}
