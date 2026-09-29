/**
 * Command-palette filtering, as a pure function — and the palette's own entry type.
 *
 * Moved out of `CommandPalette.tsx` on 2026-09-29: that module is `"use client"`, and a plain function exported
 * from a client module becomes a *client reference* — callable from a client component, fatal from a server one
 * (see src/client-boundary.test.ts for the rule and the `/terminal` 500 that taught it).
 */
import { MOBILE_TABS } from "./shortcuts";
import { t } from "@/i18n/t";

export type PaletteEntry = { group: "markets" | "traders" | "actions"; label: string; href?: string; run?: () => void; note?: string };

/** A wallet is 0x + 40 hex. Only then do we offer the trader search: an address-typed query that hits the
 *  market index returns nothing useful and teaches the user that search is broken. */
const ADDRESS = /^0x[0-9a-fA-F]{40}$/;

export function filterEntries(query: string, markets: { id: string; question: string }[]): PaletteEntry[] {
  const q = query.trim().toLowerCase();
  const groups: PaletteEntry[] = [];
  if (ADDRESS.test(q)) {
    groups.push({ group: "traders", label: q, href: `/traders/${q}` });
  }
  for (const m of markets) {
    if (!q || m.question.toLowerCase().includes(q)) groups.push({ group: "markets", label: m.question, href: `/markets/${m.id}` });
  }
  for (const tab of MOBILE_TABS) groups.push({ group: "actions", label: t("shell.palette.goTo", { label: tab.label }), href: tab.href });
  return groups;
}
