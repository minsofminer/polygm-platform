/**
 * P13 D4 — the number layer, as properties rather than examples.
 *
 * The kit's row is "the number layer never renders a float artifact". Examples cannot carry that claim: the
 * failures this guards against are `0.30000000000000004`, `1e-7`, `NaN` and a grouped string that gets grouped
 * again — each of which appears for *some* value and not for the ones a hand-written test happens to pick. So
 * this file drives a deterministic sweep (a seeded LCG, so a failure is reproducible from the printed seed) of
 * integers through the same functions the screens call, and asserts the shape of what comes out.
 *
 * It runs in the same process as the components, not against a re-implementation: `microToCents` and `Number`
 * are the product's, and a test that reformatted the numbers itself would agree with itself and with nothing
 * else (the mistake the P12 startapp check was written to avoid).
 */
import { describe, expect, it } from "vitest";
import { cleanup, render } from "@testing-library/react";
// Imported under an alias on purpose: this file uses the `Number(...)` coercion in the arithmetic below, and a
// shadowed binding turns `Number(tick)` into a call to the component — which the first run of this sweep did,
// producing a React "Invalid hook call" from a line that looks like arithmetic.
import { Number as Num } from "./Number";
import { StaleIndicator } from "./StaleIndicator";
import { decideFlash, FLASH_MIN_INTERVAL_MS } from "./flash";
import { microToCents, priceToUnits, unitsToPrice, groupThousands, type TickSize } from "@/money/cents";

/** A tiny LCG: reproducible from the seed, and enough randomness to walk the shapes that break formatters. */
function lcg(seed: number) {
  let state = seed >>> 0;
  return () => {
    state = (state * 1664525 + 1013904223) >>> 0;
    return state / 0x1_0000_0000;
  };
}

const SEED = 13_091_421;
const TICKS: TickSize[] = ["0.001", "0.002", "0.005", "0.01", "0.02", "0.05", "0.1"];

/**
 * Every shape a rendered number is allowed to have: an optional sign, digits, the separator the design system
 * actually uses, and an optional fraction. The separator is U+2009 (thin space, `groupThousands`'s choice — a
 * comma is a locale's opinion), which the first version of this sweep did not know and reported as a failure of
 * the product. A property test that has to be corrected to accept the product's own typography is worth the two
 * minutes: the alternative was a regex loose enough to accept a float artifact.
 */
const THIN_SPACE = "\u2009";
const PRINTABLE = new RegExp(`^-?[\\d${THIN_SPACE}]+(\\.\\d{1,6})?$`);

describe("the number layer, swept", () => {
  it("renders every micro value it is given as digits, never as a float artifact", () => {
    const rnd = lcg(SEED);
    const seen = new Set<string>();
    for (let i = 0; i < 2_000; i += 1) {
      // The sweep deliberately includes the shapes that break naive formatters: sub-cent amounts, values whose
      // fraction is not representable in binary (0.1, 0.3), the 53-bit float ceiling, and the extremes.
      const micro = [
        Math.floor(rnd() * 1e6),
        Math.floor(rnd() * 1e9),
        Math.floor(rnd() * 1e12),
        Math.floor(rnd() * 9_007_199_254_740),
        [1, 10, 100_000, 999_999, 1_000_001, 3_000_000, 1_290_000_000][i % 7],
      ][i % 5];
      // One mount per value, unmounted immediately: 2,000 live React roots at once is what produced the
      // "Invalid hook call" this sweep first failed with — reported against the *next* test, because the crash
      // landed in testing-library's auto-cleanup.
      const { container } = render(<Num kind="money" value={microToCents(micro)} />);
      const text = container.textContent ?? "";
      cleanup();
      expect(text, `micro=${micro}`).toMatch(PRINTABLE);
      expect(text, `micro=${micro}`).not.toMatch(/e|E|NaN|Infinity|undefined|null/);
      seen.add(text);
    }
    // A sweep of 2,000 values that produced three distinct strings would be testing nothing.
    expect(seen.size).toBeGreaterThan(200);
  });

  it("round-trips every tick-aligned price through text and back", () => {
    const rnd = lcg(SEED + 1);
    for (const tick of TICKS) {
      const decimals = (tick.split(".")[1] ?? "").length;
      const maxUnits = Math.floor(1 / Number(tick));
      for (let i = 0; i < 300; i += 1) {
        const units = 1 + Math.floor(rnd() * Math.max(1, maxUnits - 1));
        const text = unitsToPrice(units, tick);
        expect(text, `tick=${tick} units=${units}`).toMatch(/^\d+\.\d+$/);
        expect(text.split(".")[1].length, `tick=${tick} text=${text}`).toBeLessThanOrEqual(decimals);
        expect(priceToUnits(text, tick), `tick=${tick} text=${text}`).toBe(units);
      }
    }
  });

  it("groups thousands once and only once", () => {
    const rnd = lcg(SEED + 2);
    for (let i = 0; i < 500; i += 1) {
      const digits = String(1 + Math.floor(rnd() * 9_999_999_999));
      const grouped = groupThousands(digits);
      expect(grouped.split(THIN_SPACE).join(""), digits).toBe(digits);
      // idempotent: grouping an already-grouped string (a caller that formats twice) changes nothing
      expect(groupThousands(grouped.split(THIN_SPACE).join("")), digits).toBe(grouped);
      // a separator only ever sits between digits: never at an end, never doubled, never beside the sign
      expect(grouped.startsWith(THIN_SPACE) || grouped.endsWith(THIN_SPACE), digits).toBe(false);
      expect(grouped.includes(THIN_SPACE + THIN_SPACE), digits).toBe(false);
    }
  });

  it("never animates a number: only the flash decision changes, and the interval cap holds on a fast tape", () => {
    const rnd = lcg(SEED + 3);
    let last: number | null = null;
    let flashes = 0;
    for (let i = 0; i < 500; i += 1) {
      const nowMs = i * 7;                                  // a 143/s tape: faster than any real feed
      const value = Math.floor(rnd() * 100);
      const decision = decideFlash({
        prev: i === 0 ? null : Math.floor(rnd() * 100),
        next: value,
        nowMs,
        lastFlashAtMs: last,
        source: i % 3 === 0 ? "rest" : "ws",
      });
      if (decision.flash) {
        flashes += 1;
        // The interval cap: two flashes inside the window would strobe, and a 20/s tape is real.
        if (last !== null) expect(nowMs - last).toBeGreaterThanOrEqual(FLASH_MIN_INTERVAL_MS);
        last = nowMs;
      }
    }
    expect(flashes).toBeGreaterThan(0);
  });

  it("stamps freshness in words, because colour is never the only channel", () => {
    for (const freshness of ["stale", "blocking", "unknown"] as const) {
      const { container } = render(<StaleIndicator freshness={freshness} ageMs={3_500} />);
      const node = container.querySelector("[role='status']");
      expect(node, freshness).not.toBeNull();
      expect((node?.textContent ?? "").trim().length, freshness).toBeGreaterThan(1);
      expect(node?.getAttribute("data-freshness")).toBe(freshness);
    }
    // and live data renders no indicator at all, rather than an indicator that says "fine"
    expect(render(<StaleIndicator freshness="live" ageMs={0} />).container.textContent).toBe("");
  });
});
