/**
 * The rail handles are window splitters, and a window splitter a keyboard cannot use is a control that only
 * looks like one.
 *
 * `plans/design-review.md` found the shell's two handles focusable (`tabIndex=0`), labelled, value-less and
 * pointer-only: Tab reached them and nothing happened. The terminal's own handles have had arrow-key resize since
 * P10, so this file tests both halves of the promise — the pure step arithmetic, and the wiring that the shell
 * actually renders (aria values, the four keys, the collapse toggle, and that a resize the pointer could not
 * reach is refused to the keyboard too).
 */
import { describe, expect, it } from "vitest";
import { readFileSync } from "node:fs";
import path from "node:path";
import { RAIL_MAX, RAIL_MIN, RAIL_STEP, clampFraction, nudgeFraction, railsFit, widthVars } from "./rails";

const SHELL = readFileSync(path.join(process.cwd(), "src", "shell", "Shell.tsx"), "utf8");

describe("the keyboard step", () => {
  it("moves by the same step the terminal's handles use, and clamps to the rail's bounds", () => {
    expect(RAIL_STEP).toBe(0.005);
    expect(nudgeFraction(0.2, RAIL_STEP)).toBeCloseTo(0.205, 10);
    expect(nudgeFraction(0.2, -RAIL_STEP)).toBeCloseTo(0.195, 10);
    expect(nudgeFraction(RAIL_MIN, -RAIL_STEP)).toBe(RAIL_MIN);
    expect(nudgeFraction(RAIL_MAX, RAIL_STEP)).toBe(RAIL_MAX);
  });

  it("takes the coarse step with Shift, exactly as the terminal does", () => {
    expect(nudgeFraction(0.2, RAIL_STEP, true)).toBeCloseTo(0.225, 10);
    expect(nudgeFraction(0.2, -RAIL_STEP, true)).toBeCloseTo(0.175, 10);
  });

  it("is the same clamp the pointer path uses, so both paths land on the same values", () => {
    expect(nudgeFraction(0, 1)).toBe(clampFraction(1));
    expect(nudgeFraction(1, -1)).toBe(clampFraction(0));
  });
});

describe("the shell wires it up", () => {
  it("gives both handles a key handler", () => {
    expect(SHELL.match(/onKeyDown=\{handleKeys\("left"\)\}/g)?.length).toBe(1);
    expect(SHELL.match(/onKeyDown=\{handleKeys\("right"\)\}/g)?.length).toBe(1);
  });

  it("declares the values a focusable separator owes", () => {
    // WAI-ARIA: a focusable `separator` is a window splitter and must carry now/min/max. The first version of
    // this fix added the key handler and left the values off, which is an incomplete splitter, not a fixed one.
    for (const side of ["left", "right"]) {
      expect(SHELL).toContain(`aria-valuenow={Math.round(fractions.${side} * 100)}`);
    }
    expect(SHELL).toContain("aria-valuemin={Math.round(RAIL_MIN * 100)}");
    expect(SHELL).toContain("aria-valuemax={Math.round(RAIL_MAX * 100)}");
  });

  it("answers the four keys, with the right rail's delta inverted", () => {
    expect(SHELL).toContain("const growing = side === \"left\" ? 1 : -1;");
    expect(SHELL).toContain('{ ArrowLeft: -1, ArrowRight: 1 }');
    expect(SHELL).toContain('event.key === "Home" || event.key === "End"');
    expect(SHELL).toContain('event.key === "Enter" || event.key === " "');
    // …and the resize goes through the same fit gate as the drag, so the keyboard cannot reach a layout the
    // pointer is refused.
    expect(SHELL).toContain("applyRail(side, event.key === \"Home\" ? RAIL_MIN : RAIL_MAX)");
  });

  it("ships a skip link to the content, and the content is where it points", () => {
    expect(SHELL).toContain('<a className="skip" href="#content">');
    expect(SHELL).toContain('<main id="content"');
  });

  it("keeps both paths through the collapse gate rather than adding a second way to resize", () => {
    // The nudge and the drag both call `railsFit`; a keyboard path that wrote `fractions` directly would be a
    // second implementation with its own bugs.
    expect(SHELL).not.toMatch(/setFractions|writeVars/);
  });
});

