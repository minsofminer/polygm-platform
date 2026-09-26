/**
 * What the live tape costs the main thread, measured on the real functions, at the rate the venue can actually
 * produce.
 *
 * The requirement is a sentence — "60fps under live load or the component is not done" — and a sentence is not
 * a measurement. This runs the tape's own ingest path (`coalesceFills`, `arrivalRate`, `applyFilters`,
 * `virtualWindow`, all imported from `src/terminal/tape.ts` through esbuild rather than re-implemented here) at
 * **200 fills/second for 10 seconds**, and reports the JavaScript work that load costs, per second and per
 * flush, against a 16.6 ms frame.
 *
 * What this measures: the tape's own main-thread work, deterministically, on this machine.
 * What it does NOT measure: paint, layout, compositing — anything below JS. No browser exists in this
 * environment (a Playwright Chromium download fails its host-requirements check, which is why the first-load
 * numbers in P08-bundle.txt are byte counts rather than a Lighthouse run). So the honest claim is: the JS half
 * of the frame budget is measured and met, the pixels are `[UNVERIFIED]`. The artefact says both, and the gate
 * check that reads it refuses to accept the numbers without the caveat.
 *
 * Writes docs/verification/P10-perf.txt. Re-run with `npm run measure:tape`.
 */
import { build } from "esbuild";
import { createHash } from "node:crypto";
import { mkdtempSync, rmSync, writeFileSync, mkdirSync, readFileSync } from "node:fs";
import { tmpdir } from "node:os";
import path from "node:path";
import { pathToFileURL } from "node:url";

const ROOT = path.resolve(import.meta.dirname, "..");
const REPO = path.resolve(ROOT, "..");
const OUT = path.join(REPO, "docs", "verification", "P10-perf.txt");

/** The load the phase's own acceptance line names, and the one P13 restates as a soak: 200 fills/second. */
const FILLS_PER_SECOND = 200;
const SECONDS = 10;
const FRAME_MS = 1000 / 60;
const VIEWPORT_H = 700;

const dir = mkdtempSync(path.join(tmpdir(), "pgm-tape-budget-"));
const bundle = path.join(dir, "tape.mjs");
await build({
  entryPoints: [path.join(ROOT, "src", "terminal", "tape.ts")],
  bundle: true, format: "esm", platform: "node", outfile: bundle,
  // `@/…` is the app's tsconfig path alias; esbuild has to be told, or the bundle keeps an import node cannot
  // resolve. Bundling the money layer and the dictionary in is deliberate: they are what the tape really calls,
  // and a measurement of a stubbed tape would be a measurement of this file instead of the component.
  alias: { "@": path.join(ROOT, "src") },
  logLevel: "silent",
});
const tape = await import(pathToFileURL(bundle).href);

const medians = { "0xM1": 5_000_000, "0xM2": 12_000_000 };
/** The same row shape `tape.test.ts` and `perf.test.ts` use, so the load goes through the fields the tape reads. */
function fill(i, tsMs) {
  const marketId = i % 2 === 0 ? "0xM1" : "0xM2";
  return {
    tsMs, conditionId: "0xC1", tokenId: String(i), marketId, marketSlug: `market-${marketId}`,
    question: "Will this market resolve yes?", category: "Politics", tick: "0.01",
    side: i % 3 === 0 ? "SELL" : "BUY", outcome: "Yes", price: "0.42", shares: "12000",
    notionalMicro: 1_000_000 * (1 + (i % 9)), anonWallet: `w_5c037b2a${String(i % 7).padStart(2, "0")}`,
    labels: [{ label: "whale", confidence: 9000, publishable: true, rule: "r", disclaimer: "d" }],
    source: "venue", lagMs: 400, thresholdMicro: 500_000_000,
    thresholdRule: "whale = max(the p99.5 fill of this market, $500.00 absolute floor)",
    thresholdReason: "relative", severity: "notice", ratioBps: 120_000,
    rule: "severity is the fill's ratio to the threshold", isWhale: true,
  };
}

let buffer = [];
let queued = [];
let lastFlushMs = 0;
const filters = { ...tape.EMPTY_FILTERS, minNotionalMicro: 1_000_000 };

const arrivalsPerSecond = FILLS_PER_SECOND;
const perSecond = [];
let flushCount = 0;
let maxFlushMs = 0;

