"use client";
import { connectionLabel, useConnection } from "./connection";
import { t } from "@/i18n/t";

/**
 * Always visible (P08 D4), and always a word. A coloured dot is a decoration: it is the same red as a SELL
 * pill and an alert badge (web/DESIGN.md §2, the ΔE 3–5 pair), so the mark carries the colour and the text
 * carries the meaning.
 */
export function ConnectionDot() {
  const c = useConnection();
  const label = connectionLabel(c);
  const words: Record<string, string> = {
    live: t("shell.connection.live"),
    stale: t("shell.connection.stale"),
    blocking: t("shell.connection.blocking"),
    down: t("shell.connection.down"),
    unknown: t("num.stale.unknown"),
  };
  const reason = useConnection.getState().whyNot();
  return (
    <span className="dot" data-connection={label} role="status" aria-live="off">
      <span className="dot__mark" aria-hidden="true" />
      <span>{words[label]}</span>
      {c.gapCount > 0 ? <span>{t("shell.connection.gap", { count: c.gapCount })}</span> : null}
      {reason ? <span className="refusal">{t("shell.connection.tradingDisabled", { reason })}</span> : null}
    </span>
  );
}
