import type { Metadata } from "next";
import { notFound } from "next/navigation";
import { t } from "@/i18n/public";
import { publicRead } from "@/api/public-read";
import { serverRead } from "@/api/server-read";
import { TraderDossierView } from "@/terminal/DossierView";
import type { TraderDossier } from "@/terminal/wire";
import { TraderView } from "@/public/TraderView";
import { PublicState } from "@/public/Chrome";
import type { PublicTraderPage } from "@/public/wire";

export const dynamic = "force-dynamic";

/**
 * `/trader/<handle>` and `/trader/<anon>` — one segment, two publishers.
 *
 * D4 promised that a handle and a pseudonym resolve to the SAME wallet ("there is one handle per traded wallet:
 * /trader/<handle> resolves to the same wallet as its pseudonym"), so the two pages are the same page told at
 * two privacy levels: the pseudonym page is the dossier the app shows, and the handle page is the public row
 * the trader opted into by listing. Next forbids two dynamic segment names at one level, and it is right to:
 * the URL space is one space.
 *
 * The dispatch rule is written down rather than guessed:
 *   * a `w_…` segment is a pseudonym → the dossier, and if no such wallet has ever traded, the public read gets
 *     the chance to answer (a handle may legitimately look like a pseudonym; the pseudonym is just more likely);
 *   * anything else is a handle → the public page, and a 404 is a 404, because a segment that is not a
 *     pseudonym has no other reading.
 *
 * The public read is what decides indexability (`robots`), the canonical URL and the OG image, so the two pages
 * cannot disagree with the API about whether a page may be crawled.
 */
const PSEUDONYM = /^w_[0-9a-f]{6,}$/;

export async function generateMetadata({ params }: { params: Promise<{ who: string }> }): Promise<Metadata> {
  const { who } = await params;
  const seg = decodeURIComponent(who);
  if (PSEUDONYM.test(seg)) return { title: seg, robots: { index: false, follow: true } };
  const read = await publicRead<PublicTraderPage>("GET", `/v1/public/trader/${encodeURIComponent(seg)}`);
  if (read.ok === false) return { title: t("public.state.notFound"), robots: { index: false, follow: false } };
  const page = read.data;
  return {
    title: `@${page.handle}`,
    description: `${page.headline.board}: rank ${page.headline.rank} of ${page.headline.rankedTotal}. ${page.notes.join(" ")}`,
    alternates: { canonical: page.url },
    // The API's own `robots` string, split into the object Next wants: the server decides indexability, and
    // this route does not get a second opinion about it.
    robots: page.robots.startsWith("index")
      ? { index: true, follow: true }
      : { index: false, follow: true },
    openGraph: { type: "profile", url: page.url, siteName: page.card.brand },
  };
}

export default async function TraderPage({ params }: { params: Promise<{ who: string }> }) {
  const { who } = await params;
  const seg = decodeURIComponent(who);
  if (PSEUDONYM.test(seg)) {
    const read = await serverRead<TraderDossier>("GET", `/v1/traders/${encodeURIComponent(seg)}`);
    if (read.ok === true) {
      return (
        <main className="pgm-page">
          <TraderDossierView anon={seg} initial={{ anonWallet: seg, stamp: read.data as { asOf?: number; staleAfter?: number } }} />
        </main>
      );
    }
    if (read.status !== 404) {
      return <PublicState title={t("public.state.errorTitle")} body={t("public.state.errorBody")} />;
    }
  }
  const read = await publicRead<PublicTraderPage>("GET", `/v1/public/trader/${encodeURIComponent(seg)}`);
  if (read.ok === false) {
    if (read.status === 404 || read.status === 422) notFound();
    return <PublicState title={t("public.state.errorTitle")} body={t("public.state.errorBody")} />;
  }
  return <TraderView page={read.data} now={Date.now()} />;
}
