import { t } from "@/i18n/admin";
import { GamingView } from "@/terminal/GamingView";

export const dynamic = "force-dynamic";

/**
 * D7 · the anti-gaming dashboard, inside the app shell but behind an operator token.
 *
 * `force-dynamic` because this page is about the tape as it is right now and must never be served from a cache —
 * a cached suspicion list is a list of yesterday's farms with today's clock on it. The route itself carries no
 * data: `GamingView` asks for the token and does the read, so nothing about a wallet is ever rendered on the
 * server or into the document for a crawler to pick up.
 */
export default function AdminGamingPage() {
  return (
    <main className="pgm-page">
      <h1 id="admin-gaming-title">{t("admin.gaming.title")}</h1>
      <GamingView />
    </main>
  );
}
