import Link from "next/link";
import { t } from "@/i18n/t";

export default function NotFound() {
  return (
    <main style={{ padding: "var(--pgm-pad-screen-comfortable)" }}>
      <h1>{t("common.notfound.title")}</h1>
      <p>{t("common.notfound.explain")}</p>
      <Link href="/markets" className="button">
        {t("shell.nav.markets")}
      </Link>
    </main>
  );
}
