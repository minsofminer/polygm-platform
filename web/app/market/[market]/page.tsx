import type { Metadata } from "next";
import { notFound } from "next/navigation";
import { t } from "@/i18n/public";
import { publicRead } from "@/api/public-read";
import { serverRead } from "@/api/server-read";
import { MarketView } from "@/screens/MarketView";
import type { MarketDetail } from "@/screens/MarketRail";
import { MarketPublicView } from "@/public/MarketView";
import { PublicState } from "@/public/Chrome";
import { isMarketId } from "@/public/market-dispatch";
import type { PublicMarketPage } from "@/public/wire";
import { siteUrl } from "@/public/site-origin";

export const dynamic = "force-dynamic";

/**
 * `/market/<0x id>` (the app's detail page, P09) and `/market/<slug>` (the public odds page, D6).
 *
 * Two pages, one URL space, one segment: the dispatch is a property of the address, not a preference. It exists
 * as one file because Next forbids two dynamic names at the same level — and because splitting them would let
 * the two pages drift apart about what "this market" is. The rule itself lives in `@/public/market-dispatch`
 * with its own test, after the version that shipped here matched none of this database's ids.
 *
 * The public page is what a news story links to; the detail page is what the app links to. Both read the same
 * rows, and only the public one is written to be read without JavaScript.
 */

export async function generateMetadata({ params }: { params: Promise<{ market: string }> }): Promise<Metadata> {
  const { market } = await params;
  const seg = decodeURIComponent(market);
  if (isMarketId(seg)) return { title: seg };
  const read = await publicRead<PublicMarketPage>("GET", `/v1/public/market/${encodeURIComponent(seg)}`);
  if (read.ok === false) return { title: t("public.state.notFound"), robots: { index: false, follow: false } };
  const page = read.data;
  return {
    title: page.question,
    description: `${page.odds.lastPrice ?? "no quote"} · ${page.quoteNote} · ${page.odds.ageText}`,
    alternates: { canonical: siteUrl(page.url) },
    // A market page is always indexable (the odds are public data), and the API says so; this reads its answer.
    robots: page.robots.startsWith("index") ? { index: true, follow: true } : { index: false, follow: true },
    openGraph: { type: "article", url: siteUrl(page.url), siteName: page.card.brand, title: page.question },
  };
}

export default async function MarketPage({ params }: { params: Promise<{ market: string }> }) {
  const { market } = await params;
  const seg = decodeURIComponent(market);
  if (isMarketId(seg)) {
    const read = await serverRead<{ market: MarketDetail }>("GET", `/v1/markets/${encodeURIComponent(seg)}`);
    if (read.ok === false) {
      if (read.status === 404) notFound();
      return <PublicState title={t("public.state.errorTitle")} body={t("public.state.errorBody")} />;
    }
    return (
      <main className="pgm-page">
        <MarketView market={read.data.market} stamp={read.data as { asOf?: number; staleAfter?: number }} />
      </main>
    );
  }
  const read = await publicRead<PublicMarketPage>("GET", `/v1/public/market/${encodeURIComponent(seg)}`);
  if (read.ok === false) {
    if (read.status === 404 || read.status === 422) notFound();
    return <PublicState title={t("public.state.errorTitle")} body={t("public.state.errorBody")} />;
  }
  return <MarketPublicView page={read.data} now={Date.now()} />;
}