describe("the stylesheet carries the shell fixes", () => {
  const CSS = readFileSync(path.join(process.cwd(), "src", "globals.css"), "utf8");

  it("declares color-scheme per theme, so native widgets follow the product's theme", () => {
    expect(CSS).toMatch(/\[data-theme="dark"\]\s*\{[^}]*color-scheme:\s*dark/);
    expect(CSS).toMatch(/\[data-theme="light"\]\s*\{[^}]*color-scheme:\s*light/);
  });

  it("lets the handle be dragged by a finger, and hides the skip link until it is focused", () => {
    expect(CSS).toMatch(/\.handle\s*\{[^}]*touch-action:\s*none/);
    expect(CSS).toMatch(/\.skip\s*\{/);
    expect(CSS).toMatch(/\.skip:focus-visible\s*\{[^}]*transform:\s*none/);
  });

  it("contains the modal layer's scroll, and uses no transition:all anywhere", () => {
    expect(CSS).toMatch(/\.overlay\s*\{[^}]*overscroll-behavior:\s*contain/);
    expect(CSS).not.toMatch(/transition:\s*all/);
  });
});

describe("the rails still refuse an impossible layout", () => {
  it("agrees with railsFit about the bounds it clamps to", () => {
    // The keyboard path clamps first and then asks `railsFit`; a clamp that disagreed with the fit rule would
    // produce a state that is reachable by keyboard but not by pointer.
    expect(railsFit({ left: RAIL_MIN, right: RAIL_MIN }, { left: false, right: false })).toBe(true);
    expect(railsFit({ left: RAIL_MAX, right: RAIL_MAX }, { left: false, right: false })).toBe(false);
  });
});

describe("the frame's three tracks", () => {
  const fr = (v: string | undefined) => Number.parseFloat((v ?? "").replace(/^.*,\s*/, "").replace("fr)", ""));
  const track = (vars: Record<string, string>, name: string) => vars[name] ?? "";
  const css = readFileSync(path.join(process.cwd(), "src", "globals.css"), "utf8");

  it("is a share of 100, not three bare fr values", () => {
    // What shipped until 2026-09-29: rails of 18fr/22fr beside a centre of `1fr`. `fr` divides *free* space,
    // so the centre got 1 of 41 parts — 34px of a 1440px viewport, and both rails hundreds of px of nothing.
    // Every screenshot of an app screen was that, and no test looked at the sum.
    const vars = widthVars({ left: 0.18, right: 0.22 }, { left: false, right: false });
    expect(fr(vars["--pgm-rail-left"]) + fr(vars["--pgm-rail-center"]) + fr(vars["--pgm-rail-right"])).toBeCloseTo(100, 6);
    expect(track(vars, "--pgm-rail-left")).toContain("18fr");
    expect(track(vars, "--pgm-rail-center")).toContain("60fr");
    expect(track(vars, "--pgm-rail-right")).toContain("22fr");
  });

  it("gives the whole width to the centre when a rail is collapsed", () => {
    const left = widthVars({ left: 0.18, right: 0.22 }, { left: true, right: false });
    expect(track(left, "--pgm-rail-left")).toBe("0");
    expect(fr(left["--pgm-rail-center"]) + fr(left["--pgm-rail-right"])).toBeCloseTo(100, 6);
    const both = widthVars({ left: 0.18, right: 0.22 }, { left: true, right: true });
    expect(fr(both["--pgm-rail-center"])).toBe(100);
  });

  it("is read by the stylesheet: the frame's centre track is the complement variable", () => {
    expect(css).toMatch(/\.frame\s*\{[^}]*grid-template-columns:\s*var\(--pgm-rail-left[^;]*--pgm-rail-center/);
    // And the component writes all three, or the centre falls back to `1fr` and the bug returns silently.
    const useRails = readFileSync(path.join(process.cwd(), "src", "shell", "useRails.ts"), "utf8");
    for (const name of ["--pgm-rail-left", "--pgm-rail-center", "--pgm-rail-right"]) {
      expect(useRails).toContain(`style.setProperty("${name}"`);
    }
  });
});
