/**
 * The polite live region: one `role="status"` node, reused by every announcement in the app.
 *
 * Moved out of `Dialog.tsx` on 2026-09-29 — that module is `"use client"`, so `announce` was a client reference
 * for any server importer (see src/client-boundary.test.ts). It touches the DOM directly and needs no React.
 *
 * One region, reused: an `aria-live` on the tape itself would make the product unusable (web/DESIGN.md §7), so
 * announcements come from here instead, at most one per few seconds by nature.
 */
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
