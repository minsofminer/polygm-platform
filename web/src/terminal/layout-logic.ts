/**
 * The terminal layout's arithmetic: the persisted shape, the clamps, the column fractions and the keyboard step.
 *
 * Moved out of `TerminalLayout.tsx` on 2026-09-29. That module is `"use client"` (it holds the drag state and
 * writes to `localStorage`), and a plain function exported from a client module becomes a *client reference*:
 * callable from a client component, a 500 from a server one. `src/client-boundary.test.ts` states the rule and
 * cites the `/terminal` blank page that produced it — these six functions were the largest remaining cluster.
 *
 * Everything here is a pure function of numbers and strings, which is the test for whether it belongs on this
 * side of the boundary. The component keeps what actually needs a browser: pointer handling, `localStorage`,
 * `matchMedia`, and the React state around them.
 */
export const LAYOUT_VERSION = 1;
export const RAIL_MIN = 0.1;
export const RAIL_MAX = 0.34;
export const MOBILE_MAX_PX = 900;

export type PanelId = "left" | "center" | "right";
export type LayoutState = { left: number; right: number; collapsed: Record<PanelId, boolean> };

export const DEFAULT_LAYOUT: LayoutState = { left: 0.2, right: 0.24, collapsed: { left: false, center: false, right: false } };

export function layoutKey(userId: string): string {
  return `pgm.terminal.layout.v${LAYOUT_VERSION}.${userId}`;
}

/** Parse what was stored, and refuse anything that is not a layout. A corrupt preference must not blank a rail. */
export function parseLayout(raw: string | null): LayoutState {
  if (!raw) return DEFAULT_LAYOUT;
  try {
    const parsed = JSON.parse(raw) as Partial<LayoutState>;
    const left = Number(parsed.left);
    const right = Number(parsed.right);
    const collapsed = (parsed.collapsed ?? {}) as Record<string, unknown>;
    return {
      left: clampRail(Number.isFinite(left) ? left : DEFAULT_LAYOUT.left),
      right: clampRail(Number.isFinite(right) ? right : DEFAULT_LAYOUT.right),
      collapsed: {
        left: collapsed.left === true,
        center: false,                                  // the center is never collapsible: it is the screen
        right: collapsed.right === true,
      },
    };
  } catch {
    return DEFAULT_LAYOUT;
  }
}

export function clampRail(value: number): number {
  return Math.min(RAIL_MAX, Math.max(RAIL_MIN, value));
}

/** The grid at a given viewport width: rails from the layout, the center taking what is left. */
export function columnsFor(layout: LayoutState, viewportPx: number): { left: number; center: number; right: number } {
  const left = layout.collapsed.left ? 0 : clampRail(layout.left);
  const right = layout.collapsed.right ? 0 : clampRail(layout.right);
  return { left, center: Math.max(0.2, 1 - left - right), right };
}

/** A step for the keyboard: half a percent per press, and shift for a coarse move. */
export function nudge(value: number, delta: number, coarse: boolean): number {
  return clampRail(value + delta * (coarse ? 5 : 1));
}

export function isMobile(viewportPx: number): boolean {
  return viewportPx <= MOBILE_MAX_PX;
}

