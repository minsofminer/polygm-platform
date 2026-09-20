import { t } from "@/i18n/terminal";
import { ReferralsView } from "@/terminal/ReferralsView";

export const dynamic = "force-dynamic";

/**
 * D5 · the referral programme, inside the app shell.
 *
 * A route of its own rather than a panel on `/profile`: the page answers two different people — the referrer
 * reading their funnel and the referee holding a code — and the terms half of it is readable signed out, which a
 * profile page could not be. The dashboard is a client read, because its writes (claiming a short code, applying
 * one) answer with sentences this page renders as they arrive.
 */
export default function ReferralsPage() {
  return (
    <main className="pgm-page">
      <h1 id="referrals-title">{t("terminal.referrals.title")}</h1>
      <ReferralsView />
    </main>
  );
}
