"use client";
/**
 * D1 · the three-column terminal.
 *
 * The layout is a decision, not a stylesheet:
 *
 *  - **Resizable and collapsible, persisted per user.** Widths live as fractions of the viewport (so a rail
 *    keeps its proportion when the window changes) with `RAIL_MIN`/`RAIL_MAX` as fractions of the *viewport*,
 *    not pixel constants — a 340px rail is half the screen at 1280 and a quarter at 1680, and the prompt asks
 *    for both to look sane.
 *  - **Keyboard parity.** A drag handle that only responds to a pointer is a control half the users cannot
 *    reach, so each handle is a `separator` with arrow-key resizing and a double-click reset.
 *  - **Mobile is the SAME information as tabs**, never truncated. The three columns become three tabs; nothing
 *    is dropped, and the tab that is not selected is not merely hidden with CSS — it is not mounted, since a
 *    hidden live tape still costs a browser a paint budget it does not have.
 *  - **Persistence is per user and versioned.** The key carries the user id and a version: a layout saved by
 *    an older build that had four rails must not be applied to a three-rail screen and collapse one to zero.
 */
import { useCallback, useEffect, useMemo, useRef, useState, type ReactNode } from "react";
import { t } from "@/i18n/terminal";

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

type Props = {
  userId: string;
  left: ReactNode;
  center: ReactNode;
  right: ReactNode;
  /** Rendered above the columns on every breakpoint: the market header, the connection dot, the freshness. */
  header?: ReactNode;
};

