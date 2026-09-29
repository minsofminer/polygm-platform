/**
 * The terminal's poll cadence and CSV columns: two pure questions about its own data shapes. Moved out of
 * `useTerminal.ts` (a `"use client"` module) — see src/client-boundary.test.ts.
 */
import type {
  WhalesCounts,
  CopyConfig,
  CopySourceRow,
  CopyMonitor,
  Portfolio,
  RadarResult,
  TapeFacets,
  TerminalFill,
  TraderDossier,
  WhaleView,
} from "./wire";

/** The poll interval for a measured arrival rate: a busy tape is polled more often, and a dead one, less. */
export function pollMsFor(ratePerSecond: number): number {
  if (ratePerSecond >= 40) return 500;
  if (ratePerSecond >= 20) return 750;
  if (ratePerSecond >= 5) return 1_500;
  return 3_000;
}

/** The columns a CSV export must contain (from the portfolio payload), so the file matches the table. */
export function csvColumns(portfolio: Portfolio | null): string[] {
  return portfolio?.csv.columns ?? [];
}
