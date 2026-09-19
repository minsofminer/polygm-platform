import { WalletAddresses } from "@/screens/WalletAddresses";
import { WidgetBoundary } from "@/ui/WidgetBoundary";
import { Withdraw } from "@/screens/Withdraw";
import { KeyExport } from "@/screens/KeyExport";
import { Deposit } from "@/screens/Deposit";
import { BalancePanel } from "@/screens/BalancePanel";
import { t } from "@/i18n/t";

export default function WalletPage() {
  return (
    <div style={{ display: "grid", gap: "var(--pgm-space-5)" }}>
      <h1>{t("wallet.balance.title")}</h1>
      <WidgetBoundary label={t("wallet.balance.title")}>
        <BalancePanel />
      </WidgetBoundary>
      <WidgetBoundary label={t("wallet.deposit.title")}>
        <Deposit />
      </WidgetBoundary>
      <WidgetBoundary label={t("wallet.withdraw.title")}>
        <Withdraw />
      </WidgetBoundary>
      <WidgetBoundary label={t("wallet.addresses.title")}>
        <WalletAddresses />
      </WidgetBoundary>
      <WidgetBoundary label={t("wallet.keys.title")}>
        <KeyExport />
      </WidgetBoundary>
    </div>
  );
}
