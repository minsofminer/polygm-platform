import { t } from "@/i18n/t";
import { serverRead } from "@/api/server-read";
import { MarketsClient } from "@/screens/MarketsClient";
import type { MarketRow } from "@/screens/MarketCard";

export const dynamic = "force-dynamic";

/**
 * Discovery, server-rendered first then client-hydrated (D1: "server-rendered first page for SEO, then
 * client-hydrated live updates").
 *
 * The first page is fetched here, on the server, so a crawler and a first paint both see markets without
 * waiting for JavaScript. The interactive half (filters, sort, the long-tail toggle, "new markets" while
 * scrolled) is `MarketsClient`, which is handed this payload as its initial state rather than re-fetching it —
 * a client that re-fetches the page the server just rendered is a visible double-load on every navigation.
 *
 * A failed SSR read is not a failed page. `initial={null}` means exactly that, and the client then fetches the
 * first page itself through the same-origin proxy: the browser's network path to the API is not the server's,
 * and a page that has already given up cannot find out that its own server was the only thing broken. It is
 * also the difference between a route whose measured payload describes a good day and one whose payload is a
 * paragraph — the P08 budget check measures the chunks a document actually fetches.
 *
 * The freshness stamp is the API's, rendered as text: a cached snapshot is labelled, never presented as live.
 *
 * `MarketsClient` appears once in this file, in both cases. Two JSX sites for one widget is two places for it
 * to be composed differently, which is why `tools/p08-gate-check.py` c14 counts them.
 */
export default async function MarketsPage() {
  const read = await serverRead<{
    items: MarketRow[];
    nextCursor: string | null;
    facets: Record<string, number>;
    categories: string[];
    longTail: { includeLongTail: boolean; hiddenCount: number; thresholdMicro: number };
    sortKeys: string[];
    asOf?: number;
  }>("GET", "/v1/markets?limit=50&sortBy=volume24h");

  return (
    <main className="pgm-page">
      {read.ok === false ? (
        <p className="unavailable" role="status">
          <strong>{t("common.state.errorTitle")}</strong>
          <span>{t("markets.state.retry")}</span>
        </p>
      ) : (
        <p className="refusal">{read.stampLabel}</p>
      )}
      <MarketsClient initial={read.ok === false ? null : read.data} />
    </main>
  );
}
