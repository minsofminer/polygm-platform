"use client";
import { useResource } from "@/api/data";
import { Button } from "@/ui/Button";
import { RefusalNotice } from "@/ui/RefusalNotice";
import { Number as NumberView } from "@/num/Number";
import { cents } from "@/money/cents";
import { request } from "@/api/client";
import { t } from "@/i18n/t";
import { isTma } from "@/telegram/bridge";

/**
 * Two purchase surfaces, one entitlement (P08 D7). The reconciliation is stated in the UI, because it is the
 * part a user can otherwise be harmed by: a Stars receipt and a Stripe invoice are two sources of truth and
 * the entitlement is whichever the API has recorded, never what this browser remembers. If the entitlement
 * cannot be read, Pro features stay available for the session and the server still refuses what it refuses —
 * a degraded mode that punishes the paying customer is the usual way this goes wrong.
 *
 * The plan is therefore *read*, not inferred. The first version of this screen called the degraded state
 * "entitlement unknown" while deriving it from `useLive("tape")` — the tape's freshness, which says nothing
 * about billing — and the rows it compared were string literals outside the dictionary with a price the
 * table never rendered. A feature table that cannot show its own price is not an honest one.
 */
// The copy is resolved once, at module scope, from the dictionary. `rows` is deliberately *not* an array of
// keys mapped through `t()` in the render: `t(someKeyVariable)` is a dynamic lookup the build-time checker
// cannot see, and a key it cannot see is a key that can be missing — which is the exact failure DESIGN.md §8
// exists to stop. The prices are data (cents), never copy.
const PLANS = [
  {
    id: "free",
    name: t("billing.plan.free"),
    priceCents: 0,
    priceLabel: t("billing.plan.free.priceLabel"),
    rows: [t("billing.plan.free.row1"), t("billing.plan.free.row2"), t("billing.plan.free.row3")],
  },
  {
    id: "pro",
    name: t("billing.plan.pro"),
    priceCents: 2500,
    priceLabel: t("billing.plan.pro.priceLabel"),
    rows: [t("billing.plan.pro.row1"), t("billing.plan.pro.row2")],
  },
];

export function BillingClient() {
  const inTma = isTma();
  const ent = useResource(["billing", "entitlement"], () => request<Record<string, unknown>>({ key: "entitlement" }), {
    // A degraded read is a state, not a retry storm: the client already refuses an unbuilt route before it
    // reaches the network, and refetching a refusal every 3 seconds would turn an honest gap into noise.
    staleTime: 60_000,
  });
  const result = ent.data;
  const plan = result && result.ok === true ? String(result.data.plan ?? "") : null;
  const unreadable = ent.isPending ? "loading" : plan === null ? "unknown" : plan;
  return (
    <div style={{ display: "grid", gap: "var(--pgm-space-4)" }}>
      <table>
        <thead>
          <tr>
            {PLANS.map((planRow) => (
              <th key={planRow.id}>
                {planRow.name}{" "}
                <NumberView kind="money" value={cents(planRow.priceCents)} label={planRow.priceLabel} />{" "}
                <span className="refusal">{t("billing.plan.perMonth")}</span>
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          <tr>
            {PLANS.map((planRow) => (
              <td key={planRow.id}>
                <ul>
                  {planRow.rows.map((row) => (
                    <li key={row}>{row}</li>
                  ))}
                </ul>
              </td>
            ))}
          </tr>
        </tbody>
      </table>
      <p>
        <strong>{t("billing.entitlement.title")}: </strong>
        {unreadable === "loading" ? (
          <span role="status">{t("billing.entitlement.loading")}</span>
        ) : unreadable === "unknown" ? (
          <span role="status" className="refusal">{t("billing.entitlement.unknown")}</span>
        ) : (
          <span>{unreadable}</span>
        )}
      </p>
      {result && result.ok === false ? <p role="status" className="refusal">{result.error.message}</p> : null}
      {unreadable === "unknown" ? <p role="status">{t("billing.entitlement.degraded")}</p> : null}
      <p className="refusal">{t("billing.page.paywallRule")}</p>
      <div style={{ display: "flex", gap: "var(--pgm-space-2)" }}>
        <Button disabled={!inTma} why={inTma ? undefined : "Telegram Stars are only purchasable inside the Mini App"}>
          {t("billing.checkout.stars")}
        </Button>
        <Button disabled={inTma} why={inTma ? "Cards are not available inside the webview; this is deliberate, so one price is charged in one currency" : undefined}>
          {t("billing.checkout.web")}
        </Button>
      </div>
      <RefusalNotice route="starsPurchase" extra={t("billing.stars.notServed")} />
      <RefusalNotice route="stripeCheckout" extra={t("billing.checkout.unavailable")} />
      <RefusalNotice route="entitlement" extra={t("billing.entitlement.notServed")} />
      <h2>{t("billing.cancel.title")}</h2>
      <p>{t("billing.cancel.detail")}</p>
      <RefusalNotice route="invoices" />
      <h2>{t("billing.referral.title")}</h2>
      <RefusalNotice route="referrals" />
      <p className="refusal">{t("billing.page.reconciliation")}</p>
    </div>
  );
}
