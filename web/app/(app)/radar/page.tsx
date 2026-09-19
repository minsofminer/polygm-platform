import { t } from "@/i18n/terminal";
import { serverRead } from "@/api/server-read";
import { RadarView } from "@/terminal/RadarView";
import type { TapeFacets } from "@/terminal/wire";

export const dynamic = "force-dynamic";

/**
 * The Wallet Radar — D5.
 *
 * The market picker is populated on the server from the tape's own facets, the same read `/whales` uses: a picker
 * that offered every market in the database would invite a scan of ten markets with no fills, which costs a scan
 * from the daily allowance and answers nothing. The scan itself is a client action — it spends quota, so it
 * happens when the user presses the button and never on page load.
 */
export default async function RadarPage() {
  const read = await serverRead<TapeFacets>("GET", "/v1/tape/facets?windowMs=86400000");
  const markets = read.ok === false ? [] : (read.data.markets ?? []).map((m) => ({ marketId: m.marketId, question: m.question }));
  return (
    <main className="pgm-page">
      {read.ok === false ? (
        <p className="unavailable" role="status">
          <span>{t("terminal.radar.facetsUnavailable")}</span>
        </p>
      ) : null}
      <RadarView markets={markets} />
    </main>
  );
}
