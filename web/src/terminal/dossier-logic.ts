/**
 * The dossier's window labels, and the `windowLabel` lookup over them.
 *
 * Moved out of `DossierView.tsx` on 2026-09-29: that module is `"use client"`, and a plain function exported
 * from a client module cannot be called while rendering on the server (src/client-boundary.test.ts).
 *
 * The labels are built with literal `t()` keys, deliberately: `t()` with a template argument is a build failure
 * in this repo (the check cannot verify what it cannot read), and the key shape forbids a segment starting with a
 * digit — so `terminal.dossier.window.7d` was wrong twice. Every key here is literal and checkable.
 */
import { t } from "@/i18n/terminal";

const WINDOW_LABEL: Record<string, string> = {
  "7d": t("terminal.dossier.window7"),
  "30d": t("terminal.dossier.window30"),
  "90d": t("terminal.dossier.window90"),
  all: t("terminal.dossier.windowAll"),
};

export function windowLabel(key: string): string {
  return WINDOW_LABEL[key] ?? key;
}
