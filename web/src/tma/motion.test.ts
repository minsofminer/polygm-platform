/**
 * The Mini App's motion must be the design system's motion — asserted against the stylesheet, not against a copy.
 *
 * The failure this prevents is the quiet one: somebody tunes a duration in a component "just for the webview", the
 * chat and the sheet start moving at different speeds, and nobody notices until a user says the app feels slower
 * inside Telegram. So every number here is read out of `web/styles/tokens.css` at test time.
 */
import { readFileSync } from "node:fs";
import path from "node:path";
import { describe, expect, it } from "vitest";
import { CLASS, DUR, NEVER, SEQUENCE, motionFindings, withMotion } from "./motion";
import { HAPTIC_EVENTS, haptic, onceHaptic } from "./haptics";

const TOKENS = readFileSync(path.join(process.cwd(), "styles", "tokens.css"), "utf8");

function token(name: string): number {
  const m = new RegExp(`--pgm-${name}:\\s*(\\d+)ms`).exec(TOKENS);
  if (!m) throw new Error(`--pgm-${name} is not in styles/tokens.css`);
  return Number(m[1]);
}

describe("durations come from the token layer", () => {
  it("matches --pgm-dur-* and --pgm-flash-* exactly", () => {
    expect(DUR.press).toBe(token("dur-press"));
    expect(DUR.micro).toBe(token("dur-micro"));
    expect(DUR.small).toBe(token("dur-small"));
    expect(DUR.medium).toBe(token("dur-medium"));
    expect(DUR.large).toBe(token("dur-large"));
    expect(DUR.drawer).toBe(token("dur-drawer"));
    expect(DUR.flashIn).toBe(token("flash-in"));
    expect(DUR.flashOut).toBe(token("flash-out"));
  });

  it("uses the design system's easing, not its own", () => {
    expect(TOKENS).toContain("--pgm-ease-drawer: cubic-bezier(0.32, 0.72, 0, 1)");
    expect(TOKENS).toContain("--pgm-ease-out: cubic-bezier(0.23, 1, 0.32, 1)");
  });

  it("every class the module names exists in the stylesheet", () => {
    for (const name of Object.values(CLASS)) expect(TOKENS).toContain(`.${name}`), name;
  });

  it("the sheet is the only surface allowed past 300ms, and only as a drawer", () => {
    expect(DUR.drawer).toBeLessThanOrEqual(300);
    for (const [key, value] of Object.entries(DUR)) {
      if (key !== "drawer") expect(value).toBeLessThanOrEqual(250);
    }
  });
});

describe("one idea, one animation", () => {
  it("never runs more than two beats", () => {
    expect(SEQUENCE.length).toBeLessThanOrEqual(2);
    expect(motionFindings({ beats: 2 })).toEqual([]);
    expect(motionFindings({ beats: 3 }).join(" ")).toContain("cannot follow");
  });

  it("refuses to animate a number, and says so in the rules the module publishes", () => {
    expect(NEVER.join(" ")).toContain("count a number up");
    expect(motionFindings({ beats: 1, animatesNumber: true }).join(" ")).toContain("number_policy");
  });

  it("allows only the two haptics", () => {
    expect(HAPTIC_EVENTS).toEqual(["fill", "reject"]);
    expect(motionFindings({ beats: 1, haptics: ["fill"] })).toEqual([]);
    expect(motionFindings({ beats: 1, haptics: ["fill", "celebrate"] }).join(" ")).toContain("haptic");
  });

  it("withMotion() runs immediately under reduced motion and after the settle otherwise", async () => {
    const immediate: string[] = [];
    withMotion(() => immediate.push("now"), { reduced: true });
    expect(immediate).toEqual(["now"]);
    const deferred: string[] = [];
    withMotion(() => deferred.push("later"), { reduced: false, kind: "micro" });
    expect(deferred).toEqual([]);
    await new Promise((r) => setTimeout(r, DUR.micro + 30));
    expect(deferred).toEqual(["later"]);
  });
});

describe("haptics are a closed vocabulary that never throws", () => {
  it("buzzes a fill as success and a refusal as error, and nothing else", () => {
    const calls: string[] = [];
    const host = { notificationOccurred: (t: string) => calls.push(t) };
    expect(haptic("fill", host)).toBe(true);
    expect(haptic("reject", host)).toBe(true);
    expect(calls).toEqual(["success", "error"]);
    // @ts-expect-error — the type is closed, and a runtime caller that tries anyway gets a no-op, not a crash.
    expect(haptic("celebrate", host)).toBe(false);
  });

  it("survives a bridge that is absent or broken", () => {
    expect(haptic("fill", null)).toBe(false);
    expect(haptic("fill", { notificationOccurred: () => { throw new Error("no bridge"); } })).toBe(false);
  });

  it("fires once per order, however many times the path runs", () => {
    const calls: string[] = [];
    // Augment, never replace: a replaced `window` has no `document`, which breaks every other test in this file.
    (window as unknown as Record<string, unknown>).Telegram = {
      WebApp: { notificationOccurred: (t: string) => calls.push(t) },
    };
    (window as unknown as Record<string, unknown>).matchMedia = () => ({ matches: false });
    const once = onceHaptic();
    // A fill notification arrives over a poll that can answer twice for one order; the key is the order's id, not a
    // timestamp, so the second answer for the same intent is silent.
    expect(once("fill", "oi-1")).toBe(true);
    expect(once("fill", "oi-1")).toBe(false);
    expect(once("fill", "oi-2")).toBe(true);
    expect(calls).toEqual(["success", "success"]);
  });
});
