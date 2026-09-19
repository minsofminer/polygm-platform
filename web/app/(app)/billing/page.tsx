import { BillingClient } from "@/screens/BillingClient";
import { WidgetBoundary } from "@/ui/WidgetBoundary";
import { t } from "@/i18n/t";

export default function BillingPage() {
  return (
    <div style={{ display: "grid", gap: "var(--pgm-space-4)" }}>
      <h1>{t("billing.page.title")}</h1>
      <WidgetBoundary label={t("billing.page.title")}>
        <BillingClient />
      </WidgetBoundary>
    </div>
  );
}
