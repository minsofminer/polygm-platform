import { notFound } from "next/navigation";
import { t } from "@/i18n/t";
import { serverRead } from "@/api/server-read";
import { MarketView } from "@/screens/MarketView";
import type { MarketDetail } from "@/screens/MarketRail";

export const dynamic = "force-dynamic";

/**
 * A single market: the binary/few-outcome layout.
 *
 * Two reads on the server, and they are different reads on purpose: the market's own metadata (which is what a
 * crawler indexes and what a shared link must render without JavaScript) and the book (which is a live object
 * the client replaces a second later). The chart, the book and the ticket then hydrate against the same
 * endpoints the SSR used, so there is one source of truth per number.
 */
export default async function MarketPage({ params }: { params: Promise<{ market_id: string }> }) {
  const { market_id } = await params;
  const read = await serverRead<{ market: MarketDetail }>("GET", `/v1/markets/${encodeURIComponent(market_id)}`);
  if (read.ok === false) {
    if (read.status === 404) notFound();
    return (
      <main className="pgm-page">
        <p className="unavailable" role="status">
          <strong>{t("common.state.errorTitle")}</strong>
          <span>{t("markets.state.error")}</span>
        </p>
      </main>
    );
  }
  return (
    <main className="pgm-page">
      <MarketView market={read.data.market} stamp={read.data as { asOf?: number; staleAfter?: number }} />
    </main>
  );
}
