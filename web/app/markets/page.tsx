import { Number as NumberView } from "@/num/Number";
import { t } from "@/i18n/t";
import { WidgetBoundary } from "@/ui/WidgetBoundary";
import { serverRead } from "@/api/server-read";

export const dynamic = "force-dynamic";

/**
 * The public markets list, rendered on the server (this is the page a crawler should find, and the page a
 * user shares). Prices come through `Number`, so they arrive formatted by the money layer with the
 * tick-derived precision — the list is not exempt from the no-raw-numbers rule merely because it is public.
 *
 * The freshness of this list is the *stamp from the API*, and it is rendered as text: a cached snapshot is
 * labelled, never presented as live.
 */
export default async function MarketsPage() {
  const read = await serverRead<{ items?: Record<string, unknown>[]; ttlMs?: number }>("GET", "/v1/markets?limit=25");
  return (
    <main style={{ padding: "var(--pgm-pad-screen-compact)", maxWidth: "var(--pgm-shell-max-width)", margin: "0 auto" }}>
      <h1>{t("shell.nav.markets")}</h1>
      {read.ok === false ? (
        <p className="unavailable" role="status">
          <strong>{t("common.state.errorTitle")}</strong>
          <span>
            {read.message} — the list is served from the ingest cache, and the cache is not answering. Prices
            elsewhere on the page are unaffected.
          </span>
        </p>
      ) : (
        <>
          <p className="refusal">
            {read.stampLabel}
          </p>
          <WidgetBoundary label={t("shell.nav.markets")}>
            <ul style={{ listStyle: "none", padding: 0, display: "grid", gap: "var(--pgm-space-2)" }}>
              {(read.data.items ?? []).map((market) => {
                const id = String(market.id ?? "");
                const question = String(market.question ?? id);
                const bestAskUnits = typeof market.best_ask_units === "number" ? market.best_ask_units : null;
                return (
                  <li key={id}>
                    <a href={`/markets/${encodeURIComponent(id)}`} className="button" style={{ justifyContent: "space-between", inlineSize: "100%" }}>
                      <span>{question}</span>
                      {bestAskUnits !== null ? (
                        <NumberView
                          kind="price"
                          value={bestAskUnits}
                          tick={typeof market.tick === "string" ? market.tick : "0.01"}
                          freshness={read.freshness}
                          staleMs={read.ageMs}
                          source="rest"
                          label="best ask"
                        />
                      ) : (
                        <span className="refusal">{t("num.stale.unknown")}</span>
                      )}
                    </a>
                  </li>
                );
              })}
            </ul>
          </WidgetBoundary>
        </>
      )}
    </main>
  );
}
