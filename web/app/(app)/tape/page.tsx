import { Tape } from "@/screens/Tape";
import { t } from "@/i18n/t";

export default function TapePage() {
  return (
    <div style={{ display: "grid", gap: "var(--pgm-space-3)" }}>
      <h1>{t("shell.nav.tape")}</h1>
      <Tape rows={20} />
    </div>
  );
}
