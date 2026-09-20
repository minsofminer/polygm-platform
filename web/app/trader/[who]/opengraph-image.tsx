import { notFound } from "next/navigation";
import { publicRead } from "@/api/public-read";
import { OG_CONTENT_TYPE, OG_SIZE, renderCard } from "@/public/og";
import type { PublicTraderPage } from "@/public/wire";

/**
 * `/trader/<handle>/opengraph-image` — the card an unfurler fetches.
 *
 * `revalidate` is the API's own OG lifetime (`CACHE["og"]` in `packages/polygm_core/public_pages/urls.py`):
 * a day, with a week of stale-while-revalidate upstream. That is not a performance choice — a share card is a
 * statement about a rank at a moment in time, and a card that regenerates every second would be a card whose
 * numbers move under a reader who is comparing it with the page.
 */
export const revalidate = 86_400;
export const alt = "Openout share card";
export const size = OG_SIZE;
export const contentType = OG_CONTENT_TYPE;

export default async function TraderCard({ params }: { params: Promise<{ who: string }> }) {
  const { who } = await params;
  const seg = decodeURIComponent(who);
  if (/^w_[0-9a-f]{6,}$/.test(seg)) notFound();          // a pseudonym's page is the app's, and has no card
  const read = await publicRead<PublicTraderPage>("GET", `/v1/public/trader/${encodeURIComponent(seg)}`);
  if (read.ok === false) notFound();
  const image = renderCard(read.data.card);
  if (!image) notFound();
  return image;
}
