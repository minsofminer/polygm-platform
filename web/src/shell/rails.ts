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

export function widthVars(fractions: RailFractions, collapsed: { left: boolean; right: boolean }): Record<string, string> {
  return {
    "--pgm-rail-left": collapsed.left ? "0" : `minmax(var(--pgm-space-10), ${Math.round(fractions.left * 100)}fr)`,
    "--pgm-rail-right": collapsed.right ? "0" : `minmax(var(--pgm-space-10), ${Math.round(fractions.right * 100)}fr)`,
  };
}
