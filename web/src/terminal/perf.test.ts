/**
 * The 60fps requirement, as an assertion rather than a claim.
 *
 * The phase's acceptance line is "60fps under live load or the component is not done", and the honest reading
 * of it in an environment with no browser is: the *JavaScript* half of the frame budget is measurable here and
 * the pixel half is not. `scripts/measure-tape-budget.mjs` writes the reported numbers to
 * `docs/verification/P10-perf.txt`; this test is the version that fails a build when a change makes the tape
 * slower, and it runs in the same suite the P09 gate already requires to be green.
 *
 * The load is the venue's own worst case — 200 fills/second, the rate P13's soak test restates — for 10 seconds.
 * Budgets are deliberately loose (the measured cost is ~9% of one frame per second of load), because a timing
 * assertion tight enough to be flaky is an assertion people delete.
 */
import { describe, expect, it } from "vitest";
import {
  EMPTY_FILTERS,
  FLUSH_MS,
  MAX_ROWS,
  OVERSCAN,
  ROW_H,
  applyFilters,
  arrivalRate,
  coalesceFills,
  virtualWindow,
  type TapeFilters,
} from "./tape";
import type { TerminalFill } from "./wire";

const FRAME_MS = 1000 / 60;
const FILLS_PER_SECOND = 200;
const SECONDS = 10;
const VIEWPORT_H = 700;

const medians = { "0xM1": 5_000_000, "0xM2": 12_000_000 };
const filters: TapeFilters = { ...EMPTY_FILTERS, minNotionalMicro: 1_000_000 };
const MICRO = 1_000_000;

/** The same row shape `tape.test.ts` uses, so the load here goes through the fields the component reads. */
function fill(i: number, tsMs: number): TerminalFill {
  const marketId = i % 2 === 0 ? "0xM1" : "0xM2";
  return {
    tsMs, conditionId: "0xC1", tokenId: String(i), marketId, marketSlug: `market-${marketId}`,
    question: "Will this market resolve yes?", category: "Politics", tick: "0.01",
    side: i % 3 === 0 ? "SELL" : "BUY", outcome: "Yes", price: "0.42", shares: "12000",
    notionalMicro: MICRO * (1 + (i % 9)), anonWallet: `w_5c037b2a${String(i % 7).padStart(2, "0")}`,
    labels: [{ label: "whale", confidence: 9000, publishable: true, rule: "r", disclaimer: "d" }],
    source: "venue", lagMs: 400, thresholdMicro: 500 * MICRO,
    thresholdRule: "whale = max(the p99.5 fill of this market, $500.00 absolute floor)",
    thresholdReason: "relative", severity: "notice", ratioBps: 120_000,
    rule: "severity is the fill's ratio to the threshold", isWhale: true,
  };
}

/** One simulated second of arrivals, through the same calls the hook makes. Returns the work it cost. */
function second(state: { buffer: TerminalFill[]; queued: TerminalFill[]; lastFlushMs: number }, secondIndex: number) {
  const t0 = performance.now();
  let worstFlush = 0;
  let releases = 0;
  for (let k = 0; k < FILLS_PER_SECOND; k++) {
    const ts = secondIndex * 1000 + Math.floor((k * 1000) / FILLS_PER_SECOND);
    state.queued.push(fill(secondIndex * FILLS_PER_SECOND + k, ts));
    const out = coalesceFills(state.buffer, state.queued, {
      ratePerSecond: arrivalRate(state.buffer, ts), nowMs: ts, lastFlushMs: state.lastFlushMs, paused: false,
    });
    state.buffer = out.buffer;
    state.queued = out.queued;
    if (out.released > 0) {
      state.lastFlushMs = ts;
      releases++;
      const f0 = performance.now();
      const rows = applyFilters(state.buffer, filters, medians);
      virtualWindow(rows.length, (k % 7) * ROW_H, VIEWPORT_H);
      worstFlush = Math.max(worstFlush, performance.now() - f0);
    }
  }
  return { ms: performance.now() - t0, worstFlush, releases };
}

describe("the tape under live load", () => {
  it("spends a fraction of a frame per second of 200 fills/s, and never a frame on one release", () => {
    const state = { buffer: [] as TerminalFill[], queued: [] as TerminalFill[], lastFlushMs: 0 };
    const seconds: number[] = [];
    let worstFlush = 0;
    let releases = 0;
    for (let s = 0; s < SECONDS; s++) {
      const r = second(state, s);
      seconds.push(r.ms);
      worstFlush = Math.max(worstFlush, r.worstFlush);
      releases += r.releases;
    }
    const mean = seconds.reduce((a, b) => a + b, 0) / SECONDS;

    // The load really happened: 2,000 fills in, batched rather than drawn one at a time.
    expect(state.buffer.length + state.queued.length).toBeGreaterThan(0);
    expect(releases).toBeGreaterThan(0);
    expect(state.buffer.length).toBeLessThanOrEqual(MAX_ROWS);

    expect(mean).toBeLessThan(FRAME_MS);
    expect(worstFlush).toBeLessThan(FRAME_MS);
  });

  it("keeps the DOM bounded no matter how deep the buffer gets", () => {
    // Virtualisation is the reason a 400-row buffer is cheap to look at: the mounted rows are a function of the
    // viewport, not of the buffer. A regression that dropped the window would show up here first.
    const window = virtualWindow(MAX_ROWS, 12 * ROW_H, VIEWPORT_H);
    const mounted = window.end - window.start;
    expect(mounted).toBeLessThanOrEqual(Math.ceil(VIEWPORT_H / ROW_H) + 1 + 2 * OVERSCAN);
    expect(window.topPad + mounted * ROW_H + window.bottomPad).toBe(MAX_ROWS * ROW_H);
  });

  it("draws in batches above the coalescing threshold, so the frame does not carry every arrival", () => {
    const buffer = Array.from({ length: 50 }, (_, i) => fill(i, 1_000));
    const queued = Array.from({ length: 30 }, (_, i) => fill(100 + i, 2_000));
    const batched = coalesceFills(buffer, queued, {
      ratePerSecond: 200, nowMs: 2_100, lastFlushMs: 2_000, paused: false,
    });
    expect(batched.released).toBe(0);                        // 100 ms after the last flush: not due yet
    expect(batched.withheld).toBe(30);                       // and the counter the user reads is honest
    const due = coalesceFills(buffer, queued, {
      ratePerSecond: 200, nowMs: 2_000 + FLUSH_MS, lastFlushMs: 2_000, paused: false,
    });
    expect(due.released).toBe(30);
    expect(due.buffer.length).toBe(80);
    // A quiet tape is not batched: below the threshold the newest fill is on screen immediately.
    const quiet = coalesceFills(buffer, queued, {
      ratePerSecond: 3, nowMs: 2_100, lastFlushMs: 2_100, paused: false,
    });
    expect(quiet.released).toBe(30);
  });
});