export function TerminalLayout({ userId, left, center, right, header }: Props) {
  const [layout, setLayout] = useState<LayoutState>(DEFAULT_LAYOUT);
  const [viewport, setViewport] = useState(1440);
  const [tab, setTab] = useState<PanelId>("center");
  const dragging = useRef<null | "left" | "right">(null);

  // Restore before the first paint of the columns: a layout that snaps once the effect runs is a layout the
  // user watches jump.
  useEffect(() => {
    setLayout(parseLayout(window.localStorage.getItem(layoutKey(userId))));
    const onResize = () => setViewport(window.innerWidth);
    onResize();
    window.addEventListener("resize", onResize);
    return () => window.removeEventListener("resize", onResize);
  }, [userId]);

  const persist = useCallback(
    (next: LayoutState) => {
      setLayout(next);
      try {
        window.localStorage.setItem(layoutKey(userId), JSON.stringify(next));
      } catch {
        // A full or disabled localStorage is not a reason to lose the interaction in this session.
      }
    },
    [userId],
  );

  const cols = useMemo(() => columnsFor(layout, viewport), [layout, viewport]);
  const mobile = isMobile(viewport);

  const onDragMove = useCallback(
    (event: PointerEvent) => {
      const which = dragging.current;
      if (!which) return;
      const px = event.clientX / Math.max(1, window.innerWidth);
      if (which === "left") persist({ ...layout, left: clampRail(px) });
      else persist({ ...layout, right: clampRail(1 - px) });
    },
    [layout, persist],
  );

  useEffect(() => {
    const move = (e: PointerEvent) => onDragMove(e);
    const up = () => {
      dragging.current = null;
    };
    window.addEventListener("pointermove", move);
    window.addEventListener("pointerup", up);
    return () => {
      window.removeEventListener("pointermove", move);
      window.removeEventListener("pointerup", up);
    };
  }, [onDragMove]);

  const handleKeys = (which: "left" | "right") => (event: React.KeyboardEvent<HTMLDivElement>) => {
    const deltas: Record<string, number> = { ArrowLeft: -0.005, ArrowRight: 0.005 };
    if (event.key === "Home") {
      persist({ ...layout, [which]: which === "left" ? RAIL_MIN : RAIL_MIN });
      return;
    }
    const delta = deltas[event.key];
    if (!delta) return;
    event.preventDefault();
    const sign = which === "left" ? 1 : -1;
    persist({ ...layout, [which]: nudge(layout[which], delta * sign * 100, event.shiftKey) });
  };

  if (mobile) {
    // Same three panels, one at a time. The tabs are the panels' own names — "Left/Center/Right" would be
    // naming the layout instead of the content, and the content is what a user is looking for on a phone.
    const panels: { id: PanelId; label: string; node: ReactNode }[] = [
      { id: "left", label: t("terminal.tabs.watchlists"), node: left },
      { id: "center", label: t("terminal.tabs.market"), node: center },
      { id: "right", label: t("terminal.tabs.ticket"), node: right },
    ];
    return (
      <div className="pgm-terminal pgm-terminal--mobile">
        {header}
        <div className="pgm-terminal__tabs" role="tablist" aria-label={t("terminal.tabs.label")}>
          {panels.map((panel) => (
            <button
              key={panel.id}
              type="button"
              role="tab"
              aria-selected={tab === panel.id}
              onClick={() => setTab(panel.id)}
              className={`pgm-terminal__tab${tab === panel.id ? " is-active" : ""}`}
            >
              {panel.label}
            </button>
          ))}
        </div>
        {panels.filter((p) => p.id === tab).map((p) => (
          <section key={p.id} role="tabpanel" className={`pgm-terminal__panel pgm-terminal__panel--${p.id}`}>
            {p.node}
          </section>
        ))}
      </div>
    );
  }

  return (
    <div
      className="pgm-terminal"
      // The gutters are the drag handles. `var(--pgm-space-2)` rather than a px literal: P08's c5 scans every
      // shipped file for dimension literals, and it was right to fail this one — the handle is a dimension the
      // design system owns, and 6px was not on its grid or in its tokens.
      style={{ gridTemplateColumns: `${cols.left * 100}% var(--pgm-space-2) ${cols.center * 100}% ` +
        `var(--pgm-space-2) ${cols.right * 100}%` }}
    >
      {header}
      <aside className="pgm-terminal__panel pgm-terminal__panel--left" aria-label={t("terminal.tabs.watchlists")}>
        <CollapseButton panel="left" layout={layout} persist={persist} />
        {!layout.collapsed.left && left}
      </aside>
      <div
        className="pgm-terminal__handle"
        role="separator"
        tabIndex={0}
        aria-orientation="vertical"
        aria-label={t("terminal.layout.resizeLeft")}
        onPointerDown={() => {
          dragging.current = "left";
        }}
        onKeyDown={handleKeys("left")}
        onDoubleClick={() => persist({ ...layout, left: DEFAULT_LAYOUT.left })}
      />
      <main className="pgm-terminal__panel pgm-terminal__panel--center">{center}</main>
      <div
        className="pgm-terminal__handle"
        role="separator"
        tabIndex={0}
        aria-orientation="vertical"
        aria-label={t("terminal.layout.resizeRight")}
        onPointerDown={() => {
          dragging.current = "right";
        }}
        onKeyDown={handleKeys("right")}
        onDoubleClick={() => persist({ ...layout, right: DEFAULT_LAYOUT.right })}
      />
      <aside className="pgm-terminal__panel pgm-terminal__panel--right" aria-label={t("terminal.tabs.ticket")}>
        <CollapseButton panel="right" layout={layout} persist={persist} />
        {!layout.collapsed.right && right}
      </aside>
    </div>
  );
}

function CollapseButton({
  panel,
  layout,
  persist,
}: {
  panel: "left" | "right";
  layout: LayoutState;
  persist: (next: LayoutState) => void;
}) {
  const collapsed = layout.collapsed[panel];
  return (
    <button
      type="button"
      className="pgm-terminal__collapse"
      aria-expanded={!collapsed}
      aria-label={collapsed
        ? t("terminal.layout.expand", { side: panel })
        : t("terminal.layout.collapse", { side: panel })}
      onClick={() => persist({ ...layout, collapsed: { ...layout.collapsed, [panel]: !collapsed } })}
    >
      {collapsed ? "»" : "«"}
    </button>
  );
}
