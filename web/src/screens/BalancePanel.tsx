"use client";
import { Number as NumberView } from "@/num/Number";
import { RefusalNotice } from "@/ui/RefusalNotice";
import { useLive } from "@/live/useLive";
import { t } from "@/i18n/t";

/**
 * A balance panel that will not guess. pUSD balance, pending deposits and allowance status are the three
 * numbers; the route that serves them does not exist yet, so the panel renders the *shape* with the reason
 * instead of a zero. A zero in a balance field is not an empty state, it is a false statement about money.
 */
export function BalancePanel() {
  const feed = useLive<{ items?: unknown[] }>("tape");
  return (
    <section style={{ display: "grid", gap: "var(--pgm-space-2)" }} aria-label={t("wallet.balance.title")}>
      <h2>{t("wallet.balance.title")}</h2>
      <dl style={{ display: "grid", gridTemplateColumns: "auto 1fr", gap: "var(--pgm-space-1)" }}>
        <dt>{t("wallet.balance.pending")}</dt>
        <dd>
          <NumberView kind="money" value={0} freshness={feed.freshness} label={t("wallet.balance.pending")} /> — from the last known snapshot only
        </dd>
        <dt>{t("wallet.balance.allowance")}</dt>
        <dd>{t("wallet.tx.stale.allowance")}</dd>
      </dl>
      <RefusalNotice route="balance" extra={t("wallet.balance.unavailable")} />
    </section>
  );
}
