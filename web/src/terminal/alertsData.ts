/**
 * The pair of reads the alerts screen needs, in one place, **in parallel**.
 *
 * Same finding as `automationData.ts`: the page awaited the rule list and then the delivery history, and the
 * two are independent, so the page paid two round trips where it owed one. The page's comment already argued
 * that a failure to read the history must not blank the rule list — that argument was about *error isolation*,
 * and error isolation does not require ordering. `Promise.all` keeps both: each read still answers with its own
 * `ok`/`code`, and the caller branches on them separately.
 *
 * The reader is a parameter for the same testability reason as the automation loader: `serverRead` imports
 * `server-only`, which a test cannot import, so injecting it is what makes "these two run at the same time" a
 * claim a test can check rather than a claim a reviewer has to trust.
 */
import type { ServerRead } from "@/api/server-read";
import type { AlertsPayload, DeliveryPage } from "@/terminal/wire";
import type { Reader } from "@/terminal/automationData";

export async function loadAlerts(read: Reader): Promise<{
  list: ServerRead<AlertsPayload>;
  history: ServerRead<DeliveryPage>;
}> {
  const [list, history] = await Promise.all([
    read<AlertsPayload>("GET", "/v1/alerts"),
    read<DeliveryPage>("GET", "/v1/alerts/deliveries"),
  ]);
  return { list, history };
}