for (let second = 0; second < SECONDS; second++) {
  const t0 = performance.now();
  for (let k = 0; k < arrivalsPerSecond; k++) {
    const ts = second * 1000 + Math.floor((k * 1000) / arrivalsPerSecond);
    queued.push(fill(second * arrivalsPerSecond + k, ts));
    // The per-arrival path, exactly as the hook runs it: rate from the buffer we already hold, then the queue
    // decision. `coalesceFills` copies the buffer on every call, which is the cost this measurement exists for.
    const rate = tape.arrivalRate(buffer, ts);
    const out = tape.coalesceFills(buffer, queued, { ratePerSecond: rate, nowMs: ts, lastFlushMs, paused: false });
    buffer = out.buffer;
    queued = out.queued;
    if (out.released > 0) {
      lastFlushMs = ts;
      flushCount++;
      // What a released batch costs the renderer: the filter pass and the window arithmetic, then the rows that
      // would be mounted. Timed separately because it is the part that lands inside one frame.
      const f0 = performance.now();
      const rows = tape.applyFilters(buffer, filters, medians);
      tape.virtualWindow(rows.length, k % 7 * tape.ROW_H, VIEWPORT_H);
      const fms = performance.now() - f0;
      if (fms > maxFlushMs) maxFlushMs = fms;
    }
  }
  perSecond.push(performance.now() - t0);
}

const totalMs = perSecond.reduce((a, b) => a + b, 0);
const worstSecond = Math.max(...perSecond);
const meanSecond = totalMs / SECONDS;
const budgetSecond = FRAME_MS;                    // one frame's worth of work per second of 200 fills/s
const window = tape.virtualWindow(tape.MAX_ROWS, 12 * tape.ROW_H, VIEWPORT_H);

const report = [
  "P10 tape budget — generated by web/scripts/measure-tape-budget.mjs.",
  "",
  "What this measures: the tape's own JavaScript, on the functions the component calls (`src/terminal/tape.ts`),",
  `driven at ${FILLS_PER_SECOND} fills/second for ${SECONDS} seconds — ${FILLS_PER_SECOND * SECONDS} fills, ${flushCount} batch releases.`,
  "What it does NOT measure: paint, layout and compositing. No browser exists in this environment (a Playwright",
  "Chromium download fails its host-requirements check), so the pixel half of the 60fps line is [UNVERIFIED] and",
  "this file does not claim otherwise.",
  "",
  `frame budget for context: ${FRAME_MS.toFixed(1)} ms at 60fps`,
  `  mean work per second of load   ${meanSecond.toFixed(3)} ms  (budget ${budgetSecond.toFixed(1)} ms — one frame)`,
  `  worst second                   ${worstSecond.toFixed(3)} ms`,
  `  worst single batch release     ${maxFlushMs.toFixed(3)} ms  (must fit in one frame)`,
  `  batch releases                 ${flushCount} over ${SECONDS} s = ${(flushCount / SECONDS).toFixed(1)}/s at ${tape.FLUSH_MS} ms flush`,
  `  rows released per release      <= ${tape.MAX_ROWS} in the buffer`,
  `  DOM rows for a 700px viewport  ${window.end - window.start} of ${tape.MAX_ROWS} buffered (overscan ${tape.OVERSCAN}, row ${tape.ROW_H}px)`,
  "",
  `budget: mean per second <= ${budgetSecond.toFixed(1)} ms and worst release <= ${FRAME_MS.toFixed(1)} ms`,
  `status: ${meanSecond <= budgetSecond && maxFlushMs <= FRAME_MS ? "pass" : "fail"}`,
  "",
  `sources-sha256: ${sourcesSha()}`,
];


/**
 * The three files this artefact describes, hashed.
 *
 * Same reason as `measure-first-load.mjs`: judged by mtime, this record was invalidated by nothing more than a
 * `git checkout` (every file gets touched) and confirmed by nothing more than a later write. The hash is a claim
 * about *content*, so `tools/p10-gate-check.py` can fail on drift and stay quiet on a re-checkout.
 */
function sourcesSha() {
  const files = ["src/terminal/tape.ts", "scripts/measure-tape-budget.mjs", "src/terminal/perf.test.ts"].sort();
  const h = createHash("sha256");
  for (const rel of files) {
    h.update(rel);
    h.update("\0");
    h.update(readFileSync(path.join(ROOT, rel)));
    h.update("\0");
  }
  return h.digest("hex").slice(0, 16);
}

mkdirSync(path.dirname(OUT), { recursive: true });
writeFileSync(OUT, report.join("\n"));
process.stdout.write(report.join("\n"));
rmSync(dir, { recursive: true, force: true });
if (!report.join("\n").includes("status: pass")) {
  console.error("measure-tape-budget: the tape does not fit in a frame at 200 fills/s");
  process.exit(1);
}
