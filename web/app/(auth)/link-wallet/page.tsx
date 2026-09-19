import { RefusalNotice } from "@/ui/RefusalNotice";
import { t } from "@/i18n/t";

export default function LinkWalletPage() {
  return (
    <div style={{ display: "grid", gap: "var(--pgm-space-3)" }}>
      <h1>{t("auth.link.title")}</h1>
      <p>{t("auth.link.note")}</p>
      <RefusalNotice route="linkWallet" extra={t("auth.link.disabled") + "."} />
    </div>
  );
}
