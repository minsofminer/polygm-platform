/**
 * Every price surface carries a freshness indicator; the prompt's rule is "no unguarded price display", and
 * the design rule is that colour is never the only channel. So this component is a *word*, and it changes
 * the number's treatment via a data attribute the stylesheet desaturates.
 *
 * It also refuses to be decorative: the reason is part of the text, because "stale" without a cause reads
 * as "the market is quiet" to a user who should be reading "our feed died".
 */
import type { Freshness } from "@/api/envelope";
import { t } from "@/i18n/t";

export function StaleIndicator({ freshness, ageMs }: { freshness: Freshness; ageMs: number | null }) {
  if (freshness === "live") return null;
  const words = {
    stale: t("num.stale.stale"),
    blocking: t("num.stale.blocking"),
    unknown: t("num.stale.unknown"),
    live: "",
  }[freshness];
  return (
    <span className="stale" data-freshness={freshness} role="status">
      <span aria-hidden="true" className="stale__mark">
        {freshness === "blocking" ? "!" : "~"}
      </span>
      {words}
      {ageMs !== null ? <span className="stale__age"> ({ageS(ageMs)})</span> : null}
    </span>
  );
}

function ageS(ms: number): string {
  if (ms < 1_000) return "<1s";
  const s = Math.floor(ms / 1_000);
  if (s < 60) return `${s}s`;
  const m = Math.floor(s / 60);
  if (m < 60) return `${m}m${s % 60 ? " " + (s % 60) + "s" : ""}`;
  return `${Math.floor(m / 60)}h`;
}
