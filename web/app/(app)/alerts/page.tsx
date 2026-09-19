import { t } from "@/i18n/terminal";
import { serverRead } from "@/api/server-read";
import { AlertsView } from "@/terminal/AlertsView";
import type { AlertsPayload, DeliveryPage } from "@/terminal/wire";

export const dynamic = "force-dynamic";

/**
 * Alerts — D9.
 *
 * Both reads happen on the server, for the same reason the automation console reads there: the list carries the
 * plan's own sentence about which channels this account may use and what quiet hours would do to each rule
 * RIGHT NOW, and the delivery history is the record of what actually happened rather than what the last client
 * action believes. A screen that derived either from a client round trip would render "on" for a rule that is
 * being held at 2am.
 *
 * The reads are deliberately two: a failure to read the history must not blank the rule list, because the list is
 * the thing the user came to change.
 */
export default async function AlertsPage() {
  const list = await serverRead<AlertsPayload>("GET", "/v1/alerts");
  const history = await serverRead<DeliveryPage>("GET", "/v1/alerts/deliveries");
  return (
    <main className="pgm-page">
      {list.ok === false ? (
        <p className="unavailable" role="status">
          <span>{t("terminal.alerts.listUnavailable")}</span>
        </p>
      ) : null}
      <AlertsView initial={list.ok ? list.data : null} history={history.ok ? history.data.rows : []} />
    </main>
  );
}
