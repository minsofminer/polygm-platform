import { WidgetBoundary } from "@/ui/WidgetBoundary";
import { BalancePanel } from "@/screens/BalancePanel";
import { WalletPanels } from "@/screens/WalletPanels";
import { t } from "@/i18n/t";

export default function WalletPage() {
  return (
    <div style={{ display: "grid", gap: "var(--pgm-space-5)" }}>
      <h1>{t("wallet.balance.title")}</h1>
      {/* The balance reads first, above the tabs: it is the number every action on this page is measured against. */}
      <WidgetBoundary label={t("wallet.balance.title")}>
        <BalancePanel />
      </WidgetBoundary>
      {/* Deposit, withdraw, addresses and keys are four places, not one long column. */}
      <WalletPanels />
    </div>
  );
}
