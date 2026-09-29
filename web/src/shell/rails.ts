/**
 * Resizable rails, persisted per device (P08 D4). Two decisions worth stating:
 *
 *  - The size is a *fraction of the viewport*, never a pixel width. A px value is a design-token violation
 *    (web/DESIGN.md §1) and it is also wrong on a 1920 and a 2560 at the same time. The drag handler writes
 *    a CSS custom property on the element, so a resize is not a React render — at 60fps, 60 renders of the
 *    whole terminal frame is the actual bug people ship.
 *  - "Persisted per user" in the prompt presumes a settings route. There is none (`displayPrefs` is a
 *    launch item, P08-L14), so this is honest about being device-scoped, and the label the UI shows says
 *    exactly that. A control that quietly means something narrower than the spec is worse than a gap.
 */
export const RAIL_MIN = 0.1;
export const RAIL_MAX = 0.34;

export type RailFractions = { left: number; right: number };

const KEY = "pgm.rails.v1";
const DEFAULTS: RailFractions = { left: 0.18, right: 0.22 };

/**
 * The keyboard resize step, and the same number the terminal's own handles use (`TerminalLayout.tsx`'s `nudge`,
 * which steps 0.005 of the fraction per press and x5 with Shift held). Two layouts that resize on the same
 * website should not step by different amounts because they were written four phases apart.
 */
export const RAIL_STEP = 0.005;

/** One keyboard press, clamped to the rail's own bounds. `coarse` is Shift: the same step, five times over. */
export function nudgeFraction(value: number, delta: number, coarse = false): number {
  return clampFraction(value + delta * (coarse ? 5 : 1));
}

export function clampFraction(value: number): number {
  if (!Number.isFinite(value)) return DEFAULTS.left;
  return Math.min(RAIL_MAX, Math.max(RAIL_MIN, value));
}

export function loadRails(): RailFractions {
  if (typeof localStorage === "undefined") return DEFAULTS;
  try {
    const raw = localStorage.getItem(KEY);
    if (!raw) return DEFAULTS;
    const parsed = JSON.parse(raw) as Partial<RailFractions>;
    return {
      left: clampFraction(typeof parsed.left === "number" ? parsed.left : DEFAULTS.left),
      right: clampFraction(typeof parsed.right === "number" ? parsed.right : DEFAULTS.right),
    };
  } catch {
    return DEFAULTS;
  }
}

export function saveRails(fractions: RailFractions): void {
  if (typeof localStorage === "undefined") return;
  try {
    localStorage.setItem(KEY, JSON.stringify({ left: clampFraction(fractions.left), right: clampFraction(fractions.right) }));
  } catch {
    /* a private-mode quota refusal must not break the layout: the rails just stop remembering */
  }
}

/** Both rails plus the middle must fit; a rail at max on a small screen would otherwise eat the market. */
export function railsFit(fractions: RailFractions, collapsed: { left: boolean; right: boolean }): boolean {
  const used = (collapsed.left ? 0 : fractions.left) + (collapsed.right ? 0 : fractions.right);
  return used <= 0.66;
}

/**
 * The three grid tracks the shell frame is built from. The unit is a share of 100, not a bare `fr`: `fr`
 * distributes *free space*, so two tracks of `18fr` and `22fr` next to a centre of `1fr` give the centre
 * one part in forty-one — which is exactly what shipped, and what a 2026-09-29 screenshot of `/portfolio`
 * showed: both rails ~620px of empty panel, the screen itself 34px wide.
 *
 * So every track is emitted on the same scale and the centre is the complement, which makes the three sum to
 * 100fr and the rails land on their actual fractions of the viewport. `rails.test.ts` asserts the sum, because
 * this is arithmetic a unit test can check and a screenshot is a slow way to find.
 */
export function widthVars(fractions: RailFractions, collapsed: { left: boolean; right: boolean }): Record<string, string> {
  const left = collapsed.left ? 0 : fractions.left;
  const right = collapsed.right ? 0 : fractions.right;
  const centre = Math.max(0, 1 - left - right);
  return {
    "--pgm-rail-left": collapsed.left ? "0" : `minmax(var(--pgm-space-10), ${Math.round(left * 100)}fr)`,
    // Bare `<n>fr`, not a `minmax(...)`: the stylesheet wraps it in `minmax(0, …)` so the centre may shrink
    // below its content instead of overflowing, and `minmax(0, minmax(0, 60fr))` is not a valid track.
    "--pgm-rail-center": `${Math.round(centre * 100)}fr`,
    "--pgm-rail-right": collapsed.right ? "0" : `minmax(var(--pgm-space-10), ${Math.round(right * 100)}fr)`,
  };
}
