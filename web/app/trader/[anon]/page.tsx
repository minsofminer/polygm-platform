import { notFound } from "next/navigation";
import { t } from "@/i18n/t";
import { serverRead } from "@/api/server-read";
import { TraderDossierView } from "@/terminal/DossierView";
import type { TraderDossier } from "@/terminal/wire";

export const dynamic = "force-dynamic";

/**
 * A trader's dossier — D3.
 *
 * Server-rendered first so a shared link shows the pseudonym, the sample-gated win rate and the drawdown without
 * JavaScript, then the client hook replaces it with the selected window. The pseudonym is the identity this
 * product publishes: the page never receives an address, so it cannot leak one into a title, a log line or a
 * screenshot.
 */
export default async function TraderPage({ params }: { params: Promise<{ anon: string }> }) {
  const { anon } = await params;
  const read = await serverRead<TraderDossier>("GET", `/v1/traders/${encodeURIComponent(anon)}`);
  if (read.ok === false) {
    if (read.status === 404) notFound();
    return (
      <main className="pgm-page">
        <p className="unavailable" role="status">
          <strong>{t("common.state.errorTitle")}</strong>
          <span>{t("terminal.dossier.serverError")}</span>
        </p>
      </main>
    );
  }
  return (
    <main className="pgm-page">
      <TraderDossierView anon={anon} initial={{ anonWallet: anon, stamp: read.data as { asOf?: number; staleAfter?: number } }} />
    </main>
  );
}
