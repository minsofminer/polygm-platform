import { WalletAddresses } from "@/screens/WalletAddresses";
import { WidgetBoundary } from "@/ui/WidgetBoundary";
import { RefusalNotice } from "@/ui/RefusalNotice";
import { t } from "@/i18n/t";

export default function PortfolioPage() {
  return (
    <div style={{ display: "grid", gap: "var(--pgm-space-4)" }}>
      <h1>{t("shell.nav.portfolio")}</h1>
      <WidgetBoundary label={t("wallet.addresses.title")}>
        <WalletAddresses />
      </WidgetBoundary>
      <RefusalNotice route="transactions" extra="Open positions and their PnL need the wallet balance route." />
    </div>
  );
}
