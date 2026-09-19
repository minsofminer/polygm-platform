/**
 * The flash policy, as a pure function, because the interesting part of "flash on change" is every case
 * where it must NOT flash, and those cases are the ones a screenshot never catches.
 *
 * web/DESIGN.md §4 and §6, verbatim obligations implemented here:
 *   - max one flash per cell per 120 ms (a 20/sec tape otherwise strobes);
 *   - a change inside the displayed rounding window does not flash: the user cannot see the change, so
 *     animating it is a lie about resolution;
 *   - nothing animates a *number*: the animation is a background flash and nothing else — no count-up, no
 *     digit roll, no crossfade, no width animation;
 *   - REST-sourced rows never flash. The REST tape is Cloudflare-cached, so a flash on a REST row is a
 *     confident announcement of news that arrived minutes ago, which is the failure P05 found.
 *
 * Durations live in tokens (`--pgm-flash-in`, `--pgm-flash-out`) and are applied in CSS; this file decides
 * *whether* to flash, which is the part that can be unit-tested. Rounding is not done here at all: values
 * arrive in integer display units (cents, tick units, whole sizes) from `src/money/cents.ts`, and
 * `isDisplayUnits` refuses a caller that skipped that step.
 */
export const FLASH_MIN_INTERVAL_MS = 120;
export type Direction = "up" | "down";

export type FlashInput = {
  prev: number | null;
  next: number;
  /** Integers at the displayed precision: rounding happens before this call, never inside it. */
  nowMs: number;
  lastFlashAtMs: number | null;
  /** "rest" rows are tagged at the source (see docs/P05-data-ingestion.md). */
  source: "ws" | "rest";
  /** Direction is derived from the value, but the caller may pin it for a buy/sell colour decision. */
  pin?: Direction;
};

export type FlashDecision = {
  flash: Direction | null;
  reason?: "no-previous" | "inside-rounding-window" | "rate-limited" | "rest-source" | "not-integer-display-units";
  /** When the next flash becomes eligible, for the caller's own timer. */
  nextEligibleAtMs?: number;
};

export function decideFlash(input: FlashInput): FlashDecision {
  if (input.source === "rest") return { flash: null, reason: "rest-source" };
  if (input.prev === null || input.prev === undefined) return { flash: null, reason: "no-previous" };
  // "Inside the displayed rounding window" is only the same test as "equal" while the values are integers
  // in display units (cents, tick units, whole sizes). A caller that hands over 0.42 and 0.425 for a 2dp
  // surface is a caller that has mis-scaled its input, so the refusal names the contract instead of
  // silently rounding — and it refuses rather than throws, because a flash decision may not break a render.
  if (!isDisplayUnits(input.prev) || !isDisplayUnits(input.next)) {
    return { flash: null, reason: "not-integer-display-units" };
  }
  if (input.prev === input.next) return { flash: null, reason: "inside-rounding-window" };
  if (input.lastFlashAtMs !== null && input.nowMs - input.lastFlashAtMs < FLASH_MIN_INTERVAL_MS) {
    return {
      flash: null,
      reason: "rate-limited",
      nextEligibleAtMs: input.lastFlashAtMs + FLASH_MIN_INTERVAL_MS,
    };
  }
  const dir: Direction = input.pin ?? (input.next > input.prev ? "up" : "down");
  return { flash: dir, nextEligibleAtMs: input.nowMs + FLASH_MIN_INTERVAL_MS };
}

/**
 * Round to the precision the user is actually being shown, then compare. This is the whole trick behind
 * "suppressed when the change is inside the displayed rounding window": a price that moved 0.0004 on a
 * 2dp surface changed nothing on the screen.
 */
export function isDisplayUnits(value: number): boolean {
  return Number.isInteger(value);
}
