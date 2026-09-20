import { notFound } from "next/navigation";
import { publicRead } from "@/api/public-read";
import { OG_CONTENT_TYPE, OG_SIZE, renderCard } from "@/public/og";
import type { PublicMarketPage } from "@/public/wire";

/** `/market/<slug>/opengraph-image` — the odds card, with the age of the price in its footnote. */
export const revalidate = 86_400;
export const alt = "Openout market card";
export const size = OG_SIZE;
export const contentType = OG_CONTENT_TYPE;

export default async function MarketCard({ params }: { params: Promise<{ market: string }> }) {
  const { market } = await params;
  const seg = decodeURIComponent(market);
  if (/^0x[0-9a-fA-F]+$/.test(seg)) notFound();          // the app's detail page draws its own surfaces
  const read = await publicRead<PublicMarketPage>("GET", `/v1/public/market/${encodeURIComponent(seg)}`);
  if (read.ok === false) notFound();
  const image = renderCard(read.data.card);
  if (!image) notFound();
  return image;
}
