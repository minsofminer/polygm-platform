/**
 * The desktop shell's motion, asserted against the stylesheet — the same discipline as `src/tma/motion.test.ts`,
 * for the surfaces that had none.
 *
 * Why a test file and not a screenshot: everything a reviewer would otherwise eyeball is mechanical here. The
 * durations must be the token layer's (a hand-typed `200ms` in `globals.css` is the drift this catches), entrances
 * must be `ease-out` and never `ease-in`, nothing may appear from `scale(0)`, the press must be the design system's
 * own `scale(0.97)`, and both surfaces that appear over a page must have an exit that can actually run. The one
 * thing a test cannot see — whether 8px of rise feels right — is not asserted, deliberately.
 */
import { readFileSync } from "node:fs";
import path from "node:path";
import { describe, expect, it } from "vitest";

const TOKENS = readFileSync(path.join(process.cwd(), "styles", "tokens.css"), "utf8");
const SHELL = readFileSync(path.join(process.cwd(), "src", "globals.css"), "utf8");

describe("the token layer carries the motion, and the shell only references it", () => {
  it("declares the desktop panel and toast classes the components render", () => {
    for (const cls of [".pgm-panel-in", ".pgm-panel-out", ".pgm-toast-in", ".pgm-toast-out",
                       ".pgm-fade-in", ".pgm-fade-out"]) {
      expect(TOKENS).toContain(cls);
    }
  });

  it("uses the ladder's durations for them: modals are large, toasts are small, scrims are small", () => {
    expect(TOKENS).toContain(".pgm-panel-in   { animation: pgm-panel-in var(--pgm-dur-large) var(--pgm-ease-out) 1; }");
    expect(TOKENS).toContain(".pgm-toast-in   { animation: pgm-toast-in var(--pgm-dur-small) var(--pgm-ease-out) 1; }");
    expect(TOKENS).toContain(".pgm-fade-in    { animation: pgm-fade-in var(--pgm-dur-small) var(--pgm-ease-out) 1; }");
  });

  it("makes every exit hold its last frame, so the component removes the surface rather than the animation", () => {
    for (const cls of [".pgm-panel-out", ".pgm-toast-out", ".pgm-fade-out"]) {
      const rule = new RegExp(`\\${cls}\\s*\\{[^}]*forwards`).exec(TOKENS);
      expect(rule, `${cls} must run with \`forwards\``).not.toBeNull();
    }
  });

  it("never starts from scale(0) and never eases in", () => {
    const motion = TOKENS.slice(TOKENS.indexOf("@keyframes pgm-panel-in"));
    expect(motion).not.toMatch(/scale\(0\)/);
    expect(TOKENS).not.toMatch(/ease-in[^-]/);
    expect(motion).toContain("var(--pgm-rise-sm)");
  });

  it("keeps the entrance distance to the one token the design system defines", () => {
    // One entrance distance for the whole product. A second literal is how two surfaces start rising differently.
    const rises = TOKENS.match(/--pgm-rise-\w+:\s*\d+px/g) ?? [];
    expect(rises.length).toBe(1);
  });
});

describe("the shell's own rules", () => {
  it("presses at the design system's scale, with the transition on the base rule", () => {
    expect(SHELL).toMatch(/\.button:active\s*\{[^}]*transform:\s*scale\(0\.97\)/);
    // Declared inside `:active`, the release has no transition to run and the button snaps back.
    const base = /\.button\s*\{([^}]*)\}/.exec(SHELL)?.[1] ?? "";
    expect(base).toContain("transition: transform var(--pgm-dur-press) var(--pgm-ease-out)");
    const active = /\.button:active\s*\{([^}]*)\}/.exec(SHELL)?.[1] ?? "";
    expect(active).not.toContain("transition:");
  });

  it("spends --pgm-dur-micro on the controls hit tens of times a day, which is its documented job", () => {
    expect(SHELL).toMatch(/\.handle\s*\{[^}]*var\(--pgm-dur-micro\)/);
    expect(TOKENS).toContain("--pgm-dur-micro: 80ms");
  });

  it("moves the tab indicator at --pgm-dur-medium instead of swapping a shadow", () => {
    expect(SHELL).toMatch(/\.tab::after\s*\{[^}]*transition:\s*transform var\(--pgm-dur-medium\) var\(--pgm-ease-out\)/);
    expect(SHELL).toMatch(/\.tab\[aria-current="page"\]::after\s*\{[^}]*scaleX\(1\)/);
    expect(SHELL).not.toContain("box-shadow: inset 0 calc(var(--pgm-space-off-3) * -1) 0 var(--pgm-brand-primary)");
  });

  it("keeps every value a token — no duration literal, no easing literal, in the shell file", () => {
    // P08's c5 owns the px/colour scan; this is the motion half of the same rule, kept where the motion lives.
    const animated = SHELL.split("\n").filter((line) => /transition:|animation:/.test(line));
    expect(animated.length).toBeGreaterThan(3);
    for (const line of animated) {
      expect(line, line).not.toMatch(/\b\d+ms\b/);
      expect(line, line).not.toMatch(/cubic-bezier\(/);
    }
  });
});
