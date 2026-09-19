import { RefusalNotice } from "@/ui/RefusalNotice";
import { t } from "@/i18n/t";

export default function SignUpPage() {
  return (
    <div style={{ display: "grid", gap: "var(--pgm-space-3)" }}>
      <h1>{t("auth.signup.title")}</h1>
      <p>{t("auth.signup.note")}</p>
      <p className="refusal">{t("auth.signup.walletOrder")}</p>
      <RefusalNotice route="signup" extra={t("auth.signup.disabled") + "."} />
      <a href="/sign-in" className="button">
        {t("auth.signin.submit")}
      </a>
    </div>
  );
}
