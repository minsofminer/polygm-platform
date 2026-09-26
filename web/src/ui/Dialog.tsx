/**
 * A modal that does not trap focus, because web/DESIGN.md §7 says so and because the trap is the bug:
 * keyboard users get stuck in a dialog whose Esc is the only door. So: focus is *started* inside the panel
 * and restored to the trigger on close, `Tab` is allowed to wander out, and `Esc` always closes — except
 * while a request is in flight, and that exception is announced in the same breath (the design rule's
 * "and that is announced" is the part everyone skips).
 *
 * This is why the shell has no Radix: its Dialog traps by default, and un-trapping a third-party component
 * is a permanent negotiation. The version here is small enough to read.
 *
 * **The close is a two-step, and the first step is the animation.** A modal that vanishes between frames reads as
 * a glitch, so closing plays `pgm-panel-out` and then removes the panel — the same shape the Mini App's sheet uses.
 * The step that is easy to miss: `onAnimationEnd` never fires when `prefers-reduced-motion: reduce` has turned
 * animations off, so the exit path short-circuits to `onClose()` for exactly those users. A dialog that waits for
 * an animation that cannot run is a dialog nobody can close. See `plans/animation-audit.md` §P1.
 */
"use client";
import { useCallback, useEffect, useRef, useState, type ReactNode } from "react";
import { t } from "@/i18n/t";

/** True when the user has asked their system for less motion. Both exit paths ask this before they animate. */
function prefersReducedMotion(): boolean {
  return typeof window !== "undefined" && typeof window.matchMedia === "function"
    ? window.matchMedia("(prefers-reduced-motion: reduce)").matches
    : false;
}

export type DialogProps = {
  open: boolean;
  onClose: () => void;
  title: string;
  /** True while the dialog's own action is in flight: Esc is refused, and the reason is said. */
  busy?: boolean;
  children: ReactNode;
  describeBy?: string;
};

export function Dialog({ open, onClose, title, busy, children, describeBy }: DialogProps) {
  const panel = useRef<HTMLDivElement | null>(null);
  const restore = useRef<Element | null>(null);
  const [leaving, setLeaving] = useState(false);

  /** Close, with the exit played first — or immediately, when motion is off or the browser cannot animate. */
  const requestClose = useCallback(() => {
    if (prefersReducedMotion()) {
      onClose();
      return;
    }
    setLeaving(true);
  }, [onClose]);

  useEffect(() => {
    if (open) setLeaving(false);           // a reopened dialog enters again rather than reusing the exit frame
  }, [open]);

  useEffect(() => {
    if (!open) return;
    restore.current = document.activeElement;
    const el = panel.current;
    el?.querySelector<HTMLElement>("input, button, [href], [tabindex]:not([tabindex='-1'])")?.focus();
    const onKey = (event: KeyboardEvent) => {
      if (event.key !== "Escape") return;
      if (busy) {
        announce("A request is in flight. The dialog stays open until it answers.");
        return;
      }
      requestClose();
    };
    document.addEventListener("keydown", onKey);
    return () => {
      document.removeEventListener("keydown", onKey);
      if (restore.current instanceof HTMLElement) restore.current.focus();
    };
  }, [open, busy, requestClose]);

  if (!open) return null;
  return (
    <div
      className={`overlay ${leaving ? "pgm-fade-out" : "pgm-fade-in"}`}
      role="presentation"
      onMouseDown={(e) => {
        if (e.target !== e.currentTarget) return;
        if (busy) {
          announce("A request is in flight. The dialog stays open until it answers.");
          return;
        }
        requestClose();
      }}
    >
      <div
        className={`overlay__panel ${leaving ? "pgm-panel-out" : "pgm-panel-in"}`}
        role="dialog"
        aria-modal="false"
        aria-label={title}
        aria-describedby={describeBy}
        ref={panel}
        // The panel's animation is the longer of the two, so IT decides when the dialog is gone. `onAnimationEnd`
        // fires for the panel's own exit only; a child's bubbling animation is filtered out by the target check.
        onAnimationEnd={(event) => {
          if (!leaving || event.target !== event.currentTarget) return;
          onClose();
        }}
      >
        {busy ? <span className="a11y-only" role="status">{t("common.state.loading")}</span> : null}
        {children}
      </div>
    </div>
  );
}

const liveRegion = (id: string) => {
  let el = document.getElementById(id);
  if (el) return el;
  el = document.createElement("div");
  el.id = id;
  el.setAttribute("role", "status");
  el.setAttribute("aria-live", "polite");
  el.className = "a11y-only";
  document.body.appendChild(el);
  return el;
};

/** One polite region, reused. A `aria-live` on the tape itself would make the product unusable
 *  (web/DESIGN.md §7), so announcements come from here instead, at most one per few seconds by nature. */
export function announce(text: string): void {
  liveRegion("pgm-announcer").textContent = text;
}
