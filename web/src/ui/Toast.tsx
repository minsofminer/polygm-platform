/**
 * Toasts with dedupe. The dedupe is not a nicety: at 20 updates a second, a refused order produces one
 * toast per frame in the naive implementation, and the user's screen becomes an error waterfall that hides
 * the market. Key = the refusal code plus the request id, and repeats bump a counter.
 */
"use client";
import { t } from "@/i18n/t";


import { useToasts } from "./toast-store";
export type { Toast } from "./toast-store";
function prefersReducedMotion(): boolean {
  return typeof window !== "undefined" && typeof window.matchMedia === "function"
    ? window.matchMedia("(prefers-reduced-motion: reduce)").matches
    : false;
}

export function Toasts() {
  const { toasts, dismiss, requestDismiss } = useToasts();
  if (!toasts.length) return null;
  return (
    <div className="toasts" role="region" aria-label={t("common.state.errorTitle")}>
      {toasts.map((toast) => (
        <div
          key={toast.id}
          className={`toast ${toast.dismissing ? "pgm-toast-out" : "pgm-toast-in"}`}
          data-tone={toast.tone}
          role={toast.tone === "error" ? "alert" : "status"}
          onAnimationEnd={(event) => {
            if (!toast.dismissing || event.target !== event.currentTarget) return;
            dismiss(toast.id);
          }}
        >
          <span>{toast.text}</span>
          {toast.count > 1 ? (
            <span className="toast__count">
              ×{toast.count} — {t("common.button.dismiss")}
            </span>
          ) : null}
          <button
            type="button"
            className="button"
            onClick={() => (prefersReducedMotion() ? dismiss(toast.id) : requestDismiss(toast.id))}
          >
            {t("common.button.dismiss")}
          </button>
        </div>
      ))}
    </div>
  );
}
