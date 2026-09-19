import { t } from "@/i18n/t";
import { serverRead } from "@/api/server-read";
import { WhaleTracker } from "@/terminal/WhaleTracker";
import type { TapeFacets } from "@/terminal/wire";

export const dynamic = "force-dynamic";

/**
 * The whale tracker — D4.
 *
 * The market picker's options come from the tape's own facets on the server, so the select is populated in the
 * first paint and the list only contains markets that actually have fills to look at. The feed itself is a
 * client read (a live surface), and it starts on the global scope with the server's default window.
 */
export default async function WhalesPage() {
  const read = await serverRead<TapeFacets>("GET", "/v1/tape/facets?windowMs=86400000");
  const markets = read.ok === false ? [] : (read.data.markets ?? []).map((m) => ({ marketId: m.marketId, question: m.question }));
  return (
    <main className="pgm-page">
      {read.ok === false ? (
        <p className="unavailable" role="status">
          <span>{t("terminal.whales.facetsUnavailable")}</span>
        </p>
      ) : null}
      <WhaleTracker markets={markets} />
    </main>
  );
}
