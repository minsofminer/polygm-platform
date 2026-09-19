import { describe, expect, it } from "vitest";
import { decideFlash, FLASH_MIN_INTERVAL_MS } from "./flash";

const base = { nowMs: 10_000, lastFlashAtMs: null, source: "ws" as const };

describe("the flash is a rate-limited, conditional signal", () => {
  it("does not fire without a previous value, because direction would be a guess", () => {
    expect(decideFlash({ ...base, prev: null, next: 42 }).flash).toBeNull();
    expect(decideFlash({ ...base, prev: null, next: 42 }).reason).toBe("no-previous");
  });
  it("does not fire when the displayed digits did not change", () => {
    const out = decideFlash({ ...base, prev: 4_200, next: 4_200 });
    expect(out.flash).toBeNull();
    expect(out.reason).toBe("inside-rounding-window");
  });
  it("does not fire twice inside 120ms, and says when it may next", () => {
    const out = decideFlash({ ...base, prev: 1, next: 2, lastFlashAtMs: 9_950 });
    expect(out.flash).toBeNull();
    expect(out.reason).toBe("rate-limited");
    expect(out.nextEligibleAtMs).toBe(9_950 + FLASH_MIN_INTERVAL_MS);
  });
  it("never animates a REST-sourced row, at any age", () => {
    for (const now of [0, 1_000, 10_000, 10_000_000]) {
      const out = decideFlash({ ...base, nowMs: now, source: "rest", prev: 10, next: 99 });
      expect(out.flash).toBeNull();
      expect(out.reason).toBe("rest-source");
    }
  });
  it("refuses a caller that skipped the cents/tick scaling, instead of silently rounding it", () => {
    // A non-integer here means the caller handed over 0.42 and 0.425 for a surface that shows tick units.
    // Rounding it inside the policy would hide a mis-scaled feed; refusing keeps the number and the claim
    // honest, and the value still renders — a flash is a decoration, it never gets to break a render.
    for (const [prev, next] of [[0.42, 0.425], [4200.5, 4201], [4200, 4200.25]] as const) {
      const out = decideFlash({ ...base, prev, next });
      expect(out.flash).toBeNull();
      expect(out.reason).toBe("not-integer-display-units");
    }
    // ...and it is checked before the rate limit, so the reason stays the true one.
    expect(decideFlash({ ...base, prev: 0.42, next: 0.425, lastFlashAtMs: 9_995 }).reason).toBe("not-integer-display-units");
  });

  it("flips direction with the value and honours a pinned direction", () => {
    expect(decideFlash({ ...base, prev: 10, next: 11 }).flash).toBe("up");
    expect(decideFlash({ ...base, prev: 10, next: 9 }).flash).toBe("down");
    expect(decideFlash({ ...base, prev: 10, next: 11, pin: "down" }).flash).toBe("down");
  });
});
