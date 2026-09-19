import { ProfileClient } from "@/screens/ProfileClient";
import { WidgetBoundary } from "@/ui/WidgetBoundary";
import { t } from "@/i18n/t";

export default function ProfilePage() {
  return (
    <div style={{ display: "grid", gap: "var(--pgm-space-4)" }}>
      <h1>{t("profile.page.title")}</h1>
      <WidgetBoundary label={t("profile.page.title")}>
        <ProfileClient />
      </WidgetBoundary>
    </div>
  );
}
