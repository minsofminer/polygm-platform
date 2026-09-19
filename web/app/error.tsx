"use client";
import { t } from "@/i18n/t";

/** Route-level boundary (P08 D1). Per-widget boundaries live in src/ui/WidgetBoundary.tsx; the two exist
 *  so a failed panel is a panel that says so, and a failed route is a route that can be left. */
export default function RouteError({ error, reset }: { error: Error & { digest?: string }; reset: () => void }) {
  return (
    <main className="unavailable" role="alert">
      <strong>{t("common.error.route.title")}</strong>
      <span>
        {/* The error's own text wins when it has one, because that sentence came from the code that failed;
            the dictionary sentence is what a user sees when React had nothing to say. */}
        {error.message || t("common.error.route.explain")}
      </span>
      {error.digest ? <span className="refusal">{t("common.error.route.ref", { digest: error.digest })}</span> : null}
      <button type="button" className="button" onClick={reset}>
        {t("common.error.route.retry")}
      </button>
    </main>
  );
}
